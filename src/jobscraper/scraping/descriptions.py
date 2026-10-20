"""Collect source descriptions without changing posting summaries or AI answers."""

import hashlib
import html
import json
import logging
import re
import time
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup

from jobscraper import activity, adapters
from jobscraper.adapters import healthcaresource, workday
from jobscraper.config import Config
from jobscraper.geography import locations
from jobscraper.models import Company
from jobscraper.scraping import crawl, discovery
from jobscraper.storage import jsonfile
from jobscraper.storage.state import Posting

log = logging.getLogger(__name__)
VERSION = 1
REFRESH_AFTER = timedelta(days=7)
RETRY_AFTER = timedelta(days=1)
MAX_TEXT = 200_000
DESCRIPTION_SELECTORS = (
    '[data-automation-id="jobPostingDescription"]',
    '[itemprop="description"]',
    "#jobDescriptionText",
    "#jobDescription",
    "#job-description",
    ".jobdescription",
    ".job-description",
    ".job_description",
    ".jobDescription",
    ".job-description__content",
    ".job-description-content",
    ".job-details-description",
    ".job-details__description",
    ".jd-info",
    ".ds__block-content-wrapper",
)


class Unavailable(ValueError):
    """The requested job does not expose a recognizable full description."""


def plain_text(value: str) -> str:
    # Some providers HTML-encode the entire description, including its markup.
    for _ in range(2):
        value = html.unescape(value)
    soup = BeautifulSoup(value, "html.parser")
    for element in soup.select("script, style, nav, header, footer, form, noscript"):
        element.decompose()
    lines = [" ".join(line.split()) for line in soup.get_text("\n").splitlines()]
    return "\n".join(line for line in lines if line)


def _checked_text(value: str) -> str:
    text = plain_text(value)
    if not text:
        raise Unavailable("The source's description is empty.")
    if len(text) > MAX_TEXT:
        raise Unavailable("The description exceeds the collection size limit; not truncated.")
    return text


def _normalized(value: str) -> str:
    return (
        " ".join(re.findall(r"\w+", html.unescape(value).casefold()))
        if isinstance(value, str)
        else ""
    )


def _nodes(value) -> Iterable[dict]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _nodes(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _nodes(nested)


def parse_html(source: str, posting: Posting) -> tuple[str, str]:
    """Use job-specific structured data or a dedicated description element."""
    if reason := discovery.challenge(source):
        raise crawl.Blocked(reason)
    soup = BeautifulSoup(source, "html.parser")
    for notice in soup.select(".job-manager-info, [role=alert], h1, h2, h3"):
        text = notice.get_text(" ", strip=True).casefold()
        if "this listing has expired" in text or "this job posting is no longer active" in text:
            raise Unavailable(
                "The source explicitly marks this job as expired or no longer active."
            )
    jobs = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.get_text())
        except ValueError:
            continue
        for node in _nodes(data):
            kinds = node.get("@type", [])
            if "JobPosting" in (
                [kinds] if isinstance(kinds, str) else kinds if isinstance(kinds, list) else []
            ):
                jobs.append(node)
    matches = []
    for job in jobs:
        identifier = job.get("identifier", "")
        if isinstance(identifier, dict):
            identifier = identifier.get("value", "")
        same_id = str(identifier) == posting.source_id
        same_title = _normalized(job.get("title") or "") == _normalized(posting.title)
        same_url = job.get("url") == posting.url
        description = job.get("description")
        if not isinstance(description, str) or not description.strip():
            continue
        score = 4 * same_id + 2 * same_url + same_title
        if score:
            matches.append((score, job))
    matches.sort(key=lambda item: item[0], reverse=True)
    if len(matches) > 1 and matches[0][0] == matches[1][0] and matches[0][1] != matches[1][1]:
        raise Unavailable("Multiple jobs match this title; the description cannot be identified.")
    for _, job in matches[:1]:
        description = job["description"]
        sections = [description]
        for field in (
            "responsibilities",
            "qualifications",
            "educationRequirements",
            "experienceRequirements",
            "skills",
        ):
            value = job.get(field)
            if value:
                sections.append(
                    field + ": " + (value if isinstance(value, str) else json.dumps(value))
                )
        return _checked_text("\n".join(sections)), "schema.org/JobPosting"
    # Never accept a generic main/article/body: it could be a search or error page.
    headings = [
        _normalized(re.sub(r"^Job title:\s*", "", e.get_text(" ", strip=True), flags=re.I))
        for e in soup.select("h1, h2")
    ]
    if _normalized(posting.title) not in headings:
        raise Unavailable("No matching job title and full description were found on this page.")
    for selector in DESCRIPTION_SELECTORS:
        elements = soup.select(selector)
        if len(elements) == 1:
            sections = [str(elements[0])]
            for field in ("educationRequirements", "experienceRequirements", "qualifications"):
                for extra in soup.select(f'[itemprop="{field}"]'):
                    if extra not in elements[0].descendants:
                        sections.append(str(extra))
            return _checked_text("\n".join(sections)), selector
    raise Unavailable("The page has no supported full-description element.")


def fetch(posting: Posting, company: Company, client: crawl.CachedClient) -> tuple[str, str, str]:
    """Return full text, extraction method, and the response URL."""
    platform = adapters.platform(company)
    if platform == "healthcaresource":
        site = healthcaresource._site(company.url)
        if not posting.source_id.isdigit():
            raise Unavailable("HealthcareSource has no numeric requisition ID to resolve.")
        # Requisition IDs are stable posting keys, but the public URL/API uses jobPostingID.
        response = client.post(
            healthcaresource.SEARCH.format(site=site),
            params={"size": 2, "from": 0},
            json={
                "query": {
                    "bool": {
                        "must": {"match_all": {}},
                        "filter": [{"terms": {"userArea.requisitionID": [int(posting.source_id)]}}],
                    }
                }
            },
        )
        data = response.json()
        results = data.get("hits") if isinstance(data, dict) else None
        hits = results.get("hits") if isinstance(results, dict) else None
        total = results.get("total") if isinstance(results, dict) else None
        total = total.get("value") if isinstance(total, dict) else total
        if not isinstance(hits, list) or type(total) is not int or total > len(hits):
            raise Unavailable("HealthcareSource returned an incomplete posting-ID lookup.")
        matches = []
        for hit in hits:
            job = hit.get("_source") if isinstance(hit, dict) else None
            extra = job.get("userArea") if isinstance(job, dict) else None
            if not isinstance(extra, dict):
                continue
            if (
                str(extra.get("requisitionID")) == posting.source_id
                and _normalized(job.get("title")) == _normalized(posting.title)
                and extra.get("active") is True
                and not extra.get("isInternalOnly")
                and not extra.get("isHiddenOnCareerSite")
                and str(extra.get("jobPostingID", "")).isdigit()
            ):
                matches.append(str(extra["jobPostingID"]))
        if len(set(matches)) != 1:
            raise Unavailable("HealthcareSource could not resolve a unique public posting ID.")
        posting_id = matches[0]
        endpoint = f"https://pm.healthcaresource.com/JobseekerAPI/Site/{site}/api/v2/JobPostingV2"
        try:
            response = client.get(endpoint, params={"id": posting_id})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise Unavailable(
                    "HealthcareSource reports this job posting was not found (HTTP 404)."
                ) from None
            raise
        data = response.json()
        extra = data.get("userArea", {}) if isinstance(data, dict) else {}
        if (
            not isinstance(extra, dict)
            or str(extra.get("requisitionID")) != posting.source_id
            or str(extra.get("jobPostingID")) != posting_id
            or _normalized(data.get("title")) != _normalized(posting.title)
            or extra.get("active") is not True
            or extra.get("isInternalOnly")
            or extra.get("isHiddenOnCareerSite")
        ):
            raise Unavailable("HealthcareSource did not return the requested public job's details.")
        description = extra.get("jobSummaryDisplay")
        if not isinstance(description, str):
            raise Unavailable("HealthcareSource did not return its full rendered description.")
        description = _checked_text(description)
        for field in ("educationRequirements", "experienceRequirements"):
            if isinstance(data.get(field), str) and data[field]:
                description += "\n" + field + ": " + data[field]
        return _checked_text(description), "healthcaresource/JobPostingV2", str(response.url)
    if platform == "bamboohr":
        origin = urlsplit(company.url)
        if not posting.source_id.isdigit():
            raise Unavailable("The saved BambooHR job has no numeric opening ID.")
        endpoint = f"{origin.scheme}://{origin.netloc}/careers/{posting.source_id}/detail"
        response = client.get(endpoint)
        data = response.json()
        result = data.get("result") if isinstance(data, dict) else None
        info = result.get("jobOpening") if isinstance(result, dict) else None
        if not isinstance(info, dict):
            raise Unavailable("BambooHR did not return a job-opening detail record.")
        share = urlsplit(info.get("jobOpeningShareUrl") or "")
        if (
            share.netloc != origin.netloc
            or share.path.rstrip("/") != "/careers/" + posting.source_id
            or _normalized(info.get("jobOpeningName")) != _normalized(posting.title)
        ):
            raise Unavailable("BambooHR did not return the requested job's details.")
        if info.get("jobOpeningStatus") != "Open":
            raise Unavailable("BambooHR does not mark this job opening as open.")
        description = info.get("description")
        if not isinstance(description, str):
            raise Unavailable("BambooHR did not return the opening's full description.")
        description = _checked_text(description)
        if info.get("minimumExperience"):
            description += "\nMinimum experience: " + str(info["minimumExperience"])
        return _checked_text(description), "bamboohr/jobOpening.description", str(response.url)
    if platform == "oracle_hcm":
        origin = urlsplit(company.url)
        endpoint = f"{origin.scheme}://{origin.netloc}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
        response = client.get(
            endpoint,
            params={
                "onlyData": "true",
                "expand": "all",
                "finder": "ById;Id=" + posting.source_id,
            },
        )
        data = response.json()
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise Unavailable("Oracle did not return a single job detail record.")
        info = items[0]
        if str(info.get("Id")) != posting.source_id or _normalized(
            info.get("Title")
        ) != _normalized(posting.title):
            raise Unavailable("Oracle did not return the requested job's details.")
        sections = []
        for field in (
            "ExternalDescriptionStr",
            "ExternalResponsibilitiesStr",
            "ExternalQualificationsStr",
        ):
            value = info.get(field)
            if isinstance(value, str) and value.strip():
                sections.append(value)
        return _checked_text("\n".join(sections)), "oracle/requisitionDetails", str(response.url)
    if platform == "applitrack":
        for job in adapters.get("applitrack")(company, client):
            if job.source_id == posting.source_id and _normalized(job.title) == _normalized(
                posting.title
            ):
                return _checked_text(job.summary), "applitrack/DescriptionText", company.url
        raise Unavailable("AppliTrack did not return the requested job's description.")
    if platform == "workday":
        site_url = workday.site_url(company.url)
        path = urlsplit(posting.url).path
        site = urlsplit(site_url or "")
        if "/job/" not in path or urlsplit(posting.url).netloc != site.netloc:
            raise Unavailable("The saved Workday job has no matching public detail path.")
        detail = "/job/" + path.split("/job/", 1)[1]
        tenant = site.hostname.split(".")[0]
        endpoint = f"{site.scheme}://{site.netloc}/wday/cxs/{tenant}{site.path}{detail}"
        response = client.get(endpoint)
        data = response.json()
        info = data.get("jobPostingInfo") if isinstance(data, dict) else None
        if not isinstance(info, dict) or _normalized(info.get("title") or "") != _normalized(
            posting.title
        ):
            raise Unavailable("Workday did not return the requested job's details.")
        description = info.get("jobDescription")
        if not isinstance(description, str):
            raise Unavailable("Workday did not return a full job description.")
        return (
            _checked_text(description),
            "workday/jobPostingInfo.jobDescription",
            str(response.url),
        )
    response = client.get(posting.url)
    text, method = parse_html(response.text, posting)
    return text, method, str(response.url)


def signature(posting: Posting, company: Company) -> str:
    value = [
        VERSION,
        posting.source_id,
        posting.url,
        posting.title,
        posting.summary,
        adapters.platform(company),
        company.url,
    ]
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def pending(
    postings: Iterable[Posting],
    cfg: Config,
    records: dict,
    now: datetime,
    *,
    retry_unavailable: bool = False,
) -> list[Posting]:
    scopes = locations.scopes(cfg)
    companies = {c.id: c for c in cfg.companies if c.enabled}
    out = []
    for posting in postings:
        company = companies.get(posting.company_id)
        if not company or posting.status != "active" or posting.duplicate_of:
            continue
        if posting.company_id in scopes and scopes[posting.company_id].area(posting) is None:
            continue
        saved = records.get(posting.key, {})
        if retry_unavailable:
            if saved.get("status") in ("unavailable", "failed"):
                out.append(posting)
            continue
        due = saved.get("signature") != signature(posting, company)
        try:
            age = now - datetime.fromisoformat(saved["checked_at"])
            due |= age >= (REFRESH_AFTER if saved.get("status") == "ready" else RETRY_AFTER)
        except (KeyError, ValueError, TypeError):
            due = True
        if due:
            out.append(posting)
    # Unvisited jobs first, then the oldest checks; rotate companies for useful coverage.
    out.sort(
        key=lambda p: (
            records.get(p.key, {}).get("checked_at", ""),
            -p.first_seen.timestamp(),
            p.key,
        )
    )
    groups: dict[str, list[Posting]] = {}
    for posting in out:
        groups.setdefault(posting.company_id, []).append(posting)
    result = []
    while groups:
        for company_id in list(groups):
            result.append(groups[company_id].pop(0))
            if not groups[company_id]:
                del groups[company_id]
    return result


def collect(
    postings: Iterable[Posting],
    cfg: Config,
    path: Path,
    http: httpx.Client,
    *,
    limit: int = 50,
    max_seconds: float = 120,
    now: datetime | None = None,
    progress: Callable | None = None,
    validate: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    retry_unavailable: bool = False,
) -> dict[str, int]:
    """Checkpoint each attempt; one detail failure cannot remove or reclassify a job."""
    if limit < 0 or max_seconds <= 0:
        raise ValueError("Description limit must be nonnegative and time budget positive.")
    postings = list(postings)
    now = now or datetime.now(UTC)
    records = jsonfile.read(path)
    queue = pending(postings, cfg, records, now, retry_unavailable=retry_unavailable)
    queue = queue[:limit] if limit else queue
    companies = {c.id: c for c in cfg.companies}
    guarded = crawl.Client(http, limit=max(20, len(queue) * 4), validate=validate, sleep=sleep)
    client = crawl.CachedClient(guarded)
    started = time.monotonic()
    counts: Counter = Counter()
    blocked_hosts: dict[str, str] = {}
    activity.emit("descriptions_started", pending=len(queue), limit=limit)
    for posting in queue:
        if time.monotonic() - started >= max_seconds:
            break
        company = companies[posting.company_id]
        checked = now.isoformat()
        record = records.get(posting.key, {}).copy()
        record.update(signature=signature(posting, company), checked_at=checked, url=posting.url)
        host = urlsplit(posting.url).netloc
        try:
            if host in blocked_hosts:
                raise crawl.Blocked(blocked_hosts[host])
            text, method, source_url = fetch(posting, company, client)
            record.update(
                status="ready",
                text=text,
                fetched_at=checked,
                method=method,
                source_url=source_url,
                content_hash=hashlib.sha256(text.encode()).hexdigest(),
            )
            record.pop("error", None)
        except crawl.LimitExceeded:
            break
        except (ValueError, httpx.HTTPError) as exc:
            blocked = isinstance(exc, crawl.Blocked) or "robots.txt" in str(exc)
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 403):
                blocked = True
            record["status"] = (
                "blocked"
                if blocked
                else "unavailable"
                if isinstance(exc, Unavailable)
                else "failed"
            )
            record["error"] = activity.clean(f"{type(exc).__name__}: {exc}")
            if "robots.txt" in str(exc):
                blocked_hosts[host] = activity.clean(str(exc))
        records[posting.key] = record
        jsonfile.write(path, records)
        counts[record["status"]] += 1
        activity.emit(
            "description_completed",
            posting=posting.key,
            status=record["status"],
            characters=len(record.get("text", "")),
            error=record.get("error", ""),
        )
        if progress:
            progress(posting, record)
    counts["attempted"] = sum(counts.values())
    counts["remaining"] = len(pending(postings, cfg, records, now))
    activity.emit("descriptions_completed", **counts)
    return dict(counts)


def run(postings: Iterable[Posting], cfg: Config, path: Path) -> None:
    """Continue an opted-in collection after a scheduled crawl, with bounded work."""
    if not path.exists():
        return
    from jobscraper.scraping import http

    try:
        with http.client() as client:
            collect(postings, cfg, path, client)
    except Exception:
        log.exception("Description collection stopped; completed records remain saved")
