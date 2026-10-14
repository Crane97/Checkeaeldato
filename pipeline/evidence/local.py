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


def local_evidence(claim: DetectedClaim, base: IndicatorBase) -> EvidenceItem | None:
    """Local series covering the whole period of a factual claim, or None (search the web).
    Promises are about the future, so they always go to the web."""
    if claim.kind == "promesa" or not claim.indicator:
        return None
    period = claim_period(claim)
    if period is None:
        return None
    return base.evidence(claim.indicator, *period)
