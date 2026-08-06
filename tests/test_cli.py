"""Les commandes doivent toutes fonctionner de bout en bout (faux serveur)."""

import pytest

from allovalet.cli import main
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
    duration: 24h
    max_cost_per_ticket: 0
"""


@pytest.fixture
def env(tmp_path, server, monkeypatch):
    config = tmp_path / "config.yml"
    config.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("PBP_USERNAME", "+33600000000")
    monkeypatch.setenv("PBP_PASSWORD", "secret")
    monkeypatch.setenv("ALLOVALET_STATE", str(tmp_path / "state.json"))
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    return ["--config", str(config)]


def test_doctor(env, capsys):
    assert main(env + ["doctor"]) == 0
    out = capsys.readouterr().out
    assert "Tout est prêt" in out
    assert "CMI" in out


def test_status(env, capsys, server):
    server.add_session(minutes=90)
    assert main(env + ["status"]) == 0
    out = capsys.readouterr().out
    assert PLATE in out
    assert "À PRENDRE" not in out  # couvert par le ticket en cours


def test_run_dry_puis_reel(env, capsys, server):
    assert main(env + ["run", "--dry-run"]) == 0
    assert server.purchases == []

    assert main(env + ["run"]) == 0
    assert len(server.active()) == 1
    assert "ticket pris" in capsys.readouterr().out


def test_park_manuel(env, capsys, server):
    code = main(env + ["park", "--zone", "75016", "--duration", "24h",
                       "--rate", "CMI", "--yes"])
    assert code == 0
    assert "Ticket confirmé" in capsys.readouterr().out
    assert len(server.active()) == 1


def test_run_signale_lechec(env, capsys, server):
    server.swallow_purchases = True
    assert main(env + ["run"]) == 1
    assert "non confirmé" in capsys.readouterr().out


def test_config_absente():
    assert main(["--config", "/introuvable.yml", "run"]) == 1

def test_rates_donne_le_libelle_a_mettre_dans_la_config(env, capsys):
    assert main(env + ["rates", "--zone", "75016"]) == 0
    out = capsys.readouterr().out
    assert "CMI" in out and "ratePolicyId" in out
    assert main(env + ["rates", "--zone", "99999"]) == 1


def test_schema_donne_la_forme_attendue(env, capsys):
    assert main(env + ["schema", "--type", "StartParkingSessionV1Input"]) == 0
    assert "request" in capsys.readouterr().out


def test_park_refuse_un_ticket_payant(env, capsys, server):
    """`--yes` veut dire « ne me demande pas », pas « à n'importe quel prix »."""
    code = main(env + ["park", "--zone", "75016", "--duration", "2h",
                       "--rate", "VIS", "--yes"])

    assert code == 1
    assert "dépasse le plafond" in capsys.readouterr().out
    assert server.active() == []


def test_park_prend_le_tarif_de_la_config_par_defaut(env, capsys, server):
    """Sans `--rate`, prendre le premier tarif venu achèterait du visiteur."""
    assert main(env + ["park", "--zone", "75016", "--duration", "24h", "--yes"]) == 0

    assert "Ticket confirmé" in capsys.readouterr().out
    assert len(server.active()) == 1
