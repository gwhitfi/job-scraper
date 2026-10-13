from urllib.parse import urlparse

import httpx

from jobscraper.locations import STATE_CODES
from jobscraper.models import Company, RawPosting


def parse(data: dict, base: str) -> list[RawPosting]:
    """`base` is the company's BambooHR site, e.g. https://acme.bamboohr.com."""
    postings = []
    for job in data.get("result", []):
        loc = job.get("location") or {}
        state = loc.get("state") or ""
        place = ", ".join(p for p in (loc.get("city"), STATE_CODES.get(state, state)) if p)
        places = [place] if place else []
        if job.get("isRemote") or job.get("locationType") == "1":
            places.append("Remote")
        postings.append(
            RawPosting(
                source_id=str(job["id"]),
                title=" ".join(job["jobOpeningName"].split()),
                location="; ".join(places),
                url=f"{base}/careers/{job['id']}",
                category=job.get("departmentLabel") or "",
            )
        )
    return postings


def fetch(company: Company, client: httpx.Client) -> list[RawPosting]:
    host = urlparse(company.url).netloc
    resp = client.get(f"https://{host}/careers/list", headers={"Accept": "application/json"})
    resp.raise_for_status()
    return parse(resp.json(), f"https://{host}")
