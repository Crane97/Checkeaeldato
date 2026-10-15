"""Trabajo por afirmación en paralelo (fase I)."""

import datetime as dt
import threading
import time

import pytest

from pipeline.concurrency import parallel_map
from pipeline.evidence.base import EvidenceConfigError, SourceResult, collect_evidence
from pipeline.models import DetectedClaim, EvidenceItem, Source


def test_results_keep_the_order_and_run_at_once():
    active, peak = [0], [0]
    lock = threading.Lock()

    def slow(n: int) -> int:
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return n * 10

    assert parallel_map(slow, [3, 1, 2], max_workers=3) == [30, 10, 20]
    assert peak[0] > 1
    assert parallel_map(slow, [1, 2], max_workers=1) == [10, 20]


def test_an_exception_stops_the_work_and_is_raised():
    def boom(n: int) -> int:
        if n == 2:
            raise ValueError("config")
        return n

    with pytest.raises(ValueError, match="config"):
        parallel_map(boom, [1, 2, 3, 4], max_workers=2)


def claim(i: int) -> DetectedClaim:
    return DetectedClaim(
        id=f"c{i}", speaker="A", text_original=f"x{i}", text_normalized=f"x{i}",
        claim_type="estadistica",
    )  # fmt: skip


class FakeSource:
    name = "oficial"

    def __init__(self, fail_config: bool = False) -> None:
        self.fail_config = fail_config

    def search(self, c: DetectedClaim, speech_date: dt.date) -> SourceResult:
        if self.fail_config:
            raise EvidenceConfigError("clave inválida")
        time.sleep(0.01)
        src = Source(url=f"https://x.es/{c.id}", title=c.id, organization="INE", kind="oficial")
        return SourceResult(items=[EvidenceItem(id="", source=src, excerpt=c.id)])


def test_parallel_evidence_equals_sequential_and_config_errors_still_stop():
    claims = [claim(i) for i in range(6)]
    seq = collect_evidence(claims, [FakeSource()], dt.date(2026, 1, 1), max_workers=1)
    par = collect_evidence(claims, [FakeSource()], dt.date(2026, 1, 1), max_workers=4)
    assert [e.model_dump() for e in par] == [e.model_dump() for e in seq]
    with pytest.raises(EvidenceConfigError):
        collect_evidence(claims, [FakeSource(fail_config=True)], dt.date(2026, 1, 1), max_workers=4)
