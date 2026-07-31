"""L'interface : consulter l'état, et modifier les automatisations sans casser.

Les tests parlent au vrai serveur HTTP de l'interface, avec le faux PayByPhone
derrière : ce qui est vérifié, c'est ce qu'un navigateur obtiendrait.
"""

import json
import threading
import urllib.request

import pytest

from allovalet.config import Config
from allovalet.etat import markdown, snapshot
from allovalet.notify import Notifier
from allovalet.runner import Runner
from allovalet.ui import _handler
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests.fake_pbp import PLATE

CONFIG = f"""
provider: paybyphone
timezone: Europe/Paris
renew_margin_minutes: 25
rules:
  - name: secteur ouest
    plate: {PLATE}
    zones: ["75008", "75016"]
    rate: CMI
    duration: 24h
    renew_at: "20:01"
    max_cost_per_ticket: 0
"""


@pytest.fixture
def config_path(tmp_path, monkeypatch, server):
    chemin = tmp_path / "config.yml"
    chemin.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("PBP_USERNAME", "+33600000000")
    monkeypatch.setenv("PBP_PASSWORD", "secret")
    monkeypatch.setenv("ALLOVALET_STATE", str(tmp_path / "state.json"))
    return chemin


@pytest.fixture
def site(config_path):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(Path(config_path)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def appel(chemin, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        with urllib.request.urlopen(f"{base}{chemin}", data=data, timeout=10) as rep:
            corps = rep.read().decode()
        return json.loads(corps) if chemin.startswith("/api") else corps

    yield appel
    httpd.shutdown()
    httpd.server_close()


# ------------------------------------------------------------------ consulter


def test_la_page_saffiche(site):
    page = site("/")
    assert "Stationnement" in page and "Automatisations" in page


def test_letat_dit_quelle_zone_couvre(site, server):
    server.add_session(minutes=300, location="75016")

    vue = site("/api/etat")

    regle = vue["regles"][0]
    assert regle["couvert"] is True
    assert regle["zone_couvrante"] == "75016"
    assert regle["sur_la_preferee"] is False  # la préférée est le 75008
    assert regle["zones"] == ["75008", "75016"]
    assert 290 <= regle["reste_minutes"] <= 300
    assert vue["tickets"][0]["plaque"] == PLATE


def test_letat_signale_une_regle_decouverte(site):
    regle = site("/api/etat")["regles"][0]
    assert regle["couvert"] is False
    assert regle["action"] == "aucun ticket en cours"


# ------------------------------------------------------------------- modifier


def test_enregistrer_une_config_valide(site, config_path):
    texte = CONFIG.replace('renew_at: "20:01"', 'renew_at: "19:30"')

    rep = site("/api/config", {"texte": texte})

    assert rep["ok"] is True
    assert Config.load(config_path).rules[0].renew_at == "19:30"


def test_une_config_cassee_nest_jamais_ecrite(site, config_path):
    avant = config_path.read_text(encoding="utf-8")

    casse = site("/api/config", {"texte": "rules: [ceci n'est pas une règle"})
    vide = site("/api/config", {"texte": "rules: []"})

    assert casse["ok"] is False and vide["ok"] is False
    assert config_path.read_text(encoding="utf-8") == avant  # intact


def test_aucun_fichier_de_verification_ne_traine(site, config_path):
    site("/api/config", {"texte": "rules: []"})
    assert not list(config_path.parent.glob("*.verif.yml"))


# -------------------------------------------------------------------- agir


def test_simuler_nachete_rien(site, server):
    rep = site("/api/passage", {"simulation": True})
    assert rep["ok"] is True
    assert server.purchases == []


def test_le_passage_reel_prend_le_ticket_sur_le_repli(site, server):
    rep = site("/api/passage", {"simulation": False})

    assert rep["ok"] is True
    assert [s["locationId"] for s in server.active()] == ["75016"]
    assert any("ticket pris" in ligne for ligne in rep["lignes"])


# ------------------------------------------------------- résumé pour Actions


def test_le_resume_markdown_dit_par_quelle_zone(tmp_path, client, server, config_path):
    server.add_session(minutes=120, location="75016")
    cfg = Config.load(config_path)

    texte = markdown(snapshot(cfg, client))

    assert "| secteur ouest |" in texte
    assert "75016" in texte and "↪️" in texte  # couvert par un repli, pas la préférée


def test_le_resume_reste_lisible_si_le_compte_est_injoignable(config_path):
    class Muet:
        def current_sessions(self):
            raise RuntimeError("réseau coupé")

    vue = snapshot(Config.load(config_path), Muet())

    assert vue["erreur"] == "réseau coupé"
    assert "réseau coupé" in markdown(vue)
    assert "| secteur ouest |" in markdown(vue)


def test_le_runner_et_linterface_disent_la_meme_chose(config_path, client, server):
    """L'interface ne doit pas avoir sa propre idée de ce qu'il faut faire."""
    cfg = Config.load(config_path)
    server.add_session(minutes=5, location="75016")  # sous la marge de 25 min

    vue = snapshot(cfg, client)
    rapport = Runner(cfg, client, notifier=Notifier(cfg.notify), dry_run=True).tick()

    assert vue["regles"][0]["action"].startswith("expire dans")
    assert vue["regles"][0]["action"] in rapport.results[0].message


def test_la_config_livree_se_lit_dans_linterface(client, server):
    """Le fichier réellement utilisé, pas un exemple."""
    vue = snapshot(Config.load(Path(__file__).resolve().parents[1] / "config.yml"), client)

    assert [r["preferee"] for r in vue["regles"]] == ["75008", "75016"]
    assert [len(r["zones"]) for r in vue["regles"]] == [11, 9]
    assert all(r["rendez_vous"] == "20:01" for r in vue["regles"])
