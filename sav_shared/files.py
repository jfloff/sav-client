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


def scale_rect(
  rect: tuple[float, float, float, float], scale: float
) -> tuple[float, float, float, float]:
  """Scale a (x0, y0, x1, y1) rect by `scale` about its center.

  Used to grow an OCR anchor box (sized to a printed label) into the larger
  area the physical signature/stamp actually occupies before overlaying.
  """
  x0, y0, x1, y1 = rect
  cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
  half_w, half_h = (x1 - x0) / 2 * scale, (y1 - y0) / 2 * scale
  return (cx - half_w, cy - half_h, cx + half_w, cy + half_h)


def load_image_bytes(arg: bytes | str | os.PathLike[str] | None) -> bytes | None:
  """Coerce a signature/stamp argument to image bytes.

  Accepts raw image bytes (as MCP passes them after base64-decoding) or a path
  to an image file (as the CLI passes). None passes through as None so the
  caller can leave that area blank for hand completion.
  """
  if arg is None:
    return None
  if isinstance(arg, (bytes, bytearray)):
    return bytes(arg)
  with open(os.fspath(arg), "rb") as f:
    return f.read()


# Ink-keying levels for prepare_overlay_image, on the 0-255 luminance scale.
# Photographed or scanned paper is never pure white and phone captures are
# never pure black, so the curve is a ramp rather than a threshold: it keeps
# anti-aliased stroke edges soft (no halo, no jagged fringe) while pushing
# paper and JPEG noise to fully transparent.
_INK_PAPER_LUMINANCE = 240  # at or above → fully transparent
_INK_STROKE_LUMINANCE = 60  # at or below → fully opaque
# The border must be at least this light for the image to look like ink on
# paper. Darker means we do not understand the image, so we leave it alone
# rather than punching holes in it.
_INK_BORDER_LUMINANCE = 200
# Below this, a border pixel reads as cut out rather than as background the
# author left opaque. Well under half, so anti-aliasing cannot reach it.
_CUTOUT_BORDER_ALPHA = 128


def _is_cutout(alpha: "Image.Image") -> bool:
  """Whether an alpha channel is a deliberate cutout rather than incidental.

  Asked before keying, and *only* about keying: keying a real cutout could
  punch holes through its own artwork, so a cutout is passed through unkeyed.

  The test is the border, symmetric with _border_is_light: a cutout has
  transparent edges, because that is what being cut out means. A mere
  `min(alpha) < 255` is not the same question — a signature captured on an
  HTML canvas is RGBA whether or not anything is translucent, and one column
  of anti-aliased edge pixels (or a devicePixelRatio re-blit) is enough to
  make it look prepared when it is a blank canvas with a stroke on it.
  """
  if alpha.getextrema()[0] == 255:
    return False
  return _border_median(alpha) < _CUTOUT_BORDER_ALPHA


def _border_median(channel: "Image.Image") -> int:
  """Median value of the 1px border of a single-channel image."""
  w, h = channel.size
  pixels = channel.load()
  edge = [pixels[x, y] for x in range(w) for y in (0, h - 1)]
  edge += [pixels[x, y] for y in range(h) for x in (0, w - 1)]
  edge.sort()
  return edge[len(edge) // 2]


def _border_is_light(luminance: "Image.Image") -> bool:
  """Whether the 1px border of a greyscale image reads as paper."""
  return _border_median(luminance) >= _INK_BORDER_LUMINANCE


def _ink_alpha(luminance: "Image.Image") -> "Image.Image":
  """Map paper-to-ink luminance onto a 0-255 alpha ramp."""
  span = _INK_PAPER_LUMINANCE - _INK_STROKE_LUMINANCE
  return luminance.point(
    lambda value: max(0, min(255, round((_INK_PAPER_LUMINANCE - value) * 255 / span)))
  )


def prepare_overlay_image(image_bytes: bytes, *, crop: bool = True) -> bytes:
  """Key a light background to transparent and crop to the ink.

  Signatures fed in by applications arrive framed in blank space — an opaque
  photo or scan of paper, or a transparent PNG exported from a signing canvas
  the user only wrote in the middle of. Both break the overlay. A solid raster
  paints a white box over the form's printed line, and blank margins corrupt
  placement, because every overlay sizes itself from the image's own pixel
  dimensions (aspect ratio in fpb_mod4, and add_overlay's aspect-preserving fit
  inside a fixed rect in render_mod1). Placement factors are calibrated against
  the ink, so the image has to be the ink.

  Returns PNG bytes cropped to the ink, with paper keyed out when there was
  paper to key. RGB is preserved, so a blue-pen signature stays blue.

  The two steps are independently gated, because they answer different
  questions:

  * **Keying** applies only when the image is not already a cutout (see
    _is_cutout) *and* its border reads as paper (_border_is_light). Anything
    else is left un-keyed rather than risking holes in artwork we do not
    understand.
  * **Cropping** applies whenever we can tell ink from background at all —
    from the alpha channel of a cutout, or from the key we just computed. It
    is not gated on alpha: blank margins misplace a transparent image exactly
    as much as an opaque one.

  Pass ``crop=False`` for an image whose padding is part of a calibrated
  placement rather than an artifact — the club stamp is placed that way against
  the fixed CLUB_STAMP_RECT, so its callers say so explicitly instead of
  leaving it to be inferred from a pixel.

  Returns the input unchanged when there is nothing to do: an image we cannot
  read as ink on a background (a dark or busy border, with no cutout alpha to
  fall back on), one that keys to nothing, or one already tight to its ink.
  """
  with Image.open(io.BytesIO(image_bytes)) as img:
    img.load()
    rgba = img.convert("RGBA")

  alpha = rgba.getchannel("A")
  if _is_cutout(alpha):
    keyed = rgba
  else:
    luminance = rgba.convert("RGB").convert("L")
    if not _border_is_light(luminance):
      # Neither a cutout nor ink on paper — we have no basis for keying, and
      # none for telling margin from content either, so leave it alone.
      return image_bytes
    alpha = _ink_alpha(luminance)
    keyed = rgba.copy()
    keyed.putalpha(alpha)

  box = alpha.getbbox()
  if box is None:
    # Blank sheet or empty canvas — there is no ink to place.
    return image_bytes

  if crop and box != (0, 0, *rgba.size):
    keyed = keyed.crop(box)
  elif keyed is rgba:
    # A cutout we were told not to crop, or one already tight to its ink:
    # nothing was done, so hand back the caller's own bytes.
    return image_bytes

  buf = io.BytesIO()
  keyed.save(buf, format="PNG")
  return buf.getvalue()


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
  try:
    overlay_pdf = img2pdf.convert(image_bytes)
  except ValueError as exc:
    # At img2pdf's default 96 DPI, either dimension below four pixels makes
    # the intermediate PDF smaller than the library accepts. Translate that
    # image-input problem without hiding unrelated conversion errors.
    width, height = image_size(image_bytes)
    if width < 4 or height < 4:
      raise ValueError(
        f"Overlay image is too small ({width}x{height} pixels); "
        "provide a larger raster image."
      ) from exc
    raise
  out = io.BytesIO()
  with pikepdf.open(io.BytesIO(pdf_bytes)) as base_pdf:
    with pikepdf.open(io.BytesIO(overlay_pdf)) as overlay:
      base_pdf.pages[page_index].add_overlay(
        overlay.pages[0],
        rect=pikepdf.Rectangle(*rect),
      )
    base_pdf.save(out)
  return out.getvalue()


def rect_has_overlay(
  pdf_bytes: bytes,
  rect: tuple[float, float, float, float],
  *,
  page_index: int = 0,
) -> bool:
  """Return whether a drawn XObject overlaps ``rect`` on the requested page.

  The page content stream is interpreted just far enough to follow graphics
  state saves/restores and affine transforms. Form XObjects use their ``/BBox``
  (and optional ``/Matrix``); image XObjects occupy the PDF unit square.
  """
  Matrix = tuple[float, float, float, float, float, float]

  def compose(left: Matrix, right: Matrix) -> Matrix:
    """Return the affine transform ``left(right(point))``."""
    la, lb, lc, ld, le, lf = left
    ra, rb, rc, rd, re, rf = right
    return (
      la * ra + lc * rb,
      lb * ra + ld * rb,
      la * rc + lc * rd,
      lb * rc + ld * rd,
      la * re + lc * rf + le,
      lb * re + ld * rf + lf,
    )

  def placed_box(box: tuple[float, float, float, float], matrix: Matrix) -> tuple[float, float, float, float]:
    a, b, c, d, e, f = matrix
    x0, y0, x1, y1 = box
    points = (
      (a * x0 + c * y0 + e, b * x0 + d * y0 + f),
      (a * x0 + c * y1 + e, b * x0 + d * y1 + f),
      (a * x1 + c * y0 + e, b * x1 + d * y0 + f),
      (a * x1 + c * y1 + e, b * x1 + d * y1 + f),
    )
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)

  target_x0, target_y0, target_x1, target_y1 = rect
  identity: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
  with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
    page = pdf.pages[page_index]
    xobjects = page.get("/Resources", {}).get("/XObject", {})
    ctm = identity
    stack: list[Matrix] = []
    for operands, operator in pikepdf.parse_content_stream(page):
      op = str(operator)
      if op == "q":
        stack.append(ctm)
      elif op == "Q":
        if stack:
          ctm = stack.pop()
      elif op == "cm" and len(operands) == 6:
        local = tuple(float(value) for value in operands)
        ctm = compose(ctm, local)  # type: ignore[arg-type]
      elif op == "Do" and operands:
        xobject = xobjects.get(operands[0])
        if xobject is None:
          continue
        bbox_obj = xobject.get("/BBox")
        box = tuple(float(value) for value in bbox_obj) if bbox_obj is not None else (0.0, 0.0, 1.0, 1.0)
        xobject_matrix_obj = xobject.get("/Matrix")
        xobject_matrix = (
          tuple(float(value) for value in xobject_matrix_obj)
          if xobject_matrix_obj is not None else identity
        )
        x0, y0, x1, y1 = placed_box(box, compose(ctm, xobject_matrix))  # type: ignore[arg-type]
        if x1 > target_x0 and x0 < target_x1 and y1 > target_y0 and y0 < target_y1:
          return True
  return False
