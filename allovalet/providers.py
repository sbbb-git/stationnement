"""Fabrique le client du fournisseur configuré."""

from __future__ import annotations

import os

from .config import Config
from .easypark import EasyParkClient
from .errors import AuthError
from .models import parse_dt
from .paybyphone import PayByPhoneClient
from .state import State


def build_client(cfg: Config, state: State | None = None):
    state = state or State()

    if cfg.provider == "paybyphone":
        cached = state.tokens("paybyphone")
        client = PayByPhoneClient(
            username=os.getenv("PBP_USERNAME") or os.getenv("PBP_LOGIN"),
            password=os.getenv("PBP_PASSWORD"),
            refresh_token=os.getenv("PBP_REFRESH_TOKEN") or cached.get("refresh_token"),
            access_token=cached.get("access_token"),
            expires_at=parse_dt(cached.get("expires_at")),
            on_token_refresh=lambda tokens: state.set_tokens("paybyphone", tokens),
        )
        if not (client.username and client.password) and not client.refresh_token:
            raise AuthError(
                "Identifiants PayByPhone absents.\n"
                "Définis PBP_USERNAME (numéro de téléphone ou email) et PBP_PASSWORD "
                "dans .env (local) ou dans les secrets GitHub Actions."
            )
        return client

    if cfg.provider == "easypark":
        return EasyParkClient(
            id_token=os.getenv("EP_ID_TOKEN", ""),
            parking_user_id=os.getenv("EP_PARKING_USER_ID", ""),
            country=cfg.country,
        )

    raise AuthError(f"Fournisseur inconnu : {cfg.provider}")
