"""Workday public career-site JSON listings, paged by offset."""

import re
from urllib.parse import parse_qs, urlsplit

import httpx

from jobscraper import activity
from jobscraper.adapters.generic import text
from jobscraper.geography.locations import STATE_CODES, places, us_country
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


def facets_in(data: dict) -> list[dict]:
    found = []
    pending = list(data.get("facets", []))
    while pending:
        facet = pending.pop(0)
        found.append(facet)
        pending.extend(v for v in facet.get("values", []) if "facetParameter" in v)
    return found


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
    details = {}
    first = client.post(endpoint, json=body | {"offset": 0})
    first.raise_for_status()
    first_data = first.json()
    scopes = [(body, first_data, False)]
    states = set(company.options.get("states", []))
    facets = facets_in(first_data)
    country_facet = next(
        (f for f in facets if f.get("facetParameter") in ("locationCountry", "Location_Country")),
        None,
    )
    us_ids = [
        v["id"]
        for v in (country_facet or {}).get("values", [])
        if us_country(v.get("descriptor", ""))
    ]
    if (
        states
        and us_ids
        and not any(k in body["appliedFacets"] for k in ("locationCountry", "Location_Country"))
    ):
        body = body | {
            "appliedFacets": body["appliedFacets"] | {country_facet["facetParameter"]: us_ids}
        }
        scopes = [(body, None, False)]
    if states and not any(
        k in body["appliedFacets"] for k in ("State", "Location_Region_State_Province")
    ):
        state_facet = next(
            (
                f
                for f in facets
                if f.get("facetParameter") in ("State", "Location_Region_State_Province")
            ),
            None,
        )
        remote_facet = next((f for f in facets if f.get("facetParameter") == "remoteType"), None)
        if state_facet is None and "locations" not in body["appliedFacets"]:
            state_facet = next((f for f in facets if f.get("facetParameter") == "locations"), None)
        if state_facet and remote_facet:
            ids = [
                v["id"]
                for v in state_facet.get("values", [])
                if STATE_CODES.get(v.get("descriptor"), v.get("descriptor")) in states
                or any(state in states for _, state in places(v.get("descriptor", "")))
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
                remote_type = text(job.get("remoteType"))
                if re.fullmatch(r"\d+\s+locations?", location, re.I):
                    if sid not in details:
                        response = client.get(endpoint.removesuffix("/jobs") + path)
                        response.raise_for_status()
                        details[sid] = response.json().get("jobPostingInfo")
                    info = details[sid]
                    if not isinstance(info, dict) or not info.get("location"):
                        raise ValueError("Workday multi-location detail is missing its locations.")
                    location = "; ".join(
                        [
                            text(info["location"]),
                            *[text(v) for v in info.get("additionalLocations", [])],
                        ]
                    )
                    remote_type = text(info.get("remoteType")) or remote_type
                    activity.emit("workday_locations_resolved", source_id=sid, location=location)
                if remote_type:
                    if re.search(r"remote", remote_type, re.I):
                        location += "; " + remote_type
                    elif re.search(r"virtual|telecommut|work from home", remote_type, re.I):
                        location += "; Remote"
                elif remote_scope:
                    location += "; Remote"
                countries = ()
                if country_facet and us_ids:
                    selected = search["appliedFacets"].get(country_facet["facetParameter"], [])
                    if selected and set(selected) <= set(us_ids):
                        countries = ("US",)
                posting = RawPosting(
                    sid, title, location, f"{origin}/{site}{path}", countries=countries
                )
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
