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
    mode: renew
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


def test_vehicles_et_rates(env, capsys):
    assert main(env + ["vehicles"]) == 0
    assert main(env + ["rates", "--zone", "75016"]) == 0
    out = capsys.readouterr().out
    assert PLATE in out
    assert "Visiteur" in out


def test_quote(env, capsys):
    assert main(env + ["quote", "--zone", "75016", "--duration", "2h", "--rate", "VIS"]) == 0
    assert "12.00 EUR" in capsys.readouterr().out


def test_plan_smartpark(env, capsys):
    assert main(env + ["plan", "--zone", "75008", "--duration", "6h", "--rate", "VIS"]) == 0
    out = capsys.readouterr().out
    assert "36.00" in out and "75.00" in out
    assert "-52 %" in out


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
