from dataclasses import dataclass, field
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
    employer: str = ""  # set by job boards that list many employers
    area: str = "primary"  # "extended": outside the main area; only exact matches are shown
    countries: tuple[str, ...] = ()  # explicit source countries; empty means unknown


@dataclass(frozen=True)
class Company:
    id: str
    name: str
    adapter: str
    url: str
    added: date
    enabled: bool = True
    # State codes, "City, ST", or region names; empty means keep every location.
    locations: tuple[str, ...] = ()
    remote: bool = True  # keep remote jobs even outside `locations`
    about: str = ""  # one line of context for the classifier, e.g. "Medical device maker"
    aliases: tuple[str, ...] = ()  # other names it posts under on job boards, e.g. brands
    options: dict = field(default_factory=dict, hash=False)  # adapter-specific settings

    is_board: bool = False  # lists jobs from multiple employers

    def __post_init__(self) -> None:
        if not isinstance(self.is_board, bool):
            raise ValueError("is_board must be true or false.")
