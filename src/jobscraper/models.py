from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class RawPosting:
    """A posting as returned by an adapter, before state tracking."""

    source_id: str
    title: str
    location: str
    url: str
    posted_date: date | None = None
    category: str = ""  # department/sector as the site labels it
    summary: str = ""  # short description, when the listing has one


@dataclass(frozen=True)
class Company:
    id: str
    name: str
    adapter: str
    url: str
    added: date
    enabled: bool = True
    locations: tuple[str, ...] = ()
    about: str = ""  # one line of context for the classifier, e.g. "Medical device maker"
