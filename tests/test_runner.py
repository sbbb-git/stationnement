"""Bout en bout : config → règles → achat réel sur le faux serveur."""

from datetime import timedelta

from allovalet.notify import Notifier
from allovalet.runner import BLOCKED, FAILED, OK, PURCHASED, SKIPPED, Runner
from tests.conftest import make_config
from tests.fake_pbp import PLATE

CMI_24H = f"""
provider: paybyphone
timezone: Europe/Paris
renew_margin_minutes: 20
rules:
  - name: 16e CMI
    plate: {PLATE}
    location: "75016"
    rate: CMI
    mode: renew
    duration: 24h
    max_cost_per_ticket: 0
"""


def build(tmp_path, client, state, body=CMI_24H, dry_run=False):
    cfg = make_config(tmp_path, body)
    return Runner(cfg, client, state, Notifier(cfg.notify), dry_run=dry_run)


def test_prend_le_ticket_puis_ne_le_reprend_pas(tmp_path, client, state, server):
    runner = build(tmp_path, client, state)

    first = runner.tick()
    assert [r.status for r in first.results] == [PURCHASED]
    assert len(server.active()) == 1

    second = runner.tick()
    assert [r.status for r in second.results] == [OK]
    assert len(server.purchases) == 1  # aucun achat en double


def test_renouvelle_quand_le_ticket_expire(tmp_path, client, state, server):
    """L'API dit `isRenewable` : on renouvelle la session, on n'en empile pas une 2e."""
    session = server.add_session(minutes=10)  # moins que la marge de 20 min
    runner = build(tmp_path, client, state)

    report = runner.tick()
    assert [r.status for r in report.results] == [PURCHASED]
    assert len(server.active()) == 1
    assert server.sessions[0]["parkingSessionId"] == session["parkingSessionId"]
    assert report.results[0].session.remaining > timedelta(hours=20)
    assert "renewParkingSessionV1" in server.operations


def test_hors_creneau_ne_fait_rien(tmp_path, client, state, server):
    body = CMI_24H.replace(
        "    max_cost_per_ticket: 0",
        "    max_cost_per_ticket: 0\n    window:\n      from: \"03:00\"\n      to: \"03:30\"",
    )
    runner = build(tmp_path, client, state, body)
    report = runner.tick()
    assert [r.status for r in report.results] == [SKIPPED]
    assert server.purchases == []


def test_dry_run_nachete_rien(tmp_path, client, state, server):
    runner = build(tmp_path, client, state, dry_run=True)
    report = runner.tick()
    assert report.results[0].status == "simulé"
    assert server.purchases == []


def test_plafond_bloque_un_tarif_payant(tmp_path, client, state, server):
    body = CMI_24H.replace("rate: CMI", "rate: VIS").replace("duration: 24h", "duration: 2h")
    runner = build(tmp_path, client, state, body)

    report = runner.tick()
    assert report.results[0].status == BLOCKED
    assert "plafond" in report.results[0].message
    assert server.purchases == []


def test_plafond_journalier(tmp_path, client, state, server):
    body = CMI_24H.replace("rate: CMI", "rate: VIS") \
                  .replace("duration: 24h", "duration: 1h") \
                  .replace("max_cost_per_ticket: 0", "max_cost_per_day: 10")
    runner = build(tmp_path, client, state, body)

    assert runner.tick().results[0].status == PURCHASED  # 6 €
    server.sessions.clear()  # le ticket expire
    result = runner.tick().results[0]  # 6 € + 6 € > 10 €
    assert result.status == BLOCKED
    assert "aujourd'hui" in result.message


def test_smartpark_decoupe_au_lieu_dun_gros_ticket(tmp_path, client, state, server):
    body = f"""
provider: paybyphone
timezone: Europe/Paris
rules:
  - name: 8e SmartPark
    plate: {PLATE}
    location: "75008"
    rate: VIS
    mode: smartpark
    min_chunk_minutes: 30
    max_cost_per_ticket: 20
    window:
      from: "00:00"
      to: "23:59"
"""
    runner = build(tmp_path, client, state, body)
    report = runner.tick()

    assert report.results[0].status == PURCHASED
    assert server.purchases[0]["minutes"] <= 120  # jamais le bloc de 6 h à 75 €
    assert report.results[0].cost <= 12.0
    assert "SmartPark" in report.results[0].message


def test_achat_fantome_remonte_en_echec(tmp_path, client, state, server):
    server.swallow_purchases = True
    runner = build(tmp_path, client, state)
    report = runner.tick()

    assert report.results[0].status == FAILED
    assert "non confirmé" in report.results[0].message
    assert report.failures


def test_repli_sur_la_prolongation_si_doublon_refuse(tmp_path, client, state, server):
    session = server.add_session(minutes=10)
    server.reject_duplicate = True
    runner = build(tmp_path, client, state)

    report = runner.tick()
    assert report.results[0].status == PURCHASED
    assert len(server.active()) == 1  # prolongé, pas dupliqué
    assert session["expireTime"] == server.sessions[0]["expireTime"]
    assert report.results[0].session.remaining > timedelta(hours=20)


def test_une_regle_en_echec_nempeche_pas_lautre(tmp_path, client, state, server):
    body = CMI_24H + f"""
  - name: zone inexistante
    plate: {PLATE}
    location: "99999"
    rate: CMI
    mode: renew
    duration: 1h
"""
    runner = build(tmp_path, client, state, body)
    report = runner.tick()

    statuses = {r.rule: r.status for r in report.results}
    assert statuses["16e CMI"] == PURCHASED
    assert statuses["zone inexistante"] == BLOCKED
