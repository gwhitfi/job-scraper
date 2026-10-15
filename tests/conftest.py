from datetime import date
from pathlib import Path

import httpx
import pytest

from jobscraper.classification import classify
from jobscraper.models import Company

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def make_company():
    def make(adapter: str, url: str, **kw) -> Company:
        return Company(id="x", name="X", adapter=adapter, url=url, added=date(2026, 1, 1), **kw)

    return make


def llm_result(id: str, **kw) -> classify.Result:
    """A classifier result with neutral defaults for fields a test doesn't care about."""
    fields = dict(
        reason="r",
        score=5,
        relevance="none",
        level="unknown",
        employer_note="",
        employer_check="ok",
    )
    return classify.Result(id=id, **(fields | kw))


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))
