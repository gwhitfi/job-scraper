import logging
import re
import time
from datetime import date
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from jobscraper.http import with_page
from jobscraper.models import Company, RawPosting

log = logging.getLogger(__name__)

MAX_PAGES = 20
_PUBLISHED = re.compile(r"(\d{4}-\d{2}-\d{2})")


def parse(html: str, base_url: str) -> tuple[list[RawPosting], int]:
    """Return the postings on one results page and the highest page number in the pager."""
    soup = BeautifulSoup(html, "html.parser")
    postings = []
    for card in soup.select("a.ds__card-offer"):
        href = card["href"]
        details = {
            dt.get_text(" ", strip=True).rstrip(" :"): dd.get_text(" ", strip=True)
            for dt, dd in zip(card.select("dl dt"), card.select("dl dd"), strict=True)
        }
        published = card.select_one(".ds__card-offer-publication")
        m = _PUBLISHED.search(published.get_text()) if published else None
        postings.append(
            RawPosting(
                source_id=href.rstrip("/").rsplit("/", 1)[-1],
                title=card.select_one("h2").get_text(strip=True),
                location=details.get("Location", ""),
                url=urljoin(base_url, href),
                posted_date=date.fromisoformat(m.group(1)) if m else None,
                category=details.get("Sector", ""),
            )
        )

    pages = [
        int(a.get_text(strip=True))
        for a in soup.select('[aria-label="pager"] a.ds__link')
        if a.get_text(strip=True).isdigit()
    ]
    return postings, max(pages, default=1)


def fetch(company: Company, client: httpx.Client, delay: float = 1.0) -> list[RawPosting]:
    by_id: dict[str, RawPosting] = {}
    repeats = 0
    page, last_page = 1, 1
    while page <= min(last_page, MAX_PAGES):
        if page > 1:
            time.sleep(delay)
        resp = client.get(with_page(company.url, page))
        resp.raise_for_status()
        postings, last_page = parse(resp.text, company.url)
        for p in postings:
            repeats += p.source_id in by_id
            by_id.setdefault(p.source_id, p)
        page += 1
    if repeats:
        # A job repeated at a page boundary usually means another was pushed off a page, and
        # the site gives no total to check against. Splitting the search keeps it on one page.
        log.warning(
            "%s: %d job(s) repeated across pages; one may have been skipped", company.id, repeats
        )
    return list(by_id.values())
