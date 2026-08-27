"""
Exceptions for SavClient.

Hierarchy:
    SavError                  — base for all SAV errors
    ├── SavConfigError        — bad or missing configuration
    ├── SavConnectionError    — network / HTTP transport failure
    ├── SavAuthError          — login rejected by the server
    └── SavResponseError      — server returned an unexpected response shape
        └── SavServerError    — SAV2 returned an unhandled server-side error
                                (a PHP fatal) with HTTP 200; the raw body is
                                withheld from the message and logged at DEBUG

    LicenseNotEnrolledError   — license is not in any open registration batch
                                (subclasses ValueError so existing
                                `except ValueError` handlers still catch it).
"""

from typing import Any


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


class SavServerError(SavResponseError):
    """Raised when SAV2 answers with an unhandled server-side error (a PHP
    fatal) under HTTP 200.

    SAV2 reports these fatals with a 200 status, so ``raise_for_status``
    passes and the failure only surfaces when the body cannot be parsed as
    the expected JSON. The raw body is deliberately withheld from the message
    because it carries SAV's internal schema (table and constraint names),
    which must not propagate into callers' logs and databases; the full body
    is logged at DEBUG instead.

    Subclasses ``SavResponseError`` so every existing
    ``except SavResponseError`` handler keeps working.
    """


class SavRecordNotFoundError(SavResponseError):
    """Raised when the server responds successfully but the requested record is absent.

    Distinct from a parse/shape failure: the response *is* well-formed, the
    record just isn't there (e.g. asking for a player in a batch they're
    no longer in). Callers can catch this specifically to distinguish
    "the thing doesn't exist" from "the server is broken or the wire was
    corrupted."
    """


class LicenseNotEnrolledError(ValueError):
    """Raised when a license cannot be resolved to any open registration batch.

    Carries the list of open batches at the time of the lookup, so callers
    (CLI, MCP) can render a useful hint without re-fetching.

    Attributes:
        license:      The license that was looked up.
        open_batches: A list of dicts shaped as
                      ``{"number": str, "tier": str, "gender": str}``,
                      one per currently-open batch.
    """

    def __init__(self, license: int, open_batches: list[dict[str, Any]]) -> None:
        self.license = license
        self.open_batches = open_batches
        if open_batches:
            hint = ", ".join(
                f"{b['number']} ({b.get('tier', '?')}/{b.get('gender', '?')})"
                for b in open_batches
            )
            msg = (
                f"Licence {license} is not enrolled in any open batch. "
                f"Open batches: {hint}."
            )
        else:
            msg = (
                f"Licence {license} is not enrolled in any open batch. "
                f"No open batches exist."
            )
        super().__init__(msg)
