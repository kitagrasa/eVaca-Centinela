"""Monitor de carteras públicas de eVaca con avisos por Telegram.

Solo usa los endpoints PÚBLICOS que carga la propia web al abrir la cartera de
alguien (`<destino>/people/<usuario>/portfolio`). La dirección del sitio no
vive en este repositorio, que es público: llega en el secreto DESTINO_BASE
(variable de entorno `EVACA_DESTINO_BASE`). No hay login, ni cookies, ni API
key, ni KYC.
"""

from .client import (
    Blocked,
    DestinoNoConfigurado,
    EvacaClient,
    EvacaError,
    PrivatePortfolio,
    UserNotFound,
)
from .cooldown import CooldownStore
from .models import Asset, Change, EvacaUser, Instrument, Snapshot
from .state import State, StateCorruptError

__version__ = "0.2.0"

__all__ = [
    "Asset",
    "Blocked",
    "Change",
    "CooldownStore",
    "DestinoNoConfigurado",
    "EvacaClient",
    "EvacaError",
    "EvacaUser",
    "Instrument",
    "PrivatePortfolio",
    "Snapshot",
    "State",
    "StateCorruptError",
    "UserNotFound",
]
