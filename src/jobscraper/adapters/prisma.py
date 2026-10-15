import math
import re
import time
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from jobscraper.http import with_page
from jobscraper.models import Company, RawPosting

PER_PAGE = 12
MAX_PAGES = 30
_FOUND = re.compile(r"([\d,]+)\s+jobs?\s+found", re.I)
_REQ_ID = re.compile(r"/jobs/(r\d+)/")


def _field(card, label: str) -> str:
    """Text after a visually hidden label like "Locations:" in a job card."""
    for span in card.select("span.sr-only"):
        if span.get_text(strip=True).rstrip(":").strip() == label:
            return " ".join(span.parent.get_text(" ", strip=True).split()[1:]).lstrip(": ").strip()
    return ""


def parse(html: str, base_url: str) -> tuple[list[RawPosting], int]:
    """Return the postings on one results page and the total the page reports."""
    soup = BeautifulSoup(html, "html.parser")
    postings = []
    for link in soup.select("a[href^='/jobs/r']"):
        req = _REQ_ID.search(link["href"])
        card = link.find_parent(lambda t: t.name == "div" and t.select_one("span.sr-only"))
        if not req or card is None:
            continue
        postings.append(
            RawPosting(
                source_id=req.group(1),
                title=" ".join(link.get_text(" ", strip=True).split()),
                # Several locations come comma-separated, e.g. "Greenville, SC, Easley, SC".
                location=re.sub(r"(,\s*[A-Z]{2}),\s*", r"\1; ", _field(card, "Locations")),
                url=urljoin(base_url, link["href"]),
                category=_field(card, "Teams"),
            )
        )
    m = _FOUND.search(soup.get_text(" "))
    return postings, int(m.group(1).replace(",", "")) if m else len(postings)


def fetch(company: Company, client: httpx.Client, delay: float = 1.0) -> list[RawPosting]:
    by_id: dict[str, RawPosting] = {}
    page, pages = 1, 1
    while page <= min(pages, MAX_PAGES):
        if page > 1:
            time.sleep(delay)
        resp = client.get(with_page(company.url, page))
        resp.raise_for_status()
        postings, total = parse(resp.text, company.url)
        pages = math.ceil(total / PER_PAGE)
        for p in postings:
            by_id.setdefault(p.source_id, p)
        page += 1
    return list(by_id.values())
