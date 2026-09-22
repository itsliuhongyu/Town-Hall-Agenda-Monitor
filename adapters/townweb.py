from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Any, List
from urllib.parse import urljoin, urlparse

from adapters.base import AgendaAdapter, AgendaItem, _stable_filename, is_date_in_window
from adapters._common import (
    AdapterFailure,
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


def _category_key(value: str) -> str:
    value = re.sub(r"\b(?:town|village|city|county|township)\s+of\s+", "", value, flags=re.I)
    return re.sub(r"[-_/+]+", " ", value).strip().casefold()


def _category_matches(label: str, gov_body: str) -> bool:
    left, right = _category_key(label), _category_key(gov_body)
    if not left or left in {"all", "all meetings", "all categories"}:
        return False
    return left == right or left in right or right in left


class TownWebAdapter(AgendaAdapter):
    platform_name = "TownWeb"
    _table_selectors = "table.tw-meeting-repo-table, table.twpb-agendas-minutes-table"
    _card_selectors = ".twpb-agendas-minutes-card, .twpb-agendas-minutes-row, .am-row-item, .am-item"

    def _candidate(self, *, gov_body: str, meeting_date: datetime, href: str,
                   source_url: str, heading: str = "", native_key: str = "") -> AgendaCandidate:
        suffix = os.path.splitext(urlparse(href).path)[1].lower()
        ext = suffix if suffix in {".pdf", ".html", ".htm", ".docx"} else ".pdf"
        return AgendaCandidate(
            meeting_heading=heading or gov_body,
            local_date=meeting_date.date(),
            original_url=href,
            source_url=source_url,
            suggested_filename=_stable_filename(gov_body, meeting_date, href, ext),
            document_kind="agenda",
            native_meeting_key=native_key or None,
            native_document_key=href,
            raw_date=meeting_date.strftime("%Y-%m-%d"),
        )

    @staticmethod
    def _parse_date(element: Any) -> datetime | None:
        for attr in ("data-am-date", "data-date", "datetime", "aria-label"):
            parsed = parse_public_date(element_attr(element, attr))
            if parsed:
                return parsed
        return parse_public_date(element_text(element))

    @staticmethod
    def _href_from_links(element: Any, base_url: str) -> str | None:
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
            href = element_attr(link, "href")
            label = element_text(link)
            if is_direct_href(href) and is_agenda_label(label, href):
                return urljoin(base_url, href)
        return None

    def _select_category(self, page: Any, gov_body: str) -> None:
        """Select a named category; never silently use an All panel."""
        tabs = page.locator(".twpb-agendas-minutes-tab")
        count = locator_count(tabs)
        for index in range(count):
            tab = tabs.nth(index)
            label_el = tab.locator(".am-tab-label")
            label = element_text(label_el.first) if locator_count(label_el) else element_text(tab)
            if _category_matches(label, gov_body):
                try:
                    tab.click(timeout=5000)
                except Exception as exc:
                    raise failure("category_load_failed", "The TownWeb meeting category could not be selected.", retryable=True, details={"category": label[:120]}) from exc
                return

        # The older TownWeb template uses a select rather than category tabs.
        select = page.locator("#categoriesSelect")
        if locator_count(select):
            options = select.locator("option")
            for index in range(locator_count(options)):
                option = options.nth(index)
                label = element_text(option)
                if _category_matches(label, gov_body):
                    try:
                        value = element_attr(option, "value")
                        select.select_option(value=value if value else None, label=None if value else label)
                        page.wait_for_timeout(150)
                    except Exception as exc:
                        raise failure("category_load_failed", "The TownWeb meeting category could not be selected.", retryable=True, details={"category": label[:120]}) from exc
                    return
            raise failure("category_not_found", "The TownWeb page has categories but none matches the configured government body.", details={"requested_category": gov_body[:120]})

        if count == 0:
            return
        raise failure("category_not_found", "The TownWeb page has categories but none matches the configured government body.", details={"requested_category": gov_body[:120]})

    def _load_more(self, page: Any) -> None:
        buttons = page.locator(".twpb-agendas-minutes-load-more")
        for _ in range(24):
            clicked = False
            before = locator_count(page.locator(self._card_selectors))
            for index in range(locator_count(buttons)):
                button = buttons.nth(index)
                if not locator_visible(button):
                    continue
                try:
                    if not button.is_enabled():
                        continue
                    button.click(timeout=3000)
                    clicked = True
                    page.wait_for_timeout(100)
                    break
                except Exception:
                    continue
            if not clicked or locator_count(page.locator(self._card_selectors)) <= before:
                break

    def _wait_list_ready(self, page: Any, tables: Any, timeout_ms: int = 15000) -> None:
        """Wait for a populated category/list, while allowing a real empty list."""
        for _ in range(max(1, timeout_ms // 250)):
            active_panel = page.locator(".twpb-agendas-minutes-panel.twpb-active")
            scope = active_panel if locator_count(active_panel) else page
            if locator_count(scope.locator(self._card_selectors)) or locator_count(scope.locator("tbody tr")):
                return
            loading = locator_visible(page.locator(".loading, .spinner, [aria-label*='loading' i], [role='progressbar']"))
            try:
                busy = element_attr(tables.first, "aria-busy").casefold() == "true"
            except Exception:
                busy = False
            if not loading and not busy:
                page.wait_for_timeout(500)
                if locator_count(scope.locator(self._card_selectors)) or locator_count(scope.locator("tbody tr")):
                    return
                try:
                    body = element_text(page.locator("body")).casefold()
                except Exception:
                    body = ""
                if any(marker in body for marker in ("no meetings", "no records", "no agendas")):
                    return
            page.wait_for_timeout(250)
        raise failure("load_timeout", "TownWeb did not finish loading its meeting list.", retryable=True)

    def _fetch_result(self, url: str, gov_body: str, date_start: datetime, date_end: datetime, *, headless: bool) -> DiscoveryResult:
        from playwright.sync_api import sync_playwright

        response = None
        page = None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=headless)
                try:
                    page = browser.new_page()
                    response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    if response_status(response) == 429:
                        raise failure("rate_limited", "TownWeb temporarily rate-limited this request.", retryable=True, details=page_details(page, response))
                    if blocked_page(page, response):
                        raise failure("access_blocked", "TownWeb denied access or presented a security verification page.", details=page_details(page, response))
                    tables = page.locator(self._table_selectors)
                    try:
                        tables.first.wait_for(state="attached", timeout=15000)
                        self._wait_list_ready(page, tables, timeout_ms=15000)
                    except Exception as exc:
                        if blocked_page(page, response):
                            raise failure("access_blocked", "TownWeb denied access or presented a security verification page.", details=page_details(page, response)) from exc
                        raise failure("load_timeout", "TownWeb did not finish loading its agendas list.", retryable=True, details=page_details(page, response)) from exc

                    self._select_category(page, gov_body)
                    self._load_more(page)
                    active_panel = page.locator(".twpb-agendas-minutes-panel.twpb-active")
                    panels = page.locator(".twpb-agendas-minutes-panel")
                    if locator_count(active_panel):
                        scope = active_panel
                    elif locator_count(panels) == 1:
                        scope = panels.first
                    elif locator_count(panels) > 1:
                        raise failure("category_load_failed", "TownWeb did not expose the selected government-body category.", details={"requested_category": gov_body[:120]})
                    else:
                        scope = page
                    cards = scope.locator(self._card_selectors)
                    rows = scope.locator("tbody tr")
                    card_count, row_count = locator_count(cards), locator_count(rows)
                    count = card_count or row_count
                    if count == 0:
                        return DiscoveryResult(True, True, (), (), True)

                    candidates: list[AgendaCandidate] = []
                    parsed_rows = 0
                    missing_documents = 0
                    seen: set[str] = set()
                    for index in range(count):
                        row = cards.nth(index) if card_count else rows.nth(index)
                        meeting_date = self._parse_date(row)
                        if not meeting_date:
                            raise failure("date_parse_failed", "TownWeb returned a meeting row whose date could not be parsed.", details=page_details(page, response))
                        parsed_rows += 1
                        if not is_date_in_window(meeting_date, date_start, date_end):
                            continue
                        href = self._href_from_links(row, page.url)
                        if not href:
                            missing_documents += 1
                            continue
                        if href in seen:
                            continue
                        seen.add(href)
                        candidates.append(self._candidate(gov_body=gov_body, meeting_date=meeting_date, href=href, source_url=page.url, heading=element_text(row)[:160], native_key=element_attr(row, "data-index")))

                    if missing_documents:
                        raise failure("document_unavailable", "A TownWeb meeting in the requested window has no published agenda link.", details={**page_details(page, response), "missing_documents": missing_documents})
                    if not parsed_rows:
                        raise failure("date_parse_failed", "TownWeb returned meeting rows but no dates could be parsed.", details=page_details(page, response))
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
            error = result.warnings[0] if result.warnings else ErrorInfo("discovery_failed", "TownWeb discovery failed.", True, "discovery")
            raise RuntimeError(f"{error.code}: {error.message}")
        items: list[AgendaItem] = []
        for candidate in result.candidates:
            meeting_date = datetime.combine(candidate.local_date or date_start.date(), datetime.min.time())
            items.append(AgendaItem(gov_body, meeting_date, candidate.original_url or "", candidate.suggested_filename, candidate.source_url))
        return items
