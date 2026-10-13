from datetime import UTC, date, datetime

import pytest

from jobscraper import dedupe
from jobscraper.models import Company
from jobscraper.state import Posting, State

NOW = datetime(2026, 10, 13, tzinfo=UTC)
BRANDS = {"scansource", "intelisys"}
COMPANIES = [
    Company(
        id="scansource",
        name="ScanSource",
        adapter="ultipro",
        url="u",
        added=date(2026, 1, 1),
        aliases=("Intelisys",),
    ),
    Company(id="michelin", name="Michelin", adapter="michelin", url="u", added=date(2026, 1, 1)),
    Company(id="mus", name="Move Upstate SC", adapter="board", url="u", added=date(2026, 1, 1)),
]


# Pairs taken from real listings on Move Upstate SC and the companies' own sites.
@pytest.mark.parametrize(
    ("board", "company"),
    [
        ("IT Site Support Analyst", "IT Site Support Analyst"),
        ("Tech Support Analyst – Cisco", "Tech Support Analyst - Cisco"),
        (
            "Manager, Technical Solutions – Converged",
            "Manager, Technical Solutions - Converged Communications",
        ),
        ("UI Developer III", "Intelisys: UI Developer III"),
        (
            "Summer 2027 Internship: Engineering and/or Sciences , IT/IS (Anderson, SC)",
            "Summer 2027 Internship: Engineering and/or Sciences, IT/IS",
        ),
    ],
)
def test_same_job(board, company):
    assert dedupe.same_job(board, company, BRANDS)


@pytest.mark.parametrize(
    ("board", "company"),
    [
        ("UI Developer II", "UI Developer III"),
        ("UI Developer", "UI Developer III"),  # too short to trust a prefix
        ("Site IT Engineer", "IT Site Support Analyst"),
        ("Summer 2027 Internship: IT/ IS Greenville, SC", "Summer 2027 Internship: Quality"),
    ],
)
def test_different_jobs(board, company):
    assert not dedupe.same_job(board, company, BRANDS)


def posting(key, title, employer="", status="active") -> Posting:
    cid, sid = key.split(":")
    return Posting(
        key=key,
        company_id=cid,
        source_id=sid,
        title=title,
        location="Greenville, SC",
        url="u",
        posted_date=None,
        first_seen=NOW,
        last_seen=NOW,
        status=status,
        employer=employer,
    )


def run(*postings) -> State:
    s = State(postings={p.key: p for p in postings})
    dedupe.mark_duplicates(s, COMPANIES, {"board"})
    return s


def test_marks_board_copy_of_company_listing():
    s = run(
        posting("scansource:1", "Intelisys: UI Developer III"),
        posting("mus:a", "UI Developer III", employer="Intelisys"),
        posting("mus:b", "UI Developer III", employer="Some Other Co"),
        posting("mus:c", "Data Engineer", employer="ScanSource"),
    )
    assert s.postings["mus:a"].duplicate_of == "scansource:1"
    assert s.postings["mus:b"].duplicate_of is None  # different employer
    assert s.postings["mus:c"].duplicate_of is None  # no matching title


def test_board_copy_returns_when_company_listing_closes():
    company = posting("michelin:1", "IT Site Support Analyst")
    board = posting("mus:a", "IT Site Support Analyst", employer="Michelin")
    s = run(company, board)
    assert board.duplicate_of == "michelin:1"
    company.status = "removed"
    dedupe.mark_duplicates(s, COMPANIES, {"board"})
    assert board.duplicate_of is None


def test_matches_any_entry_sharing_the_company_name():
    """Michelin has one entry per city; a board listing can match any of them."""
    per_city = [
        Company(
            id="michelin", name="Michelin", adapter="michelin", url="u", added=date(2026, 1, 1)
        ),
        Company(
            id="michelin_greenville",
            name="Michelin",
            adapter="michelin",
            url="u",
            added=date(2026, 1, 1),
        ),
        COMPANIES[2],
    ]
    s = State(
        postings={
            p.key: p
            for p in [
                posting("michelin:1", "IT Site Support Analyst"),
                posting("michelin_greenville:2", "Site IT Engineer"),
                posting("mus:a", "Site IT Engineer", employer="Michelin"),
                posting("mus:b", "IT Site Support Analyst", employer="Michelin"),
            ]
        }
    )
    dedupe.mark_duplicates(s, per_city, {"board"})
    assert s.postings["mus:a"].duplicate_of == "michelin_greenville:2"
    assert s.postings["mus:b"].duplicate_of == "michelin:1"
