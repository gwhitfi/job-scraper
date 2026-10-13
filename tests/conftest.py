from datetime import date
from pathlib import Path

import pytest

from jobscraper.models import Company

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def make_company():
    def make(adapter: str, url: str, **kw) -> Company:
        return Company(id="x", name="X", adapter=adapter, url=url, added=date(2026, 1, 1), **kw)

    return make
