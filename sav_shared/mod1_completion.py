"""Shared Modelo 1 completion for SAV upload and CLI workflows.

This module owns the three overlays a club may add immediately before filing a
Modelo 1: the registration-type mark, a Revalidação licence, and the club
carimbo. Keeping the orchestration here prevents the MCP and CLI upload paths
from silently producing different federation records.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from .files import load_image_bytes, rect_has_overlay

if TYPE_CHECKING:
  from sav_parsers import ParsedField
  from .fpb_mod1 import OverlayResult

logger = logging.getLogger(__name__)


def read_mod1_template_fields(pdf_bytes: bytes) -> dict[str, "ParsedField"] | None:
  """Read a filled Modelo 1 template without classification or OCR.

  The returned entity-keyed fields match ``parse_fpb_mod1`` so completion and
  enrollment parsing can share the same overlay decisions. Scans and other
  PDFs return ``None`` and are left to the caller's OCR policy.
  """
  from .fpb_mod1 import (
    is_filled_mod1_template,
    mod1_acroform_to_fields,
    read_mod1_acroform,
  )

  raw = read_mod1_acroform(pdf_bytes)
  if raw is None or not is_filled_mod1_template(raw):
    return None
  return mod1_acroform_to_fields(raw)


def mod1_overlay_fields(tmp_path: str) -> tuple[dict[str, Any], str | None]:
  """Resolve Modelo 1 overlay fields, preferring a local template read.

  A template's carimbo slot is fixed and can be inspected locally; a scan
  needs Document AI to locate the carimbo and returns its processing session so
  the completion owner can close that session after the overlay is done.
  ``sav_parsers`` is imported only on this scan path to keep ``sav_shared``
  usable without a parser import at module load time.
  """
  from .fpb_mod1 import CLUB_STAMP_RECT

  with open(tmp_path, "rb") as f:
    pdf_bytes = f.read()
  fields = read_mod1_template_fields(pdf_bytes)
  if fields is not None:
    from sav_parsers.types import ParsedField

    fields["carimbo_clube_presente"] = ParsedField(
      value=rect_has_overlay(pdf_bytes, CLUB_STAMP_RECT),
      confidence=1.0,
    )
    return fields, None

  from sav_parsers import parse_fpb_mod1

  parse_result = parse_fpb_mod1(tmp_path)
  return parse_result["fields"], parse_result["processing_id"]


def _read_mod1_slot(
  reader: Callable[..., tuple[Any, Any]],
  parsed: dict | None,
  overlay_fields: dict[str, Any],
  *reader_args: Any,
) -> tuple[Any, Any]:
  """Read one slot, falling back to already-resolved overlay fields when safe.

  Enrollment fields can answer one slot while omitting another, so each
  question gets its own fallback. This is safe because both readers only
  inspect fields already in memory, and the fallback is capped at the
  ``overlay_fields`` produced by the existing carimbo path — no new OCR call is
  introduced. The value and bbox are always returned by the same reader call.
  """
  fields = parsed if parsed is not None else overlay_fields
  result = reader(fields, *reader_args)
  if (
    parsed is not None
    and result[0] is None
    and overlay_fields is not parsed
    and overlay_fields
  ):
    result = reader(overlay_fields, *reader_args)
  return result


@contextmanager
def mod1_completion_path(
  tmp_path: str,
  *,
  parsed: dict | None = None,
  processing_id: str | None = None,
  reg_type: int | None = None,
  license: int | str | None = None,
  dest_dir: str | os.PathLike[str] | None = None,
  allow_ocr_fallback: bool = True,
  reg_type_derived: bool = False,
  require_stamp: bool = False,
  degrade_on_lookup_error: bool = False,
  warning_action: str = "document uploaded",
) -> Iterator[tuple[str, dict[str, Any], list["OverlayResult"]]]:
  """Yield a Modelo 1 with the canonical registration, licence, and stamp overlays.

  ``parsed`` is reused when a caller already owns an OCR session. With
  ``allow_ocr_fallback=False`` no parser call is made: all slot decisions come
  from those fields, and ``processing_id`` remains caller-owned. When fallback
  is allowed and a scan needs OCR, this context records and closes only the
  session returned by its own ``mod1_overlay_fields`` call. ``dest_dir`` keeps
  an overlaid copy in a caller-owned lifecycle such as an OCR processing dir.

  The third yielded value is the raw OverlayResult list in application order:
  inscription, licence, carimbo. The status mapping is retained for MCP
  response construction, while callers such as the CLI can report each raw
  overlay result without duplicating the orchestration.
  """
  if require_stamp and not load_image_bytes(os.environ.get("CLUB_STAMP_PATH")):
    yield tmp_path, {}, []
    return

  from sav_parsers import close_processing
  from .fpb_mod1 import (
    CLUB_STAMP_RECT,
    carimbo_overlay,
    inscricao_overlay,
    licenca_overlay,
    overlaid_pdf,
    read_carimbo,
    read_licenca_fpb,
    read_tipo_inscricao,
  )

  opened_processing_id: str | None = None
  try:
    try:
      overlay_fields = parsed
      carimbo, carimbo_bbox = (
        read_carimbo(parsed) if parsed is not None else (None, None)
      )
      template_carimbo = False
      if carimbo is None:
        if parsed is not None and not allow_ocr_fallback:
          # The caller already paid for and owns this OCR session. Its parsed
          # fields are the complete source of truth in this mode; do not start
          # a second Document AI round-trip merely because a slot is unknown.
          overlay_fields = parsed
        elif parsed is None and not allow_ocr_fallback:
          # A caller forbidding OCR can still complete our own fillable template
          # locally. A non-template PDF has no safe slot location, so it stays
          # unchanged with an unknown carimbo.
          with open(tmp_path, "rb") as f:
            template_bytes = f.read()
          overlay_fields = read_mod1_template_fields(template_bytes)
          if overlay_fields is not None:
            from sav_parsers.types import ParsedField

            overlay_fields["carimbo_clube_presente"] = ParsedField(
              value=rect_has_overlay(template_bytes, CLUB_STAMP_RECT),
              confidence=1.0,
            )
            template_carimbo = True
          else:
            overlay_fields = {}
        else:
          overlay_fields, opened_processing_id = mod1_overlay_fields(tmp_path)
          carimbo, carimbo_bbox = read_carimbo(overlay_fields)
          template_carimbo = opened_processing_id is None

      tipo_checked, tipo_bbox = (
        _read_mod1_slot(
          read_tipo_inscricao, parsed, overlay_fields, reg_type,
        )
        if reg_type is not None else (None, None)
      )
      licenca_present, licenca_bbox = _read_mod1_slot(
        read_licenca_fpb, parsed, overlay_fields,
      )
      contradiction_warning: str | None = None
      if reg_type_derived and tipo_checked is False:
        # A derived type is a guess from the caller's arguments. If the form
        # explicitly marks the other box, leave both the form and the
        # attestation truthful instead of ticking a second registration type.
        other_checked, _ = _read_mod1_slot(
          read_tipo_inscricao, parsed, overlay_fields,
          1 if reg_type == 2 else 2,
        )
        if other_checked is True:
          tipo_checked, tipo_bbox = None, None
          contradiction_warning = (
            f"the form already marks the other registration type, so the "
            f"inferred type ({reg_type}) was not applied — pass `license` "
            f"explicitly if the form is wrong."
          )
      overlays = (
        inscricao_overlay(
          reg_type=reg_type,
          already_checked=tipo_checked,
          bbox=tipo_bbox,
        ),
        licenca_overlay(
          reg_type=reg_type,
          license=license,
          licenca_present=licenca_present,
          bbox=licenca_bbox,
        ),
        carimbo_overlay(
          carimbo_present=carimbo,
          bbox=carimbo_bbox,
          rect=CLUB_STAMP_RECT if template_carimbo else None,
        ),
      )
    except Exception as exc:
      if not degrade_on_lookup_error:
        raise
      logger.warning(
        "OCR for mod1 completion failed; using the original PDF",
        exc_info=True,
      )
      yield tmp_path, {"_ocr_error": exc}, []
      return

    with overlaid_pdf(tmp_path, *overlays, dest_dir=dest_dir) as (
      upload_path, results,
    ):
      inscricao_r, licenca_r, carimbo_r = results
      status: dict[str, Any] = {
        "has_club_stamp": carimbo_r.effective,
        "stamp_warning": (
          f"{carimbo_r.error} — {warning_action} without the club stamp; "
          "please stamp it manually."
        ) if carimbo_r.error else None,
        "has_inscricao_mark": inscricao_r.effective,
        "inscricao_warning": (
          f"{inscricao_r.error} — please mark the inscription checkbox manually."
        ) if inscricao_r.error else contradiction_warning,
        "has_license": licenca_r.effective,
        "license_warning": (
          f"{licenca_r.error} — please fill the Licença FPB manually."
        ) if licenca_r.error else None,
      }
      yield upload_path, status, results
  finally:
    if opened_processing_id is not None:
      try:
        close_processing(opened_processing_id)
      except Exception:
        logger.debug(
          "close_processing failed for mod1 completion", exc_info=True,
        )
