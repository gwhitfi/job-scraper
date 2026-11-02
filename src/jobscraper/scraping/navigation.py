"""Rank public job-search destinations ahead of general career navigation."""

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from jobscraper.adapters import workday

EXCLUDE = re.compile(
    r"\b(login|log-in|sign-in|signin|register|alerts?|saved|privacy|terms|faqs?|fraud|cookies|accessibility)\b",
    re.I,
)
SEARCH = re.compile(
    r"\b(search jobs|find jobs|view (?:all )?(?:jobs|openings|positions)|"
    r"current (?:openings|vacancies)|job.search|job.openings|open positions|"
    r"employment opportunities|results)\b",
    re.I,
)
CAREER = re.compile(r"\b(jobs?|careers?|vacanc(?:y|ies)|employment|positions|apply now)\b", re.I)


def canonical(url: str) -> str:
    p = urlsplit(url)
    pairs = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in ("source", "ref", "referrer")
    ]
    return p._replace(
        netloc=p.netloc.lower(), path=p.path or "/", query=urlencode(sorted(pairs)), fragment=""
    ).geturl()


def rank(url: str, label: str = "") -> int:
    p = urlsplit(url)
    combined = label + " " + p.path.replace("-", " ").replace("_", " ")
    if EXCLUDE.search(combined):
        return -1
    if SEARCH.search(combined) or re.search(r"/(?:search|results|job-search)/?$", p.path, re.I):
        return 120
    if CAREER.search(label):
        return 65
    if re.search(r"/(careers?|jobs?|employment|vacancies)(?:/|$)", p.path, re.I):
        return 35
    return 0


def candidates(html: str, url: str, identify) -> list[tuple[str, int]]:
    soup = BeautifulSoup(html, "html.parser")
    found = {}
    for element in soup.select("a[href], iframe[src], iframe[data-src], form[action]"):
        target = urljoin(
            url,
            element.get("href")
            or element.get("src")
            or element.get("data-src")
            or element.get("action"),
        )
        p = urlsplit(target)
        if p.scheme not in ("http", "https") or not p.hostname:
            continue
        if re.search(r"\.(pdf|docx?|png|jpe?g|zip)$", p.path, re.I):
            continue
        label = " ".join(
            [
                element.get_text(" ", strip=True),
                element.get("aria-label", ""),
                element.get("title", ""),
                *(img.get("alt", "") for img in element.select("img")),
            ]
        )
        score = rank(target, label)
        if score < 0:
            # A Workday sign-in link identifies a public board, but never visit the sign-in URL.
            board = workday.site_url(target)
            if board:
                board = urlsplit(board)._replace(query="").geturl()
                found[board] = max(found.get(board, 0), 75)
            continue
        if (
            element.name != "iframe"
            and re.search(r"/(?:search)/?$", p.path, re.I)
            and not CAREER.search(label)
        ):
            continue
        known = identify(target) is not None
        if (
            not known
            and element.name != "iframe"
            and re.search(r"/(?:job|jobs|position)/[^/?]+", p.path, re.I)
            and not SEARCH.search(label)
        ):
            continue
        if known:
            score = max(score, 100)
        if element.name == "form":
            if element.get("method", "get").lower() != "get" or not CAREER.search(label):
                continue
            values = [
                (e["name"], e.get("value", ""))
                for e in element.select("input[name]")
                if e.get("type", "text").lower() in ("text", "search", "hidden")
            ]
            target = p._replace(
                query=urlencode(parse_qsl(p.query, keep_blank_values=True) + values)
            ).geturl()
            score = 125
        elif element.name == "iframe":
            if not known and not CAREER.search(label + " " + p.path):
                continue
            score = max(score, 95)
        elif score == 0 or (
            p.netloc != urlsplit(url).netloc and not known and not CAREER.search(label)
        ):
            continue
        if canonical(target) != canonical(url):
            found[target] = max(found.get(target, 0), score)
    for meta in soup.select("meta[http-equiv]"):
        if meta["http-equiv"].lower() == "refresh":
            match = re.search(r"url\s*=\s*[\'\"]?([^\'\"]+)", meta.get("content", ""), re.I)
            if match:
                target = urljoin(url, match[1].strip())
                if urlsplit(target).scheme in ("http", "https"):
                    found[target] = 130
    return sorted(found.items(), key=lambda pair: -pair[1])[:30]
