"""Small, dependency-free helpers shared by the public-page adapters.

The adapters deliberately keep the Playwright layer thin.  A page is only
considered empty after its known list has loaded; all other navigation and
parsing failures are represented as an :class:`AdapterFailure` so discovery
can report a useful, bounded diagnostic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit, urlunsplit

from agenda_app.domain import ErrorInfo


MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2,
    "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def text(value: Any) -> str:
    """Return bounded, whitespace-normalised visible text."""
    value = "" if value is None else str(value)
    return re.sub(r"\s+", " ", value).strip()


def public_url(value: str | None) -> str:
    """Remove query/fragment data from URLs included in error diagnostics."""
    if not value:
        return ""
    try:
        parsed = urlsplit(str(value))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))[:240]
    except Exception:
        return str(value)[:240]


def parse_public_date(value: Any) -> datetime | None:
    """Parse dates seen in the public templates.

    This intentionally looks for a single date-shaped token rather than
    passing an entire row to a permissive date parser.  That avoids selecting
    a publication or minutes date when an agenda row contains more than one.
    """
    raw = text(value)
    if not raw:
        return None
    raw = re.sub(r"\([^)]*\)", " ", raw)
    raw = re.sub(r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b[,:]?", " ", raw, flags=re.I)
    raw = re.sub(r"\s+", " ", raw).strip()

    for candidate in (raw, raw.replace("/", "-")):
        for fmt in (
            "%Y-%m-%d", "%Y-%m-%d %H:%M", "%m-%d-%Y", "%m-%d-%Y %I:%M %p",
            "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y",
            "%B %d %Y", "%b %d %Y", "%d %B, %Y", "%d %b, %Y",
            "%B %d, %Y %I:%M %p", "%b %d, %Y %I:%M %p",
        ):
            try:
                return datetime.strptime(candidate.strip(), fmt)
            except ValueError:
                pass

    # Match both "30 SEP 2026" and "September 30, 2026" inside row text.
    month_pattern = "|".join(sorted(MONTHS, key=len, reverse=True))
    patterns = (
        rf"\b(?P<day>\d{{1,2}})\s+(?P<month>{month_pattern})\.?\s*,?\s+(?P<year>\d{{4}})\b",
        rf"\b(?P<month>{month_pattern})\.?\s+(?P<day>\d{{1,2}}),?\s+(?P<year>\d{{4}})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, flags=re.I)
        if match:
            try:
                return datetime(int(match.group("year")), MONTHS[match.group("month").lower().rstrip(".")], int(match.group("day")))
            except (KeyError, ValueError):
                return None
    return None


def element_attr(element: Any, name: str) -> str:
    try:
        return text(element.get_attribute(name))
    except Exception:
        return ""


def element_text(element: Any) -> str:
    try:
        return text(element.inner_text())
    except Exception:
        return ""


def locator_count(locator: Any) -> int:
    try:
        return int(locator.count())
    except Exception:
        return 0


def locator_visible(locator: Any) -> bool:
    try:
        return bool(locator.is_visible())
    except Exception:
        return False


def page_title(page: Any) -> str:
    try:
        return text(page.title())[:160]
    except Exception:
        return ""


def page_body(page: Any) -> str:
    try:
        return text(page.locator("body").inner_text())[:2000]
    except Exception:
        return ""


def page_details(page: Any, response: Any = None, *, stage: str = "discovery") -> dict[str, Any]:
    status = None
    try:
        status = int(response.status) if response is not None else None
    except Exception:
        status = None
    details: dict[str, Any] = {
        "platform_url": public_url(getattr(page, "url", "")),
        "stage": stage,
    }
    if status is not None:
        details["http_status"] = status
    title = page_title(page)
    if title:
        details["page_title"] = title
    return details


def response_status(response: Any) -> int | None:
    """Return a navigation status without making Playwright response access fatal."""
    try:
        value = getattr(response, "status", None)
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


BLOCKED_MARKERS = (
    "just a moment...", "checking your browser before accessing",
    "verify you are human", "enable javascript and cookies to continue",
    "attention required! | cloudflare", "access denied - cloudflare",
    "request blocked by security policy", "security verification",
)


def blocked_page(page: Any, response: Any = None) -> bool:
    try:
        if response is not None and response_status(response) in {401, 403, 406}:
            return True
    except Exception:
        pass
    haystack = f"{page_title(page)} {page_body(page)}".lower()
    return any(marker in haystack for marker in BLOCKED_MARKERS)


def accessible_text(element: Any) -> str:
    """Read the small set of accessible-name attributes used by icon controls."""
    values = [element_text(element)]
    for name in ("aria-label", "title", "data-label", "data-name"):
        value = element_attr(element, name)
        if value:
            values.append(value)
    return text(" ".join(values))


def element_disabled(element: Any) -> bool:
    """Handle boolean disabled attributes whose value is the empty string."""
    try:
        if not bool(element.is_enabled()):
            return True
    except Exception:
        pass
    try:
        if element.get_attribute("disabled") is not None:
            return True
    except Exception:
        pass
    return element_attr(element, "aria-disabled").casefold() == "true"


@dataclass(frozen=True)
class AdapterFailure(Exception):
    code: str
    message: str
    retryable: bool = False
    stage: str = "discovery"
    details: dict[str, Any] | None = None

    def error(self) -> ErrorInfo:
        return ErrorInfo(self.code, self.message, self.retryable, self.stage, self.details or {})


def failure(code: str, message: str, *, retryable: bool = False, stage: str = "discovery", details: dict[str, Any] | None = None) -> AdapterFailure:
    return AdapterFailure(code, message, retryable, stage, details or {})


def error_from_exception(exc: Exception, *, page: Any = None, response: Any = None, stage: str = "discovery") -> ErrorInfo:
    if isinstance(exc, AdapterFailure):
        return exc.error()
    details = page_details(page, response, stage=stage) if page is not None else {"stage": stage}
    name = type(exc).__name__.lower()
    if "timeout" in name or "timeout" in str(exc).lower():
        return ErrorInfo("load_timeout", "The public page did not finish loading its meeting list in time.", True, stage, details)
    return ErrorInfo("network_error", "The public page could not be loaded.", True, stage, details)


def is_agenda_label(label: str, href: str = "") -> bool:
    label_value = text(label).casefold()
    # A visible Agenda/Packet label is authoritative.  Only inspect the URL
    # basename afterward: a directory such as /agendas-minutes/ may still
    # contain a real agenda PDF.
    if "agenda" in label_value or "packet" in label_value:
        return True
    if re.search(r"\bminutes?\b", label_value):
        return False
    try:
        basename = unquote(urlsplit(text(href)).path.rsplit("/", 1)[-1]).casefold()
    except Exception:
        basename = text(href).casefold().rsplit("/", 1)[-1]
    if re.search(r"\bminutes?\b", basename):
        return False
    return "agenda" in basename or "packet" in basename


def is_direct_href(href: str | None) -> bool:
    value = text(href)
    return bool(value and value not in {"#", "javascript:void(0)", "javascript:;"} and not value.lower().startswith("javascript:"))


def choose_first(items: Iterable[Any]) -> Any:
    return next(iter(items), None)
