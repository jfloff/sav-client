"""
Exceptions for SavClient.

Hierarchy:
    SavError                  — base for all SAV errors
    ├── SavConfigError        — bad or missing configuration
    ├── SavConnectionError    — network / HTTP transport failure
    ├── SavAuthError          — login rejected by the server
    └── SavResponseError      — server returned an unexpected response shape
"""


class SavError(Exception):
    """Base exception for all SAV client errors."""


class SavConfigError(SavError):
    """Raised when required configuration is missing or invalid."""


class SavConnectionError(SavError):
    """Raised when a network or HTTP-level error prevents a request from completing."""


class SavAuthError(SavError):
    """Raised when the server explicitly rejects the login credentials."""


class SavResponseError(SavError):
    """Raised when the server returns a response that cannot be parsed or is missing expected fields."""
