"""Verrouille ce qui est réellement demandé : 75016 et 75008, toujours couverts,
rendez-vous à 20h01.

Ces tests portent sur les fichiers livrés (config.yml et le workflow), pas sur
des exemples : si quelqu'un touche à une zone, à l'horaire ou au cron, ça casse
ici.
"""

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from allovalet.config import Config

ROOT = Path(__file__).resolve().parents[1]
PARIS = ZoneInfo("Europe/Paris")
UTC = ZoneInfo("UTC")


def ma_config() -> Config:
    return Config.load(ROOT / "config.yml")


def test_une_seule_regle_couvrant_tout_paris():
    """Le tarif « Handi - toutes zones » vaut partout : deux règles feraient
    croire à deux couvertures distinctes et relanceraient des achats inutiles."""
    rules = ma_config().rules
    assert len(rules) == 1
    regle = rules[0]
    assert regle.plate == "AB123CD"
    assert regle.rate == "1321271030"  # relevé sur le compte
    assert regle.toutes_zones is True
    assert regle.enabled


def test_ticket_de_24h_et_gratuit_seulement():
    for rule in ma_config().rules:
        assert rule.duration_minutes == 24 * 60
        assert rule.max_cost_per_ticket == 0  # ne dépensera jamais un centime


def test_rendez_vous_a_20h01():
    for rule in ma_config().rules:
        assert rule.renew_at == "20:01"


def test_les_regles_veillent_24h_sur_24():
    """Pas de créneau restreint : un trou de couverture doit pouvoir être
    rattrapé à n'importe quelle heure, y compris la nuit et le dimanche."""
    for rule in ma_config().rules:
        assert rule.window.days == frozenset(range(7))
        for jour in range(7):
            for heure in (0, 3, 11, 17, 20, 23):
                moment = datetime(2025, 1, 6 + jour, heure, 30, tzinfo=PARIS)
                assert rule.window.contains(moment), f"{moment} non couvert"


def test_la_marge_reste_sous_lintervalle_des_passages():
    """Sinon le filet de sécurité se déclencherait avant le rendez-vous de 20h01
    et ferait dériver l'horaire d'un jour sur l'autre."""
    cfg = ma_config()
    for rule in cfg.rules:
        assert cfg.margin_for(rule) < 30


# ----------------------------------------------------------------------- cron


def _slots_utc() -> list[tuple[int, int]]:
    workflow = yaml.safe_load((ROOT / ".github/workflows/parking.yml").read_text())
    triggers = workflow[True] if True in workflow else workflow["on"]
    out = set()
    for entry in triggers["schedule"]:
        minutes, hours = entry["cron"].split()[:2]
        heures = range(24) if hours == "*" else _expand(hours)
        for h in heures:
            for m in _expand(minutes):
                out.add((h, m))
    return sorted(out)


def _expand(field: str) -> list[int]:
    values = []
    for part in field.split(","):
        if "-" in part:
            a, b = (int(x) for x in part.split("-"))
            values += list(range(a, b + 1))
        else:
            values.append(int(part))
    return values


def _heures_paris(mois: int) -> set[str]:
    return {
        datetime(2025, mois, 15, h, m, tzinfo=UTC).astimezone(PARIS).strftime("%H:%M")
        for h, m in _slots_utc()
    }


def test_le_cron_passe_a_20h01_ete_comme_hiver():
    for mois, saison in ((7, "été"), (1, "hiver")):
        assert "20:01" in _heures_paris(mois), saison


def test_un_passage_au_moins_toutes_les_30_minutes():
    """C'est ce qui borne la durée d'un éventuel trou de couverture."""
    slots = _slots_utc()
    moments = sorted(datetime(2025, 1, 1, h, m, tzinfo=UTC) for h, m in slots)
    ecarts = [
        (b - a).total_seconds() / 60
        for a, b in zip(moments, moments[1:] + [moments[0] + timedelta(days=1)])
    ]
    assert max(ecarts) <= 30, f"trou de {max(ecarts):.0f} min entre deux passages"


def test_renfort_autour_du_rendez_vous():
    """Le moment qui compte mérite plus qu'un seul passage."""
    for mois in (1, 7):
        proches = [h for h in _heures_paris(mois) if "20:00" <= h <= "20:59"]
        assert len(proches) >= 5, f"mois {mois} : {sorted(proches)}"
