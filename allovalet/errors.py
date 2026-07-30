"""Exceptions du projet."""


class AlloValetError(Exception):
    """Erreur générique."""


class ConfigError(AlloValetError):
    """Configuration invalide."""


class AuthError(AlloValetError):
    """Authentification impossible (identifiants, 2FA, token expiré)."""


class ApiError(AlloValetError):
    """Réponse inattendue d'une API de stationnement."""

    def __init__(self, message: str, status: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status = status
        self.body = body

    def __str__(self) -> str:
        base = super().__str__()
        if self.status is not None:
            base = f"{base} (HTTP {self.status})"
        if self.body:
            base = f"{base}\n↳ {self.body[:800]}"
        return base


class NotEligibleError(AlloValetError):
    """Le tarif demandé n'est pas disponible pour ce véhicule / cette zone."""
