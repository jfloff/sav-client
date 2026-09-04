"""Tests for prepare_overlay_image — the paper-keying/cropping pass that every
signature and stamp overlay runs its image through.

Applications feed signatures as opaque photos or scans: a white sheet that
paints a box over the form's printed line, framed by whitespace that corrupts
the placement geometry (which is derived from the image's own dimensions).
These assert the keying, the crop, and the leave-it-alone cases.
"""
import io

from PIL import Image, ImageDraw

from sav_shared.files import prepare_overlay_image


def _png(img: Image.Image) -> bytes:
  buf = io.BytesIO()
  img.save(buf, format="PNG")
  return buf.getvalue()


def _sheet(size=(800, 400), colour=(20, 30, 180)) -> Image.Image:
  """An opaque white sheet with a short ink stroke near the middle."""
  img = Image.new("RGB", size, (255, 255, 255))
  ImageDraw.Draw(img).line([(300, 190), (500, 210)], fill=colour, width=6)
  return img


def _opened(data: bytes) -> Image.Image:
  return Image.open(io.BytesIO(data))


def test_keys_white_paper_to_transparent_and_crops_to_ink():
  out = _opened(prepare_overlay_image(_png(_sheet())))
  assert out.mode == "RGBA"
  # Cropped from the 800x400 sheet down to the stroke's own box.
  assert out.size[0] < 300 and out.size[1] < 60
  alpha = out.getchannel("A")
  assert alpha.getextrema()[0] == 0        # paper is fully transparent
  assert alpha.getextrema()[1] == 255      # ink is fully opaque


def test_preserves_ink_colour():
  out = _opened(prepare_overlay_image(_png(_sheet()))).convert("RGBA")
  centre = out.getpixel((out.size[0] // 2, out.size[1] // 2))
  assert centre[:3] == (20, 30, 180)
  assert centre[3] == 255


def test_keys_a_noisy_jpeg_capture():
  """Photographed paper is never pure white; the ramp has to absorb that."""
  buf = io.BytesIO()
  _sheet().save(buf, format="JPEG", quality=70)
  out = _opened(prepare_overlay_image(buf.getvalue()))
  assert out.getchannel("A").getpixel((0, 0)) == 0
  assert out.size[0] < 300


def test_leaves_an_already_transparent_image_untouched():
  """A prepared PNG (like CLUB_STAMP_PATH) keeps its own padding and geometry."""
  img = Image.new("RGBA", (100, 100), (0, 0, 0, 0))
  ImageDraw.Draw(img).line([(10, 10), (90, 90)], fill=(0, 0, 0, 255), width=5)
  data = _png(img)
  assert prepare_overlay_image(data) is data


def test_leaves_a_dark_background_untouched():
  """A border that does not read as paper means we do not understand the image."""
  data = _png(Image.new("RGB", (100, 100), (10, 10, 10)))
  assert prepare_overlay_image(data) is data


def test_leaves_a_blank_sheet_untouched():
  """Keying an empty sheet would leave nothing to overlay."""
  data = _png(Image.new("RGB", (100, 100), (255, 255, 255)))
  assert prepare_overlay_image(data) is data


def _smask_image_count(pdf_bytes: bytes) -> int:
  """Number of soft-masked (transparent) images drawn on page 0.

  overlay_image_on_pdf wraps each overlay in a Form XObject via img2pdf, which
  emits an /SMask only for an image with an alpha channel — so this counts the
  overlays that reached the page keyed rather than as an opaque box.
  """
  import pikepdf
  count = 0
  with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
    for form in pdf.pages[0].get("/Resources", {}).get("/XObject", {}).values():
      for inner in form.get("/Resources", {}).get("/XObject", {}).values():
        if "/SMask" in inner:
          count += 1
  return count


class TestWiring:
  """The keying pass has to actually run on every caller-supplied image path."""

  def test_render_mod1_keys_an_opaque_signature(self):
    from sav_shared.fpb_mod1 import render_mod1
    out = render_mod1(
      {}, season="2026/2027", validate=False, player_signature=_png(_sheet()),
    )
    assert _smask_image_count(out) == 1

  def test_mod4_signature_overlay_keys_an_opaque_signature(self):
    import img2pdf
    from sav_parsers.types import BBox
    from sav_shared.fpb_mod4 import detentor_signature_overlay

    blank = img2pdf.convert(_png(Image.new("RGB", (1240, 1754), (255, 255, 255))))
    bbox = BBox(page=0, vertices=[(0.10, 0.62), (0.45, 0.62), (0.45, 0.64), (0.10, 0.64)])
    apply = detentor_signature_overlay(present=False, bbox=bbox, image=_png(_sheet()))
    out, result = apply(blank)
    assert result.applied is True
    assert _smask_image_count(out) == 1
