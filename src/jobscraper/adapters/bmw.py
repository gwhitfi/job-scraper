"""BMW Group job finder (bmwgroup.jobs), through the HTML fragment its search loads.

    url = ".../jobfinder30.jobfinder_table.content.html"
    options = {filters = ["location_US/Greer", "location_US/Spartanburg", ...]}

Filters within a category (e.g. locations) are combined with OR.
"""

import time
from datetime import datetime
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from jobscraper.models import Company, RawPosting

PAGE_SIZE = 50
MAX_PAGES = 20


def parse(html: str, base_url: str) -> tuple[list[RawPosting], int]:
    soup = BeautifulSoup(html, "html.parser")
    postings = []
    for row in soup.select(".grp-jobfinder__wrapper"):
        cell = row.select_one(".grp-jobfinder-cell-refno")
        link = row.select_one("a[href]")
        if cell is None or link is None:
            continue
        posted = cell.get("data-posting-date")
        postings.append(
            RawPosting(
                source_id=row.get("data-job-id") or cell.get_text(strip=True),
                title=" ".join((cell.get("data-job-title") or "").split()),
                location=cell.get("data-job-location") or "",
                url=urljoin(base_url, link["href"]),
                posted_date=datetime.strptime(posted, "%Y%m%d").date() if posted else None,
                category=" / ".join(
                    c for c in (cell.get("data-job-field"), cell.get("data-job-type")) if c
                ),
            )
        )
    counter = soup.find(attrs={"data-counter": True})
    return postings, int(counter["data-counter"]) if counter else len(postings)


def fetch(company: Company, client: httpx.Client, delay: float = 1.0) -> list[RawPosting]:
    filters = ",".join(company.options.get("filters", []))
    by_id: dict[str, RawPosting] = {}
    for page in range(MAX_PAGES):
        if page:
            time.sleep(delay)
        params = {"rowIndex": page * PAGE_SIZE, "blockCount": PAGE_SIZE}
        if filters:
            params["filterSearch"] = filters
        resp = client.get(company.url, params=params)
        resp.raise_for_status()
        postings, total = parse(resp.text, company.url)
        for p in postings:
            by_id.setdefault(p.source_id, p)
        if (page + 1) * PAGE_SIZE >= total or not postings:
            break
    return list(by_id.values())
