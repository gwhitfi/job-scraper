from datetime import date
from pathlib import Path

import pytest

from jobscraper import config

ROOT = Path(__file__).parent.parent


def test_loads_repo_config():
    cfg = config.load(ROOT / "config.toml")
    assert cfg.profile.levels == ("intern", "entry", "mid")
    arthrex = cfg.company("arthrex")
    assert arthrex.added == date(2026, 10, 12)
    assert arthrex.enabled
    assert cfg.company("scansource").locations == ("SC",)


def test_duplicate_company_ids_rejected(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[profile]\ntarget_role = "x"\n[notifications]\nemail = "a@b.c"\n'
        '[[companies]]\nid = "a"\nname = "A"\nadapter = "x"\nurl = "u"\nadded = 2026-01-01\n'
        '[[companies]]\nid = "a"\nname = "A"\nadapter = "x"\nurl = "u"\nadded = 2026-01-01\n'
    )
    with pytest.raises(ValueError):
        config.load(p)
