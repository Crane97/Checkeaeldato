"""Evidencia de la base local de indicadores, antes de gastar búsquedas web."""

import datetime as dt

from pipeline.indicators import IndicatorBase
from pipeline.models import DetectedClaim, EvidenceItem


def claim_period(claim: DetectedClaim) -> tuple[dt.date, dt.date] | None:
    """The span the claim is about: the resolved periods if any, else its years."""
    if claim.context_status == "resuelto" and claim.time_scope:
        return min(r.start for r in claim.time_scope), max(r.end for r in claim.time_scope)
    if claim.years:
        return dt.date(min(claim.years), 1, 1), dt.date(max(claim.years), 12, 31)
    return None


def local_evidence(
    claim: DetectedClaim, base: IndicatorBase, speech_date: dt.date | None = None
) -> EvidenceItem | None:
    """Local series covering the whole period of a factual claim, or None (search the web).
    Promises are about the future, so they always go to the web.

    A claim cannot refer to data after its speech date, so the period ends there at most.
    If the base was refreshed after the speech, its last value is the latest one published
    then, and it also answers claims about "now" ("el paro está en…", "este año")."""
    if claim.kind == "promesa" or not claim.indicator:
        return None
    period = claim_period(claim)
    if period is None:
        return None
    start, end = period
    if speech_date is not None:
        end = min(end, speech_date)
        ind = base.indicators.get(claim.indicator)
        if ind is not None and end > ind.cubre_hasta and ind.actualizado >= speech_date:
            # "Now": the latest value published, even if it belongs to an earlier period.
            start, end = min(start, ind.serie[-1].desde), ind.cubre_hasta
    if end < start:
        return None
    return base.evidence(claim.indicator, start, end)


def partial_local_evidence(claim: DetectedClaim, base: IndicatorBase) -> EvidenceItem | None:
    """The part of the claim's period the base covers, when it does not cover all of it."""
    if claim.kind == "promesa" or not claim.indicator:
        return None
    period = claim_period(claim)
    return base.partial_evidence(claim.indicator, *period) if period else None


def on_demand_evidence(claim: DetectedClaim, source, speech_date: dt.date) -> EvidenceItem | None:
    """The official API's series when the local base does not cover the claim's period
    (`source` is an OnDemandSeries). Promises and claims without indicator: None."""
    if claim.kind == "promesa" or not claim.indicator:
        return None
    period = claim_period(claim)
    if period is None:
        return None
    start, end = period[0], min(period[1], speech_date)
    if end < start:
        return None
    return source.evidence(claim.indicator, start, end)
