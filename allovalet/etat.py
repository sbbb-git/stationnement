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


def markdown(vue: dict, depot: str | None = None) -> str:
    """Le tableau de bord, en Markdown.

    C'est l'interface consultable de partout sans rien installer : GitHub
    l'affiche en tête de chaque passage, et une issue en garde en permanence
    la dernière version. `depot` (« proprio/nom ») ajoute les liens d'action.
    """
    # La date en tête, pas en pied : quand plus aucun passage n'aboutit, le
    # tableau se fige sans rien dire. La seule façon de s'en apercevoir est de
    # voir tout de suite qu'il date.
    lignes = [
        "## 🅿️ Stationnement",
        "",
        f"*Relevé le {vue['genere'][8:10]}/{vue['genere'][5:7]} à "
        f"{vue['genere'][11:16]}.* Un tableau qui date de plus de deux heures "
        "veut dire qu'aucun passage n'aboutit.",
        "",
    ]
    if vue["erreur"]:
        lignes += [f"> ⚠️ lecture du compte impossible : `{vue['erreur']}`", ""]

    lignes += ["| Secteur | Couvert par | Jusqu'à | Reste | Prochaine action |",
               "|---|---|---|---|---|"]
    for regle in vue["regles"]:
        if not regle["activee"]:
            etat, zone, expire, reste = "désactivé", "—", "—", "—"
        elif regle["couvert"]:
            marque = "✅" if regle["sur_la_preferee"] else "↪️"
            zone = f"{marque} **{regle['zone_couvrante']}**"
            expire = regle["expire"] or "?"
            reste = duree(regle["reste_minutes"])
            etat = regle["action"] or "rien à faire"
        else:
            zone, expire, reste = "❌ aucun", "—", "—"
            etat = regle["action"] or "à prendre"
        lignes.append(f"| {regle['nom']} | {zone} | {expire} | {reste} | {etat} |")

    lignes += ["", "<details><summary>Zones essayées, dans l'ordre</summary>", ""]
    for regle in vue["regles"]:
        chaine = " › ".join(
            f"**{z}**" if z == regle["zone_couvrante"] else z for z in regle["zones"]
        )
        lignes.append(f"- {regle['nom']} — {chaine}")
    lignes += ["", "</details>", ""]

    if depot:
        base = f"https://github.com/{depot}"
        lignes += [
            f"⚙️ [Changer un réglage]({base}/edit/main/config.yml) · "
            f"▶️ [Lancer un passage maintenant]({base}/actions/workflows/parking.yml) · "
            f"🕓 [Historique]({base}/actions)",
            "",
        ]

    lignes.append(f"_{len(vue['tickets'])} ticket(s) en cours · mis à jour tout seul_")
    return "\n".join(lignes)
