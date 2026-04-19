"""
sav_client — automation client for the FPB SA2.0 (SAV2) player registration system.
"""

from .exceptions import (
    SavAuthError,
    SavConfigError,
    SavConnectionError,
    SavError,
    SavResponseError,
)
from .models import Player, Club, Game, LoginResult, Session
from .sav_client import SavClient

__all__ = [
    "SavClient",
    # models
    "Session",
    "LoginResult",
    "Player",
    "Club",
    "Game",
    # exceptions
    "SavError",
    "SavConfigError",
    "SavConnectionError",
    "SavAuthError",
    "SavResponseError",
]
