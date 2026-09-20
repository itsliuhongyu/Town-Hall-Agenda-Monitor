from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime
from typing import List


def local_calendar_date(value: date | datetime) -> date:
    """Return the local calendar date represented by a date-like value."""
    return value.date() if isinstance(value, datetime) else value


def is_date_in_window(meeting_date: date | datetime, date_start: date | datetime, date_end: date | datetime) -> bool:
    """Compare agenda dates by calendar day, never by time of day.

    Adapter pages normally expose meetings at midnight while the downloader
    creates its window from the current clock time.  Comparing these values as
    datetimes silently drops today's meetings after midnight.
    """
    meeting_day = local_calendar_date(meeting_date)
    return local_calendar_date(date_start) <= meeting_day <= local_calendar_date(date_end)


@dataclass
class AgendaItem:
    gov_body: str
    meeting_date: datetime
    download_url: str
    filename: str
    source_url: str
    captured_bytes: bytes | None = None
    captured_media_type: str | None = None


def _stable_filename(gov_body: str, meeting_date: datetime, download_url: str, extension: str = ".pdf") -> str:
    """Build a collision-resistant, deterministic local agenda filename."""
    import hashlib
    import re

    gov_body_key = re.sub(r"[^\w\s-]", "", gov_body).strip().replace(" ", "_")
    date_key = local_calendar_date(meeting_date).isoformat()
    url_key = hashlib.sha256(download_url.strip().encode("utf-8")).hexdigest()[:10]
    extension = extension if extension.startswith(".") else f".{extension}"
    return f"{gov_body_key}_{date_key}_{url_key}{extension}"


class AgendaAdapter(ABC):
    platform_name: str = ""

    @abstractmethod
    def fetch_agendas(
        self,
        url: str,
        gov_body: str,
        date_start: datetime,
        date_end: datetime,
        headless: bool = True,
    ) -> List[AgendaItem]:
        ...
