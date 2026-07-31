"""Un instantané lisible de la situation : ce que couvre quoi, jusqu'à quand.

Sert de source unique à l'interface web et au résumé publié par GitHub
Actions. Ne modifie rien et n'achète rien : c'est de la lecture.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import Config
from .runner import Runner


def snapshot(cfg: Config, client, state=None) -> dict:
    """Lit le compte et confronte chaque règle à la réalité.

    Une erreur de lecture n'est pas une exception : elle est rapportée dans
    `erreur`, pour que l'interface puisse s'afficher quand même.
    """
    tz = ZoneInfo(cfg.timezone)
    maintenant = datetime.now(tz)
    vue = {
        "genere": maintenant.isoformat(timespec="seconds"),
        "fuseau": cfg.timezone,
        "erreur": None,
        "regles": [],
        "tickets": [],
        "passages": list(reversed((state.data.get("journal") or [])[-12:])) if state else [],
    }

    try:
        sessions = client.current_sessions()
    except Exception as exc:  # noqa: BLE001 — l'interface doit rester consultable
        vue["erreur"] = str(exc)
        sessions = None

    if sessions is not None:
        vue["tickets"] = [_ticket(s, tz) for s in sessions]

    runner = Runner(cfg, client)
    for rule in cfg.rules:
        active = (
            client.find_active(rule.plate, rule.zones, sessions)
            if sessions is not None else None
        )
        raison = None
        if sessions is not None and rule.enabled:
            raison = runner._why_act(rule, active, maintenant)
        vue["regles"].append({
            "nom": rule.name,
            "plaque": rule.plate,
            "zones": rule.zones,
            "preferee": rule.location,
            "activee": rule.enabled,
            "tarif": rule.rate,
            "rendez_vous": rule.renew_at,
            "duree_minutes": rule.duration_minutes,
            "couvert": bool(active),
            "zone_couvrante": active.location_id if active else None,
            "sur_la_preferee": bool(active and active.at_location(rule.location)),
            "expire": _local(active.expiry, tz) if active else None,
            "reste_minutes": _reste(active),
            "action": raison,
        })
    return vue


def _ticket(sess, tz) -> dict:
    return {
        "plaque": sess.plate,
        "zone": str(sess.location_id),
        "tarif": sess.rate_type or sess.rate_option_id or "?",
        "expire": _local(sess.expiry, tz),
        "reste_minutes": _reste(sess),
    }


def _local(moment, tz) -> str | None:
    return moment.astimezone(tz).strftime("%d/%m %H:%M") if moment else None


def _reste(sess) -> int:
    if not sess or not sess.expiry:
        return 0
    return max(0, int((sess.expiry - datetime.now(timezone.utc)).total_seconds() // 60))


def duree(minutes: int) -> str:
    heures, reste = divmod(max(0, int(minutes)), 60)
    return f"{heures} h {reste:02d}" if heures else f"{reste} min"


def markdown(vue: dict) -> str:
    """Le même instantané, en Markdown — pour le résumé d'un passage Actions.

    C'est la façon de consulter l'état depuis un téléphone sans rien installer :
    GitHub affiche ce résumé en tête du passage.
    """
    lignes = ["## Stationnement — état du compte", ""]
    if vue["erreur"]:
        lignes += [f"> ⚠️ lecture du compte impossible : `{vue['erreur']}`", ""]

    lignes += ["| Règle | Couvert par | Expire | Reste | Prochaine action |",
               "|---|---|---|---|---|"]
    for regle in vue["regles"]:
        if not regle["activee"]:
            etat, zone, expire, reste = "désactivée", "—", "—", "—"
        elif regle["couvert"]:
            marque = "✅" if regle["sur_la_preferee"] else "↪️"
            zone = f"{marque} {regle['zone_couvrante']}"
            expire = regle["expire"] or "?"
            reste = duree(regle["reste_minutes"])
            etat = regle["action"] or "rien à faire"
        else:
            zone, expire, reste = "❌ aucun", "—", "—"
            etat = regle["action"] or "à prendre"
        lignes.append(f"| {regle['nom']} | {zone} | {expire} | {reste} | {etat} |")

    lignes += ["", f"_{len(vue['tickets'])} ticket(s) en cours · relevé "
                   f"{vue['genere'][:16].replace('T', ' à ')}_"]
    return "\n".join(lignes)
