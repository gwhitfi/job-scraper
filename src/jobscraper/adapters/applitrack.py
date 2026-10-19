"""Frontline AppliTrack listings embedded through a public HTML-writing script."""

import re
from datetime import datetime
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

from jobscraper import activity
from jobscraper.models import Company, RawPosting

_WRITES = re.compile(r"document\.write\(\s*'((?:\\.|[^'\\])*)'\s*\)\s*;")
_ESCAPES = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|['\"\\/bfnrtv])")
_CHARS = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}


def _decode(match: re.Match) -> str:
    value = match[1]
    return chr(int(value[1:], 16)) if value[0] in "ux" else _CHARS.get(value, value)


def parse(script: str, url: str) -> list[RawPosting]:
    html = "".join(_ESCAPES.sub(_decode, part) for part in _WRITES.findall(script))
    soup = BeautifulSoup(html, "html.parser")
    header = soup.select_one("#AppliTrackSearchContainer")
    total = re.search(r"\(\s*(\d+)\s+openings?\s*\)", header.get_text(" ") if header else "")
    if total is None:
        raise ValueError("AppliTrack did not report a verifiable openings count.")
    postings = []
    for card in soup.select("ul.postingsList"):
        sid = re.fullmatch(r"p(\d+)_", card.get("id", ""))
        title = card.select_one("table.title td")
        if not sid or title is None or not title.get_text(strip=True):
            raise ValueError("AppliTrack listing lacks its job ID or title.")
        fields = {}
        for row in card.select("li"):
            label = row.select_one(".label")
            if label:
                fields[label.get_text(strip=True).rstrip(":")] = " ".join(
                    value.get_text(" ", strip=True) for value in row.select(".normal")
                )
        description = card.find(id="DescriptionText" + sid[1] + "_")
        posted = fields.get("Date Posted")
        postings.append(
            RawPosting(
                source_id=sid[1],
                title=title.get_text(" ", strip=True),
                location=fields.get("Location", ""),
                url=urlsplit(url)._replace(query="AppliTrackJobID=" + sid[1], fragment="").geturl(),
                posted_date=datetime.strptime(posted, "%m/%d/%Y").date() if posted else None,
                category=fields.get("Position Type", ""),
                summary=description.get_text(" ", strip=True) if description else "",
            )
        )
    if len(postings) != int(total[1]) or len({p.source_id for p in postings}) != len(postings):
        raise ValueError("AppliTrack listings are incomplete or contain duplicate job IDs.")
    return postings


def fetch(company: Company, client: httpx.Client) -> list[RawPosting]:
    response = client.get(company.url)
    response.raise_for_status()
    match = re.search(r'\bvar\s+targetURL\s*=\s*[\'"](jobpostings/Output\.asp)[\'"]', response.text)
    if not match:
        raise ValueError("AppliTrack public listings script is missing from the careers page.")
    endpoint = urlsplit(urljoin(str(response.url), match[1]))
    endpoint = endpoint._replace(query=urlsplit(company.url).query).geturl()
    response = client.get(endpoint)
    response.raise_for_status()
    postings = parse(response.text, company.url)
    activity.emit("extraction_page", url=endpoint, postings=len(postings), total=len(postings))
    return postings
