"""Montaje y ejecución del pipeline completo para una entrada (vídeo o texto)."""

import datetime as dt
import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pipeline.audio import extract_audio, purge_audio
from pipeline.claims import PROMPT as CLAIMS_PROMPT
from pipeline.claims import detect_claims
from pipeline.config import ConfigError, LlmStep, Settings
from pipeline.context import PROMPT as CONTEXT_PROMPT
from pipeline.context import resolve_context
from pipeline.costs import search_cost, transcription_cost
from pipeline.duplicates import mark_duplicates, with_repetitions
from pipeline.evidence.base import EvidenceSource, collect_evidence
from pipeline.evidence.local import local_evidence, on_demand_evidence, partial_local_evidence
from pipeline.evidence.search_cache import SearchCache
from pipeline.evidence.tavily import SearchStats, TavilyClient
from pipeline.governments import default_governments
from pipeline.indicators import IndicatorBase, load_indicators

if TYPE_CHECKING:
    from pipeline.knowledge.local_base import KnowledgeIndicatorBase
    from pipeline.knowledge.on_demand import OnDemandSeries
    from pipeline.knowledge.web_facts import WebFactStore
from pipeline.jobs import JobStore, Step, StepOutcome, earliest_step, prompt_hash, run_steps
from pipeline.knowledge.verdict_store import verdict_rows
from pipeline.knowledge.web_facts import facts_evidence
from pipeline.llm import ThinkingConfig
from pipeline.models import (
    AudioInfo,
    ClaimDetection,
    ClaimEvidence,
    CostEntry,
    DetectedClaim,
    EvidenceItem,
    EvidenceRun,
    InputKind,
    PromiseVerdict,
    Report,
    SpeakerInfo,
    SpeakerMap,
    StepName,
    Transcript,
    Verdict,
    VerdictRecord,
)
from pipeline.politicians import CombinedDirectory, LocalPoliticianCache, PoliticianDirectory
from pipeline.promises import PROMPT as PROMISES_PROMPT
from pipeline.promises import assess_promises
from pipeline.report import build_report, format_summary
from pipeline.speaker_research import PROMPT as RESEARCH_PROMPT
from pipeline.speaker_research import SpeakerSearch, research_speakers
from pipeline.speakers import PROMPT as SPEAKERS_PROMPT
from pipeline.speakers import identify_speakers
from pipeline.text_input import SUPPORTED_SUFFIXES, TEXT_SPEAKER, load_text
from pipeline.text_input import VERSION as TRANSCRIPT_VERSION
from pipeline.transcribe import Transcriber
from pipeline.verdict import PROMPT as VERDICT_PROMPT
from pipeline.verdict import FollowUpSearch, decide_verdicts, own_source_checker

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
SPEAKER_INFO_FILE = "speakers_input.json"
# Speakers after both stages (identification and research); what later steps read.
SPEAKERS_FILE = "speaker_research.json"
# Whether the verdicts reached verif.claims (knowledge base); read by the report step.
VERDICT_RECORD_FILE = "verdict_record.json"


class Deps(Protocol):
    """External services used by the steps; created lazily so resuming is free."""

    llm: Any
    transcriber: Transcriber
    evidence_sources: list[EvidenceSource]
    disabled_sources: list[str]
    # Web searches of this run, all steps (for the summary at the end), and per step.
    searches: SearchStats
    search_entries: list[CostEntry]
    # Claude's thinking in the verdict step (VERDICT_THINKING…); None: model default.
    verdict_thinking: ThinkingConfig | None
    # Local base of official series (data/knowledge/indicators).
    indicators: "IndicatorBase | KnowledgeIndicatorBase"
    # None when there is no search key: speakers are then not researched.
    speaker_search: SpeakerSearch | None
    # Politicians base (local cache + Supabase when configured); None: not used.
    politicians: PoliticianDirectory | None

    def model_for(self, step: LlmStep) -> str: ...

    def take_search_cost(self, step: str) -> list[CostEntry]: ...


class LiveDeps:
    """Real clients built from settings, only when a step needs them."""

    def __init__(
        self, settings: Settings, prompts_dir: Path = PROMPTS_DIR, *, use_cache: bool = True
    ) -> None:
        self.settings = settings
        self.prompts_dir = prompts_dir
        # False (--no-cache): searches skip the disk cache but still refresh it.
        self.use_cache = use_cache
        # Searches of this run (all steps), for the summary at the end.
        self.searches = SearchStats()
        self.search_entries: list[CostEntry] = []
        # Set when the knowledge base could not be read and the JSON base was used.
        self.knowledge_warning: str | None = None

    def model_for(self, step: LlmStep) -> str:
        return self.settings.model_for(step)

    @property
    def concurrency(self) -> int:
        return self.settings.pipeline_concurrency

    @property
    def record_verdicts(self) -> "Callable[[list[dict]], VerdictRecord] | None":
        """Store verdicts in verif.claims when the knowledge base is configured."""
        if self.settings.supabase_db_url is None:
            return None
        url = self.settings.supabase_db_url.get_secret_value()
        model = self.model_for("verdict")
        versions = {
            name: prompt_hash(self.prompts_dir, name)
            for name in (CLAIMS_PROMPT, CONTEXT_PROMPT, VERDICT_PROMPT)
        }

        def record(rows: list[dict]) -> VerdictRecord:
            import psycopg

            from pipeline.knowledge.verdict_store import record_verdicts

            try:
                with psycopg.connect(url, connect_timeout=10) as conn:
                    n = record_verdicts(
                        conn, rows[0]["job_id"] if rows else "", rows,
                        model=model, prompt_versions=versions,
                    )  # fmt: skip
                    conn.commit()
                return VerdictRecord(stored=n)
            except psycopg.Error as exc:
                logger.warning("No se guardaron los veredictos en la base: %s", exc)
                return VerdictRecord(error=str(exc))

        return record

    @cached_property
    def llm(self) -> Any:
        if self.settings.llm_backend == "claude-code":
            from pipeline.llm_claude_code import ClaudeCodeLLM

            return ClaudeCodeLLM(self.prompts_dir)
        from pipeline.llm import LLMClient, make_anthropic_client

        return LLMClient(make_anthropic_client(self.settings), self.prompts_dir)

    @cached_property
    def transcriber(self) -> Transcriber:
        from pipeline.transcribe import make_transcriber

        return make_transcriber(self.settings)

    @cached_property
    def tavily(self) -> TavilyClient | None:
        if not self.settings.tavily_api_key:
            return None

        cache = SearchCache(
            self.settings.data_dir / "cache" / "search",
            max_age_days=self.settings.tavily_cache_days,
            read=self.use_cache,
        )
        return TavilyClient(
            self.settings.secret("tavily_api_key"),
            search_depth=self.settings.tavily_search_depth,
            max_results=self.settings.tavily_max_results,
            cache=cache,
        )

    @cached_property
    def indicators(self) -> "IndicatorBase | KnowledgeIndicatorBase":
        """The Supabase knowledge base when configured (approved data, resolved at the
        speech date); the JSON base otherwise or if the database cannot be read (the run
        says so in its warnings)."""
        if self.settings.supabase_db_url is None:
            return load_indicators()
        import psycopg

        from pipeline.knowledge.local_base import KnowledgeIndicatorBase

        try:
            url = self.settings.supabase_db_url.get_secret_value()
            with psycopg.connect(url, connect_timeout=10) as conn:
                return KnowledgeIndicatorBase.from_db(conn)
        except psycopg.Error as exc:
            logger.warning("Base de conocimiento no disponible, se usa la base JSON: %s", exc)
            self.knowledge_warning = (
                f"Base de conocimiento no disponible ({exc}); se usó la base JSON"
            )
            return load_indicators()

    @property
    def verdict_thinking(self) -> ThinkingConfig | None:
        s = self.settings
        if s.verdict_thinking is None:
            return None
        return ThinkingConfig(s.verdict_thinking, s.verdict_effort, s.verdict_thinking_budget)

    def take_search_cost(self, step: str) -> list[CostEntry]:
        """Tavily searches made since the last call, as a cost entry of `step`."""
        if "tavily" not in self.__dict__ or self.tavily is None:
            return []
        stats = self.tavily.take_stats()
        if not (stats.real or stats.cached):
            return []
        self.searches.real += stats.real
        self.searches.cached += stats.cached
        self.searches.credits += stats.credits
        entry = search_cost(step, real=stats.real, cached=stats.cached, credits=stats.credits)
        self.search_entries.append(entry)
        return [entry]

    @cached_property
    def _sources(self) -> tuple[list[EvidenceSource], list[str]]:
        from pipeline.evidence.factcheck_api import FactCheckSource
        from pipeline.evidence.oficiales import OfficialSearchSource
        from pipeline.evidence.web import WebSearchSource

        sources: list[EvidenceSource] = []
        disabled: list[str] = []
        if self.settings.google_factcheck_api_key:
            sources.append(FactCheckSource(self.settings.secret("google_factcheck_api_key")))
        else:
            disabled.append("factcheck")
        if self.tavily is not None:
            sources += [OfficialSearchSource(self.tavily), WebSearchSource(self.tavily)]
        else:
            disabled += ["oficial", "web"]
        if not sources:
            raise ConfigError(
                "No hay ninguna fuente de evidencia configurada: define "
                "GOOGLE_FACTCHECK_API_KEY y/o TAVILY_API_KEY en .env"
            )
        return sources, disabled

    @property
    def evidence_sources(self) -> list[EvidenceSource]:
        return self._sources[0]

    @property
    def disabled_sources(self) -> list[str]:
        return self._sources[1]

    @cached_property
    def politicians(self) -> PoliticianDirectory | None:
        cache = LocalPoliticianCache(self.settings.data_dir / "cache" / "politicians.json")
        database = None
        if self.settings.supabase_db_url is not None:
            import psycopg

            from pipeline.knowledge.politicians_repo import PostgresPoliticianDirectory

            url = self.settings.supabase_db_url.get_secret_value()
            database = PostgresPoliticianDirectory(lambda: psycopg.connect(url, connect_timeout=10))
        return CombinedDirectory(cache, database)

    @cached_property
    def web_facts(self) -> "WebFactStore | None":
        """Facts extracted from the web in earlier runs (Supabase); None without it."""
        if self.settings.supabase_db_url is None:
            return None
        import psycopg

        from pipeline.knowledge.web_facts import WebFactStore

        url = self.settings.supabase_db_url.get_secret_value()
        return WebFactStore(lambda: psycopg.connect(url, connect_timeout=10), dt.date.today())

    @cached_property
    def on_demand(self) -> "OnDemandSeries":
        """Official APIs (INE, Eurostat) for periods the local base does not cover."""
        import httpx

        from pipeline.knowledge.catalog import load_catalog
        from pipeline.knowledge.on_demand import OnDemandSeries

        return OnDemandSeries(
            load_catalog(),
            lambda: httpx.Client(timeout=60, follow_redirects=True),
            today=dt.date.today(),
        )

    @cached_property
    def speaker_search(self) -> SpeakerSearch | None:
        if self.tavily is None:
            return None
        from pipeline.evidence.speaker_search import TavilySpeakerSearch

        return TavilySpeakerSearch(self.tavily)


@dataclass(frozen=True)
class JobResult:
    report: Report
    report_path: Path
    summary: str
    steps_run: list[StepName]


def _record_warning(store: JobStore) -> list[str]:
    if not store.path(VERDICT_RECORD_FILE).exists():
        return []
    record = store.read(VERDICT_RECORD_FILE, VerdictRecord)
    if record.error is None:
        return []
    return [f"Los veredictos no se guardaron en la base de conocimiento: {record.error}"]


def input_kind_for(path: Path) -> InputKind:
    return "texto" if path.suffix.lower() in SUPPORTED_SUFFIXES else "video"


def _with_costs(
    deps: Deps, step: StepName, work: Callable[[], bool], *, uses_llm: bool = True
) -> StepOutcome:
    """Run a step and return what it spent (Claude and web searches), attaching it to the
    error if it fails. `work` returns whether every item succeeded; if not, the step is
    retried next run."""

    def spent() -> list[CostEntry]:
        llm = deps.llm.take_costs() if uses_llm else []
        return [*llm, *deps.take_search_cost(step)]

    try:
        complete = work()
    except Exception as exc:
        exc.step_costs = spent()  # type: ignore[attr-defined]
        raise
    return StepOutcome(cost=spent(), complete=complete)


def follow_up_search(deps: Deps, speech_date: dt.date) -> FollowUpSearch:
    """Targeted second search with the queries Claude asks for when it cannot decide.
    Fact-checks are left out: they match whole claims, not the missing data."""

    def search(
        claim: DetectedClaim, queries: list[str], first_id: int
    ) -> tuple[list[EvidenceItem], list[str]]:
        sources = [s for s in deps.evidence_sources if s.name != "factcheck"]
        if not sources:
            return [], []
        probe = claim.model_copy(update={"search_queries": queries})
        [found] = collect_evidence([probe], sources, speech_date)
        items = [
            item.model_copy(update={"id": f"E{first_id + n}"}) for n, item in enumerate(found.items)
        ]
        return items, [f"búsqueda adicional: {e}" for e in found.errors]

    return search


def build_steps(
    store: JobStore, input_path: Path, deps: Deps, *, purge_audio_after: bool = False
) -> list[Step]:
    manifest = store.manifest
    kind = manifest.input_kind

    def audio_step(store: JobStore) -> StepOutcome:
        store.write("audio.json", extract_audio(input_path, store.root), AudioInfo)
        return StepOutcome()

    def transcript_step(store: JobStore) -> StepOutcome:
        if kind == "texto":
            store.write("transcript.json", load_text(input_path), Transcript)
            return StepOutcome()
        audio = store.read("audio.json", AudioInfo)
        if not Path(audio.path).exists():
            raise FileNotFoundError(
                f"Falta {audio.path} (¿se usó --purge-audio?); repite desde --from-step audio"
            )
        transcript = deps.transcriber.transcribe(audio)
        store.write("transcript.json", transcript, Transcript)
        if purge_audio_after:
            purge_audio(audio)
        seconds = transcript.duration or audio.duration
        return StepOutcome(cost=[transcription_cost("transcript", transcript.provider, seconds)])

    def speakers_step(store: JobStore) -> StepOutcome:
        def work() -> bool:
            info_path = store.path(SPEAKER_INFO_FILE)
            info = (
                store.read(SPEAKER_INFO_FILE, dict[str, SpeakerInfo]) if info_path.exists() else {}
            )
            speakers = identify_speakers(
                store.read("transcript.json", Transcript),
                speech_date=store.manifest.speech_date,
                llm=deps.llm,
                model=deps.model_for("detection"),
                governments=default_governments(),
                info=info,
            )
            store.write("speakers.json", speakers, SpeakerMap)
            return not speakers.errors

        return _with_costs(deps, "speakers", work)

    def speaker_research_step(store: JobStore) -> StepOutcome:
        speakers = store.read("speakers.json", SpeakerMap)
        search = deps.speaker_search
        if search is None and deps.politicians is None:
            store.write(SPEAKERS_FILE, speakers, SpeakerMap)
            return StepOutcome()

        def work() -> bool:
            researched = research_speakers(
                speakers,
                speech_date=store.manifest.speech_date,
                search=search,
                llm=deps.llm,
                model=deps.model_for("detection"),
                governments=default_governments(),
                directory=deps.politicians,
            )
            store.write(SPEAKERS_FILE, researched, SpeakerMap)
            return len(researched.errors) == len(speakers.errors)

        return _with_costs(deps, "speaker_research", work)

    def claims_step(store: JobStore) -> StepOutcome:
        def work() -> bool:
            detection = detect_claims(
                store.read("transcript.json", Transcript),
                speech_date=store.manifest.speech_date,
                input_kind=kind,
                llm=deps.llm,
                model=deps.model_for("detection"),
                indicator_catalog=deps.indicators.catalog(),
            )
            store.write("claims.json", detection, ClaimDetection)
            return not detection.failed_blocks

        return _with_costs(deps, "claims", work)

    def context_step(store: JobStore) -> StepOutcome:
        def work() -> bool:
            resolved = resolve_context(
                store.read("claims.json", ClaimDetection),
                store.read("transcript.json", Transcript),
                store.read(SPEAKERS_FILE, SpeakerMap),
                speech_date=store.manifest.speech_date,
                llm=deps.llm,
                # Interpreting who "ustedes" is needs the careful model.
                model=deps.model_for("verdict"),
                governments=default_governments(),
            )
            # Repetitions of the same claim by the same speaker are verified once.
            resolved = resolved.model_copy(update={"claims": mark_duplicates(resolved.claims)})
            store.write("context.json", resolved, ClaimDetection)
            return not resolved.context_errors

        return _with_costs(deps, "context", work)

    indicator_hits: list[int] = []

    def evidence_step(store: JobStore) -> StepOutcome:
        outcome = _with_costs(deps, "evidence", lambda: _evidence(store), uses_llm=False)
        hits = indicator_hits.pop() if indicator_hits else 0
        if hits:
            entry = CostEntry(step="evidence", provider="indicadores", indicator_hits=hits, usd=0)
            outcome.cost.append(entry)
            deps.searches.indicator_hits += hits
            deps.search_entries.append(entry)
        return outcome

    def _evidence(store: JobStore) -> bool:
        claims = store.read("context.json", ClaimDetection).claims
        # Unresolved claims are no_verificable anyway: don't spend searches on them.
        # Promises are assessed for feasibility, which does not depend on who made them.
        searchable = [
            c
            for c in claims
            if not c.duplicate_of and (c.kind == "promesa" or c.context_status != "no_resuelto")
        ]
        date = store.manifest.speech_date
        # Claims answered by the local base of indicators skip the web searches; the
        # (free) fact-check API is still asked, to recognise published debunks.
        base = deps.indicators.at(date)  # what was published on the speech date
        local = {c.id: item for c in searchable if (item := local_evidence(c, base, date))}
        # Not in the local base: the official API, fetched now, before any web search.
        on_demand = getattr(deps, "on_demand", None)
        if on_demand is not None:
            for c in searchable:
                if c.id not in local and (item := on_demand_evidence(c, on_demand, date)):
                    local[c.id] = item
        # Facts extracted in earlier runs (same indicator, territory and period): an
        # official or fact-checker one settles the claim without a web search.
        stored_facts: dict[str, list[EvidenceItem]] = {}
        fact_store = getattr(deps, "web_facts", None)
        if fact_store is not None:
            for c in searchable:
                if c.id in local or not c.indicator or c.kind == "promesa":
                    continue
                try:
                    items, settled = facts_evidence(fact_store, c.indicator, c.years)
                except Exception as exc:  # noqa: BLE001 - reported; the web search goes on
                    deps.knowledge_warning = f"Datos web guardados no disponibles ({exc})"
                    break
                if settled:
                    local[c.id] = items[0]
                elif items:
                    stored_facts[c.id] = items
        web = [c for c in searchable if c.id not in local]
        workers = getattr(deps, "concurrency", 1)
        found = {
            e.claim_id: e
            for e in collect_evidence(web, deps.evidence_sources, date, max_workers=workers)
        }
        # Partly covered: the web search ran, and the covered part of the series goes first;
        # stored web facts too.
        for c in web:
            part = partial_local_evidence(c, base)
            first = ([part] if part is not None else []) + stored_facts.get(c.id, [])
            if first and c.id in found:
                items = [*first, *found[c.id].items]
                found[c.id] = found[c.id].model_copy(
                    update={
                        "items": [
                            i.model_copy(update={"id": f"E{n}"}) for n, i in enumerate(items, 1)
                        ]
                    }
                )
        factcheck_only = [s for s in deps.evidence_sources if s.name == "factcheck"]
        checked = collect_evidence(
            [c for c in searchable if c.id in local], factcheck_only, date, max_workers=workers
        )
        for e in checked:
            items = [local[e.claim_id], *e.items]
            found[e.claim_id] = e.model_copy(
                update={
                    "items": [i.model_copy(update={"id": f"E{n}"}) for n, i in enumerate(items, 1)]
                }
            )
        indicator_hits.append(len(local))
        sources = deps.evidence_sources
        base_warnings = [w for w in [getattr(deps, "knowledge_warning", None)] if w]
        base_warnings += list(getattr(deps.indicators, "problems", []))
        base_warnings += list(getattr(on_demand, "errors", []))
        run = EvidenceRun(
            sources_used=[s.name for s in sources],
            sources_disabled=deps.disabled_sources,
            warnings=base_warnings,
            claims=[found.get(c.id, ClaimEvidence(claim_id=c.id)) for c in claims],
        )
        store.write("evidence.json", run, EvidenceRun)
        return True

    def verdicts_step(store: JobStore) -> StepOutcome:
        def work() -> bool:
            claims = [
                c for c in store.read("context.json", ClaimDetection).claims if c.kind != "promesa"
            ]
            verdicts = decide_verdicts(
                [c for c in claims if not c.duplicate_of],
                store.read("evidence.json", EvidenceRun).claims,
                speech_date=store.manifest.speech_date,
                llm=deps.llm,
                model=deps.model_for("verdict"),
                follow_up=follow_up_search(deps, store.manifest.speech_date),
                thinking=deps.verdict_thinking,
                max_workers=getattr(deps, "concurrency", 1),
                own_source=own_source_checker(
                    store.read(SPEAKERS_FILE, SpeakerMap),
                    default_governments(),
                    store.manifest.speech_date,
                ),
            )
            verdicts = with_repetitions(claims, verdicts)
            store.write("verdicts.json", verdicts, list[Verdict])
            recorder = getattr(deps, "record_verdicts", None)
            if recorder is not None:
                from pipeline.knowledge.catalog import load_catalog
                from pipeline.knowledge.coverage import load_topics

                rows = verdict_rows(
                    store.job_id,
                    claims,
                    verdicts,
                    store.read("evidence.json", EvidenceRun).claims,
                    speakers=store.read(SPEAKERS_FILE, SpeakerMap),
                    indicator_topics={k: v.tema for k, v in load_catalog().items()},
                    topics=load_topics(),
                )
                store.write(VERDICT_RECORD_FILE, recorder(rows), VerdictRecord)
            _extract_facts(store, claims, verdicts)
            return not any(v.analysis_error for v in verdicts)

        return _with_costs(deps, "verdicts", work)

    def _extract_facts(store: JobStore, claims, verdicts) -> None:
        """F2: keep the datum behind a web-based verdict on a catalog indicator."""
        fact_store = getattr(deps, "web_facts", None)
        if fact_store is None:
            return
        from pipeline.knowledge.catalog import load_catalog
        from pipeline.knowledge.web_facts import extract, period_label

        catalog = load_catalog()
        by_claim = {c.id: c for c in claims}
        evidence = {e.claim_id: e for e in store.read("evidence.json", EvidenceRun).claims}
        for v in verdicts:
            c = by_claim.get(v.claim_id)
            if c is None or v.verdict == "no_verificable" or c.indicator not in catalog:
                continue
            periodo = period_label(c.years)
            items = [i for i in evidence[c.id].items if i.id in v.evidence_used]
            if periodo is None or any(
                i.source.kind == "indicador_local" or i.observation_ids or i.web_fact_id
                for i in items
            ):
                continue
            fact = extract(
                catalog[c.indicator], periodo, items, llm=deps.llm,
                model=deps.model_for("detection"),
            )  # fmt: skip
            if fact is not None:
                try:
                    fact_store.save(fact, model=deps.model_for("detection"))
                except Exception as exc:  # noqa: BLE001 - losing a cache entry is not fatal
                    logger.warning("No se guardó el dato extraído de %s: %s", c.id, exc)

    def promises_step(store: JobStore) -> StepOutcome:
        def work() -> bool:
            promises = [
                c for c in store.read("context.json", ClaimDetection).claims if c.kind == "promesa"
            ]
            results = assess_promises(
                [c for c in promises if not c.duplicate_of],
                store.read("evidence.json", EvidenceRun).claims,
                speech_date=store.manifest.speech_date,
                llm=deps.llm,
                model=deps.model_for("verdict"),
            )
            results = with_repetitions(promises, results)
            store.write("promises.json", results, list[PromiseVerdict])
            return not any(r.analysis_error for r in results)

        return _with_costs(deps, "promises", work)

    def report_step(store: JobStore) -> StepOutcome:
        evidence = store.read("evidence.json", EvidenceRun)
        profiles = store.read(SPEAKERS_FILE, SpeakerMap)
        display = {s.label: s.display() for s in profiles.speakers if s.name or s.party}
        for label, name in store.manifest.speakers.items():  # --oradores / --autor win
            known = profiles.get(label)
            party = known.party if known else None
            display[label] = f"{name} ({party})" if party else name
        report = build_report(
            job_id=store.job_id,
            source=input_path.name,
            input_kind=kind,
            speech_date=store.manifest.speech_date,
            speakers=display,
            speaker_profiles=profiles.speakers,
            detection=store.read("context.json", ClaimDetection),
            verdicts=store.read("verdicts.json", list[Verdict]),
            promises=store.read("promises.json", list[PromiseVerdict]),
            cost=store.manifest.run_cost(),
            step_seconds={
                name: rec.seconds
                for name, rec in store.manifest.steps.items()
                if rec.seconds is not None
            },
            generated_at=dt.datetime.now(dt.UTC),
            extra_warnings=[
                f"Fuente «{name}» desactivada: falta su clave en .env"
                for name in evidence.sources_disabled
            ]
            + evidence.warnings
            + _record_warning(store),
        )
        store.write("report.json", report, Report)
        return StepOutcome()

    steps = [
        Step("transcript", transcript_step, version=TRANSCRIPT_VERSION if kind == "texto" else ""),
        Step("speakers", speakers_step, (SPEAKERS_PROMPT,), version="nombre_presidente=1"),
        Step("speaker_research", speaker_research_step, (RESEARCH_PROMPT,)),
        Step(
            "claims",
            claims_step,
            (CLAIMS_PROMPT,),
            version=f"indicadores={deps.indicators.version(coverage=False)}",
        ),
        Step("context", context_step, (CONTEXT_PROMPT,), version="repeticiones=1"),
        Step(
            "evidence",
            evidence_step,
            version=f"indicadores={deps.indicators.version(coverage=True)};regla=4;calculo=1;api=1",
        ),
        # A different thinking setting is a different verdict: recompute, do not reuse.
        Step(
            "verdicts",
            verdicts_step,
            (VERDICT_PROMPT,),
            version=f"thinking={deps.verdict_thinking.label()}" if deps.verdict_thinking else "",
        ),
        Step("promises", promises_step, (PROMISES_PROMPT,)),
        Step("report", report_step),
    ]
    if kind == "video":
        steps.insert(0, Step("audio", audio_step))
    return steps


def _store_speaker_info(store: JobStore, info: dict[str, SpeakerInfo] | None) -> bool:
    """Save the user's speaker data; True if it changed (speakers must be identified again).
    None keeps whatever was given before."""
    if info is None:
        return False
    path = store.path(SPEAKER_INFO_FILE)
    previous = store.read(SPEAKER_INFO_FILE, dict[str, SpeakerInfo]) if path.exists() else {}
    if previous == info:
        return False
    store.write(SPEAKER_INFO_FILE, info, dict[str, SpeakerInfo])
    return True


def run_job(
    input_path: Path,
    *,
    speech_date: dt.date,
    data_dir: Path,
    deps: Deps,
    prompts_dir: Path = PROMPTS_DIR,
    speakers: dict[str, str] | None = None,
    speaker_info: dict[str, SpeakerInfo] | None = None,
    author: str | None = None,
    from_step: StepName | None = None,
    purge_audio: bool = False,
) -> JobResult:
    if not input_path.is_file():
        raise FileNotFoundError(f"No existe el archivo {input_path}")
    kind = input_kind_for(input_path)
    if author is not None:
        if kind != "texto":
            raise ValueError("--autor solo se usa con texto; en vídeo usa --oradores")
        speakers = {**(speakers or {}), TEXT_SPEAKER: author}

    store, invalidated = JobStore.open_or_create(data_dir, input_path, kind, speech_date, speakers)
    if _store_speaker_info(store, speaker_info):
        invalidated = earliest_step(invalidated, "speakers")
    start = earliest_step(from_step, invalidated)
    if invalidated:
        logger.info("Parámetros cambiados: se recalcula desde %s", invalidated)
    steps = build_steps(store, input_path, deps, purge_audio_after=purge_audio)
    steps_run = run_steps(store, steps, prompts_dir, start)

    report = store.read("report.json", Report)
    report_path = store.path("report.json")
    return JobResult(
        report=report,
        report_path=report_path,
        summary=format_summary(report, report_path=str(report_path)),
        steps_run=steps_run,
    )
