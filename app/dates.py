"""A date for each page, for rules like "news before 2022": worked out from the sources a project
chooses, tried in order until one gives a date.

  url_date       a full or partial date in the path: folders (/news/2024/05/12/story, /2024/05/),
                 written out (/news/2024-05-12-story), or as 8 digits in a folder name, the way
                 some CMSs make them (/news/fri-01062023-1124-am/... is 2023-01-06)
  url_year       a year anywhere in the path: /news/2013-honors-convocation
  last_modified  the Last Modified header from the crawl (often the crawl date, so a weak fallback)
  crawl:<name>   any crawl column, such as a Screaming Frog custom extraction of the publish date

A date can be a whole year, a month, or a day. "Before" a day means the whole period is before it,
so a page dated only "2021" is before 2022-01-01, but one dated "2022" isn't before 2022-06-01.
"""
import calendar
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional
from urllib.parse import unquote

SOURCES = {
    "url_date": "A date in the URL (/2024/05/12/, /2024-05-12-…, /fri-01062023-…)",
    "url_year": "A year anywhere in the URL (/2013-convocation)",
    "last_modified": "Last Modified from the crawl",
}
DEFAULT_SOURCES = ["url_date", "url_year"]
CRAWL_PREFIX = "crawl:"

DATE_FOLDERS = re.compile(r"/((?:19|20)\d\d)(?:/(0?[1-9]|1[0-2]))?(?:/(0?[1-9]|[12]\d|3[01]))?(?=/|$)")
ISO_IN_NAME = re.compile(r"(?<!\d)((?:19|20)\d\d)-(\d\d)-(\d\d)(?!\d)")
EIGHT_DIGITS = re.compile(r"(?<!\d)(\d{8})(?!\d)")
YEAR_IN_NAME = re.compile(r"(?<!\d)((?:19|20)\d\d)(?!\d)")


@dataclass(frozen=True)
class Period:
    start: date
    end: date
    source: str

    @property
    def text(self) -> str:
        if self.start == self.end:
            return self.start.isoformat()
        if self.start.month == self.end.month and self.start.year == self.end.year:
            return self.start.strftime("%Y-%m")
        return str(self.start.year)

    def before(self, day: date) -> bool:
        return self.end < day

    def after(self, day: date) -> bool:
        return self.start > day


def _period(year: int, month: Optional[int], day: Optional[int], source: str) -> Optional[Period]:
    try:
        if day and month:
            d = date(year, month, day)
            return Period(d, d, source)
        if month:
            return Period(date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1]), source)
        return Period(date(year, 1, 1), date(year, 12, 31), source)
    except ValueError:
        return None


def parse_text(value: str, source: str) -> Optional[Period]:
    """Dates as crawls and pages write them: ISO, RFC 1123 headers, US and written-out forms."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        return Period(d, d, source)
    except ValueError:
        pass
    try:
        d = parsedate_to_datetime(value).date()
        return Period(d, d, source)
    except (TypeError, ValueError, IndexError):
        pass
    for fmt in ("%m/%d/%Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%Y/%m/%d", "%B %Y"):
        try:
            d = datetime.strptime(value, fmt).date()
            return _period(d.year, d.month, None if fmt == "%B %Y" else d.day, source)
        except ValueError:
            continue
    m = re.fullmatch(r"((?:19|20)\d\d)-(\d\d)", value) or re.fullmatch(r"((?:19|20)\d\d)", value)
    if m:
        return _period(int(m.group(1)), int(m.group(2)) if m.lastindex == 2 else None, None, source)
    return None


def _eight_digits(digits: str, kind: str) -> Optional[Period]:
    """YYYYMMDD, or MMDDYYYY (US order) when that's the only reading that's a real date."""
    if digits[:2] in ("19", "20"):
        found = _period(int(digits[:4]), int(digits[4:6]), int(digits[6:]), kind)
        if found:
            return found
    if digits[4:6] in ("19", "20"):
        return _period(int(digits[4:]), int(digits[:2]), int(digits[2:4]), kind)
    return None


def from_url(path: str, kind: str) -> Optional[Period]:
    path = unquote((path or "").split("?")[0])
    if kind == "url_date":
        best: Optional[Period] = None
        m = ISO_IN_NAME.search(path)
        if m:
            best = _period(int(m.group(1)), int(m.group(2)), int(m.group(3)), kind)
        for digits in ([] if best else EIGHT_DIGITS.findall(path)):
            best = _eight_digits(digits, kind)
            if best:
                break
        if not best:
            m = DATE_FOLDERS.search(path)
            if m:
                best = _period(int(m.group(1)), int(m.group(2)) if m.group(2) else None,
                               int(m.group(3)) if m.group(3) else None, kind)
        return best
    years = YEAR_IN_NAME.findall(path)
    return _period(int(years[0]), None, None, kind) if years else None


def clean_sources(value) -> List[str]:
    if not isinstance(value, list):
        raise ValueError("Date sources must be a list.")
    out: List[str] = []
    for s in value:
        s = str(s)
        if s in SOURCES or (s.startswith(CRAWL_PREFIX) and 0 < len(s) - len(CRAWL_PREFIX) <= 200):
            if s not in out:
                out.append(s)
        else:
            raise ValueError(f"Unknown date source: {s}")
    return out[:6]


def sources(conn) -> List[str]:
    row = conn.execute("SELECT date_sources FROM settings").fetchone()
    try:
        return clean_sources(json.loads(row["date_sources"])) if row else list(DEFAULT_SOURCES)
    except (ValueError, TypeError):
        return list(DEFAULT_SOURCES)


def page_date(row, order: List[str]) -> Optional[Period]:
    """`row` needs url_path, last_modified, and crawl_extra (JSON text)."""
    extra = None
    for kind in order:
        if kind in ("url_date", "url_year"):
            found = from_url(row["url_path"] or "", kind)
        elif kind == "last_modified":
            found = parse_text(row["last_modified"], kind)
        else:
            if extra is None:
                extra = json.loads(row["crawl_extra"] or "{}")
            found = parse_text(extra.get(kind[len(CRAWL_PREFIX):], ""), kind)
        if found:
            return found
    return None


def all_dates(conn, order: Optional[List[str]] = None) -> Dict[int, Optional[Period]]:
    order = sources(conn) if order is None else order
    return {r["id"]: page_date(r, order) for r in conn.execute(
        "SELECT id, url_path, last_modified, crawl_extra FROM pages")}


def coverage(conn, order: Optional[List[str]] = None) -> dict:
    """How many pages (from the crawl) get a date, and from which source; news and calendar items
    are counted on their own, since date rules are mostly about them."""
    order = sources(conn) if order is None else order
    out = {"pages": 0, "dated": 0, "by_source": {}, "collection_pages": 0, "collection_dated": 0}
    for r in conn.execute("SELECT url_path, last_modified, crawl_extra, collection FROM pages WHERE is_new = 0"):
        d = page_date(r, order)
        out["pages"] += 1
        out["collection_pages"] += bool(r["collection"])
        if d:
            out["dated"] += 1
            out["collection_dated"] += bool(r["collection"])
            out["by_source"][d.source] = out["by_source"].get(d.source, 0) + 1
    return out


def crawl_columns(conn) -> List[str]:
    """Extra crawl columns this project has, for choosing a date source."""
    names = set()
    for r in conn.execute("SELECT crawl_extra FROM pages WHERE crawl_extra <> '{}' LIMIT 500"):
        names.update(json.loads(r["crawl_extra"]))
    return sorted(names)
