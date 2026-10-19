"""WordPress Job Manager public listings feed (HTML cards in a JSON envelope)."""

import json
import re
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from jobscraper import activity
from jobscraper.adapters import generic
from jobscraper.models import Company, RawPosting


def fetch(company: Company, client: httpx.Client) -> list[RawPosting]:
    response = client.get(company.url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    container = soup.select_one("div.job_listings")
    match = re.search(r"\bjob_manager_ajax_filters\s*=\s*(?=\{)", response.text)
    if not container or not match:
        raise ValueError("WordPress Job Manager settings are missing from the careers page.")
    settings, _ = json.JSONDecoder().raw_decode(response.text[match.end() :])
    ajax_url = settings.get("ajax_url")
    if not isinstance(ajax_url, str) or not ajax_url:
        raise ValueError("WordPress Job Manager listings endpoint is missing.")
    endpoint = urljoin(company.url, ajax_url.replace("%%endpoint%%", "get_listings"))
    if urlsplit(endpoint).netloc != urlsplit(company.url).netloc:
        raise ValueError("Job Manager feed must remain on the careers host.")
    query = dict(parse_qsl(urlsplit(company.url).query))
    params = {
        "per_page": 100,
        "search_keywords": query.get("search_keywords", container.get("data-keywords", "")),
        "search_location": query.get("search_location", container.get("data-location", "")),
        "search_categories": container.get("data-categories", ""),
        "orderby": container.get("data-orderby", "date"),
        "order": container.get("data-order", "DESC"),
    }
    by_id = {}
    for page in range(1, 31):
        response = client.post(endpoint, data=params | {"page": page})
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get("html"), str):
            raise ValueError("Job Manager feed is not a listings response.")
        pages = data.get("max_num_pages")
        if type(pages) is not int or pages < 0 or pages > 30:
            raise ValueError("Job Manager returned an invalid or excessive page count.")
        cards = BeautifulSoup(data["html"], "html.parser").select("li.job_listing")
        previous = len(by_id)
        for card in cards:
            sid = next((c[5:] for c in card.get("class", []) if re.fullmatch(r"post-\d+", c)), "")
            title, link = card.select_one(".position h3"), card.select_one("a[href]")
            location, posted = card.select_one(".location"), card.select_one("time[datetime]")
            if not sid or title is None or link is None:
                raise ValueError("Job Manager card lacks a stable ID, title, or link.")
            p = generic.posting(
                {
                    "source_id": sid,
                    "title": title.get_text(" ", strip=True),
                    "url": link["href"],
                    "location": location.get_text(" ", strip=True) if location else "",
                    "posted_date": posted["datetime"] if posted else None,
                },
                company.url,
            )
            by_id[sid] = p
        activity.emit("extraction_page", page=page, postings=len(cards), total_pages=pages)
        if not cards and not (page == 1 and pages == 0 and data.get("found_jobs") is False):
            raise ValueError("Job Manager returned an unexpectedly empty listings page.")
        if page > 1 and len(by_id) == previous:
            raise ValueError("Job Manager pagination repeated listings.")
        if page >= pages:
            return list(by_id.values())
    raise ValueError("Job Manager pagination exceeded its limit.")
