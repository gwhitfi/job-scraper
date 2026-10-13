import re
import time
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

from jobscraper.models import Company, RawPosting

CRAWL_DELAY = 10.0  # from the site's robots.txt
MAX_PAGES = 20
_AGO = re.compile(r"(\d+)\+?\s+(day|week|month)s?\s+ago", re.I)
_DAYS = {"day": 1, "week": 7, "month": 30}


def posted_date(text: str, today: date) -> date | None:
    """Turn "Posted 3 days ago" / "Posted 2 weeks ago" / "Posted today" into an approximate date."""
    text = text.lower()
    if "today" in text or "just posted" in text:
        return today
    if "yesterday" in text:
        return today - timedelta(days=1)
    m = _AGO.search(text)
    if not m:
        return None
    return today - timedelta(days=int(m.group(1)) * _DAYS[m.group(2).lower()])


def _split_meta(meta: str) -> tuple[str, str]:
    """Split "AFL - Duncan, SC 29334" into ("AFL", "Duncan, SC 29334"); employer may be ""."""
    if " - " in meta:
        employer, location = meta.rsplit(" - ", 1)
        return employer.strip(), location.strip()
    return "", meta.strip()


def _job_id(url: str) -> str:
    # Cards link to Indeed; its `jk` parameter is the stable job id.
    jk = parse_qs(urlparse(url).query).get("jk")
    if not jk:
        raise ValueError(f"No Indeed job id in {url!r}")
    return jk[0]


def parse(html: str, today: date) -> tuple[list[RawPosting], int]:
    """Return the postings on one page and the highest page number in the pager."""
    soup = BeautifulSoup(html, "html.parser")
    postings = []
    for card in soup.select("a.c-job"):
        headline = card.select_one(".c-job__headline")
        date_el = headline.select_one(".c-job__date")
        date_text = date_el.get_text(strip=True) if date_el else ""
        if date_el:
            date_el.extract()
        meta = card.select_one(".c-job__meta")
        employer, location = _split_meta(meta.get_text(" ", strip=True) if meta else "")
        description = card.select_one(".c-job__description")
        postings.append(
            RawPosting(
                source_id=_job_id(card["href"]),
                title=" ".join(headline.get_text(" ", strip=True).split()),
                location=location,
                url=card["href"],
                posted_date=posted_date(date_text, today),
                summary=" ".join(description.get_text(" ", strip=True).split())
                if description
                else "",
                employer=employer,
            )
        )

    pages = [
        int(a.get_text(strip=True))
        for a in soup.select(".c-pagination .page-numbers")
        if a.get_text(strip=True).isdigit()
    ]
    return postings, max(pages, default=1)


def _page_url(base: str, page: int) -> str:
    base = base.rstrip("/")
    return f"{base}/" if page == 1 else f"{base}/page/{page}/"


def fetch(company: Company, client: httpx.Client, delay: float = CRAWL_DELAY) -> list[RawPosting]:
    today = date.today()
    by_id: dict[str, RawPosting] = {}
    page, last_page = 1, 1
    while page <= min(last_page, MAX_PAGES):
        if page > 1:
            time.sleep(delay)
        resp = client.get(_page_url(company.url, page))
        resp.raise_for_status()
        postings, last_page = parse(resp.text, today)
        for p in postings:
            by_id.setdefault(p.source_id, p)
        page += 1
    return list(by_id.values())
