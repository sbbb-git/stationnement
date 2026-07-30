"""Stationnement automatique pour mes propres véhicules, sur le modèle d'AlloValet."""

__version__ = "2.0.0"

from .config import Config, Rule  # noqa: F401
from .errors import AlloValetError, ApiError, AuthError, ConfigError, NotEligibleError  # noqa: F401
from .models import ParkingSession, Quote, RateOption, Vehicle  # noqa: F401
from .paybyphone import PayByPhoneClient  # noqa: F401
from .runner import Runner, TickReport  # noqa: F401
