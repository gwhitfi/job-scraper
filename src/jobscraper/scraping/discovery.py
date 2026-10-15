"""Identify a jobs source, verify its extraction settings, and explain missing information."""

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import date
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from jobscraper import activity, adapters
from jobscraper.adapters import generic, workday
from jobscraper.models import Company, RawPosting
from jobscraper.scraping import crawl, navigation


@dataclass
class Discovery:
    company: Company | None = None
    postings: list[RawPosting] = field(default_factory=list)
    message: str = ""
    help: list[str] = field(default_factory=list)
    status: str = "needs_input"


def identify(url: str, html: str = "") -> tuple[str, str, dict] | None:
    """Use platform evidence, preserving filters in the submitted search URL."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    path = parts.path
    soup = BeautifulSoup(html, "html.parser")
    token = path.strip("/").split("/")[0]
    if soup.select_one("div.job_listings") and "job_manager_ajax_filters" in html:
        return "wp_job_manager", url, {}
    if workday.site_url(url):
        return "workday", workday.site_url(url), {}
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io") and token:
        return (
            "generic",
            f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
            {
                "format": "json",
                "items": "jobs",
                "total": "meta.total",
                "fields": {
                    "source_id": "id",
                    "title": "title",
                    "url": "absolute_url",
                    "location": "location.name",
                },
            },
        )
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and token:
        api = "api.eu.lever.co" if host == "jobs.eu.lever.co" else "api.lever.co"
        return (
            "generic",
            f"https://{api}/v0/postings/{token}?mode=json",
            {
                "format": "json",
                "items": "",
                "fields": {
                    "source_id": "id",
                    "title": "text",
                    "url": "hostedUrl",
                    "location": "categories.location",
                    "summary": "descriptionPlain",
                },
            },
        )
    if host.endswith(".bamboohr.com"):
        return "bamboohr", f"https://{host}/careers", {}
    if host == "recruiting.paylocity.com" and "/recruiting/jobs/All/" in path:
        return "paylocity", url, {}
    match = re.search(r"(.*/JobBoard/[^/]+)", path, re.I)
    if host.endswith(".ultipro.com") and match:
        return "ultipro", f"{parts.scheme}://{parts.netloc}{match[1]}/", {}
    match = re.search(r"/sites/([^/]+)", path)
    if host.endswith(".oraclecloud.com") and match:
        query = parse_qs(parts.query)
        settings = {"site": match[1]}
        if query.get("locationId"):
            settings["location_id"] = query["locationId"][0]
        return "oracle_hcm", url, settings
    if host == "pm.healthcaresource.com" and re.match(r"/cs/[^/]+", path):
        return "healthcaresource", url, {}
    if soup.select_one("li.job-tile") and re.search(r"/go/[^/]+/\d+/?$", path):
        return "successfactors", url, {}
    if soup.select_one("a.ds__card-offer"):
        return "michelin", url, {}
    if soup.select_one("a.c-job") and host == "moveupstatesc.com":
        return "moveupstatesc", url, {}
    if host == "careers.prismahealth.org" and path.startswith("/jobs"):
        return "prisma", url, {}
    if "jobfinder" in path and path.endswith(".content.html") and host.endswith("bmwgroup.jobs"):
        query = parse_qs(parts.query)
        return "bmw", url, {"filters": query.get("filterSearch", [""])[0].split(",")}
    if "phenom" in html.lower() and ("phApp" in html or "ph-page" in html):
        return "phenom", url, {}
    return None


def inline_jobs(soup: BeautifulSoup) -> dict | None:
    """Job cards that open local modals have no individual detail-page links."""
    for trigger in soup.select('[data-target^="#"], [data-bs-target^="#"]'):
        target = trigger.get("data-target") or trigger.get("data-bs-target")
        modal = soup.find(id=target[1:])
        if not modal or not (modal.get("role") == "dialog" or "modal" in modal.get("class", [])):
            continue
        for card in list(trigger.parents)[:4]:
            classes = [
                c
                for c in card.get("class", [])
                if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", c)
                and re.search(r"job|position|opening", c, re.I)
            ]
            heading = card.select_one("h1, h2, h3, h4, h5, h6")
            if not classes or heading is None:
                continue
            selector = card.name + "." + sorted(classes)[0]
            cards = soup.select(selector)
            if not all(
                c.select_one(heading.name)
                and c.select_one('[data-target^="#"], [data-bs-target^="#"]')
                for c in cards
            ):
                continue
            return {
                "format": "html",
                "item": selector,
                "fields": {
                    "title": heading.name,
                    "source_id": heading.name,
                    "url": "$page",
                    "summary": "$text",
                },
            }
    return None


def infer_html(html: str, url: str) -> dict | None:
    if generic.jsonld(html, url):
        return {"format": "jsonld"}
    soup = BeautifulSoup(html, "html.parser")
    # SuccessFactors also renders table rows on search pages, rather than category tiles.
    if soup.select("tr.data-row a.jobTitle-link[href]"):
        return {
            "format": "html",
            "item": "tr.data-row",
            "fields": {
                "title": "a.jobTitle-link",
                "url": "a.jobTitle-link@href",
                "location": ".jobLocation",
            },
        }
    if recipe := inline_jobs(soup):
        return recipe
    # Repeated links to job detail paths are stronger evidence than generic careers navigation.
    for segment in ("job-postings", "job", "jobs", "position", "positions", "openings"):
        pattern = re.compile(rf"/{segment}/[^/?#]+", re.I)
        links = [
            a
            for a in soup.select("a[href]")
            if pattern.search(urlsplit(urljoin(url, a["href"])).path)
            and 3 <= len(a.get_text(" ", strip=True)) <= 180
            and a.get_text(" ", strip=True).lower()
            not in ("apply", "apply now", "learn more", "view job")
        ]
        # Only auto-select a repeated, explicit job-link class; ask for a selector otherwise.
        classes = set.intersection(*(set(a.get("class", [])) for a in links)) if links else set()
        classes = {c for c in classes if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", c)}
        classes = {
            c for c in classes if any(word in c.lower() for word in ("job", "position", "opening"))
        }
        if classes:
            cls = sorted(classes)[0]
            return {
                "format": "html",
                "item": f"a.{cls}[href]",
                "fields": {"title": "", "url": "@href"},
            }
        # Some cards label their link "Apply" and put the actual title in a heading.
        for link in soup.select(f'a[href*="/{segment}/"]'):
            for card in list(link.parents)[:3]:
                heading = card.select_one("h2, h3, h4, h5, h6")
                classes = [
                    c for c in card.get("class", []) if re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_-]*", c)
                ]
                if heading is None or not classes or card.name in ("body", "html", "main"):
                    continue
                selector = card.name + "." + ".".join(classes)
                cards = soup.select(selector)
                if all(
                    len(c.select(f'a[href*="/{segment}/"]')) == 1 and c.select_one(heading.name)
                    for c in cards
                ):
                    return {
                        "format": "html",
                        "item": selector,
                        "fields": {
                            "title": heading.name,
                            "url": f'a[href*="/{segment}/"]@href',
                        },
                    }
    return None


def infer_json(data) -> dict | None:
    candidates = [("", data)]
    while candidates:
        path, value = candidates.pop(0)
        if isinstance(value, dict):
            candidates.extend(
                (f"{path}.{k}".lstrip("."), v)
                for k, v in value.items()
                if isinstance(v, dict | list)
            )
        elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
            sample = value[0]
            title = next(
                (
                    k
                    for k in ("title", "jobTitle", "text", "name")
                    if isinstance(sample.get(k), str)
                ),
                None,
            )
            url = next(
                (
                    k
                    for k in ("url", "absolute_url", "hostedUrl", "jobUrl", "applyUrl")
                    if isinstance(sample.get(k), str)
                ),
                None,
            )
            if not title or not url:
                continue
            links = [str(item.get(url, "")) for item in value]
            if any(
                re.search(r"\.(?:mp4|webm|mov|mp3|png|jpe?g|svg)(?:[?#]|$)", link, re.I)
                for link in links
            ):
                continue
            job_evidence = (
                re.search(r"job|posting|position|requisition|vacanc", path, re.I)
                or title == "jobTitle"
                or url in ("jobUrl", "applyUrl", "hostedUrl")
                or all(re.search(r"/(?:jobs?|positions?|openings)/", link, re.I) for link in links)
            )
            if not job_evidence:
                continue
            fields = {"title": title, "url": url}
            for field, names in {
                "source_id": ("id", "jobId", "identifier"),
                "location": ("location", "categories.location"),
                "summary": ("description", "descriptionPlain", "content"),
                "posted_date": ("datePosted", "publishedAt"),
            }.items():
                found = next(
                    (name for name in names if generic.path_value(sample, name) is not None), None
                )
                if found:
                    fields[field] = found
            settings = {"format": "json", "items": path, "fields": fields}
            prefixes = [
                ".".join(path.split(".")[:i]) for i in range(len(path.split(".")) - 1, -1, -1)
            ]
            for prefix in prefixes:
                parent = generic.path_value(data, prefix)
                if not isinstance(parent, dict):
                    continue
                for key in ("total", "totalCount", "meta.total"):
                    full = f"{prefix}.{key}".lstrip(".")
                    if "total" not in settings and type(generic.path_value(data, full)) is int:
                        settings["total"] = full
                for key in ("next", "links.next", "nextPageUrl"):
                    full = f"{prefix}.{key}".lstrip(".")
                    if "next" not in settings and isinstance(generic.path_value(data, full), str):
                        settings["next"] = full
            return settings
    return None


def _name(html: str, url: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    name = soup.select_one('meta[property="og:site_name"]')
    if name and name.get("content"):
        return name["content"][:120]
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    title = re.sub(
        r"\b(careers?|jobs?|opportunities|join us|open positions)\b", "", title, flags=re.I
    )
    return (
        re.sub(r"^(?:at|with)\s+", "", title.strip(" |-–—:"), flags=re.I)[:120]
        or urlsplit(url).hostname
    )


def career_links(html: str, url: str) -> list[str]:
    return [target for target, _ in navigation.candidates(html, url, identify)]


def challenge(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    visible = soup.get_text(" ", strip=True).lower()
    if len(visible) < 1000 and soup.select_one(
        'script[src*="_Incapsula_Resource"], iframe[src*="_Incapsula_Resource"]'
    ):
        return "The site returned an automated-access security challenge instead of job listings."
    if len(visible) < 2000 and (
        "verify you are human" in visible
        or "checking your browser" in visible
        or soup.select_one('script[src*="/cdn-cgi/challenge-platform/"]')
    ):
        return "The site requires browser verification before job listings can be read."
    return None


def embedded_recipe(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.select('script[type="application/json"][id]'):
        try:
            recipe = infer_json(json.loads(script.get_text()))
        except ValueError:
            continue
        if recipe:
            return recipe | {"script_id": script["id"]}
    return None


def validate_source(
    detected, client, name: str, html: str, original: str, states: tuple[str, ...]
) -> Discovery:
    strategy, url, recipe = detected
    if strategy == "workday" and states:
        recipe = recipe | {"states": list(states)}
    activity.emit(
        "strategy_detected",
        strategy=strategy,
        url=url,
        format=recipe.get("format"),
        field_mapping=[{"field": k, "selector": v} for k, v in recipe.get("fields", {}).items()],
    )
    company_name = name.strip()[:120] or _name(html, original)
    key = re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-")[:45] or "company"
    key += "-" + hashlib.sha256(url.encode()).hexdigest()[:8]
    company = Company(key, company_name, strategy, url, date.today(), options=recipe)
    activity.emit("validation_started", company=key)
    jobs = adapters.get(strategy)(company, client)
    activity.emit("extraction_completed", postings=len(jobs))
    if not jobs and not (strategy == "generic" and recipe.get("none_text")):
        raise ValueError(
            "The source returned no jobs, so its extraction fields could not be verified."
        )
    if any(not p.source_id or not p.title or not p.url for p in jobs):
        raise ValueError("Some jobs lack identifiers, titles, or links.")
    if len({p.source_id for p in jobs}) != len(jobs):
        raise ValueError("The extraction settings produced duplicate job identifiers.")
    activity.emit("validation_completed", postings=len(jobs), unique_ids=len(jobs))
    company = replace(company, adapter="auto", options={"strategy": strategy, "settings": recipe})
    return Discovery(
        company=company,
        postings=jobs,
        message=f"Found {len(jobs)} jobs at {company_name}.",
        status="added",
    )


def inspect(
    url: str,
    client: crawl.Client,
    *,
    name: str = "",
    settings: dict | None = None,
    states: tuple[str, ...] = (),
) -> Discovery:
    original = crawl.normalize_url(url)
    if not isinstance(name, str):
        raise ValueError("Company name must be text.")
    if settings is not None:
        generic.validate(settings)
    cached = crawl.CachedClient(client)
    pending = {original: 200}
    visited, attempted = set(), set()
    failures: list[Exception] = []
    validation_failures: list[Exception] = []
    blocked = []
    origin_html = ""
    for index in range(12):
        pending = {u: s for u, s in pending.items() if navigation.canonical(u) not in visited}
        if not pending:
            break
        url = max(pending, key=pending.get)
        score = pending.pop(url)
        visited.add(navigation.canonical(url))
        activity.emit("discovery_page", url=url, score=score, manual_settings=settings is not None)
        detected = ("generic", url, settings) if settings is not None else identify(url)
        html = ""
        # Platforms with a known public API don't need to render their browser shell.
        if not detected:
            try:
                response = cached.get(url)
                url = str(response.url)
                visited.add(navigation.canonical(url))
                html = response.text
                if index == 0:
                    origin_html = html
                if reason := challenge(html):
                    raise crawl.Blocked(reason)
                detected = identify(url, html)
                if not detected:
                    try:
                        recipe = infer_json(response.json())
                    except ValueError:
                        recipe = infer_html(html, url) or embedded_recipe(html)
                    if recipe:
                        detected = ("generic", url, recipe)
            except crawl.LimitExceeded as exc:
                failures.append(exc)
                break
            except (ValueError, httpx.HTTPError) as exc:
                activity.emit("discovery_candidate_failed", url=url, error=str(exc))
                if isinstance(exc, crawl.Blocked) or (
                    isinstance(exc, httpx.HTTPStatusError)
                    and exc.response.status_code in (401, 403)
                ):
                    blocked.append((url, str(exc)))
                    if score >= 120:
                        break
                else:
                    failures.append(exc)
                continue
        if detected:
            key = (
                detected[0],
                navigation.canonical(detected[1]),
                json.dumps(detected[2], sort_keys=True),
            )
            if key not in attempted:
                attempted.add(key)
                try:
                    return validate_source(
                        detected, cached, name, origin_html or html, original, states
                    )
                except crawl.LimitExceeded as exc:
                    failures.append(exc)
                    break
                except (ValueError, httpx.HTTPError) as exc:
                    activity.emit(
                        "candidate_validation_failed", url=url, strategy=detected[0], error=str(exc)
                    )
                    if settings is not None:
                        raise
                    if isinstance(exc, crawl.Blocked):
                        blocked.append((url, str(exc)))
                    else:
                        failures.append(exc)
                        validation_failures.append(exc)
            if not html:
                # A wrong platform guess must not prevent following its other public links.
                try:
                    response = cached.get(url)
                    html = response.text
                    url = str(response.url)
                    visited.add(navigation.canonical(url))
                except (ValueError, httpx.HTTPError):
                    html = ""
        candidates = (
            navigation.candidates(html, url, identify)
            if html and not (detected and detected[0] == "generic")
            else []
        )
        for target, priority in candidates:
            if navigation.canonical(target) not in visited:
                pending[target] = max(priority, pending.get(target, 0))
        activity.emit(
            "discovery_candidates",
            url=url,
            candidates=[{"url": u, "score": p} for u, p in candidates],
            remaining_candidates=len(pending),
        )
    if blocked:
        target, reason = blocked[0]
        return Discovery(
            status="blocked",
            message=reason,
            help=[
                f"Blocked source: {target}",
                "Use a permitted public jobs feed or the site's own job alerts. "
                "Tracking was not enabled.",
            ],
        )
    if failures:
        # Preserve a concrete extraction error rather than replacing it with generic advice.
        raise (validation_failures or failures)[-1]
    return Discovery(
        message="No validated job list was found on the pages inspected.",
        help=[
            "Try the site's Search jobs or Current openings results URL.",
            "For browser-loaded listings, open F12 → Network → Fetch/XHR and look for a public "
            "jobs response. Paste its Request URL or use the advanced fields.",
            "The attempt log shows which pages and extraction strategies were tried. "
            "No tracking was enabled.",
        ],
    )
