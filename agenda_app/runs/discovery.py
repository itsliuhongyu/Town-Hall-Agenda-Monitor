from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..domain import AgendaCandidate, DateWindow, DiscoveryResult, ErrorInfo, SourceSnapshot


def discover_source(adapter: Any, source: SourceSnapshot, window: DateWindow, *, headless: bool = True) -> DiscoveryResult:
    """Call either a stage-2 adapter or a legacy adapter at one boundary."""
    try:
        if hasattr(adapter, "fetch_agendas_result"):
            result = adapter.fetch_agendas_result(source, window, headless=headless)
            if isinstance(result, DiscoveryResult):
                return result
            return DiscoveryResult(False, False, (), (ErrorInfo("invalid_discovery_result", "The adapter returned an invalid discovery result.", False, "discovery", {"adapter": type(adapter).__name__}),), False)
        raw = adapter.fetch_agendas(source.collection_url, source.name,
                                    datetime.combine(window.start, datetime.min.time()),
                                    datetime.combine(window.end, datetime.max.time()), headless=headless)
        candidates = []
        for item in raw or []:
            value = item.meeting_date.date() if isinstance(item.meeting_date, datetime) else item.meeting_date
            candidates.append(AgendaCandidate(meeting_heading=source.name, local_date=value, original_url=item.download_url,
                                              source_url=item.source_url, suggested_filename=item.filename, document_kind="agenda"))
        if candidates:
            return DiscoveryResult(True, False, tuple(candidates), (), True)
        # A legacy list API cannot tell an empty, successfully loaded page
        # from a timeout, blocked page, or parser failure.  Treating [] as an
        # explicit empty result caused every old adapter to hide those errors.
        return DiscoveryResult(False, False, (), (ErrorInfo("legacy_empty_unverified", "The legacy adapter returned no agendas without proving that its public list loaded.", True, "discovery", {"adapter": type(adapter).__name__, "source_url": source.collection_url[:240]}),), False)
    except Exception as exc:
        return DiscoveryResult(False, False, (), (ErrorInfo("discovery_failed", str(exc), True, "discovery", {"type": type(exc).__name__}),), False)
