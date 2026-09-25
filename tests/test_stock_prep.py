# -*- coding: utf-8 -*-
"""Stock preparation: rotation, windowing, and the small formatters.

The rotation tests carry real weight. Vertically shot clips carry a rotation
tag, and the pipeline decodes with -noautorotate so it can control the geometry
itself. That means the transpose filter is the ONLY thing standing between a
vertical clip and a sideways delivery, and a sideways file is the kind of
mistake that gets noticed by the customer rather than by the tool.
"""
import pytest

from stock_prep import (IDEAL_HI, MAX_SEC, MIN_SEC, hhmmss, human_bytes, slug,
                        transpose_for, window_event)


class TestTransposeFor:
    """Each angle must map to the filter that UNDOES it."""

    def test_upright_needs_no_filter(self):
        assert transpose_for(0) is None

    def test_ninety(self):
        assert transpose_for(90) == "transpose=1"

    def test_one_eighty_is_two_turns(self):
        assert transpose_for(180) == "transpose=1,transpose=1"

    def test_two_seventy_turns_the_other_way(self):
        assert transpose_for(270) == "transpose=2"

    @pytest.mark.parametrize("deg", [360, 450, 720, -270])
    def test_angles_are_normalised(self, deg):
        """ffprobe can report 450 or a negative angle depending on whether the
        value came from the legacy tag or the display matrix."""
        assert transpose_for(deg) == transpose_for(deg % 360)

    def test_an_unexpected_angle_returns_none_rather_than_guessing(self):
        """A 45 degree tag is not something a transpose filter can fix. Silently
        picking the nearest right angle would ship a visibly wrong frame."""
        assert transpose_for(45) is None

    def test_ninety_and_two_seventy_are_not_the_same_filter(self):
        """Swapping these is the classic version of this bug: the clip is
        upright, just upside down."""
        assert transpose_for(90) != transpose_for(270)


class TestWindowEvent:
    def test_too_short_is_rejected(self, make_event):
        """Under the floor no agency accepts the clip, so it must not be cut."""
        assert window_event(make_event(10, 10 + MIN_SEC - 0.1)) is None

    def test_a_clip_inside_the_band_is_passed_through_whole(self, make_event):
        ev = make_event(10, 10 + (MIN_SEC + MAX_SEC) / 2)
        assert window_event(ev) == (ev["start"], ev["end"])

    def test_a_long_visit_is_centred_not_truncated_from_the_start(self, make_event):
        """A ninety second visit becomes one good window. Taking the first
        fifteen seconds would usually catch the bird still landing."""
        ev = make_event(100, 190)
        start, end = window_event(ev)
        mid_event = (ev["start"] + ev["end"]) / 2
        assert start == pytest.approx(mid_event - IDEAL_HI / 2)
        assert end == pytest.approx(mid_event + IDEAL_HI / 2)
        assert (end - start) == pytest.approx(IDEAL_HI)

    def test_the_window_never_escapes_the_event(self, make_event):
        ev = make_event(5, 5 + MAX_SEC + 0.5)
        start, end = window_event(ev)
        assert start >= ev["start"] and end <= ev["end"]

    def test_exactly_at_the_upper_bound_is_kept_whole(self, make_event):
        ev = make_event(0, MAX_SEC)
        assert window_event(ev) == (0.0, MAX_SEC)

    def test_the_band_is_the_right_way_round(self):
        assert MIN_SEC < IDEAL_HI <= MAX_SEC


class TestSlug:
    @pytest.mark.parametrize("raw,expected", [
        ("Mountain Chickadee", "mountain-chickadee"),
        ("Cassin's Finch", "cassin-s-finch"),
        ("  leading and trailing  ", "leading-and-trailing"),
        ("Dark-eyed Junco", "dark-eyed-junco"),
        ("multiple   spaces", "multiple-spaces"),
    ])
    def test_slugs(self, raw, expected):
        assert slug(raw) == expected

    def test_result_is_filename_safe(self):
        assert slug("a/b\\c:d*e?f") == "a-b-c-d-e-f"

    def test_punctuation_only_collapses_to_empty(self):
        assert slug("!!!") == ""

    def test_non_string_input_does_not_raise(self):
        assert slug(42) == "42"


class TestHumanBytes:
    @pytest.mark.parametrize("n,expected", [
        (0, "0B"),
        (512, "512B"),
        (1024, "1KB"),
        (1536, "2KB"),
        (1024 ** 2, "1MB"),
        (1024 ** 3, "1GB"),
        (1024 ** 4, "1TB"),
    ])
    def test_units(self, n, expected):
        assert human_bytes(n) == expected

    def test_beyond_terabytes_does_not_fall_off_the_end(self):
        assert human_bytes(1024 ** 5).endswith("PB")


class TestHhmmss:
    def test_includes_milliseconds_for_seek_accuracy(self):
        """These strings are fed to ffmpeg -ss. Whole seconds would move every
        cut point by up to a second."""
        assert hhmmss(3661.5) == "01:01:01.500"

    def test_zero(self):
        assert hhmmss(0) == "00:00:00.000"

    def test_negative_is_clamped(self):
        assert hhmmss(-3) == "00:00:00.000"

    def test_sub_second(self):
        assert hhmmss(0.25) == "00:00:00.250"
