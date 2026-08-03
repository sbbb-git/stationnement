from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from allovalet.errors import ConfigError
from allovalet.schedule import Window, parse_days, parse_duration, parse_time


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """Lundi 6 janvier 2025 = jour 0."""
    return datetime(2025, 1, 6 + day, hour, minute)


@pytest.mark.parametrize(
    "text,minutes",
    [("24h", 1440), ("2h30", 150), ("90m", 90), ("45", 45), ("1d", 1440), ("1j 2h", 1560)],
)
def test_durees(text, minutes):
    assert parse_duration(text) == minutes


def test_duree_invalide():
    with pytest.raises(ConfigError):
        parse_duration("bientôt")


def test_heures():
    assert parse_time("20:00") == time(20, 0)
    assert parse_time("9h30") == time(9, 30)
    assert parse_time("23:59").hour == 23  # fin de journée


def test_jours():
    assert parse_days("lun-sam") == frozenset({0, 1, 2, 3, 4, 5})
    assert parse_days(["sam", "dim"]) == frozenset({5, 6})
    assert parse_days("weekdays") == frozenset({0, 1, 2, 3, 4})
    assert parse_days(None) == frozenset(range(7))
    with pytest.raises(ConfigError):
        parse_days("lunedi")


def test_creneau_simple():
    window = Window.parse({"from": "19:30", "to": "23:59", "days": ["lun-sam"]})
    assert window.contains(at(0, 20, 0))
    assert window.contains(at(0, 23, 59))  # dernière minute incluse
    assert not window.contains(at(0, 19, 0))
    assert not window.contains(at(6, 20, 0))  # dimanche


def test_creneau_de_nuit():
    window = Window.parse({"from": "20:00", "to": "08:00", "days": ["lun"]})
    assert window.contains(at(0, 22, 0))       # lundi soir
    assert window.contains(at(1, 3, 0))        # nuit de lundi à mardi
    assert not window.contains(at(1, 22, 0))   # mardi soir : hors jours
    assert not window.contains(at(0, 12, 0))


def test_fin_de_creneau():
    window = Window.parse({"from": "09:00", "to": "19:00"})
    assert window.end_after(at(0, 10, 0)) == at(0, 19, 0)
    assert window.end_after(at(0, 20, 0)) == at(1, 19, 0)


def test_creneau_par_defaut_toujours_actif():
    window = Window.parse(None)
    assert window.contains(at(3, 4, 30))
    assert window.contains(at(6, 23, 59))


# --------------------------------------------- attendre l'heure exacte du relais

from allovalet.schedule import secondes_avant  # noqa: E402

PARIS_TZ = ZoneInfo("Europe/Paris")


def _soir(heure: int, minute: int):
    return datetime(2026, 8, 4, heure, minute, tzinfo=PARIS_TZ)


def test_un_passage_en_avance_attend_lheure_pile():
    assert secondes_avant("20:05", _soir(19, 40), 35) == 25 * 60
    assert secondes_avant("20:05", _soir(20, 4), 35) == 60


def test_un_passage_deja_en_retard_nattend_pas():
    """Le ticket a expiré à 20h00 : chaque minute d'attente serait un trou."""
    assert secondes_avant("20:05", _soir(20, 6), 35) == 0
    assert secondes_avant("20:05", _soir(23, 0), 35) == 0


def test_un_passage_trop_en_avance_rend_la_main():
    """Sinon un passage de l'après-midi bloquerait le poste des heures durant,
    et brûlerait les minutes d'Actions pour rien."""
    assert secondes_avant("20:05", _soir(19, 0), 35) == 0
    assert secondes_avant("20:05", _soir(3, 0), 35) == 0


def test_lattente_ne_depasse_jamais_le_plafond():
    for minute in range(0, 60):
        for heure in (18, 19, 20, 21):
            attente = secondes_avant("20:05", _soir(heure, minute), 35)
            assert 0 <= attente <= 35 * 60
