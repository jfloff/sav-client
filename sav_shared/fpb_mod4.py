"""SAV-side signature/stamp overlay for FPB Modelo 4 (Subida de escalão) forms.

parse_fpb_mod4 detects two signature slots and reports each as a `_presente`
boolean plus a folded anchor bbox (the printed label locating the slot):

  * ``assinatura_detentor_presente`` — the paternal holder's signature, in the
    "AUTORIZAÇÃO DETENTOR PATERNAL" block.
  * ``club_signature_presente``      — the club's signature/stamp, under
    "(Assinatura do diretor / Carimbo do clube)".

This module builds overlay callables that stamp a supplied signature/stamp image
onto whichever slot OCR found *empty*, for composition through
fpb_mod1.overlaid_pdf on the mod4 *upload* path — the Modelo 4 mirror of the
mod1 club-stamp overlay (see fpb_mod1.carimbo_overlay). Slots OCR found already
signed, or for which no image was supplied, are left untouched.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from .files import bbox_to_pdf_rect, image_size, overlay_image_on_pdf
from .fpb_mod1 import OverlayResult

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
  from sav_parsers import ParsedField
  from sav_parsers.types import BBox

# Placement geometry. We size the image to `WIDTH_FACTOR × anchor_width`, derive
# its height from the image's own aspect (so a round stamp stays round and a wide
# signature stays flat), centre it on the anchor, and offset its bottom edge by
# `LIFT_FACTOR × output_height` (PDF origin is bottom-left, so a positive lift
# moves it up, negative down).
#
# The two anchors have very different widths, so their WIDTH_FACTORs differ a lot:
# the club caption ("Assinatura do diretor e carimbo do clube") is wide, so <1×
# keeps the stamp on its line; the detentor "(Assinatura)" caption is a short
# label, so it needs several × to reach a realistic signature width. Calibrated
# 2026-07-11 by overlaying onto fpb-mod4.template.pdf at the anchors a live
# parse_fpb_mod4 detected — re-check visually if the form template changes.
_CLUB_STAMP_WIDTH_FACTOR = 0.6
_CLUB_STAMP_LIFT_FACTOR  = 0.4
_DETENTOR_SIG_WIDTH_FACTOR = 4.0
_DETENTOR_SIG_LIFT_FACTOR  = 0.7


def _read_presence(
  parsed: dict[str, ParsedField], entity: str,
) -> tuple[bool | None, BBox | None]:
  """Read a `_presente` signal + its folded anchor bbox from parsed fields.

  Returns (present, bbox). present is True/False when OCR resolved the slot,
  None when the field is missing or unresolved. bbox is the anchor box
  pair_region_bboxes folded onto the `_presente` field, or None when the
  anchor wasn't detected. Mirrors fpb_mod1.read_carimbo.
  """
  field = parsed.get(entity)
  if field is None:
    return (None, None)
  present = None if field.value is None else bool(field.value)
  return (present, field.bbox)


def read_detentor_signature(
  parsed: dict[str, ParsedField],
) -> tuple[bool | None, BBox | None]:
  """Read 'assinatura_detentor_presente' (present, bbox) from parse_fpb_mod4 fields."""
  return _read_presence(parsed, "assinatura_detentor_presente")


def read_club_signature(
  parsed: dict[str, ParsedField],
) -> tuple[bool | None, BBox | None]:
  """Read 'club_signature_presente' (present, bbox) from parse_fpb_mod4 fields."""
  return _read_presence(parsed, "club_signature_presente")


def overlay_signature(
  pdf_bytes: bytes,
  *,
  present: bool | None,
  bbox: BBox | None,
  image: bytes,
  width_factor: float,
  lift_factor: float,
  label: str,
) -> bytes:
  """Overlay `image` just above the printed label located by `bbox`.

  The image is sized to ``width_factor × anchor_width`` with its height derived
  from its own aspect ratio, centred horizontally on the anchor, and lifted so
  its bottom sits ``lift_factor × output_height`` above the label. This keeps a
  round stamp round and a wide signature flat regardless of the anchor's shape.

  Only fires when `present` is False (OCR ran and found the slot empty); any
  other value returns the PDF unchanged. Raises ValueError when an overlay is
  wanted but `bbox` is None (OCR gave no location). `label` names the slot in
  the error. Use signature_overlay() for the error-catching wrapper.
  """
  if present is not False:
    return pdf_bytes
  if bbox is None:
    raise ValueError(f"OCR did not return a location for {label}")
  ax0, ay0, ax1, ay1 = bbox_to_pdf_rect(pdf_bytes, bbox.vertices, page_index=bbox.page)
  img_w, img_h = image_size(image)
  aspect = (img_h / img_w) if img_w else 1.0
  out_w = (ax1 - ax0) * width_factor
  out_h = out_w * aspect
  cx = (ax0 + ax1) / 2
  # ay1 is the label's top edge (PDF origin is bottom-left, so larger y is up);
  # place the image's bottom just above it.
  y_bottom = ay1 + lift_factor * out_h
  rect = (cx - out_w / 2, y_bottom, cx + out_w / 2, y_bottom + out_h)
  return overlay_image_on_pdf(pdf_bytes, image, rect=rect, page_index=bbox.page)


def signature_overlay(
  *,
  present: bool | None,
  bbox: BBox | None,
  image: bytes | None,
  width_factor: float,
  lift_factor: float,
  label: str,
) -> Callable[[bytes], tuple[bytes, OverlayResult]]:
  """Return an overlay callable that stamps `image` when OCR found the slot empty.

  Skips (applied=None) when the slot is already signed, unresolved, or no image
  was supplied. `effective` reflects the slot's final state in the PDF whether
  or not *we* signed it. Mirrors fpb_mod1.carimbo_overlay; captures params via
  closure so it composes with the same overlay-runner.
  """
  def apply(pdf_bytes: bytes) -> tuple[bytes, OverlayResult]:
    if present is True:
      return pdf_bytes, OverlayResult(applied=None, effective=True)
    if present is None:
      return pdf_bytes, OverlayResult(applied=None, effective=None)
    # present is False — the slot is empty.
    if not image:
      return pdf_bytes, OverlayResult(applied=None, effective=False)
    try:
      return (
        overlay_signature(
          pdf_bytes, present=present, bbox=bbox, image=image,
          width_factor=width_factor, lift_factor=lift_factor, label=label,
        ),
        OverlayResult(applied=True, effective=True),
      )
    except Exception as exc:
      logger.warning("%s overlay failed", label, exc_info=True)
      return pdf_bytes, OverlayResult(applied=False, effective=False, error=f"{label} failed: {exc}")
  return apply


def detentor_signature_overlay(
  *, present: bool | None, bbox: BBox | None, image: bytes | None,
) -> Callable[[bytes], tuple[bytes, OverlayResult]]:
  """Overlay callable for the holder (detentor paternal) signature slot.

  Wraps signature_overlay with the detentor placement factors. Pass
  read_detentor_signature(parsed)'s (present, bbox) and the appended signature
  image (bytes; None → skip). Compose with fpb_mod1.overlaid_pdf on the mod4
  upload path, exactly like fpb_mod1.carimbo_overlay.
  """
  return signature_overlay(
    present=present, bbox=bbox, image=image,
    width_factor=_DETENTOR_SIG_WIDTH_FACTOR, lift_factor=_DETENTOR_SIG_LIFT_FACTOR,
    label="assinatura_detentor",
  )


def club_signature_overlay(
  *, present: bool | None, bbox: BBox | None, image: bytes | None,
) -> Callable[[bytes], tuple[bytes, OverlayResult]]:
  """Overlay callable for the club signature/stamp slot on a Modelo 4.

  Wraps signature_overlay with the club placement factors. Pass
  read_club_signature(parsed)'s (present, bbox) and the club-stamp image
  (bytes; None → skip). Compose with fpb_mod1.overlaid_pdf on the mod4 upload
  path, exactly like fpb_mod1.carimbo_overlay.
  """
  return signature_overlay(
    present=present, bbox=bbox, image=image,
    width_factor=_CLUB_STAMP_WIDTH_FACTOR, lift_factor=_CLUB_STAMP_LIFT_FACTOR,
    label="club_signature",
  )
