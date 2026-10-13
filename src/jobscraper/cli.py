import argparse
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import anthropic
from dotenv import find_dotenv, load_dotenv

from jobscraper import adapters, classify, config, http, notify, runner, state


def cmd_test(args: argparse.Namespace) -> None:
    cfg = config.load(args.config)
    company = cfg.company(args.company_id)
    adapter = adapters.get(company.adapter)
    with http.client() as c:
        postings = adapter(company, c)
    for p in postings:
        print(f"{p.source_id}\t{p.title}\t{p.location}\t{p.posted_date or '-'}\t{p.url}")
    print(f"\n{len(postings)} postings from {company.name}")


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
    with http.client() as c:
        result = runner.run(cfg, st, c, _llm_client(not args.no_llm))

    names = {c.id: c.name for c in cfg.companies}
    for company_id, count in result.fetched.items():
        print(f"{names[company_id]}: {count} postings")
    for company_id, error in result.failed.items():
        print(f"{names[company_id]}: FAILED ({error})")
    print()
    print(notify.subject(result.digest))
    print(notify.render_text(result.digest, names, show_reasons=True))

    if args.dry_run:
        print("\n(dry run: state not saved, no email sent)")
        return
    state.save(st, args.state_dir)
    if result.digest.is_empty() or args.no_email:
        return
    sender = os.environ.get("SMTP_USER", cfg.notifications.email)
    notify.send(notify.build_message(result.digest, names, cfg.notifications.email, sender))
    print(f"\nEmail sent to {cfg.notifications.email}")


def cmd_status(args: argparse.Namespace) -> None:
    cfg = config.load(args.config)
    st = state.load(args.state_dir)
    now = datetime.now(UTC)
    print(f"{'Company':<14}{'Active':>8}{'Relevant':>10}{'New 3d':>8}{'New 7d':>8}  Health")
    for c in cfg.companies:
        active = [p for p in st.postings.values() if p.company_id == c.id and p.status == "active"]
        relevant = [p for p in active if classify.is_relevant(p.classification, cfg.profile)]
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
            and (args.all or classify.is_relevant(p.classification, cfg.profile))
        ),
        key=lambda p: (p.company_id, p.title),
    )
    for p in postings:
        c = p.classification or {}
        print(f"{p.company_id:<12}{c.get('relevance', '-'):<10}{c.get('level', '-'):<9}{p.title}")
        print(f"{'':<31}{c.get('reason', '-')}")
        print(f"{'':<31}{p.url}")
    print(f"\n{len(postings)} postings")


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
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="Per-company counts and health")
    status.set_defaults(func=cmd_status)

    lst = sub.add_parser("list", help="Show active relevant postings")
    lst.add_argument("--company")
    lst.add_argument("--all", action="store_true", help="Include postings that aren't relevant")
    lst.set_defaults(func=cmd_list)

    test = sub.add_parser("test", help="Run one adapter and print raw postings")
    test.add_argument("company_id")
    test.set_defaults(func=cmd_test)

    args = parser.parse_args()
    args.func(args)
