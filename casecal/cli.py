"""Command line: ``python -m casecal <sync|check|demo|passphrase>``."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from typing import Sequence

from . import r2
from .config import (
    COSTS_SECRETS,
    DEFAULT_CONFIG,
    ConfigError,
    credentials_for,
    family_groups,
    load_config,
    missing_secrets,
)
from .crypto import PassphraseError, check_passphrase, generate_passphrase
from .errors import SyncAborted
from .familylog import token_problem
from .status import StatusReport, collect_status
from .sync import (
    PreviousPage,
    alert_lake,
    alert_owner,
    collect,
    costs_state,
    family_state,
    photos_state,
    previous_from_site,
    publish,
    refresh_status,
)

EXIT_OK, EXIT_ABORTED, EXIT_CONFIG = 0, 1, 2
# What was done with the owner's alert, for a log that is public: no counts, no dates.
ALERT_WORDS = {
    "nothing": "nessuna novità",
    "open": "segnalazione aperta",
    "new": "nuovi periodi segnalati",
    "remind": "promemoria inviato",
    "close": "segnalazione chiusa",
    "worse": "soglia di allarme segnalata",
}


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
    """Configuration, credentials and the page's passphrase, validated before any network call."""
    config = load_config(config_path)
    missing = missing_secrets(config, os.environ)
    if missing:
        raise ConfigError("secret mancanti: " + ", ".join(missing))
    credentials = {prop.id: credentials_for(prop, os.environ) for prop in config.properties}
    passphrase = os.environ["FAMILY_PASSPHRASE"]
    forbidden = [value for cred in credentials.values() for value in (cred.username, cred.password)]
    check_passphrase(passphrase, forbidden=forbidden)
    return config, credentials, passphrase


def _say_status(report: StatusReport) -> None:
    """The control panel's readings, for a public log: which sources, never what they said."""
    if report.read:
        _say(f"pannello di controllo: letti {', '.join(report.read)}")
    if report.off:
        _say(f"pannello di controllo: non configurati {', '.join(report.off)} (secret assenti)")
    for note in report.warnings:
        _problem("warning", f"pannello: {note}")


def _lake_alert(status, previous) -> None:
    action, problem = alert_lake(
        status,
        previous,
        token=os.environ.get("ALERTS_TOKEN", "").strip() or None,
        repository=os.environ.get("GITHUB_REPOSITORY", "").strip() or None,
        owner=os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip() or None,
        api=os.environ.get("GITHUB_API_URL", "").strip() or "https://api.github.com",
    )
    if problem:
        _problem("warning", f"pannello: {problem}")
    if action and action != "nothing":
        _say(f"avviso del lago: {ALERT_WORDS[action]}")


def cmd_check(args: argparse.Namespace) -> int:
    config, credentials, _ = _load_settings(args.config)
    _say(f"configurazione valida: {len(config.properties)} case, {len(credentials) * 3 + 1} secret presenti")
    token = os.environ.get("FAMILY_LOG_TOKEN", "").strip()
    if not token:
        _say("registro di famiglia: non attivo (FAMILY_LOG_TOKEN assente), la pagina resta in sola lettura")
    elif token_problem(token):
        _problem("warning", f"registro di famiglia non attivo: {token_problem(token)}")
    else:
        _say("registro di famiglia: attivo")
    costs = [name for name in COSTS_SECRETS if os.environ.get(name, "").strip()]
    if not costs:
        _say("spese per il P&L: non configurate (COSTS_R2_URL assente), la pagina resta senza P&L")
    elif len(costs) < len(COSTS_SECRETS):
        missing = ", ".join(name for name in COSTS_SECRETS if name not in costs)
        _problem("warning", f"spese per il P&L: configurazione incompleta, manca {missing}")
    else:
        try:
            # Only the shape of the address can be told without asking Cloudflare.
            r2.canonical(os.environ["COSTS_R2_URL"], config.bucket)
        except r2.StorageError as exc:
            _problem("warning", f"spese per il P&L: {exc}")
        else:
            _say("spese per il P&L: configurate")
    return EXIT_OK


def cmd_sync(args: argparse.Namespace) -> int:
    from .portal import fetch_property  # imported late: `check` and `demo` need no scraper

    config, credentials, passphrase = _load_settings(args.config)
    site_url = os.environ.get("SITE_URL", "").strip() or None
    now = datetime.now(timezone.utc).replace(microsecond=0)

    fetched_page: list[PreviousPage] = []

    def load_previous() -> PreviousPage:
        # Fetched at most once: both a failed portal and a failed log may need it.
        if not fetched_page:
            # OWNER_PASSPHRASE: only for a page published when the figures had their own.
            fetched_page.append(
                previous_from_site(
                    site_url,
                    passphrase=passphrase,
                    legacy_owner_passphrase=os.environ.get("OWNER_PASSPHRASE", "").strip() or None,
                )
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
        family_passphrase=passphrase,
        old_family_passphrase=os.environ.get("OLD_FAMILY_PASSPHRASE", "").strip() or None,
        load_previous=load_previous,
        api=os.environ.get("GITHUB_API_URL", "").strip() or "https://api.github.com",
    )
    snapshot.entries, snapshot.log = family_layer.entries, family_layer.log
    snapshot.groups, group_problems = family_groups(os.environ)
    if family_layer.warnings or group_problems:
        report.warnings["registro"] = family_layer.warnings + group_problems
    if family_layer.log:
        _say("registro di famiglia: letto")
    # The running costs, from the owner's workbook: optional, they turn on the profit and loss.
    costs = costs_state(
        config,
        url=os.environ.get("COSTS_R2_URL", "").strip() or None,
        key_id=os.environ.get("COSTS_R2_ACCESS_KEY_ID", "").strip() or None,
        secret=os.environ.get("COSTS_R2_SECRET_ACCESS_KEY", "").strip() or None,
        load_previous=load_previous,
    )
    snapshot.costs = costs.sheets
    if costs.warnings:
        report.warnings["spese"] = costs.warnings
    if costs.fresh:
        _say("spese: lette")
    # A photo of each house, kept beside that workbook: optional too.
    photos = photos_state(
        config,
        url=os.environ.get("COSTS_R2_URL", "").strip() or None,
        key_id=os.environ.get("COSTS_R2_ACCESS_KEY_ID", "").strip() or None,
        secret=os.environ.get("COSTS_R2_SECRET_ACCESS_KEY", "").strip() or None,
        load_previous=load_previous,
    )
    snapshot.photos = photos.photos
    if photos.warnings:
        report.warnings["foto"] = photos.warnings
    if photos.fresh:
        _say(f"foto: lette ({', '.join(photos.fresh)})")
    if photos.absent:
        # Not a fault, yet worth a line: a photo uploaded under another name is found here.
        _say(f"foto: nessun file per {', '.join(photos.absent)} (atteso <casa>.jpg accanto al foglio delle spese)")
    if photos.stray and len(photos.fresh) < len(config.properties):
        # How many, never which: what a file is called is its owner's business.
        _say(f"foto: nel bucket ci sono {photos.stray} immagini che non portano il nome di una casa")
    # The owner's alert: written by the workflow's own token (ALERTS_TOKEN), so that
    # GitHub notifies the owner; a token of the owner's would write as the owner.
    alert, alert_problem = alert_owner(
        config,
        snapshot,
        report,
        family_layer,
        token=os.environ.get("ALERTS_TOKEN", "").strip() or None,
        repository=os.environ.get("GITHUB_REPOSITORY", "").strip() or None,
        owner=os.environ.get("GITHUB_REPOSITORY_OWNER", "").strip() or None,
        load_previous=load_previous,
        now=now,
        api=os.environ.get("GITHUB_API_URL", "").strip() or "https://api.github.com",
    )
    if alert_problem:
        report.warnings["avvisi"] = [alert_problem]
    if alert:
        _say(f"avviso al proprietario: {ALERT_WORDS[alert]}")
    # The control panel: WiFi, heating and cooling, the lake. Its faults are told, and shown
    # in the panel itself; they never make the night's run a failed one.
    previous_status = load_previous().status if any(p.climate or p.lake for p in config.properties) else None
    snapshot.status, status_report = collect_status(config, os.environ, previous=previous_status, now=now)
    _say_status(status_report)
    _lake_alert(snapshot.status, previous_status)
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
        passphrase=passphrase,
        site_id=config.site_id,
        out_dir=args.out,
    )
    _set_output("degraded", "true" if report.degraded else "false")
    _say(f"pagina scritta in {page}" + (" (parziale)" if report.degraded else ""))
    return EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    """Between one night and the next: the page online again, with its control panel read afresh."""
    # The managers' portals are not visited, so their secrets are not asked for.
    config = load_config(args.config)
    passphrase = os.environ.get("FAMILY_PASSPHRASE", "")
    if not passphrase.strip():
        raise ConfigError("secret mancanti: FAMILY_PASSPHRASE")
    check_passphrase(passphrase)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    page, report, status, previous = refresh_status(
        config,
        os.environ,
        site_url=os.environ.get("SITE_URL", "").strip() or None,
        passphrase=passphrase,
        out_dir=args.out,
        now=now,
    )
    _say_status(report)
    _lake_alert(status, previous)
    _say(f"pagina scritta in {page} (solo il pannello di controllo è cambiato)")
    return EXIT_OK


def cmd_lake(args: argparse.Namespace) -> int:
    """Read the lake and say what was read: public data, and the quickest way to see that the reading works."""
    from .lake import LakeError, fetch_reading

    try:
        reading = fetch_reading(args.name)
    except LakeError as exc:
        _problem("error", f"lago: {exc}")
        return EXIT_ABORTED
    _say(f"{reading.name}: {reading.level_cm:+.1f} cm sullo zero idrometrico, letto alle "
         f"{reading.read_at:%Y-%m-%d %H:%M} UTC, tendenza {reading.trend or 'non indicata'}")
    return EXIT_OK


def cmd_daikin_login(args: argparse.Namespace) -> int:
    """Once, at a keyboard: allow this program to read the Daikin units, and get the first pass."""
    import requests

    from .devices import DeviceError, daikin_authorize_url, daikin_grant

    client_id = os.environ.get("DAIKIN_CLIENT_ID", "").strip()
    client_secret = os.environ.get("DAIKIN_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise ConfigError("servono DAIKIN_CLIENT_ID e DAIKIN_CLIENT_SECRET (dal portale sviluppatori Daikin)")
    print("1. Apri questo indirizzo nel browser, entra con l'account dell'app Onecta e acconsenti:\n")
    print("   " + daikin_authorize_url(client_id, args.redirect_uri) + "\n")
    print("2. Il browser viene mandato a un indirizzo che contiene «code=…».")
    pasted = input("   Incolla qui quell'indirizzo intero (o il solo codice): ").strip()
    code = pasted
    if "code=" in pasted:
        from urllib.parse import parse_qs, urlsplit

        code = (parse_qs(urlsplit(pasted).query).get("code") or [""])[0]
    if not code:
        raise ConfigError("nessun codice nell'indirizzo incollato")
    try:
        granted = daikin_grant(
            requests.Session(), client_id, client_secret,
            grant_type="authorization_code", code=code, redirect_uri=args.redirect_uri,
        )
    except DeviceError as exc:
        _problem("error", str(exc))
        return EXIT_ABORTED
    print("\n3. Salva questo valore nel secret DAIKIN_REFRESH_TOKEN (vale una volta: non usarlo altrove):\n")
    print("   " + str(granted["refresh_token"]))
    return EXIT_OK


def cmd_demo(args: argparse.Namespace) -> int:
    from .demo import DEMO_PASSPHRASE, demo_snapshot

    now = datetime.now(timezone.utc).replace(microsecond=0)
    page = publish(
        demo_snapshot(now, variant=args.variant),
        passphrase=DEMO_PASSPHRASE,
        site_id="casecal-demo",
        out_dir=args.out,
    )
    _say(f"pagina dimostrativa (dati inventati) scritta in {page}")
    _say(f"password: {DEMO_PASSPHRASE}")
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

    status = commands.add_parser("status", help="aggiorna il solo pannello di controllo della pagina già online")
    status.add_argument("--out", default="dist")
    status.add_argument("--config", default=DEFAULT_CONFIG)
    status.set_defaults(handler=cmd_status)

    lake = commands.add_parser("lake", help="legge il livello di un lago da laghi.net e lo stampa")
    lake.add_argument("--name", default="Lago di Como")
    lake.set_defaults(handler=cmd_lake)

    daikin = commands.add_parser("daikin-login", help="prima autorizzazione di Daikin Onecta (una volta, a mano)")
    daikin.add_argument("--redirect-uri", required=True, help="il Redirect URI registrato nel portale Daikin (https://…)")
    daikin.set_defaults(handler=cmd_daikin_login)

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
