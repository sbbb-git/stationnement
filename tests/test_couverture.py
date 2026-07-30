"""L'invariant : il doit **toujours** y avoir un ticket en cours.

20h01 est le rendez-vous normal ; ce n'est pas la seule occasion d'agir.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from allovalet.models import ParkingSession, utcnow
from allovalet.notify import Notifier
from allovalet.runner import OK, PURCHASED, Runner
from tests.conftest import make_config
from tests.fake_pbp import PLATE

PARIS = ZoneInfo("Europe/Paris")

CONFIG = f"""
provider: paybyphone
timezone: Europe/Paris
renew_margin_minutes: 25
rules:
  - name: 16e CMI
    plate: {PLATE}
    location: "75016"
    rate: CMI
    duration: 24h
    renew_at: "20:01"
    max_cost_per_ticket: 0
"""


def build(tmp_path, client, state, dry_run=False):
    cfg = make_config(tmp_path, CONFIG)
    return Runner(cfg, client, state, Notifier(cfg.notify), dry_run=dry_run)


def ticket(expiry_local: datetime) -> ParkingSession:
    return ParkingSession(
        id="s1", plate=PLATE, location_id="75016",
        start=expiry_local - timedelta(hours=24), expiry=expiry_local,
    )


def why(runner, now_local, session):
    return runner._why_act(runner.cfg.rules[0], session, now_local)


# ------------------------------------------------------- garantie de couverture


def test_sans_ticket_on_agit_meme_en_pleine_nuit(tmp_path, client, state, server):
    runner = build(tmp_path, client, state)
    nuit = datetime(2026, 3, 12, 3, 30, tzinfo=PARIS)
    assert why(runner, nuit, None) == "aucun ticket en cours"


def test_ticket_qui_va_expirer_est_repris_a_toute_heure(tmp_path, client, state, server):
    """11h du matin, loin du rendez-vous : un ticket qui va finir est repris."""
    runner = build(tmp_path, client, state)
    matin = datetime(2026, 3, 12, 11, 0, tzinfo=PARIS)
    presque_fini = ticket(matin + timedelta(minutes=10))  # < marge de 25 min
    assert why(runner, matin, presque_fini) == "expire dans 10min"


def test_ticket_confortable_ne_declenche_rien(tmp_path, client, state, server):
    runner = build(tmp_path, client, state)
    midi = datetime.now(PARIS).replace(hour=12, minute=0, second=0, microsecond=0)
    tranquille = ticket(utcnow() + timedelta(hours=8))
    assert why(runner, midi, tranquille) is None


# ------------------------------------------------------ rendez-vous quotidien


def test_le_rendez_vous_de_20h01_reprend_le_ticket(tmp_path, client, state, server):
    runner = build(tmp_path, client, state)
    soir = datetime(2026, 3, 12, 20, 1, tzinfo=PARIS)
    # ticket qui expire ce soir : il ne tient pas jusqu'au rendez-vous de demain
    assert why(runner, soir, ticket(soir + timedelta(minutes=30))) == "rendez-vous de 20:01"


def test_le_rendez_vous_ne_se_declenche_quune_fois(tmp_path, client, state, server):
    runner = build(tmp_path, client, state)
    soir = datetime(2026, 3, 12, 20, 1, tzinfo=PARIS)
    # ticket valide jusqu'à demain 19h : confortable, mais il ne tient pas
    # jusqu'au rendez-vous de demain 20h01
    presque = ticket(soir + timedelta(hours=23))

    assert why(runner, soir, presque) == "rendez-vous de 20:01"
    assert why(runner, soir + timedelta(minutes=30), presque) is None  # passage suivant
    assert why(runner, soir + timedelta(hours=2), presque) is None

    # …mais le lendemain, le rendez-vous a de nouveau lieu
    demain = soir + timedelta(days=1)
    assert why(runner, demain, ticket(demain + timedelta(hours=23))) == "rendez-vous de 20:01"


def test_pas_de_rendez_vous_avant_lheure(tmp_path, client, state, server):
    runner = build(tmp_path, client, state)
    avant = datetime(2026, 3, 12, 19, 31, tzinfo=PARIS)
    assert why(runner, avant, ticket(avant + timedelta(hours=1))) is None


def test_ticket_qui_tient_jusquau_prochain_rendez_vous(tmp_path, client, state, server):
    """Après le renouvellement du soir, les passages suivants ne font rien."""
    runner = build(tmp_path, client, state)
    soir = datetime(2026, 3, 12, 20, 1, tzinfo=PARIS)
    frais = ticket(soir + timedelta(hours=24))
    assert why(runner, soir + timedelta(minutes=30), frais) is None
    assert why(runner, soir + timedelta(hours=3), frais) is None


def test_la_simulation_ne_consomme_pas_le_rendez_vous(tmp_path, client, state, server):
    soir = datetime(2026, 3, 12, 20, 1, tzinfo=PARIS)
    court = ticket(soir + timedelta(minutes=30))

    simulation = build(tmp_path, client, state, dry_run=True)
    assert why(simulation, soir, court) == "rendez-vous de 20:01"

    reel = build(tmp_path, client, state)
    assert why(reel, soir, court) == "rendez-vous de 20:01"  # toujours à faire


# ------------------------------------------------------------- bout en bout


def test_couverture_retablie_puis_stable(tmp_path, client, state, server):
    runner = build(tmp_path, client, state)

    premier = runner.tick()  # aucun ticket : on couvre tout de suite
    assert [r.status for r in premier.results] == [PURCHASED]
    assert "aucun ticket en cours" in premier.results[0].message
    assert len(server.active()) == 1

    second = runner.tick()  # déjà couvert : rien à faire
    assert [r.status for r in second.results] == [OK]
    assert len(server.purchases) == 1


def test_trou_de_couverture_rattrape_au_passage_suivant(tmp_path, client, state, server):
    runner = build(tmp_path, client, state)
    runner.tick()
    server.sessions.clear()  # ticket arrêté à la main, ou disparu

    report = runner.tick()
    assert report.results[0].status == PURCHASED
    assert len(server.active()) == 1
