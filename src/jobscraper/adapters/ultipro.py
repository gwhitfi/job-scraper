import time
from datetime import datetime

import httpx

from jobscraper.models import Company, RawPosting

PAGE_SIZE = 50
MAX_PAGES = 20


def _search_body(skip: int) -> dict:
    # Mirrors the request the job board's own search page sends.
    return {
        "opportunitySearch": {
            "Top": PAGE_SIZE,
            "Skip": skip,
            "QueryString": "",
            "OrderBy": [
                {"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}
            ],
            "Filters": [
                {"t": "TermsSearchFilterDto", "fieldName": n, "extra": None, "values": []}
                for n in (4, 5, 6)
            ],
        },
        "matchCriteria": {
            "PreferredJobs": [],
            "Educations": [],
            "LicenseAndCertifications": [],
            "Skills": [],
            "hasNoLicenses": False,
            "SkippedSkills": [],
        },
    }


def _location(loc: dict) -> tuple[str, str]:
    """Return (display text, state code) for one location."""
    address = loc.get("Address") or {}
    state = (address.get("State") or {}).get("Code") or ""
    city = address.get("City") or ""
    text = ", ".join(p for p in (city, state) if p) or loc.get("LocalizedDescription") or ""
    return text, state


def _is_remote(opp: dict) -> bool:
    # JobLocationType's values aren't documented, so match on text instead.
    if not opp.get("Locations"):
        return True
    texts = [opp.get("Title") or ""] + [
        f"{loc.get('LocalizedDescription') or ''} {loc.get('LocalizedName') or ''}"
        for loc in opp["Locations"]
    ]
    return any("remote" in t.lower() for t in texts)


def parse(data: dict, board_url: str, states: tuple[str, ...] = ()) -> list[RawPosting]:
    """Convert one search response; if `states` is set, keep postings there or remote."""
    postings = []
    for opp in data["opportunities"]:
        locations = [_location(loc) for loc in opp.get("Locations") or []]
        in_states = any(state in states for _, state in locations)
        if states and not in_states and not _is_remote(opp):
            continue
        posted = opp.get("PostedDate")
        postings.append(
            RawPosting(
                source_id=opp["Id"],
                title=" ".join(opp["Title"].split()),
                location="; ".join(text for text, _ in locations) or "Remote",
                url=f"{board_url.rstrip('/')}/OpportunityDetail?opportunityId={opp['Id']}",
                posted_date=datetime.fromisoformat(posted).date() if posted else None,
                category=opp.get("JobCategoryName") or "",
                summary=" ".join((opp.get("BriefDescription") or "").split()),
            )
        )
    return postings


def fetch(company: Company, client: httpx.Client, delay: float = 1.0) -> list[RawPosting]:
    board_url = company.url.split("?")[0].rstrip("/")
    endpoint = f"{board_url}/JobBoardView/LoadSearchResults"
    postings: dict[str, RawPosting] = {}
    for page in range(MAX_PAGES):
        if page:
            time.sleep(delay)
        resp = client.post(endpoint, json=_search_body(page * PAGE_SIZE))
        resp.raise_for_status()
        data = resp.json()
        for p in parse(data, board_url, company.locations):
            postings.setdefault(p.source_id, p)
        if (page + 1) * PAGE_SIZE >= data["totalCount"] or not data["opportunities"]:
            break
    return list(postings.values())
