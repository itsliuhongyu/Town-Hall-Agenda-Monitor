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


class BoardDocsAdapter(AgendaAdapter):
    platform_name = "BoardDocs"

    def _candidate(self, gov_body: str, meeting_date: datetime, href: str, source_url: str, heading: str, key: str = "", *, captured: bytes | None = None, media_type: str | None = None) -> AgendaCandidate:
        # Client-generated BoardDocs agendas have a page/hash URL.  Their
        # captured detailed print view is HTML and must retain that type; URL
        # suffixes are not evidence of a PDF.
        ext = ".html" if captured is not None and (media_type or "").startswith("text/html") else os.path.splitext(urlparse(href).path)[1]
        if ext.lower() not in {".pdf", ".html", ".htm", ".docx"}:
            ext = ".pdf"
        # A rendered print panel has no downloadable URL.  Keep the real
        # public page as ``original_url`` and use the stable meeting id for
        # document identity/filenames; a fabricated fragment must never be
        # presented as a navigable remote document URL.
        filename_url = f"{href}|{key}" if captured is not None and key else href
        document_key = f"{key}:detailed-agenda" if captured is not None and key else href
        return AgendaCandidate(
            meeting_heading=heading or gov_body,
            local_date=meeting_date.date(),
            original_url=href,
            source_url=source_url,
            suggested_filename=_stable_filename(gov_body, meeting_date, filename_url, ext),
            document_kind="agenda",
            native_meeting_key=key or None,
            native_document_key=document_key,
            raw_date=meeting_date.strftime("%Y-%m-%d"),
            captured_bytes=captured,
            captured_media_type=media_type,
        )

    @staticmethod
    def _parse_date(element: Any) -> datetime | None:
        for attr in ("data-date", "datetime", "aria-label"):
            value = element_attr(element, attr)
            parsed = parse_public_date(value)
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
        href = element_attr(element, "href")
        if is_direct_href(href) and is_agenda_label(element_text(element), href):
            return urljoin(base_url, href)
        return None

    def _click_meetings(self, page: Any, timeout_ms: int = 15000) -> bool:
        """Open the observed Welcome/Featured → Meetings view if needed."""
        for _ in range(max(1, timeout_ms // 250)):
            controls = page.locator("a, button, [role='tab'], [role='button']")
            for index in range(locator_count(controls)):
                control = controls.nth(index)
                if not locator_visible(control):
                    continue
                label = accessible_text(control).strip().casefold()
                href = element_attr(control, "href").casefold()
                # BoardDocs renders the label through an icon font in the live
                # page.  The stable public tab target is the observed href.
                if not (label == "meetings" or "meetings" in label or href == "#tab-meetings"):
                    continue
                try:
                    control.click(timeout=5000)
                    return True
                except Exception:
                    continue
            page.wait_for_timeout(250)
        return False

    @staticmethod
    def _wait_initial_ready(page: Any, timeout_ms: int = 15000) -> None:
        """Wait for BoardDocs' initial app shell before selecting a tab."""
        loading = page.locator("#loading-boarddocs").first
        if locator_count(loading):
            loading.wait_for(state="hidden", timeout=timeout_ms)

    def _meeting_entries(self, page: Any) -> tuple[Any, int]:
        # On live BoardDocs, Featured and Meetings are separate regions.  A
        # site can expose a featured meeting before the Meetings tab is
        # opened, so scope to that tab whenever it exists and never let the
        # Welcome container masquerade as the selected list.
        meetings_panel = page.locator("#tab-meetings")
        if locator_count(meetings_panel):
            entries = meetings_panel.locator("a.icon.meeting, a.meeting")
            return entries, locator_count(entries)
        # Static LT/legacy fixtures and some public pages expose meeting cards.
        panels = page.locator(".meeting-panel, .board-meeting, li.meeting")
        if locator_count(panels):
            return panels, locator_count(panels)
        # The live BoardDocs Meetings view exposes dynamic anchors whose href
        # is '#'; the click opens the detail in the same public application.
        entries = page.locator("a.icon.meeting, a.meeting")
        if locator_count(entries):
            return entries, locator_count(entries)
        entries = page.locator("[class~='meeting']")
        return entries, locator_count(entries)

    @staticmethod
    def _has_explicit_empty(page: Any) -> bool:
        try:
            value = element_text(page.locator("body")).casefold()
        except Exception:
            return False
        return any(marker in value for marker in ("no meetings", "no meeting records", "no upcoming meetings"))

    def _expand_year_groups(self, page: Any, date_start: datetime, date_end: datetime) -> None:
        years = {str(year) for year in range(date_start.year, date_end.year + 1)}
        controls = page.locator("a, button, [role='tab'], [role='button']")
        for index in range(locator_count(controls)):
            control = controls.nth(index)
            if not locator_visible(control):
                continue
            label = accessible_text(control).strip()
            if not re.fullmatch(r"(?:20\d{2})(?:\s+20\d{2})?", label):
                continue
            if not any(year in label.split() for year in years):
                continue
            if element_attr(control, "aria-expanded").casefold() == "true":
                continue
            try:
                control.click(timeout=3000)
                page.wait_for_timeout(250)
            except Exception:
                continue

    @staticmethod
    def _meeting_signature(page: Any) -> str:
        entries, count = BoardDocsAdapter()._meeting_entries(page)
        values = []
        for index in range(count):
            entry = entries.nth(index)
            values.append(element_attr(entry, "class") or element_text(entry)[:100])
        return "|".join(values)

    @staticmethod
    def _meeting_key(entry: Any, index: int, meeting_date: datetime) -> str:
        element_id = element_attr(entry, "id")
        if element_id:
            return element_id
        data_id = element_attr(entry, "data-id")
        if data_id:
            return data_id
        classes = element_attr(entry, "class")
        match = re.search(r"(?:^|\s)meeting([A-Za-z0-9_-]+)", classes)
        return match.group(1) if match else f"{meeting_date.date().isoformat()}-{index}"

    @staticmethod
    def _meeting_title(value: str) -> str:
        value = re.sub(r"\s*\((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?\)\s*", " ", value or "", flags=re.I)
        value = re.sub(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:day)?\b", " ", value, flags=re.I)
        value = re.sub(r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+\d{1,2},?\s+\d{4}\b", " ", value, flags=re.I)
        return re.sub(r"\s+", " ", value).strip().casefold()

    def _capture_detailed_agenda(self, page: Any, expected_date: datetime | None = None, expected_heading: str = "") -> tuple[bytes, str] | None:
        """Capture BoardDocs' complete detailed print panel after it settles."""
        print_buttons = page.locator("#btn-print-agenda1")
        print_button = None
        for index in range(locator_count(print_buttons)):
            candidate = print_buttons.nth(index)
            if locator_visible(candidate):
                print_button = candidate
                break
        if print_button is None:
            print_button = print_buttons.first
        try:
            print_button.wait_for(state="visible", timeout=12000)
            print_button.click(timeout=5000)
        except Exception:
            return None

        dialog = page.locator("[role='dialog']").filter(has_text="What would you like to print?")
        if not locator_count(dialog):
            dialog = page.locator("[role='dialog']").first
        try:
            dialog.wait_for(state="visible", timeout=10000)
        except Exception:
            return None

        tabs = dialog.locator("[role='tab']")
        detailed = None
        for index in range(locator_count(tabs)):
            tab = tabs.nth(index)
            if "detailed agenda" in accessible_text(tab).casefold():
                detailed = tab
                break
        if detailed is None:
            return None
        try:
            detailed.click(timeout=5000)
        except Exception:
            return None

        panel = dialog.locator("#tab-2").first
        if not locator_count(panel):
            panel = dialog.locator("[role='tabpanel']").filter(has_text="Agenda").first
        for _ in range(40):
            try:
                panel_date = self._parse_date(panel.locator(".print-meeting-date").first) if locator_count(panel.locator(".print-meeting-date")) else None
                panel_heading = element_text(panel.locator(".print-meeting-name").first) if locator_count(panel.locator(".print-meeting-name")) else ""
                expected_title = self._meeting_title(expected_heading)
                actual_title = self._meeting_title(panel_heading)
                heading_ok = bool(expected_title and actual_title and (expected_title == actual_title or expected_title in actual_title or actual_title in expected_title))
                if (expected_date is None or (panel_date and panel_date.date() == expected_date.date())) and heading_ok and locator_visible(panel) and locator_count(panel.locator(".container.item.agendaorder, .itembody")):
                    html = panel.evaluate("node => node.outerHTML")
                    if html and len(html) <= 50 * 1024 * 1024:
                        return html.encode("utf-8"), "text/html"
            except Exception:
                pass
            page.wait_for_timeout(250)
        return None

    def _wait_meeting_detail(self, page: Any, expected_heading: str, expected_date: datetime | None = None, timeout_ms: int = 12000) -> None:
        """Wait until a clicked entry's detail replaces any prior meeting."""
        expected_title = self._meeting_title(expected_heading)
        details = page.locator("#view-meeting, #wrap-meeting, .meeting-detail")
        if not locator_count(details):
            page.locator("#btn-print-agenda1").wait_for(state="visible", timeout=timeout_ms)
            return
        for _ in range(max(1, timeout_ms // 250)):
            for index in range(locator_count(details)):
                detail = details.nth(index)
                if not locator_visible(detail):
                    continue
                name = detail.locator(".meeting-name, [role='heading']")
                actual_title = self._meeting_title(element_text(name.first)) if locator_count(name) else self._meeting_title(element_text(detail))
                meeting_date = detail.locator(".meeting-date")
                actual_date = self._parse_date(meeting_date.first) if locator_count(meeting_date) else None
                if expected_title and actual_title and (expected_title in actual_title or actual_title in expected_title):
                    if expected_date is not None and actual_date is not None and actual_date.date() != expected_date.date():
                        continue
                    buttons = page.locator("#btn-print-agenda1")
                    if any(locator_visible(buttons.nth(i)) for i in range(locator_count(buttons))):
                        return
            page.wait_for_timeout(250)
        raise RuntimeError("selected BoardDocs meeting detail did not settle")

    @staticmethod
    def _close_dialog(page: Any) -> None:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

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
                        raise failure("rate_limited", "BoardDocs temporarily rate-limited this request.", retryable=True, details=page_details(page, response))
                    if blocked_page(page, response):
                        raise failure("access_blocked", "BoardDocs denied access or presented a security verification page.", details=page_details(page, response))

                    try:
                        self._wait_initial_ready(page)
                    except Exception as exc:
                        raise failure("load_timeout", "BoardDocs did not finish loading its public page.", retryable=True, details=page_details(page, response)) from exc

                    entries, count = self._meeting_entries(page)
                    has_meetings_tab = locator_count(page.locator("[href='#tab-meetings']")) > 0
                    if has_meetings_tab or not count:
                        if not self._click_meetings(page):
                            body = element_text(page.locator("body"))
                            if not body:
                                raise failure("load_timeout", "BoardDocs did not finish loading its public page.", retryable=True, details=page_details(page, response))
                            raise failure("unsupported_structure", "The BoardDocs page did not expose a Meetings tab.", details=page_details(page, response))
                        try:
                            for _ in range(60):
                                entries, count = self._meeting_entries(page)
                                if count and any(locator_visible(entries.nth(i)) for i in range(count)):
                                    break
                                if self._has_explicit_empty(page):
                                    return DiscoveryResult(True, True, (), (), True)
                                page.wait_for_timeout(250)
                            else:
                                raise RuntimeError("meeting list did not populate")
                        except Exception as exc:
                            if blocked_page(page, response):
                                raise failure("access_blocked", "BoardDocs denied access or presented a security verification page.", details=page_details(page, response)) from exc
                            # Only a platform-provided empty-state message is
                            # evidence of an empty loaded list.  A shell or
                            # loading container can still be waiting for JS.
                            if self._has_explicit_empty(page):
                                return DiscoveryResult(True, True, (), (), True)
                            raise failure("load_timeout", "BoardDocs did not finish loading its Meetings list.", retryable=True, details=page_details(page, response)) from exc
                        entries, count = self._meeting_entries(page)

                    # Some BoardDocs releases render one expanded year on
                    # first load and keep the requested year groups as
                    # collapsed accordions.  Expand only controls whose
                    # entire accessible label is a year; meeting links also
                    # contain the year but are deliberately ignored.
                    self._expand_year_groups(page, date_start, date_end)
                    entries, count = self._meeting_entries(page)

                    if not count:
                        # The legacy explicit-empty fixture contains a panel
                        # shell.  It proves the platform template was loaded.
                        if locator_count(page.locator(".meeting-panel")):
                            return DiscoveryResult(True, True, (), (), True)
                        raise failure("unsupported_structure", "The BoardDocs page did not expose a recognized meeting list.", details=page_details(page, response))

                    candidates: list[AgendaCandidate] = []
                    parsed_count = 0
                    in_window_missing = 0
                    empty_shells = 0
                    seen: set[str] = set()
                    for index in range(count):
                        entry = entries.nth(index)
                        # BoardDocs keeps mobile/desktop and Featured clones
                        # in the DOM.  Hidden clones are not selectable and
                        # can point at stale detail state; process the active
                        # visible entry after expanding its year group.
                        if not locator_visible(entry):
                            continue
                        raw = element_text(entry)
                        href = self._agenda_href(entry, page.url)
                        meeting_date = self._parse_date(entry)
                        if not meeting_date:
                            # A completely empty panel is the maintained
                            # explicit-empty fixture; a non-empty entry is a
                            # malformed date and must remain a failure.
                            if not raw and not href:
                                empty_shells += 1
                                continue
                            if not href and self._has_explicit_empty(page):
                                empty_shells += 1
                                continue
                            raise failure("date_parse_failed", "BoardDocs returned a meeting entry whose date could not be parsed.", details=page_details(page, response))
                        parsed_count += 1
                        if not is_date_in_window(meeting_date, date_start, date_end):
                            continue
                        captured = None
                        captured_media_type = None
                        meeting_key = self._meeting_key(entry, index, meeting_date)
                        if not href and element_attr(entry, "href") in {"#", ""}:
                            try:
                                entry.click(timeout=5000)
                            except Exception:
                                raise failure("meeting_detail_failed", "BoardDocs did not finish loading the selected meeting details.", retryable=True, details={**page_details(page, response), "meeting": raw[:160]})
                            # Wait for this meeting's details, then use the
                            # detailed print panel.  Searching the whole body
                            # for the first agenda link can select an old
                            # meeting or an attachment from another meeting.
                            try:
                                self._wait_meeting_detail(page, raw, meeting_date, timeout_ms=12000)
                            except Exception:
                                raise failure("meeting_detail_failed", "BoardDocs did not finish loading the selected meeting details.", retryable=True, details={**page_details(page, response), "meeting": raw[:160]})
                            detail = self._capture_detailed_agenda(page, meeting_date, raw)
                            self._close_dialog(page)
                            if detail:
                                captured, captured_media_type = detail
                                # The captured bytes are associated with the
                                # actual public page; ``meeting_key`` keeps
                                # same-page meetings distinct in storage.
                                href = url
                            else:
                                raise failure("document_unavailable", "BoardDocs did not expose a complete Detailed Agenda for the selected meeting.", retryable=True, details={**page_details(page, response), "meeting": raw[:160]})
                        if not href:
                            # BoardDocs' observed Download Agenda as PDF control
                            # is client-side (href="#").  There is no stable
                            # public URL to pass to the downloader, so report
                            # this explicitly instead of inventing one.
                            in_window_missing += 1
                            continue
                        identity = f"{meeting_key}:detailed-agenda" if captured is not None else href
                        if identity in seen:
                            continue
                        seen.add(identity)
                        candidates.append(self._candidate(gov_body, meeting_date, href, url, raw[:160], meeting_key, captured=captured, media_type=captured_media_type))

                    if in_window_missing:
                        raise failure("document_unavailable", "A BoardDocs meeting in the requested window has no stable public agenda URL.", details={**page_details(page, response), "missing_documents": in_window_missing})
                    if not parsed_count and empty_shells:
                        return DiscoveryResult(True, True, (), (), True)
                    if not parsed_count:
                        raise failure("date_parse_failed", "BoardDocs returned meeting entries but no dates could be parsed.", details=page_details(page, response))
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
            error = result.warnings[0] if result.warnings else ErrorInfo("discovery_failed", "BoardDocs discovery failed.", True, "discovery")
            raise RuntimeError(f"{error.code}: {error.message}")
        return [AgendaItem(gov_body, datetime.combine(candidate.local_date or date_start.date(), datetime.min.time()), candidate.original_url or "", candidate.suggested_filename, candidate.source_url, candidate.captured_bytes, candidate.captured_media_type) for candidate in result.candidates]
