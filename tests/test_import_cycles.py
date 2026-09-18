"""Every public module must import cleanly as a consumer's *first* import.

0.107.0 shipped a circular import that bricked every drive-to-sav command.
`sav_client/sav_client.py` gained a module-scope `from sav_shared.enrollment
import ...`, while `sav_shared/enrollment.py` imports `sav_client.exceptions`
— which cannot be reached without executing `sav_client/__init__.py`, which
imports `sav_client.sav_client`. Importing `sav_client` first happened to
work, so the whole suite stayed green; a consumer whose first import was
`sav_shared.scan`-style module died on a partially initialised module.

The entire suite imports `sav_client` first (conftest does), so no existing
test could have caught it. These run each module as the first import in a
fresh interpreter, which is the only way to see the failure.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every module a consumer might reasonably import first. sav_shared is the
# interesting half — those are the ones that import back into sav_client.
MODULES = [
  "sav_shared.clubs",
  "sav_shared.dates",
  "sav_shared.enrollment",
  "sav_shared.estatuto",
  "sav_shared.fields",
  "sav_shared.files",
  "sav_shared.flags",
  "sav_shared.fpb_mod1",
  "sav_shared.fpb_mod4",
  "sav_shared.games",
  "sav_shared.identifiers",
  "sav_shared.lookups",
  "sav_shared.medical_exam",
  "sav_shared.mod1_completion",
  "sav_shared.serializers",
  "sav_shared.text",
  "sav_client",
  "sav_client.exceptions",
  "sav_client.sav_client",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_imports_as_the_first_import(module):
  """A fresh interpreter importing only this module must succeed.

  `-I` isolates the interpreter so a stray PYTHONPATH or user site-packages
  cannot mask a cycle by having imported something else first.
  """
  result = subprocess.run(
    [sys.executable, "-I", "-c", f"import {module}"],
    cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
  )

  assert result.returncode == 0, (
    f"`import {module}` fails as a first import:\n{result.stderr}"
  )


def test_decode_sav_flag_still_raises_the_client_exception():
  """The lazy import in sav_shared.flags must not change the exception type.

  `SavResponseError` is imported inside `decode_sav_flag` to keep that module
  a leaf. Callers catch the client's exception, so a look-alike raised from
  elsewhere would be a silent break of every `except SavResponseError`.
  """
  from sav_client.exceptions import SavResponseError
  from sav_shared.flags import decode_sav_flag

  with pytest.raises(SavResponseError):
    decode_sav_flag("maybe", field="menor_idade")
