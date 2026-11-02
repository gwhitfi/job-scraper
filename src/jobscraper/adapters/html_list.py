"""Small company sites with a hand-made jobs list, configured per company with CSS selectors.

    options = {item = "...", title = "...", link = "...", summary = "...", none_text = "..."}

- item: selector for each job (required)
- title / link / summary: selectors inside an item; title defaults to the item's text and
  link to the item itself (if it's a link) or its first link, else the careers page
- none_text: text the page shows when there are no openings
"""

import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from jobscraper.models import Company, RawPosting


def _text(el) -> str:
    return " ".join(el.get_text(" ", strip=True).split()) if el else ""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def parse(html: str, company: Company) -> list[RawPosting]:
    opts = company.options
    soup = BeautifulSoup(html, "html.parser")
    postings = []
    for item in soup.select(opts["item"]):
        title = _text(item.select_one(opts["title"])) if opts.get("title") else _text(item)
        if not title:
            continue
        if opts.get("link"):
            link = item.select_one(opts["link"])
        else:
            link = item if item.name == "a" else item.find("a", href=True)
        href = link.get("href") if link else None
        url = urljoin(company.url, href) if href else company.url
        summary = _text(item.select_one(opts["summary"])) if opts.get("summary") else ""
        postings.append(
            RawPosting(
                # Without a job link, the title is the only stable thing to key on.
                source_id=_slug(href if href else title),
                title=title,
                location="",
                url=url,
                summary=summary,
            )
        )

    none_text = opts.get("none_text", "").lower()
    if not postings and none_text and none_text not in soup.get_text(" ").lower():
        raise ValueError(f"{company.name} careers page changed; check the extraction settings.")
    return postings


def fetch(company: Company, client: httpx.Client) -> list[RawPosting]:
    if "item" not in company.options:
        raise ValueError(f"{company.id}: html_list needs options.item")
    resp = client.get(company.url)
    resp.raise_for_status()
    return parse(resp.text, company)
