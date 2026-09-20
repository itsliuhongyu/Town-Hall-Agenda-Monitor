from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Any, List
from urllib.parse import urljoin, urlparse

from adapters.base import AgendaAdapter, AgendaItem, _stable_filename, is_date_in_window
from adapters._common import (
    AdapterFailure,
    accessible_text,
    element_disabled,
    element_attr,
    element_text,
    error_from_exception,
    failure,
    is_agenda_label,
    is_direct_href,
    locator_count,
    locator_visible,
    parse_public_date,
    page_details,
    blocked_page,
    response_status,
)
from agenda_app.domain import AgendaCandidate, DateWindow, DiscoveryResult, ErrorInfo, SourceSnapshot


def _gov_body_safe(gov_body: str) -> str:
    return re.sub(r"[^\w\s-]", "", gov_body).strip().replace(" ", "_")


class CivicClerkAdapter(AgendaAdapter):
    platform_name = "CivicClerk"

    def _candidate(self, gov_body: str, meeting_date: datetime, href: str, source_url: str, heading: str, key: str = "", *, extension: str = ".pdf") -> AgendaCandidate:
        suffix = os.path.splitext(urlparse(href).path)[1].lower()
        ext = suffix if suffix in {".pdf", ".html", ".htm", ".docx"} else extension
        return AgendaCandidate(
            meeting_heading=heading or gov_body,
            local_date=meeting_date.date(),
            original_url=href,
            source_url=source_url,
            suggested_filename=_stable_filename(gov_body, meeting_date, href, ext),
            document_kind="agenda",
            native_meeting_key=key or None,
            native_document_key=href,
            raw_date=meeting_date.strftime("%Y-%m-%d"),
        )

    @staticmethod
    def _parse_date(element: Any) -> datetime | None:
        for attr in ("data-date", "datetime", "aria-label", "title"):
            parsed = parse_public_date(element_attr(element, attr))
            if parsed:
                return parsed
        return parse_public_date(element_text(element))

    @staticmethod
    def _agenda_href(element: Any, base_url: str) -> str | None:
        try:
            links = element.locator("a")
            count = locator_count(links)
        except Exception:
            try:
                links = element.query_selector_all("a")
                count = len(links)
            except Exception:
                links, count = [], 0
        for index in range(count):
            link = links.nth(index) if hasattr(links, "nth") else links[index]
            href, label = element_attr(link, "href"), element_text(link)
            if is_direct_href(href) and is_agenda_label(label, href):
                return urljoin(base_url, href)
        return None

    def _legacy_rows(self, page: Any, url: str, gov_body: str, date_start: datetime, date_end: datetime, response: Any) -> DiscoveryResult | None:
        table = page.locator("table.meetings-table")
        if not locator_count(table):
            return None
        rows = table.locator("tbody tr")
        if not locator_count(rows):
            if self._has_explicit_empty(page, table):
                return DiscoveryResult(True, True, (), (), True)
            raise failure("load_timeout", "CivicClerk exposed an empty events table without proving that loading completed.", retryable=True, details=page_details(page, response))
        candidates: list[AgendaCandidate] = []
        parsed = 0
        missing = 0
        for index in range(locator_count(rows)):
            row = rows.nth(index)
            meeting_date = self._parse_date(row)
            if not meeting_date:
                # Legacy rows with no date are malformed, even if another row
                # happens to be valid.
                raise failure("date_parse_failed", "CivicClerk returned a meeting row whose date could not be parsed.", details=page_details(page, response))
            parsed += 1
            if not is_date_in_window(meeting_date, date_start, date_end):
                continue
            href = self._agenda_href(row, page.url)
            if not href:
                missing += 1
                continue
            candidates.append(self._candidate(gov_body, meeting_date, href, page.url, element_text(row)[:160]))
        if missing:
            raise failure("document_unavailable", "A CivicClerk meeting in the requested window has no published agenda link.", details={**page_details(page, response), "missing_documents": missing})
        if not parsed:
            raise failure("date_parse_failed", "CivicClerk returned rows but no dates could be parsed.", details=page_details(page, response))
        unique = {candidate.original_url: candidate for candidate in candidates}
        return DiscoveryResult(True, not unique, tuple(unique.values()), (), True)

    @staticmethod
    def _event_heading(event: Any, gov_body: str) -> str:
        # CivicClerk renders the event date as h2 and the committee title as
        # h3.  Prefer the title so the candidate is never named after a date.
        headings = event.locator("h3, h1, h4, [role='heading']")
        for index in range(locator_count(headings)):
            value = element_text(headings.nth(index))
            if value:
                return value[:160]
        headings = event.locator("h2")
        for index in range(locator_count(headings)):
            value = element_text(headings.nth(index))
            if value and not parse_public_date(value):
                return value[:160]
        return element_text(event)[:160] or gov_body

    def _event_file_href(self, page: Any, event: Any, base_url: str) -> str | None:
        # The observed CivicClerk flow is event → Download Files → menuitem.
        # Read the href rendered by the page; never construct GetMeetingFileStream
        # URLs from an event id.
        buttons = event.locator("button")
        target = None
        for index in range(locator_count(buttons)):
            button = buttons.nth(index)
            label = accessible_text(button).casefold()
            if "download files" in label and locator_visible(button):
                target = button
                break
        if target is None:
            return None
        try:
            target.click(timeout=5000)
            page.wait_for_selector("[role='menu']:visible, [role='menuitem']:visible", timeout=5000)
        except Exception:
            return None

        menus = page.locator("[role='menu']:visible")
        menu = menus.last if locator_count(menus) else page.locator("[role='menuitem']:visible").first
        menu_items = menu.locator("[role='menuitem']") if locator_count(menus) else page.locator("[role='menuitem']:visible")
        links = menu.locator("a") if locator_count(menus) else page.locator("[role='menuitem']:visible a")
        candidates: list[tuple[str, str]] = []
        for index in range(locator_count(menu_items)):
            item = menu_items.nth(index)
            label = element_text(item)
            href = element_attr(item, "href")
            if not href:
                nested = item.locator("a")
                if locator_count(nested):
                    href = element_attr(nested.first, "href")
            if is_direct_href(href) and is_agenda_label(label, href):
                candidates.append((label.casefold(), urljoin(base_url, href)))
        # Some releases render role menuitems on anchors without a role on the
        # parent.  Restrict fallback links to a visible menu-ish label.
        if not candidates:
            for index in range(locator_count(links)):
                link = links.nth(index)
                label, href = element_text(link), element_attr(link, "href")
                if is_direct_href(href) and is_agenda_label(label, href) and "agenda" in label.casefold():
                    candidates.append((label.casefold(), urljoin(base_url, href)))
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        if not candidates:
            return None
        candidates.sort(key=lambda item: (0 if "agenda (pdf" in item[0] else 1 if "agenda" in item[0] else 2, len(item[0])))
        return candidates[0][1]

    @staticmethod
    def _event_signature(events: Any) -> str:
        parts = []
        for index in range(locator_count(events)):
            event = events.nth(index)
            parts.append(element_attr(event, "id") or element_attr(event, "aria-label") or element_text(event)[:120])
        return "|".join(parts)

    def _next_page(self, page: Any, current_signature: str) -> bool:
        # CivicClerk's observed event list uses a bounded "Load more
        # previous events" control.  Calendar navigation buttons such as
        # "Next month" are unrelated and must never be clicked here.
        controls = page.locator("#startScreen, button, a")
        for index in range(locator_count(controls)):
            control = controls.nth(index)
            if not locator_visible(control):
                continue
            label = accessible_text(control).casefold()
            if "load more previous event" not in label and "load previous event" not in label:
                continue
            if element_disabled(control):
                continue
            try:
                control.click(timeout=3000)
                for _ in range(24):
                    page.wait_for_timeout(250)
                    events = page.locator("ul[aria-label='Events by date'] li.meeting-event")
                    if self._event_signature(events) != current_signature:
                        return True
                raise failure("load_timeout", "CivicClerk did not finish loading older events.", retryable=True)
            except AdapterFailure:
                raise
            except Exception:
                continue
        return False

    @staticmethod
    def _has_explicit_empty(page: Any, events_list: Any) -> bool:
        markers = ("no events", "no meetings", "no upcoming events", "no results")
        try:
            value = f"{element_text(events_list)} {element_text(page.locator('body'))}".casefold()
        except Exception:
            return False
        return any(marker in value for marker in markers)

    def _wait_events_ready(self, page: Any, events_list: Any, timeout_ms: int = 15000) -> None:
        """Wait past the empty JavaScript list shell before declaring empty."""
        try:
            events_list.wait_for(state="attached", timeout=timeout_ms)
        except Exception as exc:
            raise exc
        for _ in range(max(1, timeout_ms // 250)):
            events = events_list.locator("li.meeting-event")
            if locator_count(events):
                return
            try:
                busy = element_attr(events_list, "aria-busy").casefold() == "true"
                loading = locator_visible(page.locator(".loading, .spinner, [aria-label*='loading' i], [role='progressbar']"))
            except Exception:
                busy = False; loading = False
            if not busy and not loading:
                # An aria-busy=false shell is still not enough evidence that
                # the app queried events.  Require its explicit empty-state
                # message; otherwise keep waiting and report load_timeout.
                page.wait_for_timeout(500)
                if locator_count(events_list.locator("li.meeting-event")):
                    return
                if self._has_explicit_empty(page, events_list):
                    return
            page.wait_for_timeout(250)
        raise failure("load_timeout", "CivicClerk did not finish loading its events list.", retryable=True)

    def _fetch_result(self, url: str, gov_body: str, date_start: datetime, date_end: datetime, *, headless: bool) -> DiscoveryResult:
        from playwright.sync_api import sync_playwright

        page = None
        response = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)
                try:
                    page = browser.new_page()
                    response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    if response_status(response) == 429:
                        raise failure("rate_limited", "CivicClerk temporarily rate-limited this request.", retryable=True, details=page_details(page, response))
                    if blocked_page(page, response):
                        raise failure("access_blocked", "CivicClerk denied access or presented a security verification page.", details=page_details(page, response))

                    legacy = self._legacy_rows(page, url, gov_body, date_start, date_end, response)
                    if legacy is not None:
                        return legacy
                    events_list = page.locator("ul[aria-label='Events by date']")
                    if not locator_count(events_list):
                        try:
                            self._wait_events_ready(page, events_list, timeout_ms=15000)
                        except Exception as exc:
                            if blocked_page(page, response):
                                raise failure("access_blocked", "CivicClerk denied access or presented a security verification page.", details=page_details(page, response)) from exc
                            if locator_count(page.locator("#root, #clerk-embed-listing")):
                                raise failure("load_timeout", "CivicClerk did not finish loading its events list.", retryable=True, details=page_details(page, response)) from exc
                            raise failure("unsupported_structure", "The CivicClerk page did not expose a recognized events list.", details=page_details(page, response)) from exc
                        events_list = page.locator("ul[aria-label='Events by date']")
                    else:
                        self._wait_events_ready(page, events_list, timeout_ms=15000)

                    candidates: list[AgendaCandidate] = []
                    parsed = 0
                    missing = 0
                    seen_pages: set[str] = set()
                    page_limit_reached = False
                    for page_number in range(24):
                        events = events_list.locator("li.meeting-event")
                        if not locator_count(events):
                            # The recognized list can legitimately be empty for
                            # a date window with no meetings.
                            break
                        for index in range(locator_count(events)):
                            event = events.nth(index)
                            meeting_date = self._parse_date(event)
                            if not meeting_date:
                                raise failure("date_parse_failed", "CivicClerk returned an event whose date could not be parsed.", details=page_details(page, response))
                            parsed += 1
                            if not is_date_in_window(meeting_date, date_start, date_end):
                                continue
                            href = self._event_file_href(page, event, page.url)
                            if not href:
                                missing += 1
                                continue
                            if href in {candidate.original_url for candidate in candidates}:
                                continue
                            candidates.append(self._candidate(gov_body, meeting_date, href, page.url, self._event_heading(event, gov_body), element_attr(event, "id")))
                        signature = self._event_signature(events)
                        if signature in seen_pages:
                            break
                        seen_pages.add(signature)
                        # The current CivicClerk list is date ordered and
                        # initially contains a broad range.  Load older pages
                        # only when the earliest observed event is still
                        # newer than the requested window start.
                        parsed_dates = [self._parse_date(events_list.locator("li.meeting-event").nth(i)) for i in range(locator_count(events_list.locator("li.meeting-event")))]
                        earliest = min((value for value in parsed_dates if value), default=None)
                        if earliest is not None and earliest.date() <= date_start.date():
                            break
                        if page_number == 23:
                            next_controls = page.locator("#startScreen, button, a")
                            page_limit_reached = any(locator_visible(next_controls.nth(i)) and "load more previous event" in accessible_text(next_controls.nth(i)).casefold() and not element_disabled(next_controls.nth(i)) for i in range(locator_count(next_controls)))
                            break
                        if not self._next_page(page, signature):
                            break
                    if page_limit_reached:
                        raise failure("pagination_limit", "CivicClerk has more event pages than the bounded discovery limit; retry or narrow the date window.", retryable=True, details={**page_details(page, response), "max_pages": 24})
                    if missing:
                        raise failure("document_unavailable", "A CivicClerk meeting in the requested window has no published agenda file.", details={**page_details(page, response), "missing_documents": missing})
                    if not parsed:
                        return DiscoveryResult(True, True, (), (), True)
                    return DiscoveryResult(True, not candidates, tuple(candidates), (), True)
                finally:
                    browser.close()
        except AdapterFailure as exc:
            return DiscoveryResult(False, False, (), (exc.error(),), True)
        except Exception as exc:
            return DiscoveryResult(False, False, (), (error_from_exception(exc, page=page, response=response),), False)

    def fetch_agendas_result(self, source: SourceSnapshot, window: DateWindow, *, headless: bool = True) -> DiscoveryResult:
        return self._fetch_result(source.collection_url, source.name, datetime.combine(window.start, datetime.min.time()), datetime.combine(window.end, datetime.max.time()), headless=headless)

    def fetch_agendas(self, url: str, gov_body: str, date_start: datetime, date_end: datetime, headless: bool = True) -> List[AgendaItem]:
        result = self._fetch_result(url, gov_body, date_start, date_end, headless=headless)
        if not result.recognized:
            error = result.warnings[0] if result.warnings else ErrorInfo("discovery_failed", "CivicClerk discovery failed.", True, "discovery")
            raise RuntimeError(f"{error.code}: {error.message}")
        return [AgendaItem(gov_body, datetime.combine(candidate.local_date or date_start.date(), datetime.min.time()), candidate.original_url or "", candidate.suggested_filename, candidate.source_url) for candidate in result.candidates]
