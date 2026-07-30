import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from allovalet import paybyphone  # noqa: E402
from allovalet.config import Config  # noqa: E402
from allovalet.paybyphone import PayByPhoneClient  # noqa: E402
from allovalet.state import State  # noqa: E402
from tests.fake_pbp import FakePayByPhone  # noqa: E402


@pytest.fixture
def server(monkeypatch):
    fake = FakePayByPhone()
    base = fake.start()
    monkeypatch.setattr(paybyphone, "API_BASE", base)
    monkeypatch.setattr(paybyphone, "AUTH_URL", f"{base}/token")
    monkeypatch.setattr(paybyphone.time, "sleep", lambda *_: None)
    yield fake
    fake.stop()


@pytest.fixture
def client(server):
    return PayByPhoneClient(username="+33600000000", password="secret")


@pytest.fixture
def state(tmp_path):
    return State(tmp_path / "state.json")


def make_config(tmp_path, body: str) -> Config:
    path = tmp_path / "config.yml"
    path.write_text(body, encoding="utf-8")
    return Config.load(path)
