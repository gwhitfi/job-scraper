from collections.abc import Callable

import httpx

from jobscraper.adapters import michelin, successfactors, ultipro
from jobscraper.models import Company, RawPosting

Adapter = Callable[[Company, httpx.Client], list[RawPosting]]

ADAPTERS: dict[str, Adapter] = {
    "michelin": michelin.fetch,
    "successfactors": successfactors.fetch,
    "ultipro": ultipro.fetch,
}


def get(name: str) -> Adapter:
    try:
        return ADAPTERS[name]
    except KeyError:
        raise KeyError(f"Unknown adapter {name!r}. Known: {sorted(ADAPTERS)}") from None
