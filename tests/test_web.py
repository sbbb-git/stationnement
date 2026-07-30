"""Tableau de bord : chaque route doit fonctionner contre le faux serveur."""

import json

import pytest
import requests

from allovalet.web import Dashboard, PAGE, _make_handler
from tests.fake_pbp import PLATE

CONFIG = f"""
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


@pytest.fixture
def dash(tmp_path, server, monkeypatch):
    config = tmp_path / "config.yml"
    config.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("PBP_USERNAME", "+33600000000")
    monkeypatch.setenv("PBP_PASSWORD", "secret")
    monkeypatch.setenv("ALLOVALET_STATE", str(tmp_path / "state.json"))
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    return Dashboard(str(config))


@pytest.fixture
def web(dash):
    from http.server import ThreadingHTTPServer
    import threading

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(dash))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_snapshot_regle_a_prendre(dash):
    snap = dash.snapshot()
    assert snap["error"] is None
    assert snap["sessions"] == []
    assert snap["rules"][0]["status"] == "due"


def test_snapshot_regle_couverte(dash, server):
    server.add_session(minutes=180)
    snap = dash.snapshot()
    assert snap["rules"][0]["status"] == "covered"
    assert snap["sessions"][0]["plate"] == PLATE


def test_pause_depuis_le_tableau_de_bord(dash, server):
    assert dash.toggle("16e CMI") == {"name": "16e CMI", "enabled": False}
    assert dash.snapshot()["rules"][0]["status"] == "off"

    dash.run()  # la règle en pause ne doit rien acheter
    assert server.purchases == []

    dash.toggle("16e CMI")
    dash.run()
    assert len(server.purchases) == 1


def test_run_depuis_le_tableau_de_bord(dash, server):
    result = dash.run(dry_run=True)
    assert server.purchases == []
    assert "achèterait" in result["lines"][0]

    result = dash.run()
    assert result["purchases"] == 1
    assert len(server.active()) == 1
    assert dash.snapshot()["journal"]  # le passage est journalisé


def test_ticket_manuel(dash, server):
    result = dash.park("75016", "2h", "VIS", PLATE)
    assert result["cost"] == 12.0
    assert len(server.active()) == 1
    assert dash.snapshot()["spend"]["total"] == 12.0


def test_simulation_et_economies(dash, server):
    plan = dash.plan("75008", 360, "VIS", PLATE)
    assert plan["cost"] == 36.0
    assert plan["single"] == 75.0
    assert plan["savingsPct"] == 52.0
    assert plan["chunks"] == [120, 120, 120]


def test_economies_creditees_une_seule_fois(tmp_path, dash, server):
    body = CONFIG.replace("rate: CMI", "rate: VIS") \
                 .replace("mode: renew", "mode: smartpark") \
                 .replace("    duration: 24h\n", "") \
                 .replace("max_cost_per_ticket: 0", "max_cost_per_ticket: 20") \
                 .replace('location: "75016"', 'location: "75008"')
    (tmp_path / "config.yml").write_text(body, encoding="utf-8")

    dash.run()
    total = dash.snapshot()["savings"]["total"]
    assert total > 0

    server.sessions.clear()  # le morceau expire, on rachète dans le même créneau
    dash.run()
    assert dash.snapshot()["savings"]["total"] == total  # pas de double comptage


def test_routes_http(web, server):
    server.add_session(minutes=60)

    page = requests.get(web + "/", timeout=10)
    assert page.status_code == 200
    assert "AlloValet perso" in page.text

    state = requests.get(web + "/api/state", timeout=10).json()
    assert state["sessions"][0]["plate"] == PLATE

    plan = requests.get(web + "/api/plan", params={"zone": "75008", "minutes": 360,
                                                   "rate": "VIS"}, timeout=30).json()
    assert plan["savings"] > 0

    run = requests.post(web + "/api/run", json={"dryRun": True}, timeout=30).json()
    assert run["purchases"] == 0

    assert requests.get(web + "/api/inconnu", timeout=10).status_code == 404


def test_erreur_renvoyee_proprement(web):
    resp = requests.post(web + "/api/park",
                         json={"zone": "99999", "duration": "1h"}, timeout=30)
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_la_page_est_autonome():
    assert "<script" in PAGE and "http://" not in PAGE.split("</style>")[0]
    assert "prefers-color-scheme: dark" in PAGE  # lisible en thème sombre
    assert json.dumps(PAGE)  # aucun caractère qui casserait le service
