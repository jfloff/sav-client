"""Overlay placement on pages carrying /Rotate.

Scanners and phone cameras routinely emit a page whose mediabox is landscape
with /Rotate 270, displaying portrait. Document AI measures the page as
*displayed*; overlays are placed in unrotated user space. Getting that
conversion wrong put the club stamp in the middle of the Escalão row on a real
submission — and reported success, because the overlay *was* applied.
"""
import io

import img2pdf
import pikepdf
import pytest
from PIL import Image

from sav_shared.files import (
  bbox_to_pdf_rect,
  get_displayed_page_size,
  get_pdf_page_rotation,
)

# A landscape page, so a rotation swap is visible in the numbers.
PAGE_W, PAGE_H = 800.0, 400.0


def _page(rotate: int) -> bytes:
  buf = io.BytesIO()
  Image.new("RGB", (int(PAGE_W), int(PAGE_H)), (255, 255, 255)).save(buf, "PNG")
  out = io.BytesIO()
  with pikepdf.open(io.BytesIO(img2pdf.convert(buf.getvalue()))) as pdf:
    page = pdf.pages[0]
    page.mediabox = [0, 0, PAGE_W, PAGE_H]
    if rotate:
      page.Rotate = rotate
    pdf.save(out)
  return out.getvalue()


# A small box in the top-left of the page *as displayed*.
TOP_LEFT = [(0.0, 0.0), (0.1, 0.0), (0.1, 0.1), (0.0, 0.1)]


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_rotation_is_read_back(rotate):
  assert get_pdf_page_rotation(_page(rotate)) == rotate


def test_displayed_size_swaps_axes_only_for_quarter_turns():
  assert get_displayed_page_size(_page(0)) == (PAGE_W, PAGE_H)
  assert get_displayed_page_size(_page(180)) == (PAGE_W, PAGE_H)
  assert get_displayed_page_size(_page(90)) == (PAGE_H, PAGE_W)
  assert get_displayed_page_size(_page(270)) == (PAGE_H, PAGE_W)


def test_unrotated_top_left_maps_to_the_user_space_top_left():
  x0, y0, x1, y1 = bbox_to_pdf_rect(_page(0), TOP_LEFT)
  assert (x0, x1) == (0.0, 0.1 * PAGE_W)          # left edge
  assert (y0, y1) == (0.9 * PAGE_H, PAGE_H)       # top edge (origin is bottom-left)


@pytest.mark.parametrize("rotate", [90, 180, 270])
def test_a_rotated_page_does_not_reuse_the_unrotated_placement(rotate):
  """The bug: every rotation produced the /Rotate 0 rect, so overlays moved."""
  assert bbox_to_pdf_rect(_page(rotate), TOP_LEFT) != bbox_to_pdf_rect(_page(0), TOP_LEFT)


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_the_mapped_rect_always_lands_on_the_page(rotate):
  x0, y0, x1, y1 = bbox_to_pdf_rect(_page(rotate), TOP_LEFT)
  assert 0 <= x0 < x1 <= PAGE_W
  assert 0 <= y0 < y1 <= PAGE_H

def test_270_maps_the_displayed_top_left_to_the_user_space_top_right():
  """Rotating content 270° clockwise for display puts user (W,H) at top-left.

  Pinned against the real Modelo 1 scan this was derived from: a marker at the
  OCR carimbo box landed exactly on 'Diretor(a) e Carimbo Clube'.
  """
  x0, y0, x1, y1 = bbox_to_pdf_rect(_page(270), TOP_LEFT)
  assert x1 == pytest.approx(PAGE_W)              # right edge
  assert y1 == pytest.approx(PAGE_H)              # top edge


def _displayed_position(rect, rotate):
  """Where a user-space rect appears on the displayed page, as normalized coords.

  Derived independently of bbox_to_pdf_rect so the round-trip below is a real
  check rather than the same arithmetic twice.
  """
  x0, y0, x1, y1 = rect
  corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
  out = []
  for x, y in corners:
    if rotate == 90:
      nx, ny = y / PAGE_H, x / PAGE_W
    elif rotate == 180:
      nx, ny = 1 - x / PAGE_W, y / PAGE_H
    elif rotate == 270:
      nx, ny = 1 - y / PAGE_H, 1 - x / PAGE_W
    else:
      nx, ny = x / PAGE_W, 1 - y / PAGE_H
    out.append((nx, ny))
  xs = [p[0] for p in out]
  ys = [p[1] for p in out]
  return (min(xs), min(ys), max(xs), max(ys))


@pytest.mark.parametrize("rotate", [0, 90, 180, 270])
def test_placement_round_trips_back_to_where_ocr_saw_it(rotate):
  """A box OCR saw at X must be placed where a reader sees X, at any rotation.

  This is the assertion the original bug would have failed: it placed overlays
  in a plausible-looking spot that simply was not the slot OCR pointed at.
  """
  box = [(0.62, 0.30), (0.88, 0.30), (0.88, 0.42), (0.62, 0.42)]
  rect = bbox_to_pdf_rect(_page(rotate), box)
  back = _displayed_position(rect, rotate)

  assert back == pytest.approx((0.62, 0.30, 0.88, 0.42), abs=1e-6)
