"""Restore location metadata from a verified adapter snapshot, without recrawling."""

import argparse
import json
from pathlib import Path

from jobscraper import config
from jobscraper.models import RawPosting
from jobscraper.storage import company_store, state
from jobscraper.workflow import scheduling, tracker


def repair(data_dir: Path, company_id: str, source: Path, *, apply: bool = False) -> int:
    with scheduling.run_lock(data_dir):
        cfg = company_store.merge(
            config.load(data_dir / "config.toml"), data_dir / "companies.json"
        )
        company = next(c for c in cfg.companies if c.id == company_id)
        saved = json.loads(source.read_text())
        if saved.get("company") != company.name:
            raise ValueError("Snapshot company does not match")
        rows = saved["postings"] + saved["excluded"]
        if len(rows) != saved["fetched"]:
            raise ValueError("Incomplete snapshot")
        results = []
        ids = set()
        for row in rows:
            sid, location, countries = row["source_id"], row["location"], row["countries"]
            if not isinstance(sid, str) or not sid or sid in ids:
                raise ValueError("Invalid or duplicate source ID")
            if (
                not isinstance(location, str)
                or not isinstance(countries, list)
                or not all(isinstance(c, str) and c.strip() for c in countries)
            ):
                raise ValueError("Invalid location metadata")
            ids.add(sid)
            results.append(RawPosting(sid, "", location, "", countries=tuple(countries)))
        st = state.load(data_dir / "state")
        changed = tracker.refresh_locations(st, company_id, results)
        if apply and changed:
            backup = scheduling.snapshot(data_dir, "before-location-repair")
            state.save(st, data_dir / "state")
            print(f"Backup: {backup}")
        return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("company")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--data-dir", type=Path, default=state.DEFAULT_DIR.parent)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    count = repair(args.data_dir, args.company, args.snapshot, apply=args.apply)
    print(f"{'Updated' if args.apply else 'Would update'} {count} saved job locations")


if __name__ == "__main__":
    main()
