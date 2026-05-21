"""File-prep helpers for upload and OCR ingestion, plus PDF/image overlay primitives."""

from __future__ import annotations

import io
import logging
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

import img2pdf
import pikepdf
from PIL import Image

# img2pdf logs a WARNING for every alpha-channel PNG it wraps ("Image contains
# an alpha channel. Computing a separate soft mask (/SMask) image..."). Our
# club-stamp overlay is a transparent PNG, so this fires on every stamp and is
# just noise — quiet it to ERROR.
logging.getLogger("img2pdf").setLevel(logging.ERROR)

_PDF_MAGIC = b"%PDF-"
_IMAGE_MAGICS: tuple[tuple[bytes, str], ...] = (
  (b"\xff\xd8\xff", "JPEG"),
  (b"\x89PNG\r\n\x1a\n", "PNG"),
  (b"BM", "BMP"),
  (b"II*\x00", "TIFF"),
  (b"MM\x00*", "TIFF"),
  (b"GIF87a", "GIF"),
  (b"GIF89a", "GIF"),
)
_MAX_BYTES = 20 * 1024 * 1024  # Document AI limit


def ensure_pdf(data: bytes) -> bytes:
  """Return PDF bytes for any Document-AI-supported input.

  Detects format via magic bytes. PDFs pass through unchanged; supported
  images (JPEG, PNG, BMP, TIFF, GIF) are wrapped into a PDF via img2pdf.
  """
  if len(data) > _MAX_BYTES:
    raise ValueError(
      f"File is {len(data)} bytes; Document AI accepts at most {_MAX_BYTES}."
    )
  if data.startswith(_PDF_MAGIC):
    return data
  for magic, _ in _IMAGE_MAGICS:
    if data.startswith(magic):
      return img2pdf.convert(data)
  raise ValueError(
    "Unsupported file format. Accepted: PDF, JPEG, PNG, BMP, TIFF, GIF."
  )


@contextmanager
def staged_pdf(input_path: str) -> Iterator[tuple[str, bool]]:
  """Yield (pdf_path, was_converted) for `input_path`.

  PDFs are yielded as-is with was_converted=False (no copy). Supported image
  inputs are wrapped into a PDF in a temp file (was_converted=True), which
  is cleaned up on exit. The size guard in ensure_pdf applies to both cases.
  """
  with open(input_path, "rb") as f:
    data = f.read()
  pdf_bytes = ensure_pdf(data)
  if pdf_bytes is data:
    yield (input_path, False)
    return
  with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
    f.write(pdf_bytes)
    tmp_path = f.name
  try:
    yield (tmp_path, True)
  finally:
    if os.path.exists(tmp_path):
      os.unlink(tmp_path)


def image_size(image_bytes: bytes) -> tuple[int, int]:
  """Return (width, height) of an image in pixels."""
  with Image.open(io.BytesIO(image_bytes)) as img:
    return img.size


def get_pdf_page_box(
  pdf_bytes: bytes, page_index: int = 0,
) -> tuple[float, float, float, float]:
  """Return the mediabox of `pdf_bytes` page `page_index` as (x0, y0, x1, y1).

  Coordinates are in PDF user-space units (typically points; origin at the
  page's bottom-left).
  """
  with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
    mb = pdf.pages[page_index].mediabox
    return (float(mb[0]), float(mb[1]), float(mb[2]), float(mb[3]))


def bottom_right_rect(
  pdf_bytes: bytes,
  image_bytes: bytes,
  *,
  width_fraction: float,
  margin_fraction: float,
  page_index: int = 0,
) -> tuple[float, float, float, float]:
  """Compute a rect for placing `image_bytes` in the bottom-right corner
  of a PDF page, sized to `width_fraction` of the page width and inset by
  `margin_fraction` of the page width. Aspect ratio of the image is
  preserved (height derived from the image's own aspect).

  Returns (x0, y0, x1, y1) in PDF user-space units, suitable for passing
  to overlay_image_on_pdf.
  """
  img_w, img_h = image_size(image_bytes)
  aspect = img_h / img_w if img_w else 1.0
  page_x0, page_y0, page_x1, _ = get_pdf_page_box(pdf_bytes, page_index=page_index)
  page_w = page_x1 - page_x0
  out_w = page_w * width_fraction
  out_h = out_w * aspect
  margin = page_w * margin_fraction
  return (
    page_x1 - out_w - margin,
    page_y0 + margin,
    page_x1 - margin,
    page_y0 + margin + out_h,
  )


def bbox_to_pdf_rect(
  pdf_bytes: bytes,
  normalized_vertices: list[tuple[float, float]],
  *,
  page_index: int = 0,
) -> tuple[float, float, float, float]:
  """Convert normalized [0..1] top-left-origin vertices (Document AI's
  format) to a PDF user-space rect (origin bottom-left, units in points).

  Axis-aligned to vertex min/max — Document AI polys can be 4-point quads
  with skew, and `add_overlay` wants a clean rect. Returns (x0, y0, x1, y1)
  suitable for passing to overlay_image_on_pdf.
  """
  page_x0, page_y0, page_x1, page_y1 = get_pdf_page_box(pdf_bytes, page_index=page_index)
  page_w = page_x1 - page_x0
  page_h = page_y1 - page_y0
  xs = [v[0] for v in normalized_vertices]
  ys = [v[1] for v in normalized_vertices]
  x0 = page_x0 + min(xs) * page_w
  x1 = page_x0 + max(xs) * page_w
  y0 = page_y1 - max(ys) * page_h
  y1 = page_y1 - min(ys) * page_h
  return (x0, y0, x1, y1)


def overlay_image_on_pdf(
  pdf_bytes: bytes,
  image_bytes: bytes,
  *,
  rect: tuple[float, float, float, float],
  page_index: int = 0,
) -> bytes:
  """Overlay an image onto a specific rectangle of a PDF page.

  `rect` is (x0, y0, x1, y1) in PDF user-space units (origin bottom-left).
  The image is wrapped into a single-page PDF via img2pdf and composited via
  pikepdf.Page.add_overlay. add_overlay preserves the overlay's aspect by
  centering inside `rect`; size `rect` to match the image's aspect (use
  image_size) if you want it to fill the rect exactly.

  Use this for any raster overlay — club stamps, checkbox marks, signatures.
  Use image_size + get_pdf_page_box to compute `rect`.
  """
  overlay_pdf = img2pdf.convert(image_bytes)
  out = io.BytesIO()
  with pikepdf.open(io.BytesIO(pdf_bytes)) as base_pdf:
    with pikepdf.open(io.BytesIO(overlay_pdf)) as overlay:
      base_pdf.pages[page_index].add_overlay(
        overlay.pages[0],
        rect=pikepdf.Rectangle(*rect),
      )
    base_pdf.save(out)
  return out.getvalue()
