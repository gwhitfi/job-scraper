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
    assert cfg.company("scansource").locations == ("upstate_sc",)
    assert "Pendleton, SC" in cfg.regions["upstate_sc"]


def test_duplicate_company_ids_rejected(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[profile]\ntarget_role = "x"\n[notifications]\nemail = "a@b.c"\n'
        '[[companies]]\nid = "a"\nname = "A"\nadapter = "x"\nurl = "u"\nadded = 2026-01-01\n'
        '[[companies]]\nid = "a"\nname = "A"\nadapter = "x"\nurl = "u"\nadded = 2026-01-01\n'
    )
    with pytest.raises(ValueError):
        config.load(p)


def test_unknown_location_rejected(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        '[profile]\ntarget_role = "x"\n[notifications]\nemail = "a@b.c"\n'
        '[[companies]]\nid = "a"\nname = "A"\nadapter = "x"\nurl = "u"\nadded = 2026-01-01\n'
        'locations = ["upstate"]\n'
    )
    with pytest.raises(ValueError, match="upstate"):
        config.load(p)


@pytest.mark.parametrize(
    ("section", "line", "message"),
    [
        ("profile", 'levels = ["Entry"]', "Unknown levels"),
        ("profile", 'mode = "loose"', "Unknown mode"),
        ("web", 'home = "Atlantis, SC"', "Unknown home"),
    ],
)
def test_config_typos_rejected(tmp_path, section, line, message):
    profile = 'target_role = "x"\n' + (line + "\n" if section == "profile" else "")
    web = f"[web]\n{line}\n" if section == "web" else ""
    p = tmp_path / "config.toml"
    p.write_text(f'[profile]\n{profile}[notifications]\nemail = "a@b.c"\n{web}')
    with pytest.raises(ValueError, match=message):
        config.load(p)


def test_health_names_tell_same_named_entries_apart():
    cfg = config.load(ROOT / "config.toml")
    names = cfg.health_names()
    assert names["michelin_greenville"] == "Michelin (michelin_greenville)"
    assert names["arthrex"] == "Arthrex"
