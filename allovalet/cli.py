"""Ligne de commande AlloValet perso.

    python -m allovalet run            # un passage (ce que fait GitHub Actions)
    python -m allovalet status         # tickets en cours + état des règles
    python -m allovalet doctor         # diagnostic complet, à faire en premier
    python -m allovalet rates --zone 75016
    python -m allovalet plan  --zone 75016 --until 19:00
    python -m allovalet park  --zone 75016 --duration 2h
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .config import Config
from .errors import AlloValetError
from .models import utcnow
from .notify import Notifier
from .paybyphone import best_duration
from .providers import build_client
from .runner import Runner
from .schedule import parse_duration, parse_time
from .smartpark import build_curve, candidate_durations, cheapest_plan
from .state import State

log = logging.getLogger("allovalet")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _context(args):
    cfg = Config.load(args.config)
    state = State()
    client = build_client(cfg, state)
    return cfg, state, client


# ---------------------------------------------------------------- commandes


def cmd_run(args) -> int:
    cfg, state, client = _context(args)
    runner = Runner(cfg, client, state, Notifier(cfg.notify), dry_run=args.dry_run)

    if args.loop:
        print(f"Boucle locale : un passage toutes les {args.loop} min. Ctrl-C pour arrêter.")
        while True:
            _print_report(runner.tick())
            time.sleep(args.loop * 60)

    report = runner.tick()
    _print_report(report)
    return 1 if report.failures else 0


def _print_report(report) -> None:
    print()
    print(report.text() or "aucune règle active")
    print()


def cmd_status(args) -> int:
    cfg, state, client = _context(args)
    tz = ZoneInfo(cfg.timezone)
    sessions = client.current_sessions()

    print(f"\nTickets en cours ({len(sessions)}) :")
    if not sessions:
        print("  — aucun")
    for sess in sessions:
        expiry = sess.expiry.astimezone(tz).strftime("%a %d/%m %H:%M") if sess.expiry else "?"
        remaining = int(sess.remaining.total_seconds() // 60)
        print(
            f"  • {sess.plate} · zone {sess.location_id} · {sess.rate_type or '?'} "
            f"· jusqu'à {expiry} (reste {remaining // 60}h{remaining % 60:02d})"
        )

    print("\nRègles :")
    now_local = datetime.now(tz)
    for rule in cfg.rules:
        active = client.find_active(rule.plate, rule.location, sessions)
        state_txt = "hors créneau"
        if rule.window.contains(now_local):
            margin = timedelta(minutes=cfg.margin_for(rule))
            state_txt = (
                "couvert" if active and active.covers(utcnow(), margin) else "À PRENDRE"
            )
        flag = "" if rule.enabled else " (désactivée)"
        print(
            f"  • {rule.name}{flag} — {rule.plate} zone {rule.location} "
            f"[{rule.mode}] {rule.window.describe()} → {state_txt}"
        )
    print()
    return 0


def cmd_vehicles(args) -> int:
    _, _, client = _context(args)
    vehicles = client.vehicles()
    print(f"\nVéhicules du compte ({len(vehicles)}) :")
    for veh in vehicles:
        print(f"  • {veh.plate}  (id {veh.id}, {veh.country or '?'}, {veh.type or '?'})")
    if not vehicles:
        print("  — aucun véhicule enregistré sur le compte")
    print()
    return 0


def cmd_rates(args) -> int:
    cfg, _, client = _context(args)
    plate = args.plate or (cfg.rules[0].plate if cfg.rules else None)
    options = client.rate_options(args.zone, plate)
    print(f"\nTarifs zone {args.zone} pour {plate or '(sans plaque)'} :")
    for opt in options:
        default = " ⭐ défaut" if opt.is_default else ""
        max_stay = f", max {opt.max_stay_minutes} min" if opt.max_stay_minutes else ""
        units = f", unités {'/'.join(opt.accepted_time_units)}" if opt.accepted_time_units else ""
        print(f"  • id={opt.id}  type={opt.type or '?'}  « {opt.name} »{max_stay}{units}{default}")
    if not options:
        print("  — aucun tarif : zone inconnue ou plaque non éligible")
    print()
    return 0


def cmd_quote(args) -> int:
    cfg, _, client = _context(args)
    plate = args.plate or cfg.rules[0].plate
    rate = client.pick_rate_option(args.zone, plate, args.rate)
    minutes = parse_duration(args.duration)
    duration = best_duration(minutes, rate.accepted_time_units)
    quote = client.quote(args.zone, plate, duration, rate_option_id=rate.id)
    tz = ZoneInfo(cfg.timezone)
    end = quote.expiry.astimezone(tz).strftime("%d/%m %H:%M") if quote.expiry else "?"
    print(
        f"\n{plate} · zone {args.zone} · tarif {rate.type or rate.name} · {duration}"
        f"\n  → {quote.cost:.2f} {quote.currency}, valable jusqu'à {end}\n"
    )
    return 0


def cmd_plan(args) -> int:
    """Simulation SmartPark : combien coûte la journée découpée vs. d'un bloc."""
    cfg, _, client = _context(args)
    plate = args.plate or cfg.rules[0].plate
    tz = ZoneInfo(cfg.timezone)
    now_local = datetime.now(tz)

    if args.until:
        end_time = parse_time(args.until)
        end = now_local.replace(hour=end_time.hour, minute=end_time.minute, second=0, microsecond=0)
        if end <= now_local:
            end += timedelta(days=1)
        minutes = int((end - now_local).total_seconds() // 60)
    else:
        minutes = parse_duration(args.duration or "6h")

    rate = client.pick_rate_option(args.zone, plate, args.rate)
    durations = candidate_durations(rate.max_stay_minutes)
    print(f"\nInterrogation des tarifs réels zone {args.zone} ({rate.type or rate.name})…")

    def price_of(mins: int):
        quote = client.quote(
            args.zone, plate, best_duration(mins, rate.accepted_time_units), rate_option_id=rate.id
        )
        real = quote.minutes
        if real and abs(real - mins) > 5:
            return None
        return quote.cost

    curve = build_curve(price_of, durations)
    if not curve:
        print("Aucun devis obtenu — zone ou tarif non facturable.\n")
        return 1

    print("\nBarème constaté :")
    for mins in sorted(curve):
        hours, rest = divmod(mins, 60)
        label = f"{hours}h{rest:02d}" if hours else f"{rest}min"
        print(f"  {label:>7} → {curve[mins]:6.2f} €")

    plan = cheapest_plan(minutes, curve)
    print(f"\nPour {minutes // 60}h{minutes % 60:02d} de stationnement :")
    print(f"  {plan.describe()}\n")
    return 0


def cmd_park(args) -> int:
    cfg, state, client = _context(args)
    plate = args.plate or cfg.rules[0].plate
    rate = client.pick_rate_option(args.zone, plate, args.rate)
    minutes = parse_duration(args.duration)
    duration = best_duration(minutes, rate.accepted_time_units)

    quote = client.quote(args.zone, plate, duration, rate_option_id=rate.id)
    print(
        f"\n{plate} · zone {args.zone} · tarif {rate.type or rate.name} · {duration}"
        f"  →  {quote.cost:.2f} {quote.currency}"
    )
    if not args.yes:
        answer = input("Confirmer l'achat ? [o/N] ").strip().lower()
        if answer not in ("o", "oui", "y", "yes"):
            print("Annulé.")
            return 1

    payment = client.payment_account_id() if quote.cost else None
    session = client.start_session(
        location_id=args.zone,
        plate=plate,
        duration=duration,
        rate_option_id=rate.id,
        payment_account_id=payment,
    )
    tz = ZoneInfo(cfg.timezone)
    expiry = session.expiry.astimezone(tz).strftime("%d/%m %H:%M") if session.expiry else "?"
    print(f"✅ Ticket confirmé — expire {expiry}\n")
    if quote.cost:
        state.add_spend(f"{plate}@{args.zone}", quote.cost)
    return 0


def cmd_doctor(args) -> int:
    """Vérifie toute la chaîne, règle par règle, sans rien acheter."""
    problems = 0
    print("\n=== Diagnostic AlloValet ===\n")

    try:
        cfg = Config.load(args.config)
        print(f"[ok] config      {cfg.path} — {len(cfg.rules)} règle(s), "
              f"fournisseur {cfg.provider}, fuseau {cfg.timezone}")
    except AlloValetError as exc:
        print(f"[KO] config      {exc}")
        return 1

    state = State()
    try:
        client = build_client(cfg, state)
        client.authenticate()
        print("[ok] connexion   authentifié")
    except AlloValetError as exc:
        print(f"[KO] connexion   {exc}")
        return 1

    try:
        print(f"[ok] compte      id {client.account_id()}")
    except AlloValetError as exc:
        print(f"[KO] compte      {exc}")
        return 1

    plates = set()
    try:
        vehicles = client.vehicles()
        plates = {v.plate for v in vehicles}
        print(f"[ok] véhicules   {', '.join(sorted(plates)) or 'aucun'}")
    except AlloValetError as exc:
        print(f"[--] véhicules   non listés ({exc})")

    payment = None
    try:
        payment = client.payment_account_id()
        print(f"[{'ok' if payment else '--'}] paiement    "
              f"{'carte enregistrée' if payment else 'aucune carte (ok si tarif gratuit)'}")
    except AlloValetError as exc:
        print(f"[--] paiement    {exc}")

    try:
        sessions = client.current_sessions()
        print(f"[ok] tickets     {len(sessions)} en cours")
    except AlloValetError as exc:
        print(f"[KO] tickets     {exc}")
        problems += 1

    tz = ZoneInfo(cfg.timezone)
    for rule in cfg.rules:
        print(f"\n--- règle « {rule.name} » ({rule.plate} zone {rule.location}, {rule.mode})")
        if plates and rule.plate not in plates:
            print(f"    [KO] plaque {rule.plate} absente du compte")
            problems += 1
        try:
            rate = client.pick_rate_option(rule.location, rule.plate, rule.rate)
            print(f"    [ok] tarif   {rate.type or '?'} « {rate.name} » (id {rate.id})")
        except AlloValetError as exc:
            print(f"    [KO] tarif   {exc}")
            problems += 1
            continue
        minutes = rule.duration_minutes or 60
        try:
            duration = best_duration(minutes, rate.accepted_time_units)
            quote = client.quote(rule.location, rule.plate, duration, rate_option_id=rate.id)
            print(f"    [ok] devis   {duration} → {quote.cost:.2f} {quote.currency}")
            if quote.cost and rule.max_cost_per_ticket == 0:
                print("    [KO] ce tarif est payant alors que max_cost_per_ticket vaut 0")
                problems += 1
        except AlloValetError as exc:
            print(f"    [--] devis   {exc}")
        print(f"    [ok] créneau {rule.window.describe()} "
              f"({'actif maintenant' if rule.window.contains(datetime.now(tz)) else 'inactif'})")

    print(f"\n=== {'Tout est prêt ✅' if not problems else str(problems) + ' problème(s) ❌'} ===\n")
    return 1 if problems else 0


def cmd_login(args) -> int:
    cfg, state, client = _context(args)
    client.authenticate()
    print(f"\n✅ Connecté — compte {client.account_id()}")
    print("Token mis en cache dans .allovalet_state.json\n")
    return 0


def cmd_easypark_login(args) -> int:
    """Auth EasyPark par SMS (interactif, une seule fois)."""
    import json
    import uuid

    import requests

    base = "https://app-bff.easyparksystem.net"
    headers = {
        "easypark-application-channel-name": "Android",
        "easypark-application-device-os": "Android Mobile",
        "easypark-application-version-number": "16.5.0",
        "easypark-application-market-country": "FR",
        "easypark-application-phone-number-country": "FR",
        "easypark-application-preferred-language": "fr-FR",
        "easypark-application-install-id": str(uuid.uuid4()),
        "Content-Type": "application/json; charset=UTF-8",
        "User-Agent": "okhttp/4.9.3",
    }
    secure_install_id = str(uuid.uuid4())
    phone = input("Numéro de téléphone EasyPark (ex : +33612345678) : ").strip()

    requests.post(base + "/android/api/account/requestVerificationCode", headers=headers,
                  json={"loginId": "", "phoneNumber": phone}, timeout=30).raise_for_status()
    print("Code SMS envoyé.")
    code = input("Code reçu : ").strip()

    resp = requests.post(
        base + "/android/api/account/loginWithVerificationCode", headers=headers,
        json={"countryCode": "FR", "phoneNumber": phone,
              "secureInstallId": secure_install_id, "verificationCode": code}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    action = data.get("action", "")

    if "multiFactorVerification" in action:
        pending = action.split("pendingAccessToken=")[-1].split("&")[0]
        plate = input("Vérification — plaque d'immatriculation : ").strip().upper()
        resp = requests.post(
            base + "/account/verifyAccountWithLicensePlateNumber", headers=headers,
            json={"licensePlateNumber": plate, "pendingAccessToken": pending,
                  "phoneNumber": phone}, timeout=30)
        resp.raise_for_status()
        data = resp.json()

    try:
        id_token = data["sso"]["idToken"]
        user_id = str(data["status"]["accounts"][0]["parkingUserId"])
    except (KeyError, IndexError):
        print("Réponse inattendue :\n" + json.dumps(data, indent=2)[:2000])
        return 1

    print("\n✅ Authentifié. Ajoute ces deux secrets GitHub (Settings → Secrets → Actions) :")
    print(f"   EP_ID_TOKEN         = {id_token}")
    print(f"   EP_PARKING_USER_ID  = {user_id}\n")
    return 0


# -------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="allovalet", description="Stationnement automatique, usage personnel."
    )
    parser.add_argument("--config", default=os.getenv("ALLOVALET_CONFIG", "config.yml"))
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="applique les règles (un passage)")
    run.add_argument("--dry-run", action="store_true", help="n'achète rien, dit ce qu'il ferait")
    run.add_argument("--loop", type=int, metavar="MINUTES", help="boucle locale toutes les N min")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="tickets en cours et état des règles")
    status.set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="diagnostic complet (n'achète rien)")
    doctor.set_defaults(func=cmd_doctor)

    vehicles = sub.add_parser("vehicles", help="véhicules du compte")
    vehicles.set_defaults(func=cmd_vehicles)

    rates = sub.add_parser("rates", help="tarifs disponibles sur une zone")
    rates.add_argument("--zone", required=True)
    rates.add_argument("--plate")
    rates.set_defaults(func=cmd_rates)

    quote = sub.add_parser("quote", help="prix d'une durée")
    quote.add_argument("--zone", required=True)
    quote.add_argument("--duration", required=True)
    quote.add_argument("--rate")
    quote.add_argument("--plate")
    quote.set_defaults(func=cmd_quote)

    plan = sub.add_parser("plan", help="simulation SmartPark (barème réel + découpage)")
    plan.add_argument("--zone", required=True)
    plan.add_argument("--until", help="heure de fin, ex. 19:00")
    plan.add_argument("--duration", help="ou une durée, ex. 6h")
    plan.add_argument("--rate")
    plan.add_argument("--plate")
    plan.set_defaults(func=cmd_plan)

    park = sub.add_parser("park", help="prendre un ticket maintenant")
    park.add_argument("--zone", required=True)
    park.add_argument("--duration", required=True)
    park.add_argument("--rate")
    park.add_argument("--plate")
    park.add_argument("--yes", action="store_true", help="sans confirmation")
    park.set_defaults(func=cmd_park)

    login = sub.add_parser("login", help="teste la connexion et met le token en cache")
    login.set_defaults(func=cmd_login)

    ep = sub.add_parser("easypark-login", help="authentification EasyPark par SMS")
    ep.set_defaults(func=cmd_easypark_login)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(args.verbose)

    if not getattr(args, "func", None):
        parser.print_help()
        return 0

    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nInterrompu.")
        return 130
    except AlloValetError as exc:
        log.error("%s", exc)
        try:
            cfg = Config.load(args.config)
            Notifier(cfg.notify).send("Stationnement — erreur", str(exc), success=False)
        except Exception:  # noqa: BLE001 — la notif d'erreur ne doit rien casser
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
