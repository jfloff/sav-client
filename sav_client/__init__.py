"""
sav_client — automation client for the FPB SA2.0 (SAV2) player registration system.
"""

from .exceptions import (
    LicenseNotEnrolledError,
    SavAuthError,
    SavConfigError,
    SavConnectionError,
    SavError,
    SavRecordNotFoundError,
    SavResponseError,
)
from .models import Coach, Player, Club, Game, LoginResult, PlayerRegistrationBatch, Session
from .sav_client import SavClient

__all__ = [
    "SavClient",
    # models
    "Session",
    "LoginResult",
    "Player",
    "Coach",
    "Club",
    "Game",
    "PlayerRegistrationBatch",
    # exceptions
    "SavError",
    "SavConfigError",
    "SavConnectionError",
    "SavAuthError",
    "SavResponseError",
    "SavRecordNotFoundError",
    "LicenseNotEnrolledError",
]
