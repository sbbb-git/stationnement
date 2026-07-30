"""Verrouille ce qui est réellement demandé : 75016 et 75008, tous les jours, 20h01.

Ces tests portent sur les fichiers livrés (config.yml et le workflow), pas sur
des exemples : si quelqu'un touche à une zone ou à l'horaire, ça casse ici.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from allovalet.config import Config

ROOT = Path(__file__).resolve().parents[1]
PARIS = ZoneInfo("Europe/Paris")


def ma_config() -> Config:
    return Config.load(ROOT / "config.yml")


def test_les_deux_zones_demandees():
    rules = ma_config().rules
    assert [r.location for r in rules] == ["75016", "75008"]
    assert {r.plate for r in rules} == {"AB123CD"}
    assert all(r.enabled and r.mode == "renew" for r in rules)


def test_ticket_de_24h_et_gratuit_seulement():
    for rule in ma_config().rules:
        assert rule.duration_minutes == 24 * 60
        assert rule.max_cost_per_ticket == 0  # ne dépensera jamais un centime


def test_tous_les_jours_a_partir_de_20h01():
    for rule in ma_config().rules:
        assert rule.window.days == frozenset(range(7)), "dimanche compris"
        for jour in range(7):  # lundi 6 janvier 2025 = jour 0
            date = 6 + jour
            assert rule.window.contains(datetime(2025, 1, date, 20, 1, tzinfo=PARIS))
            assert rule.window.contains(datetime(2025, 1, date, 23, 59, tzinfo=PARIS))
            assert not rule.window.contains(datetime(2025, 1, date, 20, 0, tzinfo=PARIS))
            assert not rule.window.contains(datetime(2025, 1, date, 12, 0, tzinfo=PARIS))
            assert not rule.window.contains(datetime(2025, 1, date, 3, 0, tzinfo=PARIS))


def _heures_paris(mois: int) -> set[str]:
    """Les créneaux du cron, convertis en heure de Paris pour un mois donné."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/parking.yml").read_text())
    triggers = workflow[True] if True in workflow else workflow["on"]
    minutes, heures = triggers["schedule"][0]["cron"].split()[:2]

    debut, fin = (int(x) for x in heures.split("-"))
    return {
        datetime(2025, mois, 15, h, int(m), tzinfo=ZoneInfo("UTC"))
        .astimezone(PARIS).strftime("%H:%M")
        for h in range(debut, fin + 1)
        for m in minutes.split(",")
    }


def test_le_cron_passe_bien_a_20h01_ete_comme_hiver():
    ete = _heures_paris(7)
    hiver = _heures_paris(1)
    assert "20:01" in ete, f"heure d'été : {sorted(ete)}"
    assert "20:01" in hiver, f"heure d'hiver : {sorted(hiver)}"


def test_des_passages_de_rattrapage_apres_20h01():
    """GitHub décale souvent le déclenchement : il faut des repasses derrière."""
    for mois in (1, 7):
        apres = [h for h in _heures_paris(mois) if "20:01" <= h <= "23:59"]
        assert len(apres) >= 8, f"mois {mois} : seulement {sorted(apres)}"
