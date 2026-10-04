"""Structured facts that NSE embeds in its own disclosure links.

NSE corporate-filing attachments are served as
``https://nsearchives.nseindia.com/corporate/<SYMBOL>_<DDMMYYYYHHMMSS>_<Subject>.pdf``
(e.g. ``.../corporate/ADSL_21052026224133_OutcomeofBoardMeeting.pdf``).
The prefix is the NSE trading symbol and the 14 digits are the exchange
dissemination time in IST. These are first-party identifiers, so when they
are present they beat any free-text name matching for both entity resolution
and the event clock.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

_CORPORATE_LINK = re.compile(
    r"/corporate/(?P<symbol>[A-Z0-9&\-]+)_(?P<stamp>\d{14})_(?P<subject>[^/]+?)\.(?:pdf|xml|zip|html?)$",
    re.IGNORECASE,
)
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


@dataclass(frozen=True)
class NSEFilingLink:
    symbol: str
    disseminated_at: datetime
    subject: str


def parse_nse_filing_link(url: str) -> NSEFilingLink | None:
    if not url:
        return None
    parts = urlsplit(url)
    if not parts.netloc.lower().endswith("nseindia.com"):
        return None
    match = _CORPORATE_LINK.search(unquote(parts.path))
    if not match:
        return None
    try:
        local = datetime.strptime(match.group("stamp"), "%d%m%Y%H%M%S").replace(tzinfo=IST)
    except ValueError:
        return None
    subject = _CAMEL.sub(" ", match.group("subject").replace("_", " ")).strip()
    return NSEFilingLink(
        symbol=match.group("symbol").upper(),
        disseminated_at=local.astimezone(timezone.utc),
        subject=subject,
    )
