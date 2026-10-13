import re
import time
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from jobscraper.models import Company, RawPosting

# "Showing 1 to 18 of 18 Jobs"
_SHOWING = re.compile(r"Showing\s+\d+\s+to\s+\d+\s+of\s+(\d+)\s+Jobs")
# Numeric job id at the end of the job URL, e.g. .../SC-29670/1358704157/
_JOB_ID = re.compile(r"/(\d+)/?$")


def _field(tile, label: str) -> str:
    """Value of a labeled tile field; field ids (customfield1, ...) vary by site."""
    for span in tile.select(".section-label"):
        if span.get_text(strip=True) == label:
            value = span.find_next_sibling("div")
            return value.get_text(strip=True) if value else ""
    return ""


def parse(html: str, base_url: str) -> tuple[list[RawPosting], int]:
    """Return the job tiles on a category page and the total job count it reports."""
    soup = BeautifulSoup(html, "html.parser")
    postings = []
    for tile in soup.select("li.job-tile"):
        href = tile["data-url"]
        job_id = _JOB_ID.search(href)
        if not job_id:
            raise ValueError(f"No job id in {href!r}")
        # Each tile repeats its fields for desktop/tablet/mobile; the first copy is enough.
        title = tile.select_one("a.jobTitle-link")
        location = tile.select_one("[id$='-section-location-value']")
        category = _field(tile, "Category")
        postings.append(
            RawPosting(
                source_id=job_id.group(1),
                title=title.get_text(strip=True),
                location=location.get_text(strip=True) if location else "",
                url=urljoin(base_url, href),
                category=category,
            )
        )

    m = _SHOWING.search(soup.get_text(" "))
    return postings, int(m.group(1)) if m else len(postings)


def _tiles_url(page_url: str, start: int) -> str:
    """The endpoint behind the page's "More Search Results" button, e.g.
    /tile-search-results/category/7566200/?startrow=25 for /go/Jobs-in-South-Carolina/7566200/.
    """
    category = _JOB_ID.search(page_url.split("?")[0])
    if not category:
        raise ValueError(f"No category id in {page_url!r}")
    return urljoin(page_url, f"/tile-search-results/category/{category.group(1)}/?startrow={start}")


def fetch(company: Company, client: httpx.Client, delay: float = 1.0) -> list[RawPosting]:
    resp = client.get(company.url)
    resp.raise_for_status()
    first, total = parse(resp.text, company.url)
    by_id = {p.source_id: p for p in first}
    while len(by_id) < total:
        time.sleep(delay)
        resp = client.get(_tiles_url(company.url, len(by_id)))
        resp.raise_for_status()
        more, _ = parse(resp.text, company.url)
        new = [p for p in more if p.source_id not in by_id]
        if not new:
            raise ValueError(f"Page lists {total} jobs but paging stopped at {len(by_id)}")
        by_id.update((p.source_id, p) for p in new)
    return list(by_id.values())
