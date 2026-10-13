import re

from jobscraper.employers import normalize_name
from jobscraper.models import Company
from jobscraper.state import Posting, State

MIN_PREFIX_WORDS = 3  # boards often cut titles short; a shorter overlap is too ambiguous
# "(Anderson, SC)" style location notes some sites add to titles
_PAREN_PLACE = re.compile(r"\([^()]*,\s*[A-Z]{2}\)")


def _title_words(title: str, brands: set[str]) -> list[str]:
    """Comparable words of a title, minus a leading "Brand: " prefix and location notes."""
    if ":" in title:
        prefix, rest = title.split(":", 1)
        if normalize_name(prefix) in brands:
            title = rest
    title = _PAREN_PLACE.sub(" ", title)
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).split()


def same_job(board_title: str, company_title: str, brands: set[str]) -> bool:
    a, b = _title_words(board_title, brands), _title_words(company_title, brands)
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    # Word-level prefix only: "UI Developer II" must not match "UI Developer III".
    return len(shorter) >= MIN_PREFIX_WORDS and longer[: len(shorter)] == shorter


def mark_duplicates(state: State, companies: list[Company], boards: set[str]) -> None:
    """Point each active job-board posting at the matching company-site posting, if any.

    Recomputed every run, so a board listing reappears if the company's own listing closes.
    """
    brands_by_company = {
        c.id: {normalize_name(n) for n in (c.name, *c.aliases)} - {""}
        for c in companies
        if c.adapter not in boards
    }
    company_for_brand = {b: cid for cid, brands in brands_by_company.items() for b in brands}
    active = [p for p in state.postings.values() if p.status == "active"]
    by_company: dict[str, list[Posting]] = {}
    for p in active:
        by_company.setdefault(p.company_id, []).append(p)

    board_ids = {c.id for c in companies if c.adapter in boards}
    for p in active:
        if p.company_id not in board_ids:
            continue
        cid = company_for_brand.get(normalize_name(p.employer))
        brands = brands_by_company.get(cid, set())
        match = next(
            (o for o in by_company.get(cid, []) if same_job(p.title, o.title, brands)), None
        )
        p.duplicate_of = match.key if match else None
