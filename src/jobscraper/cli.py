import argparse
import contextlib
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anthropic
import httpx
from dotenv import find_dotenv, load_dotenv

from jobscraper import activity, config
from jobscraper.classification import (
    classification_cache,
    classify,
    jev,
    jev_triage,
    shadow,
    usage,
)
from jobscraper.geography import locations
from jobscraper.presentation import web
from jobscraper.scraping import http
from jobscraper.storage import company_store, jsonfile, state
from jobscraper.workflow import notify, runner, scheduling, tracker

# Where the scheduled run publishes state for the web page, and where my marks live.
DATA_DIR = Path.home() / ".local" / "share" / "jobscraper"


def cmd_test(args: argparse.Namespace) -> None:
    cfg = company_store.merge(config.load(args.config), args.data_dir / "companies.json")
    company = cfg.company(args.company_id)
    with http.client() as c:
        postings, dropped = runner.fetch_company(
            company, c, cfg.regions, extended=cfg.extended_locations
        )
    # "check" marks jobs outside `locations` but in one of its states, which usually means
    # a local town is missing from a region list.
    gaps: set[int] = set()
    if company.locations:
        f = locations.build_filter(company.locations, cfg.regions, company.remote)
        outside = [p for p in postings if p.area == "extended"] + dropped
        gaps = {id(p) for p in locations.possible_gaps(outside, f)}
    for p in postings:
        tag = ""
        if p.area == "extended":
            tag = " [extended, check]" if id(p) in gaps else " [extended]"
        print(f"{p.source_id}\t{p.title}\t{p.location}{tag}\t{p.posted_date or '-'}\t{p.url}")
    extended = sum(p.area == "extended" for p in postings)
    print(f"\n{len(postings)} postings from {company.name} ({extended} in the extended area)")
    if dropped:
        print(f"{len(dropped)} dropped by location filter")
        for p in dropped:
            print(f"  {'check:' if id(p) in gaps else '      '} {p.title} ({p.location})")


def _llm_client(enabled: bool) -> anthropic.Anthropic | None:
    if not enabled:
        return None
    try:
        return anthropic.Anthropic(timeout=120.0, max_retries=1)
    except anthropic.AnthropicError as e:
        logging.warning("No Anthropic credentials, using keywords: %s", e)
        return None


def cmd_run(args: argparse.Namespace) -> None:
    lock = contextlib.nullcontext() if args.dry_run else scheduling.run_lock(args.data_dir)
    with lock:
        _traced_run(args)


def _traced_run(args: argparse.Namespace) -> None:
    path = None if args.dry_run else args.state_dir / "crawl-history.json"
    with activity.Trace(path, "crawl") as trace:
        _run(args, trace)


def cmd_scheduled_run(args: argparse.Namespace) -> None:
    missing = [
        key
        for key in ("ANTHROPIC_API_KEY", "SMTP_USER", "SMTP_APP_PASSWORD")
        if not os.environ.get(key)
    ]
    if missing:
        raise ValueError("Local scheduler credentials are missing: " + ", ".join(missing))
    if args.check:
        scheduling.validate(args.config, args.data_dir)
        company_store.merge(config.load(args.config), args.data_dir / "companies.json")
        print("Local scheduler ready: config, existing state, and required credentials present.")
        return

    def crawl(config_path: Path, state_dir: Path) -> None:
        _traced_run(
            argparse.Namespace(
                config=config_path,
                state_dir=state_dir,
                data_dir=args.data_dir,
                dry_run=False,
                no_llm=False,
                no_email=False,
            )
        )

    scheduling.run(args.config, args.data_dir, crawl)


def _run(args: argparse.Namespace, trace: activity.Trace) -> None:
    cfg = company_store.merge(config.load(args.config), args.data_dir / "companies.json")
    st = state.load(args.state_dir)
    decisions = jsonfile.read(args.data_dir / "employers.json")
    checkpoint = (
        None
        if args.dry_run
        else classification_cache.Checkpoint(
            args.state_dir / "classification-cache.json",
            trace.record["id"],
            cfg.classifier_model,
            datetime.fromisoformat(trace.record["started"]),
        )
    )
    with http.client() as c:
        result = runner.run(
            cfg, st, c, _llm_client(not args.no_llm), decisions=decisions, checkpoint=checkpoint
        )

    trace.record["status"] = "partial_failure" if result.failed else "completed"
    activity.emit(
        "crawl_summary",
        successful=len(result.fetched),
        failed=len(result.failed),
        postings=sum(result.fetched.values()),
    )
    names = {c.id: c.name for c in cfg.companies}
    health_names = cfg.health_names()
    for company_id, count in result.fetched.items():
        print(f"{health_names[company_id]}: {count} postings")
    for company_id, error in result.failed.items():
        print(f"{health_names[company_id]}: FAILED ({error})")
    if result.usage.calls:
        cost = result.usage.cost(cfg.classifier_model)
        print(
            f"Claude: {result.usage.calls} call(s), {result.usage.input_tokens:,} in / "
            f"{result.usage.output_tokens:,} out tokens"
            + (f", ${cost:.4f}" if cost is not None else "")
        )
    print()
    print(notify.subject(result.digest))
    print(notify.render_text(result.digest, names, show_reasons=True))

    if args.dry_run:
        print("\n(dry run: state not saved, no email sent)")
        return
    marks = jsonfile.read(args.data_dir / "marks.json")
    archived = tracker.archive_closed(st, set(marks), datetime.now(UTC))
    if archived:
        state.append_archive(archived, args.state_dir)
        print(
            f"Archived {len(archived)} postings closed over {tracker.ARCHIVE_AFTER_DAYS} days ago"
        )
    state.save(st, args.state_dir)
    activity.emit("state_saved", archived=len(archived))
    usage.record(
        args.state_dir / "usage.json",
        result.usage,
        cfg.classifier_model,
        datetime.fromisoformat(trace.record["started"]),
        trace.record["id"],
    )
    if not args.no_email:
        _send_alerts(cfg, result.digest, names, args.state_dir / "email.json")
    checkpoint.finish(st.postings)
    if not args.no_llm:
        shadow.run(result.assessed, cfg, os.environ.get("JEV_API_KEY", ""))
        jev_key = os.environ.get("JEV_API_KEY", "")
        jev_triage.run(st.postings.values(), cfg, args.data_dir / "jev", jev_key)


def _send_alerts(cfg: config.Config, digest: notify.Digest, names: dict, outbox_path: Path) -> None:
    """Queue this run's alert (if any) and send everything waiting in the outbox."""
    outbox = notify.load_outbox(outbox_path)
    if notify.needs_alert(digest):
        notify.queue(outbox, notify.compose(digest, names, cfg.web.url))
    if outbox["pending"]:
        sender = os.environ.get("SMTP_USER", cfg.notifications.email)
        sent = notify.flush(outbox, cfg.notifications.email, sender)
        waiting = len(outbox["pending"])
        print(f"\n{sent} email(s) sent to {cfg.notifications.email}", end="")
        print(f"; {waiting} waiting to retry" if waiting else "")
    jsonfile.write(outbox_path, outbox)
    activity.emit("notifications", pending=len(outbox["pending"]))


def cmd_status(args: argparse.Namespace) -> None:
    cfg = company_store.merge(config.load(args.config), args.data_dir / "companies.json")
    st = state.load(args.state_dir)
    now = datetime.now(UTC)
    names = cfg.health_names()
    width = max(len(n) for n in names.values()) + 2
    print(f"{'Company':<{width}}{'Active':>8}{'Relevant':>10}{'New 3d':>8}{'New 7d':>8}  Health")
    for c in cfg.companies:
        active = [p for p in st.postings.values() if p.company_id == c.id and p.status == "active"]
        relevant = [p for p in active if classify.posting_group(p, cfg.profile) is not None]
        new3 = sum(p.first_seen >= now - timedelta(days=3) for p in relevant)
        new7 = sum(p.first_seen >= now - timedelta(days=7) for p in relevant)
        h = st.health.get(c.id)
        if h is None:
            health = "never checked"
        elif h.consecutive_failures:
            health = f"{h.consecutive_failures} failures ({h.last_error})"
        else:
            health = f"ok {h.last_success:%Y-%m-%d %H:%M} UTC"
        if not c.enabled:
            health += " [disabled]"
        print(
            f"{names[c.id]:<{width}}{len(active):>8}{len(relevant):>10}{new3:>8}{new7:>8}  {health}"
        )


def cmd_list(args: argparse.Namespace) -> None:
    cfg = company_store.merge(config.load(args.config), args.data_dir / "companies.json")
    st = state.load(args.state_dir)
    postings = sorted(
        (
            p
            for p in st.postings.values()
            if p.status == "active"
            and (args.company is None or p.company_id == args.company)
            and (args.all or classify.posting_group(p, cfg.profile) is not None)
        ),
        key=lambda p: (p.company_id, p.title),
    )
    for p in postings:
        c = p.classification or {}
        print(f"{p.company_id:<12}{c.get('relevance', '-'):<10}{c.get('level', '-'):<9}{p.title}")
        print(f"{'':<31}{c.get('reason', '-')}")
        print(f"{'':<31}{p.url}")
    print(f"\n{len(postings)} postings")


def cmd_serve(args: argparse.Namespace) -> None:
    # Prefer the config the last scheduled run used (published next to its state), so the page
    # groups jobs the same way the alerts did. Falls back to the repo's config.toml.
    published = args.data_dir / "config.toml"
    config_path = args.config if args.config != config.DEFAULT_PATH else None
    config_path = config_path or (published if published.exists() else config.DEFAULT_PATH)
    cfg = config.load(config_path)
    host = args.host or cfg.web.host
    port = args.port or cfg.web.port
    app = web.App(
        cfg,
        args.data_dir / "state",
        args.data_dir / "marks.json",
        args.data_dir / "employers.json",
        config_path,
    )
    server = web.make_server(app, host, port)
    logging.info("Serving on http://%s:%d using %s", host, port, config_path)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


def cmd_compare_jev(args: argparse.Namespace) -> None:
    if args.limit < 0:
        raise SystemExit("--limit must be non-negative (0 means all)")
    published = args.data_dir / "config.toml"
    path = args.config
    if path == config.DEFAULT_PATH and published.exists():
        path = published
    cfg = company_store.merge(config.load(path), args.data_dir / "companies.json")
    postings = jev.select_postings(
        state.load(args.state_dir).postings, args.limit, args.include_removed
    )
    if not postings:
        raise SystemExit("No eligible saved AI evaluations to compare")
    directory = args.data_dir / "comparisons" / "jev"
    print(f"Selected {len(postings)} saved evaluations; reports: {directory}", flush=True)
    if args.dry_run:
        print("Dry run: no API calls or file writes")
        return
    key = os.environ.get("JEV_API_KEY", "").strip()
    if not key:
        raise SystemExit("Set JEV_API_KEY in .env or the environment")
    try:
        with httpx.Client(headers={"Authorization": f"Bearer {key}"}, timeout=30) as client:
            report = jev.compare(postings, cfg, directory, client, args.model)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    print(f"Agreed: {report['agreed']}/{report['compared']}; new calls: {report['new_calls']}")
    print(f"Potential missed matches to review: {report['potential_missed_matches']}")
    print(f"Report: {directory / 'report.md'}")


def cmd_jev_triage(args: argparse.Namespace) -> None:
    if args.limit < 0:
        raise SystemExit("--limit must be non-negative (0 means all)")
    published = args.data_dir / "config.toml"
    path = args.config
    if path == config.DEFAULT_PATH and published.exists():
        path = published
    cfg = company_store.merge(config.load(path), args.data_dir / "companies.json")
    postings = state.load(args.state_dir).postings
    store = jev_triage.store_path(args.data_dir / "jev", args.version)
    waiting = jev_triage.pending(postings.values(), cfg, jsonfile.read(store), args.version)
    todo = min(len(waiting), args.limit) if args.limit else len(waiting)
    print(
        f"Questions {args.version}: {len(waiting)} jobs awaiting Jev; this run asks about "
        f"{todo}. Store: {store}"
    )
    if args.dry_run:
        print("Dry run: no API calls or file writes")
        return
    key = os.environ.get("JEV_API_KEY", "").strip()
    if not key:
        raise SystemExit("Set JEV_API_KEY in .env or the environment")

    def progress(done: int, total: int) -> None:
        if done % 50 == 0 or done == total:
            print(f"  {done}/{total}", flush=True)

    try:
        with httpx.Client(headers={"Authorization": f"Bearer {key}"}, timeout=30) as client:
            done = jev_triage.collect(
                postings.values(), cfg, store, client, args.limit, progress, args.version
            )
    except RuntimeError as exc:
        raise SystemExit(f"{exc}. Finished answers are saved; rerun to continue.") from None
    print(f"Saved {done} Jev answers. Compare on the page at /jev?jev={args.version}.")


def main() -> None:
    # Real environment variables (e.g. Actions secrets) take precedence over .env.
    load_dotenv(find_dotenv(usecwd=True))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="jobscraper")
    parser.add_argument("--config", type=Path, default=config.DEFAULT_PATH)
    parser.add_argument("--state-dir", type=Path, default=None)
    sub = parser.add_subparsers(required=True)

    run = sub.add_parser("run", help="Fetch all companies, diff, classify, notify")
    run.add_argument("--dry-run", action="store_true", help="Don't save state or send email")
    run.add_argument("--no-email", action="store_true", help="Save state but don't send email")
    run.add_argument("--no-llm", action="store_true", help="Classify with keywords only")
    run.add_argument(
        "--data-dir", type=Path, default=DATA_DIR, help="Where marks.json and employers.json live"
    )
    run.set_defaults(func=cmd_run)

    scheduled = sub.add_parser("scheduled-run", help="Run locally with locking and backups")
    scheduled.add_argument("--data-dir", type=Path, default=DATA_DIR)
    scheduled.add_argument("--check", action="store_true", help="Validate setup without crawling")
    scheduled.set_defaults(func=cmd_scheduled_run)

    status = sub.add_parser("status", help="Per-company counts and health")
    status.add_argument("--data-dir", type=Path, default=DATA_DIR)
    status.set_defaults(func=cmd_status)

    lst = sub.add_parser("list", help="Show active relevant postings")
    lst.add_argument("--company")
    lst.add_argument("--all", action="store_true", help="Include postings that aren't relevant")
    lst.add_argument("--data-dir", type=Path, default=DATA_DIR)
    lst.set_defaults(func=cmd_list)

    serve = sub.add_parser("serve", help="Serve the web page")
    serve.add_argument("--data-dir", type=Path, default=DATA_DIR)
    serve.add_argument("--host", help="Default from [web] in config")
    serve.add_argument("--port", type=int, help="Default from [web] in config")
    serve.set_defaults(func=cmd_serve)

    test = sub.add_parser("test", help="Run one adapter and print raw postings")
    test.add_argument("company_id")
    test.add_argument("--data-dir", type=Path, default=DATA_DIR)
    test.set_defaults(func=cmd_test)

    compare = sub.add_parser("compare-jev", help="Compare Jev with saved AI relevance evaluations")
    compare.add_argument("--data-dir", type=Path, default=DATA_DIR)
    compare.add_argument("--limit", type=int, default=20, help="Balanced sample size; 0 for all")
    compare.add_argument("--include-removed", action="store_true")
    compare.add_argument("--model", default=jev.MODEL)
    compare.add_argument(
        "--dry-run", action="store_true", help="Select only; no API calls or writes"
    )
    compare.set_defaults(func=cmd_compare_jev)

    triage = sub.add_parser(
        "jev-triage", help="Ask Jev each job's type and level for the /jev page"
    )
    triage.add_argument("--data-dir", type=Path, default=DATA_DIR)
    triage.add_argument("--limit", type=int, default=0, help="Most jobs to ask about; 0 for all")
    triage.add_argument("--dry-run", action="store_true", help="Count only; no API calls or writes")
    triage.add_argument("--version", choices=list(jev_triage.VERSIONS), default=jev_triage.LATEST)
    triage.set_defaults(func=cmd_jev_triage)

    args = parser.parse_args()
    if args.state_dir is None:
        args.state_dir = args.data_dir / "state"
    args.func(args)
