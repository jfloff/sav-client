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
    SavServerError,
    SavWriteUnverifiedError,
)
from .models import (
    Coach, Player, Club, Game, LoginResult, PlayerRegistrationBatch, Season, Session,
    SubidaStatus,
)
from .sav_client import SavClient

__all__ = [
    "SavClient",
    # models
    "Session",
    "Season",
    "LoginResult",
    "Player",
    "SubidaStatus",
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
    "SavServerError",
    "SavWriteUnverifiedError",
    "SavRecordNotFoundError",
    "LicenseNotEnrolledError",
]
