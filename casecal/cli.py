"""Command line: ``python -m casecal <sync|check|demo|passphrase>``."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Sequence

from .config import DEFAULT_CONFIG, ConfigError, credentials_for, load_config, missing_secrets
from .crypto import PassphraseError, check_passphrases, generate_passphrase
from .errors import SyncAborted
from .familylog import token_problem
from .sync import PreviousPage, collect, family_state, previous_from_site, publish

EXIT_OK, EXIT_ABORTED, EXIT_CONFIG = 0, 1, 2


def _in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def _say(message: str) -> None:
    print(f"[casecal] {message}", flush=True)


def _problem(level: str, message: str) -> None:
    """``level`` is ``warning`` or ``error``; GitHub renders these as annotations."""
    if _in_actions():
        print(f"::{level}::{message}", flush=True)
    else:
        print(f"[casecal] {level.upper()}: {message}", file=sys.stderr, flush=True)


def _set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def _load_settings(config_path: str):
    """Configuration, credentials and passphrases, validated before any network call."""
    config = load_config(config_path)
    missing = missing_secrets(config, os.environ)
    if missing:
        raise ConfigError("secret mancanti: " + ", ".join(missing))
    credentials = {prop.id: credentials_for(prop, os.environ) for prop in config.properties}
    family, owner = os.environ["FAMILY_PASSPHRASE"], os.environ["OWNER_PASSPHRASE"]
    forbidden = [value for cred in credentials.values() for value in (cred.username, cred.password)]
    check_passphrases(family, owner, forbidden=forbidden)
    return config, credentials, family, owner


def cmd_check(args: argparse.Namespace) -> int:
    config, credentials, _, _ = _load_settings(args.config)
    _say(f"configurazione valida: {len(config.properties)} case, {len(credentials) * 3 + 2} secret presenti")
    token = os.environ.get("FAMILY_LOG_TOKEN", "").strip()
    if not token:
        _say("registro di famiglia: non attivo (FAMILY_LOG_TOKEN assente), la pagina resta in sola lettura")
    elif token_problem(token):
        _problem("warning", f"registro di famiglia non attivo: {token_problem(token)}")
    else:
        _say("registro di famiglia: attivo")
    return EXIT_OK


def cmd_sync(args: argparse.Namespace) -> int:
    from .portal import fetch_property  # imported late: `check` and `demo` need no scraper

    config, credentials, family, owner = _load_settings(args.config)
    site_url = os.environ.get("SITE_URL", "").strip() or None
    now = datetime.now(timezone.utc).replace(microsecond=0)

    fetched_page: list[PreviousPage] = []

    def load_previous() -> PreviousPage:
        # Fetched at most once: both a failed portal and a failed log may need it.
        if not fetched_page:
            fetched_page.append(
                previous_from_site(site_url, family_passphrase=family, owner_passphrase=owner)
            )
        return fetched_page[0]

    snapshot, report = collect(
        config, credentials, fetch=fetch_property, load_previous=load_previous, now=now
    )
    # The family's own entries. FAMILY_LOG_TOKEN is optional: without it the page is read-only.
    family_layer = family_state(
        config,
        token=os.environ.get("FAMILY_LOG_TOKEN", "").strip() or None,
        repository=os.environ.get("GITHUB_REPOSITORY", "").strip() or None,
        family_passphrase=family,
        old_family_passphrase=os.environ.get("OLD_FAMILY_PASSPHRASE", "").strip() or None,
        load_previous=load_previous,
        api=os.environ.get("GITHUB_API_URL", "").strip() or "https://api.github.com",
    )
    snapshot.entries, snapshot.log = family_layer.entries, family_layer.log
    if family_layer.warnings:
        report.warnings["registro"] = family_layer.warnings
    if family_layer.log:
        _say("registro di famiglia: letto")
    for prop_id in report.fresh:
        _say(f"{prop_id}: letto")
    for prop_id, notes in report.warnings.items():
        for note in notes:
            _problem("warning", f"{prop_id}: {note}")
    for prop_id, reason in report.errors.items():
        outcome = "riuso i dati già pubblicati" if prop_id in report.reused else "nessun dato precedente"
        _problem("error", f"{prop_id}: lettura fallita ({reason}); {outcome}")

    page = publish(
        snapshot,
        family_passphrase=family,
        owner_passphrase=owner,
        site_id=config.site_id,
        out_dir=args.out,
    )
    _set_output("degraded", "true" if report.degraded else "false")
    _say(f"pagina scritta in {page}" + (" (parziale)" if report.degraded else ""))
    return EXIT_OK


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import DEMO_FAMILY_PASSPHRASE, DEMO_OWNER_PASSPHRASE, demo_snapshot

    now = datetime.now(timezone.utc).replace(microsecond=0)
    page = publish(
        demo_snapshot(now, variant=args.variant),
        family_passphrase=DEMO_FAMILY_PASSPHRASE,
        owner_passphrase=DEMO_OWNER_PASSPHRASE,
        site_id="casecal-demo",
        out_dir=args.out,
    )
    _say(f"pagina dimostrativa (dati inventati) scritta in {page}")
    _say(f"password famiglia:  {DEMO_FAMILY_PASSPHRASE}")
    _say(f"password proprietà: {DEMO_OWNER_PASSPHRASE}")
    return EXIT_OK


def cmd_passphrase(_: argparse.Namespace) -> int:
    print(generate_passphrase())
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="casecal", description="Calendario unico e cifrato delle case."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    sync = commands.add_parser("sync", help="legge i portali e costruisce la pagina cifrata")
    sync.add_argument("--out", default="dist", help="cartella di output (default: dist)")
    sync.add_argument("--config", default=DEFAULT_CONFIG)
    sync.set_defaults(handler=cmd_sync)

    check = commands.add_parser("check", help="verifica configurazione e secret, senza rete")
    check.add_argument("--config", default=DEFAULT_CONFIG)
    check.set_defaults(handler=cmd_check)

    demo = commands.add_parser("demo", help="costruisce una pagina di prova con dati inventati")
    demo.add_argument("--out", default="dist-demo")
    demo.add_argument("--variant", choices=("ok", "lagging", "missing"), default="ok")
    demo.set_defaults(handler=cmd_demo)

    passphrase = commands.add_parser("passphrase", help="genera una passphrase robusta")
    passphrase.set_defaults(handler=cmd_passphrase)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except (ConfigError, PassphraseError) as exc:
        _problem("error", str(exc))
        return EXIT_CONFIG
    except SyncAborted as exc:
        _problem("error", str(exc))
        return EXIT_ABORTED
