"""Workday public career-site JSON listings, paged by offset."""

import re
from urllib.parse import parse_qs, urlsplit

import httpx

from jobscraper import activity
from jobscraper.adapters.generic import text
from jobscraper.locations import STATE_CODES
from jobscraper.models import Company, RawPosting

FACETS = {
    "locations",
    "locationCountry",
    "jobFamilyGroup",
    "jobFamily",
    "workerSubType",
    "timeType",
    "remoteType",
    "jobRequisitionLocation",
    "State",
    "Location_Country",
    "Location_Region_State_Province",
    "primaryLocation",
}


def site_url(url: str) -> str | None:
    parts = urlsplit(url)
    if not (parts.hostname or "").endswith(".myworkdayjobs.com"):
        return None
    segments = parts.path.strip("/").split("/")
    if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segments[0]):
        segments = segments[1:]
    if not segments or not re.fullmatch(r"[A-Za-z0-9_-]+", segments[0]):
        return None
    if segments[0] in ("wday", "job"):
        return None
    return parts._replace(path="/" + segments[0], fragment="").geturl()


def fetch(company: Company, client: httpx.Client) -> list[RawPosting]:
    url = site_url(company.url)
    if not url:
        raise ValueError("Use a Workday career-site URL containing its site name.")
    parts = urlsplit(url)
    tenant = parts.hostname.split(".")[0]
    site = parts.path.strip("/")
    origin = f"{parts.scheme}://{parts.netloc}"
    endpoint = f"{origin}/wday/cxs/{tenant}/{site}/jobs"
    query = parse_qs(parts.query)
    unknown = set(query) - FACETS - {"q", "source"}
    unknown = {k for k in unknown if not k.startswith("utm_")}
    if unknown:
        raise ValueError("Unsupported Workday search filters: " + ", ".join(sorted(unknown)))
    body = {
        "appliedFacets": {k: v for k, v in query.items() if k in FACETS},
        "searchText": query.get("q", [""])[0],
        "limit": 20,
    }
    by_id = {}
    first = client.post(endpoint, json=body | {"offset": 0})
    first.raise_for_status()
    first_data = first.json()
    scopes = [(body, first_data, False)]
    states = set(company.options.get("states", []))
    if states and not any(
        k in body["appliedFacets"] for k in ("State", "Location_Region_State_Province")
    ):
        facets = first_data.get("facets", [])
        state_facet = next(
            (
                f
                for f in facets
                if f.get("facetParameter") in ("State", "Location_Region_State_Province")
            ),
            None,
        )
        remote_facet = next((f for f in facets if f.get("facetParameter") == "remoteType"), None)
        if state_facet and remote_facet:
            ids = [
                v["id"]
                for v in state_facet.get("values", [])
                if STATE_CODES.get(v.get("descriptor"), v.get("descriptor")) in states
            ]
            remote_ids = [
                v["id"]
                for v in remote_facet.get("values", [])
                if re.search(r"remote|virtual|telecommut|home", v.get("descriptor", ""), re.I)
            ]
            # A complete state facet plus an explicit remote facet lets us narrow without
            # dropping remote jobs elsewhere. Re-resolve IDs each crawl rather than saving them.
            scopes = []
            if ids:
                scopes.append(
                    (
                        body
                        | {
                            "appliedFacets": body["appliedFacets"]
                            | {state_facet["facetParameter"]: ids}
                        },
                        None,
                        False,
                    )
                )
            if remote_ids:
                scopes.append(
                    (
                        body
                        | {"appliedFacets": body["appliedFacets"] | {"remoteType": remote_ids}},
                        None,
                        True,
                    )
                )
            if not scopes:
                raise ValueError(
                    "Workday has no matching state or remote facet values to validate."
                )
            activity.emit("workday_scope", states=sorted(states), searches=len(scopes))
    for search, cached, remote_scope in scopes:
        offset = 0
        scope_ids = set()
        expected_total = None
        for _ in range(100):
            if cached is not None:
                data, cached = cached, None
            else:
                response = client.post(endpoint, json=search | {"offset": offset})
                response.raise_for_status()
                data = response.json()
            jobs, total = data.get("jobPostings"), data.get("total")
            if not isinstance(jobs, list) or type(total) is not int or total < 0:
                raise ValueError("Workday returned an unexpected job list or total.")
            # Subsequent Workday pages report zero even while results remain.
            if expected_total is None:
                expected_total = total
            total = expected_total
            if total >= 2000:
                raise ValueError(
                    "Workday caps large searches at 2,000 jobs. Select a location "
                    "or paste a narrower "
                    "filtered results URL; an incomplete list will not be tracked."
                )
            previous = len(scope_ids)
            for job in jobs:
                path = job.get("externalPath", "")
                title = text(job.get("title"))
                if not title or not isinstance(path, str) or not path.startswith("/job/"):
                    raise ValueError("Workday job is missing its title or public detail path.")
                sid = path.rsplit("_", 1)[-1]
                if "/" in sid or not sid:
                    raise ValueError("Workday job is missing its requisition identifier.")
                location = text(job.get("locationsText"))
                if remote_scope or re.search(
                    r"remote|virtual|telecommut|home", text(job.get("remoteType")), re.I
                ):
                    location += "; Remote"
                posting = RawPosting(sid, title, location, f"{origin}/{site}{path}")
                if sid in by_id and by_id[sid].url != posting.url:
                    raise ValueError("Workday returned conflicting requisition identifiers.")
                by_id[sid] = posting
                scope_ids.add(sid)
            activity.emit(
                "workday_page", fetched=len(jobs), unique=len(scope_ids), total=total, offset=offset
            )
            if len(scope_ids) >= total:
                break
            if len(scope_ids) == previous:
                raise ValueError("Workday pagination stopped before its reported total.")
            offset += len(jobs)
        else:
            raise ValueError("Too many Workday jobs. Use a filtered career-site URL.")
    return list(by_id.values())
