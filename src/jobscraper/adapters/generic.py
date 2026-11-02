"""Fetch postings with saved HTML, JSON, or Schema.org extraction settings."""

import hashlib
import json
from copy import deepcopy
from datetime import date
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from soupsieve import SelectorSyntaxError
from soupsieve import compile as compile_selector

from jobscraper import activity
from jobscraper.geography.locations import STATE_CODES
from jobscraper.models import Company, RawPosting
from jobscraper.scraping import crawl

MAX_PAGES = 30
NEXT_SELECTOR = (
    'a[rel~="next"], a[aria-label="Next" i], a[aria-label="Next page" i], '
    'a[title="Next" i], a[title="Next page" i]'
)


def validate(options: dict) -> None:
    if not isinstance(options, dict) or options.get("format") not in ("html", "json", "jsonld"):
        raise ValueError("Choose HTML job cards, JSON feed, or structured job data.")
    if options["format"] != "jsonld":
        fields = options.get("fields")
        if not isinstance(fields, dict) or not {"title", "url"} <= fields.keys():
            raise ValueError("Supply the title and job-link fields.")
        if any(not isinstance(v, str) for v in fields.values()):
            raise ValueError("Field paths and selectors must be text.")
    if options["format"] == "html" and not options.get("item"):
        raise ValueError("Supply the selector for each job card.")
    for key in ("item", "items", "next", "total", "none_text", "script_id"):
        if key in options and not isinstance(options[key], str):
            raise ValueError(f"{key} must be text.")
    if options["format"] != "json":
        selectors = [options.get("item"), options.get("next")]
        selectors += [
            v.partition("@")[0]
            for v in options.get("fields", {}).values()
            if v not in ("$page", "$text")
        ]
        try:
            for selector in filter(None, selectors):
                compile_selector(selector)
        except SelectorSyntaxError as e:
            raise ValueError(f"Invalid CSS selector: {e}") from e
    if options.get("method", "GET") not in ("GET", "POST"):
        raise ValueError("Jobs feeds must use GET or POST.")
    if not isinstance(options.get("body", {}), dict):
        raise ValueError("The public request body must be a JSON object.")
    if options.get("page_field"):
        if not isinstance(options["page_field"], str) or not options.get("total"):
            raise ValueError(
                "Paged JSON feeds need both a page/offset field and total-count field."
            )
        if type(options.get("page_step", 1)) is not int or options.get("page_step", 1) < 1:
            raise ValueError("The page/offset increment must be a positive integer.")
        if type(options.get("page_start", 0)) is not int or options.get("page_start", 0) < 0:
            raise ValueError("The first page/offset must be a nonnegative integer.")


def text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return text(value.get("name") or value.get("value"))
    if isinstance(value, list):
        return "; ".join(filter(None, (text(v) for v in value)))
    return " ".join(BeautifulSoup(str(value), "html.parser").get_text(" ", strip=True).split())


def path_value(data, path: str):
    if not path:
        return data
    for part in path.split("."):
        if isinstance(data, dict):
            data = data.get(part)
        elif isinstance(data, list) and part.isdigit() and int(part) < len(data):
            data = data[int(part)]
        else:
            return None
    return data


def posting(values: dict, base: str) -> RawPosting:
    title = text(values.get("title"))
    link = values.get("url")
    if not title or not isinstance(link, str) or not link.strip():
        raise ValueError("A job is missing its title or link. Check the extraction settings.")
    url = urljoin(base, link)
    if urlsplit(url).scheme not in ("http", "https"):
        raise ValueError("A job link is not a public web URL.")
    source_id = text(values.get("source_id")) or hashlib.sha256(url.encode()).hexdigest()[:24]
    posted = text(values.get("posted_date"))
    try:
        posted_date = date.fromisoformat(posted[:10]) if posted else None
    except ValueError:
        posted_date = None
    country_values = values.get("countries") or []
    if not isinstance(country_values, list | tuple):
        country_values = [country_values]
    countries = tuple(
        dict.fromkeys(
            country.strip()
            for value in country_values
            for country in text(value).split(";")
            if country.strip()
        )
    )
    return RawPosting(
        source_id=source_id,
        title=title,
        url=url,
        location=text(values.get("location")),
        posted_date=posted_date,
        category=text(values.get("category")),
        summary=text(values.get("summary"))[:1000],
        employer=text(values.get("employer")),
        countries=countries,
    )


def structured_jobs(data):
    if isinstance(data, list):
        for item in data:
            yield from structured_jobs(item)
    elif isinstance(data, dict):
        types = data.get("@type", [])
        if "JobPosting" in ([types] if isinstance(types, str) else types or []):
            yield data
        else:
            for value in data.values():
                if isinstance(value, dict | list):
                    yield from structured_jobs(value)


def jsonld(html: str, url: str) -> list[RawPosting]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.get_text())
        except (ValueError, TypeError):
            continue
        for job in structured_jobs(data):
            locations = job.get("jobLocation") or []
            if isinstance(locations, dict):
                locations = [locations]
            places = []
            countries = []
            for place in locations:
                address = place.get("address", {}) if isinstance(place, dict) else {}
                if isinstance(address, str):
                    places.append(address)
                elif isinstance(address, dict):
                    countries.append(address.get("addressCountry"))
                    places.append(
                        ", ".join(
                            filter(
                                None,
                                (
                                    text(address.get("addressLocality")),
                                    STATE_CODES.get(
                                        text(address.get("addressRegion")),
                                        text(address.get("addressRegion")),
                                    ),
                                ),
                            )
                        )
                    )
            if job.get("jobLocationType") == "TELECOMMUTE":
                places.append("Remote")
            link = job.get("url") or job.get("mainEntityOfPage") or url
            if isinstance(link, dict):
                link = link.get("@id") or link.get("url")
            out.append(
                posting(
                    {
                        "title": job.get("title"),
                        "url": link,
                        "source_id": (
                            job["identifier"].get("value")
                            if isinstance(job.get("identifier"), dict)
                            else job.get("identifier")
                        ),
                        "location": places,
                        "countries": countries,
                        "employer": job.get("hiringOrganization"),
                        "posted_date": job.get("datePosted"),
                        "summary": job.get("description"),
                        "category": job.get("occupationalCategory"),
                    },
                    url,
                )
            )
    return out


def next_page(soup: BeautifulSoup, url: str, selector: str | None = None) -> str | None:
    def enabled(link) -> bool:
        return (
            bool(link.get("href"))
            and link.get("aria-disabled") != "true"
            and "disabled" not in link.get("class", [])
            and not (link.parent and "disabled" in link.parent.get("class", []))
        )

    # Explicit Next links take precedence over numbered pagination.
    for link in soup.select(selector or NEXT_SELECTOR):
        if enabled(link):
            return urljoin(url, link["href"])
    if selector:
        return None
    for current in soup.select('.pagination li.active, .pagination li:has([aria-current="page"])'):
        number = current.get_text(strip=True)
        following = current.find_next_sibling("li")
        link = following.select_one("a[href]") if following else None
        # The sibling after the final numbered page may be a First/Last control.
        if (
            number.isdigit()
            and link
            and enabled(link)
            and link.get_text(strip=True) == str(int(number) + 1)
        ):
            return urljoin(url, link["href"])
    return None


def parse_html(html: str, url: str, options: dict) -> tuple[list[RawPosting], str | None]:
    soup = BeautifulSoup(html, "html.parser")
    if options["format"] == "jsonld":
        out = jsonld(html, url)
    else:
        out = []
        for card in soup.select(options["item"]):
            values = {}
            for field, spec in options["fields"].items():
                if spec == "$page":
                    values[field] = url
                    continue
                if spec == "$text":
                    values[field] = card.get_text(" ", strip=True)
                    continue
                selector, _, attr = spec.partition("@")
                element = card.select_one(selector.strip()) if selector.strip() else card
                values[field] = (
                    element.get(attr)
                    if element and attr
                    else text(element.get_text(" "))
                    if element
                    else ""
                )
            out.append(posting(values, url))
    if options.get("fields", {}).get("url") == "$page" and len({p.source_id for p in out}) != len(
        out
    ):
        raise ValueError("Inline jobs share an identifier. Supply a unique job ID field.")
    if not out:
        none_text = options.get("none_text", "")
        if not none_text or none_text.lower() not in soup.get_text(" ").lower():
            raise ValueError(
                "No recognizable jobs or confirmed no-openings message. The page may have changed."
            )
    next_url = next_page(soup, url, options.get("next"))
    button_next = any(
        (button.get("aria-label") or button.get_text(" ", strip=True)).strip().casefold()
        in ("next", "next page", "load more", "load more jobs")
        and not button.has_attr("disabled")
        and button.get("aria-disabled") != "true"
        for button in soup.select("button")
    )
    if not next_url and (
        button_next
        or soup.select('button[aria-label*="Load more"], button.load-more, [data-load-more]')
    ):
        raise ValueError(
            "Jobs load through a button. Supply the public jobs-data URL from the Network tab."
        )
    return out, next_url


def fetch(company: Company, client: httpx.Client) -> list[RawPosting]:
    options = company.options
    validate(options)
    url = company.url
    seen_pages = set()
    by_id: dict[str, RawPosting] = {}
    expected_total: int | None = None
    for page in range(MAX_PAGES):
        page_field = options.get("page_field")
        body = deepcopy(options.get("body", {}))
        method = options.get("method", "GET")
        if page_field:
            value = options.get("page_start", 0) + page * options.get("page_step", 1)
            if method == "POST":
                body[page_field] = value
            else:
                parts = urlsplit(company.url)
                query = [
                    (k, v)
                    for k, v in parse_qsl(parts.query, keep_blank_values=True)
                    if k != page_field
                ]
                url = parts._replace(query=urlencode(query + [(page_field, value)])).geturl()
        page_key = (url, json.dumps(body, sort_keys=True))
        if page_key in seen_pages:
            raise ValueError(
                "Pagination repeated a page; refusing to accept an incomplete job list."
            )
        seen_pages.add(page_key)
        if options.get("format") == "json":
            response = client.post(url, json=body) if method == "POST" else client.get(url)
            response.raise_for_status()
            if options.get("script_id"):
                script = BeautifulSoup(response.text, "html.parser").find(
                    "script", id=options["script_id"]
                )
                if script is None:
                    raise ValueError("The embedded jobs data is missing from the page.")
                data = json.loads(script.get_text())
            else:
                data = response.json()
            items = path_value(data, options.get("items", ""))
            if not isinstance(items, list):
                raise ValueError("The configured jobs field is no longer a JSON list.")
            base_url = str(response.url)
            postings = [
                posting({k: path_value(item, v) for k, v in options["fields"].items()}, base_url)
                for item in items
            ]
            next_value = path_value(data, options["next"]) if options.get("next") else None
            next_url = urljoin(base_url, str(next_value)) if next_value else None
            if options.get("total"):
                total = path_value(data, options["total"])
                if type(total) is not int or total < 0:
                    raise ValueError("The jobs feed no longer reports its expected total.")
                if expected_total is not None and total != expected_total:
                    raise ValueError(
                        "The jobs feed total changed during pagination. Retry the crawl."
                    )
                expected_total = total
            else:
                total = None
        else:
            response = client.get(url)
            response.raise_for_status()
            postings, next_url = parse_html(response.text, str(response.url), options)
            total = None
        activity.emit(
            "extraction_page",
            url=url,
            page=page + 1,
            postings=len(postings),
            next_url=next_url,
            total=total,
        )
        previous = len(by_id)
        for p in postings:
            if p.source_id in by_id and (
                by_id[p.source_id].url != p.url
                or by_id[p.source_id].title != p.title
                or by_id[p.source_id].employer != p.employer
                or by_id[p.source_id].location != p.location
                or by_id[p.source_id].countries != p.countries
            ):
                raise ValueError("Different jobs share an identifier. Check the job ID field.")
            by_id.setdefault(p.source_id, p)
        if total is not None and len(by_id) > total:
            raise ValueError("The feed returned more jobs than its reported total.")
        if page and postings and len(by_id) == previous:
            raise ValueError("Pagination returned no new jobs; refusing an incomplete job list.")
        if page_field and total is not None and len(by_id) < total:
            if not postings:
                raise ValueError("The feed stopped before its reported total. Check pagination.")
            continue
        if not next_url:
            if total is not None and len(by_id) < total:
                raise ValueError(
                    "The feed has more jobs than returned. Supply its next-page field."
                )
            return list(by_id.values())
        if urlsplit(next_url).netloc != urlsplit(company.url).netloc:
            raise ValueError("Pagination left the jobs site. Check the next-page setting.")
        url = next_url
    raise crawl.LimitExceeded("Too many results pages. Use a filtered jobs URL.")
