from collections.abc import Callable

import httpx

from jobscraper.adapters import (
    auto,
    bamboohr,
    bmw,
    generic,
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
    workday,
    wp_job_manager,
)
from jobscraper.models import Company, RawPosting

Adapter = Callable[[Company, httpx.Client], list[RawPosting]]

ADAPTERS: dict[str, Adapter] = {
    "auto": auto.fetch,
    "generic": generic.fetch,
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
    "workday": workday.fetch,
    "wp_job_manager": wp_job_manager.fetch,
}


# Adapters for job boards that list many employers, as opposed to one company's site.
BOARD_ADAPTERS = {"moveupstatesc"}


def get(name: str) -> Adapter:
    try:
        return ADAPTERS[name]
    except KeyError:
        raise KeyError(f"Unknown adapter {name!r}. Known: {sorted(ADAPTERS)}") from None


def platform(company: Company) -> str:
    return company.options.get("strategy", "") if company.adapter == "auto" else company.adapter
