import argparse
import contextlib
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anthropic
from dotenv import find_dotenv, load_dotenv

from jobscraper import (
    classify,
    config,
    employers,
    http,
    locations,
    notify,
    runner,
    state,
    usage,
    web,
)

# Where the scheduled run publishes state for the web page, and where my marks live.
DATA_DIR = Path.home() / ".local" / "share" / "jobscraper"


def cmd_test(args: argparse.Namespace) -> None:
    cfg = config.load(args.config)
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
        return anthropic.Anthropic()
    except anthropic.AnthropicError as e:
        logging.warning("No Anthropic credentials, using keywords: %s", e)
        return None


def cmd_run(args: argparse.Namespace) -> None:
    cfg = config.load(args.config)
    st = state.load(args.state_dir)
    decisions = employers.load(args.data_dir / "employers.json")
    with http.client() as c:
        result = runner.run(cfg, st, c, _llm_client(not args.no_llm), decisions=decisions)

    names = {c.id: c.name for c in cfg.companies}
    for company_id, count in result.fetched.items():
        print(f"{names[company_id]}: {count} postings")
    for company_id, error in result.failed.items():
        print(f"{names[company_id]}: FAILED ({error})")
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
    state.save(st, args.state_dir)
    usage.record(
        args.state_dir / "usage.json", result.usage, cfg.classifier_model, datetime.now(UTC)
    )
    if args.no_email:
        return
    outbox_path = args.state_dir / "email.json"
    outbox = notify.load_outbox(outbox_path)
    if notify.needs_alert(result.digest):
        notify.queue(outbox, notify.compose(result.digest, names, cfg.web.url))
    if outbox["pending"]:
        sender = os.environ.get("SMTP_USER", cfg.notifications.email)
        sent = notify.flush(outbox, cfg.notifications.email, sender)
        print(f"\n{sent} email(s) sent to {cfg.notifications.email}", end="")
        print(f"; {len(outbox['pending'])} waiting to retry" if outbox["pending"] else "")
    notify.save_outbox(outbox_path, outbox)


def cmd_status(args: argparse.Namespace) -> None:
    cfg = config.load(args.config)
    st = state.load(args.state_dir)
    now = datetime.now(UTC)
    print(f"{'Company':<14}{'Active':>8}{'Relevant':>10}{'New 3d':>8}{'New 7d':>8}  Health")
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
        print(f"{c.name:<14}{len(active):>8}{len(relevant):>10}{new3:>8}{new7:>8}  {health}")


def cmd_list(args: argparse.Namespace) -> None:
    cfg = config.load(args.config)
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
    cfg = config.load(args.config)
    host = args.host or cfg.web.host
    port = args.port or cfg.web.port
    app = web.App(
        cfg,
        args.data_dir / "state",
        args.data_dir / "marks.json",
        args.data_dir / "employers.json",
    )
    server = web.make_server(app, host, port)
    logging.info("Serving on http://%s:%d (data in %s)", host, port, args.data_dir)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


def main() -> None:
    # Real environment variables (e.g. Actions secrets) take precedence over .env.
    load_dotenv(find_dotenv(usecwd=True))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="jobscraper")
    parser.add_argument("--config", type=Path, default=config.DEFAULT_PATH)
    parser.add_argument("--state-dir", type=Path, default=state.DEFAULT_DIR)
    sub = parser.add_subparsers(required=True)

    run = sub.add_parser("run", help="Fetch all companies, diff, classify, notify")
    run.add_argument("--dry-run", action="store_true", help="Don't save state or send email")
    run.add_argument("--no-email", action="store_true", help="Save state but don't send email")
    run.add_argument("--no-llm", action="store_true", help="Classify with keywords only")
    run.add_argument("--data-dir", type=Path, default=DATA_DIR, help="Where employers.json lives")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="Per-company counts and health")
    status.set_defaults(func=cmd_status)

    lst = sub.add_parser("list", help="Show active relevant postings")
    lst.add_argument("--company")
    lst.add_argument("--all", action="store_true", help="Include postings that aren't relevant")
    lst.set_defaults(func=cmd_list)

    serve = sub.add_parser("serve", help="Serve the web page")
    serve.add_argument("--data-dir", type=Path, default=DATA_DIR)
    serve.add_argument("--host", help="Default from [web] in config")
    serve.add_argument("--port", type=int, help="Default from [web] in config")
    serve.set_defaults(func=cmd_serve)

    test = sub.add_parser("test", help="Run one adapter and print raw postings")
    test.add_argument("company_id")
    test.set_defaults(func=cmd_test)

    args = parser.parse_args()
    args.func(args)
