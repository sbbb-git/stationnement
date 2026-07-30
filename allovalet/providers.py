"""Construction du client PayByPhone à partir de l'environnement.

Comme AlloValet, on se connecte au compte personnel de l'utilisateur avec ses
identifiants PayByPhone. Il n'y a pas d'autre porte d'entrée : pas d'API
partenaire, pas d'OAuth.
"""

from __future__ import annotations

import os

from .config import Config
from .errors import AuthError
from .models import parse_dt
from .paybyphone import PayByPhoneClient
from .state import State


def build_client(cfg: Config, state: State | None = None) -> PayByPhoneClient:
    state = state or State()
    cached = state.tokens("paybyphone")

    client = PayByPhoneClient(
        username=os.getenv("PBP_USERNAME") or os.getenv("PBP_LOGIN"),
        password=os.getenv("PBP_PASSWORD"),
        refresh_token=os.getenv("PBP_REFRESH_TOKEN") or cached.get("refresh_token"),
        access_token=cached.get("access_token"),
        expires_at=parse_dt(cached.get("expires_at")),
        on_token_refresh=lambda tokens: state.set_tokens("paybyphone", tokens),
        country=cfg.country,
        schema_cache=state.data.setdefault("schema", {}),
    )
    if not (client.username and client.password) and not client.refresh_token:
        raise AuthError(
            "Identifiants PayByPhone absents.\n"
            "Définis PBP_USERNAME (numéro avec indicatif « +336… » ou email) et "
            "PBP_PASSWORD dans .env en local, ou dans les secrets GitHub Actions."
        )
    return client
