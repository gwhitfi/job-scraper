"""Phenom career sites (e.g. careers.bonsecours.com), through the search API the page calls.

    options = {keywords = ["analyst", ...], states = ["South Carolina"], title_only = true}

Each keyword is a separate search; results are combined. No keywords means one unfiltered search.
The site matches keywords anywhere in a job's text ("data" hits most nursing jobs), so
title_only keeps just the jobs with a keyword in the title.
"""

import re
import time
from datetime import datetime
from urllib.parse import urlparse

import httpx

from jobscraper.geography.locations import STATE_CODES, us_country
from jobscraper.models import Company, RawPosting

PAGE_SIZE = 50
MAX_PAGES = 20


def _body(keyword: str, states: list[str], start: int) -> dict:
    return {
        "lang": "en_us",
        "deviceType": "desktop",
        "country": "us",
        "pageName": "search-results",
        "ddoKey": "refineSearch",
        "sortBy": "",
        "subsearch": "",
        "from": start,
        "jobs": True,
        "counts": True,
        "all_fields": ["category", "subCategory", "state", "city"],
        "size": PAGE_SIZE,
        "clearAll": False,
        "jdsource": "facets",
        "isSliderEnable": False,
        "keywords": keyword,
        "global": True,
        "selected_fields": {"state": states} if states else {},
    }


def parse(data: dict, base: str) -> tuple[list[RawPosting], int]:
    """Return one page of postings and the total hits."""
    search = data.get("refineSearch", {})
    postings = []
    for job in search.get("data", {}).get("jobs", []):
        sites = job.get("multi_location_array") or [job]
        places, countries = [], []
        for site in sites:
            state = site.get("state") or (job.get("state") if len(sites) == 1 else "") or ""
            city = site.get("city") or (job.get("city") if len(sites) == 1 else "")
            place = ", ".join(p for p in (city, STATE_CODES.get(state, state)) if p)
            place = place or site.get("location") or ""
            if place:
                places.append(place)
            country = site.get("country") or (job.get("country") if len(sites) == 1 else "")
            if country:
                countries.append(country)
                if place and not us_country(country):
                    places[-1] += ", " + country
        # Incomplete multi-location metadata must not imply every location is foreign.
        if len(countries) != len(sites):
            countries = []
        slug = re.sub(r"[^a-z0-9]+", "-", job["title"].lower()).strip("-")
        posted = job.get("postedDate")
        postings.append(
            RawPosting(
                source_id=job["jobSeqNo"],
                title=" ".join(job["title"].split()),
                location="; ".join(dict.fromkeys(places)),
                countries=tuple(dict.fromkeys(countries)),
                url=f"{base}/us/en/job/{job.get('jobId') or job['jobSeqNo']}/{slug}",
                posted_date=datetime.fromisoformat(posted[:10]).date() if posted else None,
                category=" / ".join(c for c in (job.get("category"), job.get("subCategory")) if c),
                summary=" ".join((job.get("descriptionTeaser") or "").split()),
            )
        )
    return postings, int(search.get("totalHits") or 0)


def fetch(company: Company, client: httpx.Client, delay: float = 1.0) -> list[RawPosting]:
    base = f"https://{urlparse(company.url).netloc}"
    keywords = company.options.get("keywords") or [""]
    states = company.options.get("states", [])
    in_title = None
    if company.options.get("title_only") and any(keywords):
        words = "|".join(re.escape(k) for k in keywords if k)
        in_title = re.compile(rf"\b(?:{words})\b", re.I)
    by_id: dict[str, RawPosting] = {}
    for i, keyword in enumerate(keywords):
        start = 0
        for page in range(MAX_PAGES):
            if i or page:
                time.sleep(delay)
            resp = client.post(f"{base}/widgets", json=_body(keyword, states, start))
            resp.raise_for_status()
            postings, total = parse(resp.json(), base)
            for p in postings:
                if in_title is None or in_title.search(p.title):
                    by_id.setdefault(p.source_id, p)
            start += PAGE_SIZE
            if start >= total or not postings:
                break
    return list(by_id.values())
