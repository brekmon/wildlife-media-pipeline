# -*- coding: utf-8 -*-
"""Release gate: the overlay safe zone and the delivery thresholds.

overlay_safe_zone measures where burned-in text actually sits, by alpha bounding
box rather than by trusting the position the renderer was asked for. That
distinction is the point: a caption can be drawn at the right coordinates and
still end up under the Shorts UI once the platform puts its own controls on top.
"""
import numpy as np
import pytest
from PIL import Image

from release_gate import (CLIP_HI_WARN, EPS, LUFS_TARGET, LUFS_TOL, SAFE_BOTTOM,
                          SAFE_LEFT, SAFE_RIGHT, SHORT_MAX_SEC, TP_CEILING,
                          overlay_safe_zone)

W, H = 1080, 1920          # a Short


def overlay(tmp_path, box=None, name="o.png", size=(W, H)):
    """A transparent frame with one opaque rectangle, as the title renderer
    would produce. box is (left, top, right, bottom) in pixels."""
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    if box:
        a = np.asarray(im).copy()
        l, t, r, b = box
        a[t:b, l:r] = (255, 255, 255, 255)
        im = Image.fromarray(a, "RGBA")
    p = tmp_path / name
    im.save(p)
    return str(p)


class TestOverlaySafeZone:
    def test_measures_the_alpha_bounding_box(self, tmp_path):
        png = overlay(tmp_path, box=(108, 192, 972, 384))     # 10%..90%, 10%..20%
        z = overlay_safe_zone(png)
        assert z["left"] == pytest.approx(0.10, abs=0.002)
        assert z["top"] == pytest.approx(0.10, abs=0.002)
        assert z["right"] == pytest.approx(0.90, abs=0.002)
        assert z["bottom"] == pytest.approx(0.20, abs=0.002)
        assert z["size"] == (W, H)

    def test_empty_overlay_returns_none(self, tmp_path):
        """No text is not a failure. It must not be reported as text at 0,0."""
        assert overlay_safe_zone(overlay(tmp_path)) is None

    def test_faint_antialiasing_is_not_counted_as_text(self, tmp_path):
        """The threshold is alpha > 8. A barely-visible edge must not drag the
        bounding box out to the frame edge and fail an otherwise good caption."""
        a = np.zeros((H, W, 4), np.uint8)
        a[192:384, 108:972] = (255, 255, 255, 255)
        a[0, 0] = (255, 255, 255, 4)                  # one nearly-transparent pixel
        p = tmp_path / "faint.png"
        Image.fromarray(a, "RGBA").save(p)
        z = overlay_safe_zone(str(p))
        assert z["top"] == pytest.approx(0.10, abs=0.002), "a 4/255 pixel moved the box"

    def test_caption_inside_the_safe_zone_passes_the_gate_rule(self, tmp_path):
        z = overlay_safe_zone(overlay(tmp_path, box=(108, 192, 972, 384)))
        assert z["bottom"] <= SAFE_BOTTOM
        assert z["right"] <= SAFE_RIGHT
        assert z["left"] >= SAFE_LEFT

    def test_caption_too_low_is_caught(self, tmp_path):
        """This is the real failure: text that looks fine in the file and is
        covered by the Shorts UI on a phone."""
        z = overlay_safe_zone(overlay(tmp_path, box=(108, 1700, 972, 1900)))
        assert z["bottom"] > SAFE_BOTTOM

    def test_caption_running_off_the_right_edge_is_caught(self, tmp_path):
        z = overlay_safe_zone(overlay(tmp_path, box=(108, 192, W, 384)))
        assert z["right"] > SAFE_RIGHT

    def test_caption_hard_against_the_left_edge_is_caught(self, tmp_path):
        z = overlay_safe_zone(overlay(tmp_path, box=(0, 192, 500, 384)))
        assert z["left"] < SAFE_LEFT

    def test_fractions_are_resolution_independent(self, tmp_path):
        """The same layout at 1080 and at 2160 must measure identically, or the
        gate's verdict depends on the export preset."""
        small = overlay_safe_zone(overlay(tmp_path, (108, 192, 972, 384), "s.png"))
        big = overlay_safe_zone(overlay(tmp_path, (216, 384, 1944, 768), "b.png",
                                        size=(2 * W, 2 * H)))
        for k in ("left", "top", "right", "bottom"):
            assert small[k] == pytest.approx(big[k], abs=0.002), k

    def test_the_caller_is_responsible_for_the_file_existing(self, tmp_path):
        """Documenting the real contract rather than inventing one. main() checks
        os.path.exists before calling, and tries two filename candidates, so a
        missing overlay surfaces as the "not measured" warning. The function
        itself does not swallow a bad path, which is right: silently returning
        None here would make a typo in --overlays look like "no overlay found"
        and quietly skip the check on every clip in the batch."""
        with pytest.raises(Exception):
            overlay_safe_zone(str(tmp_path / "nope.png"))


class TestDeliveryThresholds:
    """These are delivery facts, not preferences. Pin them so a casual edit
    cannot quietly change what ships."""

    def test_youtube_targets(self):
        assert LUFS_TARGET == -14.0
        assert TP_CEILING == -1.0

    def test_tolerance_is_tight_but_not_zero(self):
        assert 0 < LUFS_TOL <= 1.0

    @pytest.mark.parametrize("measured", [-14.0, -13.5, -14.5])
    def test_values_inside_the_band_pass(self, measured):
        assert abs(measured - LUFS_TARGET) <= LUFS_TOL + EPS

    @pytest.mark.parametrize("measured", [-13.4, -14.6, -16.9])
    def test_values_outside_the_band_fail(self, measured):
        assert not abs(measured - LUFS_TARGET) <= LUFS_TOL + EPS

    def test_a_value_exactly_on_the_boundary_is_not_failed_by_float_error(self):
        """EPS exists for this. -13.5 is exactly at +0.5 and binary floating
        point makes that comparison a coin toss without the epsilon."""
        assert EPS > 0
        assert abs(-13.5 - LUFS_TARGET) <= LUFS_TOL + EPS

    def test_true_peak_ceiling_is_inclusive(self):
        assert -1.0 <= TP_CEILING + EPS
        assert not -0.9 <= TP_CEILING + EPS

    def test_safe_zone_bounds_are_coherent(self):
        assert 0 < SAFE_LEFT < SAFE_RIGHT <= 1.0
        assert 0 < SAFE_BOTTOM < 1.0

    def test_shorts_length_limit(self):
        assert SHORT_MAX_SEC == 60.0

    def test_clipping_warning_is_a_percentage(self):
        assert 0 < CLIP_HI_WARN < 100
