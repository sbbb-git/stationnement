"""Les zones de repli : « si 75008 refuse, alors 75007, sinon 75006… ».

L'invariant reste le même — toujours un ticket actif — mais il porte
désormais sur un **secteur** entier plutôt que sur une seule zone. Ce qui se
vérifie ici : on descend la liste jusqu'à ce qu'une zone accepte, un ticket
sur n'importe laquelle du secteur compte comme couverture, et on n'en prend
jamais deux pour une même règle.
"""

from allovalet.notify import Notifier
from allovalet.runner import BLOCKED, OK, PURCHASED, Runner
from tests.conftest import make_config
from tests.fake_pbp import PLATE

# 75008 n'a pas de tarif CMI sur le faux serveur ; 75016 et 75017 en ont un.
SECTEUR = f"""
provider: paybyphone
timezone: Europe/Paris
renew_margin_minutes: 25
rules:
  - name: secteur ouest
    plate: {PLATE}
    zones: ["75008", "75016", "75017"]
    rate: CMI
    duration: 24h
    max_cost_per_ticket: 0
"""


def build(tmp_path, client, state, body=SECTEUR, dry_run=False):
    cfg = make_config(tmp_path, body)
    return Runner(cfg, client, state, Notifier(cfg.notify), dry_run=dry_run)


def test_replie_sur_la_zone_suivante_quand_la_premiere_refuse(
    tmp_path, client, state, server
):
    report = build(tmp_path, client, state).tick()

    assert [r.status for r in report.results] == [PURCHASED]
    assert [s["locationId"] for s in server.active()] == ["75016"]
    # le message doit dire pourquoi on n'est pas sur la zone habituelle
    assert "zone 75016" in report.results[0].message
    assert "repli" in report.results[0].message and "75008" in report.results[0].message


def test_un_ticket_sur_un_repli_couvre_la_regle(tmp_path, client, state, server):
    """Le point capital : ne pas racheter parce que la zone préférée est vide."""
    server.add_session(minutes=600, location="75016")

    report = build(tmp_path, client, state).tick()

    assert [r.status for r in report.results] == [OK]
    assert "par la zone 75016" in report.results[0].message
    assert server.purchases == []


def test_un_seul_ticket_par_regle_meme_apres_un_repli(tmp_path, client, state, server):
    runner = build(tmp_path, client, state)

    runner.tick()
    runner.tick()

    assert len(server.active()) == 1
    assert len(server.purchases) == 1


def test_zone_deja_occupee_ne_fait_pas_prendre_un_second_ticket(
    tmp_path, client, state, server
):
    """Un « véhicule déjà stationné » veut dire couvert, pas « essaie ailleurs ».

    Sans la relecture du compte après un refus, la règle irait acheter sur la
    zone suivante alors qu'elle est déjà couverte — exactement le ticket
    superflu qu'on ne veut pas.
    """
    server.reject_duplicate = True
    server.add_session(minutes=600, location="75016", renewable=False)
    runner = build(tmp_path, client, state)
    regle = runner.cfg.rules[0]

    resultat = runner._take_ticket(regle, "rendez-vous du soir")

    assert resultat.status == OK
    assert [s["locationId"] for s in server.active()] == ["75016"]


def test_quand_tout_le_secteur_refuse_la_regle_est_bloquee(tmp_path, client, state, server):
    body = SECTEUR.replace('zones: ["75008", "75016", "75017"]', 'zones: ["75008", "99999"]')

    report = build(tmp_path, client, state, body).tick()

    assert [r.status for r in report.results] == [BLOCKED]
    message = report.results[0].message
    assert "aucune des 2 zones" in message
    assert "75008" in message and "99999" in message  # le détail de chaque refus


def test_la_simulation_annonce_la_zone_de_repli(tmp_path, client, state, server):
    report = build(tmp_path, client, state, dry_run=True).tick()

    assert report.results[0].status == "simulé"
    assert "zone 75016" in report.results[0].message
    assert server.purchases == []


def test_a_egalite_cest_la_zone_preferee_qui_est_citee(tmp_path, client, state, server):
    """Plusieurs tickets couvrent le secteur : c'est la zone voulue qu'on nomme."""
    server.add_session(minutes=600, location="75017")
    server.add_session(minutes=600, location="75016")
    body = SECTEUR.replace('zones: ["75008", "75016", "75017"]', 'zones: ["75016", "75017"]')

    report = build(tmp_path, client, state, body).tick()

    assert report.results[0].status == OK
    assert "75017" not in report.results[0].message


def test_un_repli_qui_dure_plus_longtemps_lemporte(tmp_path, client, state, server):
    """La décision suit la couverture réelle, pas la préférence : sinon on
    renouvellerait alors que le secteur tient encore des heures."""
    server.add_session(minutes=30, location="75016")   # la préférée, presque finie
    server.add_session(minutes=600, location="75017")  # un repli, confortable
    body = SECTEUR.replace('zones: ["75008", "75016", "75017"]', 'zones: ["75016", "75017"]')

    report = build(tmp_path, client, state, body).tick()

    assert report.results[0].status == OK
    assert "par la zone 75017" in report.results[0].message
    assert server.purchases == []
