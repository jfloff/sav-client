"""Shared decoding helpers for SAV boolean flags."""

from __future__ import annotations

from typing import Any

# `SavResponseError` is imported lazily, inside the function, on purpose.
# `sav_client.exceptions` cannot be reached without executing
# `sav_client/__init__.py`, which imports `sav_client.sav_client`, which
# imports this module — so importing it at module scope makes
# `import sav_shared.flags` fail outright whenever a consumer's first import
# is a `sav_shared.*` module rather than `sav_client`. That bricked every
# drive-to-sav command in 0.107.0. The exception is only needed at raise
# time, so binding it there costs nothing and keeps this module a leaf.


def decode_sav_flag(
  value: Any, *, field: str, absent_is: bool | None = None,
) -> bool:
  """Decode one of SAV's boolean flags without Python truthiness.

  SAV is inconsistent about how it encodes these: op=31 returns
  ``menor_idade`` as the int ``1`` while its consent flags come back as the
  strings ``'1'`` / ``'0'`` / ``None``. ``bool('0')`` is ``True``, so reading
  any of them with plain truthiness inverts a false value.

  For ``menor_idade`` that inversion has a direction that matters: an adult
  decoded as a minor is blocked from enrolling until guardian details are
  invented for them, and a minor decoded as an adult walks straight past the
  guardian check that exists to stop exactly that (see the LOAD-BEARING note
  in ``_commit_registration_step3``). An unrecognised encoding therefore
  raises rather than guessing in either direction.

  ``absent_is`` decides what a *missing* value means, and defaults to ``None``
  — meaning absence raises too. Treating "SAV didn't say" as "no" would
  silently disable whatever the flag gates, which for ``menor_idade`` means
  skipping the guardian check entirely. Pass ``absent_is=False`` only where
  the field is genuinely optional.

  Args:
      value:      The raw flag as SAV sent it.
      field:      Field name to name in the error.
      absent_is:  Result for ``None``; ``None`` itself means raise instead.

  Raises:
      SavResponseError: The value is absent with no ``absent_is`` given, or is
          encoded in a way we do not recognise.
  """
  from sav_client.exceptions import SavResponseError

  if isinstance(value, bool):
    return value
  if value is None:
    if absent_is is None:
      raise SavResponseError(
        f"SAV did not return {field!r}; refusing to assume a default for a "
        f"flag that gates a validation check."
      )
    return absent_is
  if isinstance(value, int) and value in (0, 1):
    return bool(value)
  if isinstance(value, str):
    normalized = value.strip().casefold()
    if normalized in ("1", "true", "sim"):
      return True
    if normalized in ("", "0", "false", "nao", "não"):
      return False
  raise SavResponseError(
    f"SAV returned an unrecognised value for {field!r}; refusing to guess "
    f"whether it means true or false."
  )
