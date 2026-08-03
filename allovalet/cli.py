"""Ligne de commande.

    python -m allovalet ui         # interface : état + modification des règles
    python -m allovalet doctor     # diagnostic complet, à faire en premier
    python -m allovalet run        # un passage (ce que fait GitHub Actions)
    python -m allovalet status     # tickets en cours et état des règles
    python -m allovalet rates --zone 75016
    python -m allovalet park  --zone 75016 --duration 24h
    python -m allovalet schema     # forme exacte attendue par l'API
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from .config import Config
from .errors import AlloValetError
from .models import money
from .notify import Notifier
from .paybyphone import OPERATION_INPUTS, best_duration
from .providers import build_client
from .runner import Runner
from .schedule import parse_duration
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
    return cfg, state, build_client(cfg, state)


# ---------------------------------------------------------------- commandes


def cmd_run(args) -> int:
    cfg, state, client = _context(args)
    runner = Runner(cfg, client, state, Notifier(cfg.notify), dry_run=args.dry_run)
    report = runner.tick()
    print()
    print(report.text() or "aucune règle active")
    print()
    return 1 if report.failures else 0


def cmd_status(args) -> int:
    cfg, _, client = _context(args)
    tz = ZoneInfo(cfg.timezone)
    sessions = client.current_sessions()

    print(f"\nTickets en cours ({len(sessions)}) :")
    for sess in sessions:
        reste = int(sess.remaining.total_seconds() // 60)
        print(f"  • {sess.plate} · zone {sess.location_id} · {sess.rate_type or '?'} "
              f"· jusqu'à {sess.expiry.astimezone(tz):%a %d/%m %H:%M} "
              f"(reste {reste // 60}h{reste % 60:02d})")
    if not sessions:
        print("  — aucun")

    print("\nRègles :")
    now_local = datetime.now(tz)
    runner = Runner(cfg, client)
    for rule in cfg.rules:
        active = client.find_active(rule.plate, rule.zones, sessions)
        etat = runner._why_act(rule, active, now_local) or "couvert"
        if active:
            etat += f" par la zone {active.location_id}"
        flag = "" if rule.enabled else " (désactivée)"
        print(f"  • {rule.name}{flag} — {rule.plate} zone {rule.location} → {etat}")
        if rule.fallbacks:
            print(f"      replis : {' → '.join(rule.fallbacks)}")
    print()
    return 0


def cmd_wait(args) -> int:
    """Attend l'heure du relais, quand le passage arrive un peu en avance.

    C'est ce qui rend le rendez-vous ponctuel : GitHub déclenche quand il veut,
    mais le passage, lui, sait attendre l'heure exacte. Au-delà du plafond, il
    rend la main tout de suite — un passage plus tardif s'en chargera.
    """
    from .schedule import secondes_avant

    cfg = Config.load(args.config)
    maintenant = datetime.now(ZoneInfo(cfg.timezone))
    delai = secondes_avant(args.at, maintenant, args.max_minutes)
    if not delai:
        print(f"{maintenant:%H:%M} — pas d'attente (relais de {args.at}).")
        return 0
    print(f"{maintenant:%H:%M} — attente de {delai // 60} min pour agir à {args.at} pile.")
    time.sleep(delai)
    return 0


def cmd_ui(args) -> int:
    """L'interface : voir l'état, modifier les règles, lancer un passage."""
    from .ui import serve  # importé ici : une commande en ligne n'en a pas besoin

    return serve(args.config, port=args.port, ouvrir=not args.no_open)


def cmd_summary(args) -> int:
    """L'état, en Markdown — pour le résumé d'un passage GitHub Actions.

    C'est ce qui permet de consulter la situation depuis un téléphone, sans
    rien installer : GitHub affiche ce résumé en tête du passage.
    """
    from .etat import markdown, snapshot

    cfg, state, client = _context(args)
    texte = markdown(snapshot(cfg, client, state), depot=os.getenv("GITHUB_REPOSITORY"))
    for chemin in filter(None, [args.out, args.aussi]):
        with open(chemin, "a", encoding="utf-8") as sortie:
            sortie.write(texte + "\n")
    print(texte)
    return 0


def cmd_rates(args) -> int:
    cfg, _, client = _context(args)
    plate = args.plate or (cfg.rules[0].plate if cfg.rules else None)
    options = client.rate_options(args.zone, plate)
    print(f"\nTarifs zone {args.zone} pour {plate} :")
    for opt in options:
        maxi = f", max {opt.max_stay_minutes} min" if opt.max_stay_minutes else ""
        units = f", unités {'/'.join(opt.accepted_time_units)}" if opt.accepted_time_units else ""
        print(f"  • type={opt.type or '?'}  « {opt.name} »  (ratePolicyId {opt.id}{maxi}{units})")
    if not options:
        print("  — aucun : zone inconnue, ou plaque non éligible sur ce compte")
        return 1
    print("\nC'est la valeur de `type` (ou du nom) à mettre dans `rate:`.\n")
    return 0


def cmd_park(args) -> int:
    cfg, state, client = _context(args)
    plate = args.plate or cfg.rules[0].plate
    rate = client.pick_rate_option(args.zone, plate, args.rate)
    duration = best_duration(parse_duration(args.duration), rate.accepted_time_units)

    quote = client.quote(args.zone, plate, duration, rate_option_id=rate.id)
    print(f"\n{plate} · zone {args.zone} · {rate.type or rate.name} · {duration}"
          f"  →  {money(quote.cost, quote.currency)}")
    if not args.yes and input("Confirmer l'achat ? [o/N] ").strip().lower() not in ("o", "oui"):
        print("Annulé.")
        return 1

    payment = client.payment_account_id() if quote.cost else None
    session = client.start_session(
        location_id=args.zone, plate=plate, duration=duration,
        rate_option_id=rate.id, payment_account_id=payment,
    )
    tz = ZoneInfo(cfg.timezone)
    print(f"✅ Ticket confirmé — expire {session.expiry.astimezone(tz):%d/%m %H:%M}\n")
    if quote.cost:
        state.add_spend(f"{plate}@{args.zone}", quote.cost)
    return 0


def cmd_sweep(args) -> int:
    """Tente un ticket sur une plage de zones, puis liste ce qui existe vraiment.

    Sert à savoir quelles zones acceptent réellement le tarif. Aucun achat
    payant n'est possible : une zone dont le devis n'est pas à 0 € est ignorée.
    """
    cfg, _, client = _context(args)
    tz = ZoneInfo(cfg.timezone)
    plate = args.plate or cfg.rules[0].plate
    voulu = args.rate or (cfg.rules[0].rate if cfg.rules else None)
    zones = [str(z) for z in range(args.debut, args.fin + 1)]

    print(f"\nBalayage {zones[0]} → {zones[-1]} pour {plate}, tarif « {voulu} »")
    print("(aucun achat payant possible : un devis non nul est ignoré)\n")

    avant = {s.id for s in client.current_sessions()}
    resultats = []

    for zone in zones:
        etat = ""
        try:
            rate = client.pick_rate_option(zone, plate, voulu)
        except AlloValetError:
            resultats.append((zone, "—", "pas de tarif Handi"))
            continue
        try:
            duration = best_duration(cfg.rules[0].duration_minutes, rate.accepted_time_units)
            quote = client.quote(zone, plate, duration, rate_option_id=rate.id)
        except AlloValetError as exc:
            resultats.append((zone, rate.id, f"devis refusé : {str(exc)[:90]}"))
            continue
        if quote.cost:
            resultats.append((zone, rate.id, f"ignoré — payant ({money(quote.cost)})"))
            continue
        if args.dry_run:
            resultats.append((zone, rate.id, f"devis ok, quoteId {'oui' if quote.quote_id else 'non'}"))
            continue
        try:
            # Pas de vérification unitaire : on l'établit une fois pour toutes
            # à la fin, en relisant les tickets réellement en cours.
            session = client.start_session(
                location_id=zone, plate=plate, duration=duration,
                rate_option_id=rate.id, verify=False,
            )
            etat = f"achat accepté, id {(session.id or '?')[:8]}"
        except AlloValetError as exc:
            etat = f"achat refusé : {str(exc)[:90]}"
        resultats.append((zone, rate.id, etat))

    print(f"{'zone':<8}{'ratePolicyId':<14}résultat")
    for zone, rid, etat in resultats:
        print(f"{zone:<8}{str(rid):<14}{etat}")

    apres = client.current_sessions()
    print(f"\n--- tickets réellement en cours après le balayage ({len(apres)}) ---")
    for sess in apres:
        neuf = " ← NOUVEAU" if sess.id not in avant else ""
        fin = f"{sess.expiry.astimezone(tz):%d/%m %H:%M}" if sess.expiry else "?"
        print(f"  • zone {str(sess.location_id):<8} {sess.plate}  {sess.rate_type or '?':<8}"
              f" jusqu'à {fin}{neuf}")
    crees = [s for s in apres if s.id not in avant]
    print(f"\n=== {len(crees)} ticket(s) créé(s) par ce balayage ===\n")
    return 0


def cmd_history(args) -> int:
    """Tickets passés — c'est la trace de ce qui a réellement été créé."""
    cfg, _, client = _context(args)
    tz = ZoneInfo(cfg.timezone)
    sessions = sorted(
        client.history(limit=args.limit), key=lambda s: s.start or s.expiry, reverse=True
    )
    print(f"\n{len(sessions)} derniers tickets :")
    for sess in sessions:
        debut = f"{sess.start.astimezone(tz):%d/%m %H:%M}" if sess.start else "?"
        fin = f"{sess.expiry.astimezone(tz):%d/%m %H:%M}" if sess.expiry else "?"
        etat = (sess.raw or {}).get("status") or "?"
        print(f"  • {debut} → {fin}  {sess.plate}  zone {str(sess.location_id):<8}"
              f"{(sess.rate_type or '?'):<8} {etat}  {money(sess.cost) if sess.cost else 'gratuit'}")
    if not sessions:
        print("  — aucun")
    print()
    return 0


def cmd_schema(args) -> int:
    """Introspecte l'API : la forme exacte attendue par chaque opération."""
    _, _, client = _context(args)
    client.authenticate()
    names = [args.type] if args.type else sorted(set(OPERATION_INPUTS.values()))
    problemes = 0
    for name in names:
        try:
            fields = client.input_fields(name)
        except AlloValetError as exc:
            print(f"\n{name} : introspection refusée — {exc}")
            problemes += 1
            continue
        if not fields:
            print(f"\n{name} : type inconnu de l'API")
            problemes += 1
            continue
        print(f"\n{name}")
        for field, kind in fields:
            print(f"    {field:<32} {kind}")
    print()
    return 1 if problemes else 0


def cmd_probe(args) -> int:
    """Sonde complète : interroge l'API sur elle-même et n'abandonne jamais.

    Chaque bloc capture son erreur et continue, pour qu'un seul passage
    rapporte tout ce qu'il y a à savoir plutôt qu'une erreur à la fois.
    N'achète rien.
    """
    def essai(titre, action):
        print(f"\n### {titre}")
        try:
            action()
        except Exception as exc:  # noqa: BLE001 — une sonde ne s'arrête pas
            print(f"    ÉCHEC : {exc}")

    print("\n" + "=" * 72)
    print("SONDE — aucune donnée n'est modifiée, aucun ticket n'est acheté")
    print("=" * 72)

    cfg = Config.load(args.config)
    client = build_client(cfg, State())
    client.authenticate()
    print(f"\nConnecté. Membre : {client.member_id}")

    def operations():
        interessant = ("session", "parking", "rate", "vehicle", "payment", "eligib")
        for nom, retour in sorted(client.root_fields()):
            if any(k in nom.lower() for k in interessant):
                print(f"    {nom:<42} → {retour}")

    essai("Opérations de lecture disponibles (filtrées)", operations)

    types = sorted({
        *OPERATION_INPUTS.values(),
        "GetParkingSessionsInput", "PeriodType", "ParkingSessionResponse",
        "RateOption", "Quote",
    })
    def decrire():
        for nom in types:
            info = client.describe_type(nom)
            if not info.get("name"):
                print(f"    {nom} : inconnu de l'API")
                continue
            print(f"    {info['name']} ({info['kind']})")
            if info["enum"]:
                print(f"        valeurs : {', '.join(info['enum'])}")
            for champ, kind in info["inputs"]:
                print(f"        ← {champ:<32} {kind}")
            for champ, kind in info["outputs"][:40]:
                print(f"        → {champ:<32} {kind}")

    essai("Formes exactes des types", decrire)
    essai("Véhicules du compte", lambda: [
        print(f"    {v.plate}  (id {v.id}, {v.country}, {v.type})") for v in client.vehicles()
    ])
    essai("Tickets en cours", lambda: [
        print(f"    {s.describe()}") for s in client.current_sessions()
    ] or None)

    for rule in cfg.rules:
        def zone(rule=rule):
            print(f"    replis prévus : {' → '.join(rule.fallbacks) or 'aucun'}")
            options = client.rate_options(rule.location, rule.plate)
            if not options:
                print("    aucun tarif renvoyé")
                return
            for opt in options:
                print(f"    type={opt.type!r} nom={opt.name!r} id={opt.id} "
                      f"max={opt.max_stay_minutes} unités={opt.accepted_time_units}")
            try:
                rate = client.pick_rate_option(rule.location, rule.plate, rule.rate)
            except AlloValetError as exc:
                print(f"    tarif « {rule.rate} » : {exc}")
                return
            duration = best_duration(rule.duration_minutes, rate.accepted_time_units)
            quote = client.quote(rule.location, rule.plate, duration, rate_option_id=rate.id)
            print(f"    devis {duration} → {money(quote.cost, quote.currency)} "
                  f"| quoteId={'oui' if quote.quote_id else 'MANQUANT'} "
                  f"| début={quote.start} fin={quote.expiry}")

        essai(f"Zone {rule.location} pour {rule.plate}", zone)

    print("\n" + "=" * 72 + "\n")
    return 0


def cmd_doctor(args) -> int:
    """Vérifie toute la chaîne, règle par règle, sans rien acheter."""
    problems = 0
    print("\n=== Diagnostic ===\n")

    try:
        cfg = Config.load(args.config)
        print(f"[ok] config      {cfg.path} — {len(cfg.rules)} règle(s), fuseau {cfg.timezone}")
    except AlloValetError as exc:
        print(f"[KO] config      {exc}")
        return 1

    state = State()
    try:
        client = build_client(cfg, state)
        client.authenticate()
        print(f"[ok] connexion   authentifié — membre {client.member_id}")
    except AlloValetError as exc:
        print(f"[KO] connexion   {exc}")
        print("\n↳ L'identifiant est le numéro de téléphone avec indicatif (+336…) "
              "ou l'email du compte PayByPhone.\n")
        return 1

    plates = set()
    try:
        vehicles = client.vehicles()
        plates = {v.plate for v in vehicles}
        print(f"[ok] véhicules   {', '.join(sorted(plates)) or 'aucun'}")
    except AlloValetError as exc:
        print(f"[KO] véhicules   {exc}")
        problems += 1

    try:
        sessions = client.current_sessions()
        print(f"[ok] tickets     {len(sessions)} en cours")
        for sess in sessions:
            print(f"                 {sess.describe()}")
    except AlloValetError as exc:
        print(f"[KO] tickets     {exc}")
        problems += 1

    for rule in cfg.rules:
        print(f"\n--- règle « {rule.name} » ({rule.plate}, zones {' → '.join(rule.zones)})")
        if plates and rule.plate not in plates:
            print(f"    [KO] plaque {rule.plate} absente du compte PayByPhone")
            problems += 1
        # On descend la liste comme le fait le programme : la règle va bien
        # tant qu'**une** zone du secteur accepte, pas seulement la préférée.
        prete = False
        for zone in rule.zones:
            marque = "→" if zone == rule.location else " ↳"
            try:
                rate = client.pick_rate_option(zone, rule.plate, rule.rate)
            except AlloValetError as exc:
                print(f"    {marque} {zone} tarif refusé : {exc}")
                continue
            try:
                duration = best_duration(rule.duration_minutes, rate.accepted_time_units)
                quote = client.quote(zone, rule.plate, duration, rate_option_id=rate.id)
            except AlloValetError as exc:
                print(f"    {marque} {zone} devis refusé : {exc}")
                continue
            if not quote.quote_id:
                print(f"    {marque} {zone} sans quoteId — achat impossible")
                continue
            if quote.cost and rule.max_cost_per_ticket == 0:
                print(f"    {marque} {zone} payant ({money(quote.cost, quote.currency)}) "
                      "— écarté par max_cost_per_ticket")
                continue
            print(f"    [ok] {zone} · {rate.type or '?'} « {rate.name} » · {duration} → "
                  f"{money(quote.cost, quote.currency)}")
            prete = True
            break
        if not prete:
            print(f"    [KO] aucune des {len(rule.zones)} zones ne peut donner de ticket")
            problems += 1

    print(f"\n=== {'Tout est prêt ✅' if not problems else str(problems) + ' problème(s) ❌'} ===")
    print("Aucun ticket n'a été acheté par ce diagnostic.\n")
    return 1 if problems else 0


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
    run.set_defaults(func=cmd_run)

    doctor = sub.add_parser("doctor", help="diagnostic complet (n'achète rien)")
    doctor.set_defaults(func=cmd_doctor)

    status = sub.add_parser("status", help="tickets en cours et état des règles")
    status.set_defaults(func=cmd_status)

    attendre = sub.add_parser("wait", help="attendre l'heure exacte du relais")
    attendre.add_argument("--at", default="20:05", help="heure visée (fuseau de la config)")
    attendre.add_argument("--max-minutes", type=int, default=35,
                          help="au-delà, rendre la main tout de suite")
    attendre.set_defaults(func=cmd_wait)

    ui = sub.add_parser("ui", help="interface web locale : état + modification des règles")
    ui.add_argument("--port", type=int, default=8787)
    ui.add_argument("--no-open", action="store_true", help="ne pas ouvrir le navigateur")
    ui.set_defaults(func=cmd_ui)

    summary = sub.add_parser("summary", help="l'état en Markdown (résumé GitHub Actions)")
    summary.add_argument("--out", help="fichier où ajouter le résumé ($GITHUB_STEP_SUMMARY)")
    summary.add_argument("--aussi", help="second fichier (corps du tableau de bord)")
    summary.set_defaults(func=cmd_summary)

    rates = sub.add_parser("rates", help="tarifs disponibles sur une zone")
    rates.add_argument("--zone", required=True)
    rates.add_argument("--plate")
    rates.set_defaults(func=cmd_rates)

    park = sub.add_parser("park", help="prendre un ticket maintenant")
    park.add_argument("--zone", required=True)
    park.add_argument("--duration", default="24h")
    park.add_argument("--rate")
    park.add_argument("--plate")
    park.add_argument("--yes", action="store_true")
    park.set_defaults(func=cmd_park)

    probe = sub.add_parser("probe", help="sonde complète de l'API (n'achète rien)")
    probe.set_defaults(func=cmd_probe)

    sweep = sub.add_parser("sweep", help="tenter un ticket sur une plage de zones")
    sweep.add_argument("--debut", type=int, default=75001)
    sweep.add_argument("--fin", type=int, default=75020)
    sweep.add_argument("--rate")
    sweep.add_argument("--plate")
    sweep.add_argument("--dry-run", action="store_true")
    sweep.set_defaults(func=cmd_sweep)

    history = sub.add_parser("history", help="tickets passés — trace de ce qui a été créé")
    history.add_argument("--limit", type=int, default=15)
    history.set_defaults(func=cmd_history)

    schema = sub.add_parser("schema", help="forme exacte attendue par l'API (introspection)")
    schema.add_argument("--type")
    schema.set_defaults(func=cmd_schema)

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
            Notifier(Config.load(args.config).notify).send(
                "Stationnement — erreur", str(exc), success=False
            )
        except Exception:  # noqa: BLE001 — la notif d'erreur ne doit rien casser
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
