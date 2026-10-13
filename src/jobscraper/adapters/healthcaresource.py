"""HealthcareSource job sites (e.g. AnMed), through the search API the page queries.

    options = {categories = ["Professional - Non-Clinical", ...]}

The site name comes from the careers URL, e.g. pm.healthcaresource.com/cs/anmed.
"""

import time
from datetime import datetime
from urllib.parse import urlparse

import httpx

from jobscraper.models import Company, RawPosting

PAGE_SIZE = 100
MAX_PAGES = 20
SEARCH = "https://pm.healthcaresource.com/JobseekerSearchAPI/{site}/api/Search"


def _site(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2 or parts[0].lower() != "cs":
        raise ValueError(f"Expected a .../cs/<site> careers URL, got {url!r}")
    return parts[1]


def _query(categories: list[str]) -> dict:
    query: dict = {"bool": {"must": {"match_all": {}}}}
    if categories:
        query["bool"]["filter"] = [{"terms": {"occupationalCategory.raw": categories}}]
    return {"query": query}


def parse(data: dict, site: str) -> tuple[list[RawPosting], int]:
    hits = data.get("hits", {})
    postings = []
    for hit in hits.get("hits", []):
        job = hit["_source"]
        extra = job.get("userArea") or {}
        if extra.get("isInternalOnly") or extra.get("active") is False:
            continue
        address = (job.get("jobLocation") or {}).get("address") or {}
        posted = job.get("datePosted")
        req_id = extra.get("requisitionID") or hit["_id"]
        postings.append(
            RawPosting(
                source_id=str(req_id),
                title=" ".join(job["title"].split()),
                location=address.get("addressLocalityRegion") or "",
                url=f"https://pm.healthcaresource.com/cs/{site}#/job/{req_id}",
                posted_date=datetime.fromisoformat(posted[:10]).date() if posted else None,
                category=job.get("occupationalCategory") or "",
            )
        )
    total = hits.get("total") or {}
    return postings, int(total.get("value", 0) if isinstance(total, dict) else total)


def fetch(company: Company, client: httpx.Client, delay: float = 1.0) -> list[RawPosting]:
    site = _site(company.url)
    body = _query(company.options.get("categories", []))
    by_id: dict[str, RawPosting] = {}
    for page in range(MAX_PAGES):
        if page:
            time.sleep(delay)
        params = {"size": PAGE_SIZE, "from": page * PAGE_SIZE}
        resp = client.post(SEARCH.format(site=site), params=params, json=body)
        resp.raise_for_status()
        postings, total = parse(resp.json(), site)
        for p in postings:
            by_id.setdefault(p.source_id, p)
        if (page + 1) * PAGE_SIZE >= total:
            break
    return list(by_id.values())
