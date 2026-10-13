from collections.abc import Callable

import httpx

from jobscraper.adapters import (
    bamboohr,
    bmw,
    healthcaresource,
    html_list,
    michelin,
    moveupstatesc,
    oracle_hcm,
    paylocity,
    phenom,
    prisma,
    successfactors,
    ultipro,
)
from jobscraper.models import Company, RawPosting

Adapter = Callable[[Company, httpx.Client], list[RawPosting]]

ADAPTERS: dict[str, Adapter] = {
    "bamboohr": bamboohr.fetch,
    "bmw": bmw.fetch,
    "healthcaresource": healthcaresource.fetch,
    "html_list": html_list.fetch,
    "michelin": michelin.fetch,
    "moveupstatesc": moveupstatesc.fetch,
    "oracle_hcm": oracle_hcm.fetch,
    "paylocity": paylocity.fetch,
    "phenom": phenom.fetch,
    "prisma": prisma.fetch,
    "successfactors": successfactors.fetch,
    "ultipro": ultipro.fetch,
}


# Adapters for job boards that list many employers, as opposed to one company's site.
BOARD_ADAPTERS = {"moveupstatesc"}


def get(name: str) -> Adapter:
    try:
        return ADAPTERS[name]
    except KeyError:
        raise KeyError(f"Unknown adapter {name!r}. Known: {sorted(ADAPTERS)}") from None
