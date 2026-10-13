import httpx

USER_AGENT = "jobscraper/0.1 (personal job alert tool)"


def client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=20.0,
        follow_redirects=True,
    )
