"""Bounded public-site requests for URL-submitted companies."""

import ipaddress
import socket
import time
from collections.abc import Callable
from urllib.parse import urljoin, urlsplit

import httpx

from jobscraper import activity
from jobscraper.scraping import robots
from jobscraper.scraping.http import USER_AGENT

MAX_BYTES = 5_000_000


class Blocked(ValueError):
    """Access policy or a challenge prevented reading the source."""


class LimitExceeded(ValueError):
    """The bounded crawl cannot be completed with this source scope."""


def normalize_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Enter a public careers-page URL.")
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Use an http or https careers-page URL.")
    if parts.username or parts.password or parts.port not in (None, 80, 443):
        raise ValueError("Use a public URL without credentials or a custom port.")
    return parts._replace(fragment="").geturl()


def public_host(url: str) -> None:
    host = urlsplit(normalize_url(url)).hostname
    try:
        addresses = {r[4][0] for r in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except OSError as e:
        raise ValueError(f"Could not resolve {host}. Check the address and try again.") from e
    if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
        raise ValueError(
            "Only public career sites can be tracked; local network addresses are refused."
        )


class Client:
    """Apply robots rules, request limits, and redirect checks to an existing HTTP client."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        limit: int = 60,
        validate: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.client = client
        self.limit = limit
        self.validate = validate or public_host
        self.sleep = sleep
        self.requests = 0
        self.robots: dict[str, robots.Rules] = {}
        self.last_request: dict[str, float] = {}

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        activity.emit("public_url_check", url=url)
        self.validate(url)
        if self.requests >= self.limit:
            raise LimitExceeded(
                "This search exceeds the crawl limit. Use a more narrowly filtered jobs URL."
            )
        self.requests += 1
        # Stream the response so a submitted URL cannot make us download an unbounded file.
        with self.client.stream(method, url, follow_redirects=False, **kwargs) as response:
            chunks = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("This page is too large to inspect. Use its jobs search URL.")
                chunks.append(chunk)
            headers = dict(response.headers)
            # iter_bytes has already decoded compression and transfer framing.
            for name in ("content-encoding", "content-length", "transfer-encoding"):
                headers.pop(name, None)
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=b"".join(chunks),
                request=response.request,
            )

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        url = normalize_url(url)
        for _ in range(6):
            parts = urlsplit(url)
            origin = f"{parts.scheme}://{parts.netloc}"
            if origin not in self.robots:
                robots_url = origin + "/robots.txt"
                for _hop in range(6):
                    response = self._request("GET", robots_url)
                    if not response.is_redirect:
                        break
                    robots_url = normalize_url(urljoin(robots_url, response.headers["location"]))
                    activity.emit("robots_redirect", url=robots_url)
                else:
                    raise ValueError("Too many redirects while checking robots.txt.")
                if response.status_code in (404, 410):
                    rules = robots.Rules("", USER_AGENT)
                elif response.status_code == 200:
                    if response.text.lstrip().lower().startswith(("<!doctype html", "<html")):
                        raise ValueError(
                            "robots.txt returned an HTML page; crawling cannot be verified."
                        )
                    rules = robots.Rules(response.text, USER_AGENT)
                else:
                    raise ValueError(
                        f"Could not verify robots.txt (HTTP {response.status_code}). "
                        "Try its direct jobs-board URL."
                    )
                self.robots[origin] = rules
                activity.emit(
                    "robots_loaded",
                    url=robots_url,
                    status_code=response.status_code,
                    crawl_delay=rules.delay,
                )
            rules = self.robots[origin]
            target = str(httpx.URL(url, params=kwargs.get("params"))) if "params" in kwargs else url
            allowed, rule = rules.check(target)
            activity.emit("robots_check", url=target, allowed=allowed, rule=rule)
            if not allowed:
                raise Blocked(
                    f"This site disallows crawling this page ({rule}). "
                    "Use an allowed public jobs feed."
                )
            delay = rules.delay
            elapsed = time.monotonic() - self.last_request.get(origin, 0)
            if elapsed < delay:
                activity.emit("crawl_delay", seconds=round(delay - elapsed, 2))
                self.sleep(delay - elapsed)
            response = self._request(method, url, **kwargs)
            self.last_request[origin] = time.monotonic()
            if not response.is_redirect:
                response.raise_for_status()
                return response
            url = normalize_url(urljoin(str(response.url), response.headers["location"]))
            activity.emit("redirect", url=url, status_code=response.status_code)
            kwargs.pop("params", None)
            if response.status_code == 303 or (
                method == "POST" and response.status_code in (301, 302)
            ):
                method = "GET"
                kwargs.pop("json", None)
                kwargs.pop("data", None)
        raise ValueError("Too many redirects while opening this careers page.")

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.request("POST", url, **kwargs)


class CachedClient:
    """Reuse already-read GET pages during one discovery attempt only."""

    def __init__(self, client: Client):
        self.client = client
        self.pages: dict[str, httpx.Response] = {}

    def get(self, url: str, **kwargs) -> httpx.Response:
        import json

        key = url + json.dumps(kwargs, sort_keys=True)
        if key not in self.pages:
            response = self.client.get(url, **kwargs)
            self.pages[key] = response
            if not kwargs:
                self.pages[str(response.url) + "{}"] = response
        return self.pages[key]

    def post(self, url: str, **kwargs) -> httpx.Response:
        return self.client.post(url, **kwargs)
