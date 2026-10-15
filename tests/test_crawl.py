import socket

import httpx
import pytest
from conftest import mock_client

from jobscraper.scraping import crawl


def test_normalizes_bare_domain():
    assert crawl.normalize_url("careers.example.com") == "https://careers.example.com"


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "http://user:pass@example.com", "http://example.com:8080", ""]
)
def test_refuses_non_public_url_forms(url):
    with pytest.raises(ValueError):
        crawl.normalize_url(url)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "::1"])
def test_rejects_private_destinations(monkeypatch, address):
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: [(None, None, None, None, (address, 80))]
    )
    with pytest.raises(ValueError, match="public"):
        crawl.public_host("https://example.com")


def test_robots_disallow_prevents_page_request():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /jobs")

    c = crawl.Client(mock_client(handler), validate=lambda url: None)
    with pytest.raises(ValueError, match="disallows"):
        c.get("https://example.com/jobs")
    assert seen == ["/robots.txt"]


def test_redirect_target_is_checked():
    seen = []

    def validate(url):
        seen.append(url)
        if "127.0.0.1" in url:
            raise ValueError("private")

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/secret"})

    c = crawl.Client(mock_client(handler), validate=validate)
    with pytest.raises(ValueError, match="private"):
        c.get("https://example.com/jobs")
    assert any("127.0.0.1" in url for url in seen)


def test_request_budget():
    c = crawl.Client(
        mock_client(lambda request: httpx.Response(404)), limit=1, validate=lambda url: None
    )
    with pytest.raises(ValueError, match="crawl limit"):
        c.get("https://example.com/jobs")


def test_compressed_response_is_decoded_once():
    import gzip

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(
            200, headers={"Content-Encoding": "gzip"}, content=gzip.compress(b"jobs")
        )

    c = crawl.Client(mock_client(handler), validate=lambda url: None)
    assert c.get("https://example.com/jobs").text == "jobs"


def test_redirected_robots_still_applies_to_original_host():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "example.com":
            return httpx.Response(
                302, headers={"Location": "https://static.example.com/robots.txt"}
            )
        return httpx.Response(200, text="User-agent: *\nDisallow: /jobs")

    c = crawl.Client(mock_client(handler), validate=lambda url: None)
    with pytest.raises(ValueError, match="Disallow: /jobs"):
        c.get("https://example.com/jobs")
    assert seen == ["https://example.com/robots.txt", "https://static.example.com/robots.txt"]


def test_robots_html_response_is_not_permission():
    c = crawl.Client(
        mock_client(lambda r: httpx.Response(200, text="<html>Challenge</html>")),
        validate=lambda url: None,
    )
    with pytest.raises(ValueError, match="HTML"):
        c.get("https://example.com/jobs")
