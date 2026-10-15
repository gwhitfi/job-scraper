from urllib.parse import parse_qsl, urlencode, urlparse

import httpx

from jobscraper import activity

USER_AGENT = "jobscraper/0.1 (personal job alert tool)"


def client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=20.0,
        follow_redirects=True,
        event_hooks={"request": [activity.request_hook], "response": [activity.response_hook]},
    )


def with_page(url: str, page: int) -> str:
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "page"]
    query.append(("page", str(page)))
    return parts._replace(query=urlencode(query)).geturl()
