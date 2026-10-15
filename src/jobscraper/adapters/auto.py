"""Run the extraction strategy verified when a company was added through the page."""

from dataclasses import replace

import httpx

from jobscraper.models import Company, RawPosting
from jobscraper.scraping import crawl


def fetch(company: Company, client: httpx.Client) -> list[RawPosting]:
    from jobscraper.adapters import get

    strategy = company.options["strategy"]
    if strategy == "auto":
        raise ValueError("An automatic source must have a concrete extraction strategy.")
    configured = replace(company, adapter=strategy, options=company.options.get("settings", {}))
    return get(strategy)(configured, crawl.Client(client, limit=200))
