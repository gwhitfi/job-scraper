"""Oracle Recruiting Cloud sites (e.g. Vertiv), through the public job search REST API.

    options = {site = "CX", location_id = "300000004340041"}

location_id comes from the site's location facet ("SC, United States" for Vertiv).
"""

import time
from datetime import date
from urllib.parse import urlparse

import httpx

from jobscraper.models import Company, RawPosting

PAGE_SIZE = 25
MAX_PAGES = 40
SEARCH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"


def _finder(site: str, location_id: str, offset: int) -> str:
    parts = [
        f"siteNumber={site}",
        f"limit={PAGE_SIZE}",
        f"offset={offset}",
        "sortBy=POSTING_DATES_DESC",
    ]
    if location_id:
        parts.append(f"locationId={location_id}")
    return "findReqs;" + ",".join(parts)


def parse(data: dict, base: str, site: str) -> tuple[list[RawPosting], int]:
    item = (data.get("items") or [{}])[0]
    postings = []
    for job in item.get("requisitionList", []):
        places = [job.get("PrimaryLocation") or ""]
        places += [s.get("Name", "") for s in job.get("secondaryLocations") or []]
        if (job.get("WorkplaceType") or "").lower().startswith("remote"):
            places.append("Remote")
        posted = job.get("PostedDate")
        postings.append(
            RawPosting(
                source_id=str(job["Id"]),
                title=" ".join(job["Title"].split()),
                location="; ".join(p for p in places if p),
                url=f"{base}/hcmUI/CandidateExperience/en/sites/{site}/job/{job['Id']}",
                posted_date=date.fromisoformat(posted) if posted else None,
                category=job.get("JobFamily") or job.get("JobFunction") or "",
                summary=" ".join((job.get("ShortDescriptionStr") or "").split()),
            )
        )
    return postings, int(item.get("TotalJobsCount") or 0)


def fetch(company: Company, client: httpx.Client, delay: float = 1.0) -> list[RawPosting]:
    base = f"https://{urlparse(company.url).netloc}"
    site = company.options.get("site", "CX")
    location_id = str(company.options.get("location_id", ""))
    by_id: dict[str, RawPosting] = {}
    for page in range(MAX_PAGES):
        if page:
            time.sleep(delay)
        params = {
            "onlyData": "true",
            "expand": "requisitionList.secondaryLocations",
            "finder": _finder(site, location_id, page * PAGE_SIZE),
        }
        resp = client.get(base + SEARCH, params=params)
        resp.raise_for_status()
        postings, total = parse(resp.json(), base, site)
        for p in postings:
            by_id.setdefault(p.source_id, p)
        if (page + 1) * PAGE_SIZE >= total or not postings:
            break
    return list(by_id.values())
