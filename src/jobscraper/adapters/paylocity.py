import json
import re
from datetime import datetime

import httpx

from jobscraper.models import Company, RawPosting

# The company's public board, /recruiting/jobs/All/<company guid>, embeds its jobs as JSON.
_PAGE_DATA = re.compile(r"window\.pageData\s*=\s*(\{.*?\});\s*$", re.S | re.M)
DETAILS_URL = "https://recruiting.paylocity.com/recruiting/jobs/Details/{id}"


def parse(html: str) -> list[RawPosting]:
    m = _PAGE_DATA.search(html)
    if not m:
        raise ValueError("No job data (window.pageData) on the Paylocity page")
    postings = []
    for job in json.loads(m.group(1)).get("Jobs", []):
        if job.get("IsInternal"):
            continue
        loc = job.get("JobLocation") or {}
        places = [", ".join(p for p in (loc.get("City"), loc.get("State")) if p)]
        if job.get("IsRemote"):
            places.append("Remote")
        published = job.get("PublishedDate")
        postings.append(
            RawPosting(
                source_id=str(job["JobId"]),
                title=" ".join(job["JobTitle"].split()),
                location="; ".join(p for p in places if p),
                url=DETAILS_URL.format(id=job["JobId"]),
                posted_date=datetime.fromisoformat(published).date() if published else None,
                category=job.get("HiringDepartment") or "",
                summary=" ".join((job.get("Description") or "").split()),
            )
        )
    return postings


def fetch(company: Company, client: httpx.Client) -> list[RawPosting]:
    resp = client.get(company.url)
    resp.raise_for_status()
    return parse(resp.text)
