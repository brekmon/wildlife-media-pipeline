# -*- coding: utf-8 -*-
"""bird_sweep helpers.

The detector itself needs footage and a GPU, so what is tested here is the
arithmetic around it: the formatting that ends up in a plan file, the codec
decision that determines whether a stream-copy cut lands where it was asked to,
and the seasonal week BirdNET is weighted by.
"""
import datetime
import importlib.util
import os

import pytest

import bird_sweep
from bird_sweep import (AUDIO_KEEP_CONF, AUDIO_SCAN_CONF, DET_FPS, MAX_MASK_FRAC,
                        SUSTAIN, all_intra, birdnet_week, hhmmss, human_bytes)


class TestHhmmss:
    @pytest.mark.parametrize("sec,expected", [
        (0, "00:00:00"),
        (61, "00:01:01"),
        (3600, "01:00:00"),
        (7325, "02:02:05"),
    ])
    def test_formatting(self, sec, expected):
        assert hhmmss(sec) == expected

    def test_negative_is_clamped(self):
        assert hhmmss(-10) == "00:00:00"

    def test_hours_do_not_wrap_at_a_day(self):
        """Source files from a full morning run past 24 hours in aggregate."""
        assert hhmmss(100000) == "27:46:40"

    def test_fractional_seconds_truncate_rather_than_round_up(self):
        """Rounding a cut point up moves it past the frame it was meant to mark."""
        assert hhmmss(59.9) == "00:00:59"


class TestAllIntra:
    """All-Intra means every frame is a keyframe, so a stream-copy cut lands
    exactly where asked. On Long GOP the cut snaps back to the previous
    keyframe, which silently moves the start of every clip."""

    @pytest.mark.parametrize("profile", [
        "High 4:2:2 Intra",      # what ffprobe actually reports for XAVC S-I
        "High 10 Intra",
        "all-intra",
        "ALL-INTRA",
    ])
    def test_intra_profiles_are_detected(self, profile):
        assert all_intra({"profile": profile}) is True

    @pytest.mark.parametrize("profile", ["High", "Main", "XAVC S", "High 4:2:0"])
    def test_long_gop_profiles_are_not(self, profile):
        assert all_intra({"profile": profile}) is False

    def test_the_sony_format_name_is_not_what_gets_matched(self):
        """Worth pinning, because it is the obvious wrong assumption. ffprobe
        reports the H.264 profile, so "XAVC S-I" never appears in this field.
        Anyone "fixing" this by matching the marketing name would be matching a
        string the metadata does not contain."""
        assert all_intra({"profile": "XAVC S-I"}) is False

    def test_missing_profile_is_treated_as_long_gop(self):
        """The safe default. Assuming All-Intra when the profile is unknown
        would produce cuts that quietly land in the wrong place."""
        assert all_intra({}) is False
        assert all_intra({"profile": None}) is False


class TestBirdnetWeek:
    """BirdNET takes a 1-48 week, four per month, so the model is weighted to
    what is actually present at this latitude in this season."""

    def touch(self, tmp_path, when):
        p = tmp_path / "clip.mp4"
        p.write_bytes(b"x")
        ts = datetime.datetime(when.year, when.month, when.day, 12, 0).timestamp()
        os.utime(p, (ts, ts))
        return str(p)

    @pytest.mark.parametrize("date,expected", [
        (datetime.date(2026, 1, 1), 1),
        (datetime.date(2026, 1, 8), 2),
        (datetime.date(2026, 1, 31), 4),
        (datetime.date(2026, 2, 1), 5),
        (datetime.date(2026, 12, 31), 48),
    ])
    def test_week_numbers(self, tmp_path, date, expected):
        assert birdnet_week(self.touch(tmp_path, date)) == expected

    def test_always_within_the_valid_range(self, tmp_path):
        for month in range(1, 13):
            for day in (1, 8, 9, 16, 17, 24, 25, 28):
                w = birdnet_week(self.touch(tmp_path, datetime.date(2026, month, day)))
                assert 1 <= w <= 48, (month, day, w)

    def test_a_month_never_yields_more_than_four_weeks(self, tmp_path):
        """The +6)//8+1 arithmetic would give 5 on the 31st without the min()."""
        weeks = {birdnet_week(self.touch(tmp_path, datetime.date(2026, 3, d)))
                 for d in range(1, 32)}
        assert weeks == {9, 10, 11, 12}


class TestDetectionConstants:
    def test_audio_scan_is_looser_than_audio_keep(self):
        """BirdNET is run once at a low confidence and cached, then filtered
        harder. Scanning at the strict threshold would mean re-running the model
        to loosen it later."""
        assert AUDIO_SCAN_CONF < AUDIO_KEEP_CONF

    def test_sustain_is_below_one_so_events_can_continue(self):
        assert 0 < SUSTAIN < 1.0

    def test_mask_can_never_cover_most_of_the_frame(self):
        """The restless-pixel mask suppresses moving scenery. Letting it grow
        past a quarter of the frame would mask the birds as well."""
        assert 0 < MAX_MASK_FRAC <= 0.5

    def test_detection_rate_is_sane(self):
        assert 1 <= DET_FPS <= 30


class TestSidecarMarks:
    """Catalyst shot marks are the one channel with a human in it. The docstring
    says nothing a detector decides may throw one away.

    NOTE, and this is the finding worth reading: sidecar_marks imports `marks`,
    which is NOT part of this repository. The import sits inside a bare
    `except Exception: return [], 0`, so on a fresh clone the force-keep does
    nothing at all and says nothing about it. These tests pin the behaviour that
    IS correct - a missing or unreadable sidecar must never crash a five hour
    sweep - so that if the module is added later, the difference is visible.
    """

    def test_no_sidecar_xml_returns_nothing_quietly(self, tmp_path):
        clip = tmp_path / "C0527.MP4"
        clip.write_bytes(b"x")
        assert bird_sweep.sidecar_marks(str(clip), 29.97) == ([], 0)

    def test_an_unreadable_sidecar_does_not_abort_the_sweep(self, tmp_path):
        """A truncated or malformed XML must degrade, not raise. Losing the marks
        is bad; losing five hours of scan because of one bad sidecar is worse."""
        clip = tmp_path / "C0527.MP4"
        clip.write_bytes(b"x")
        (tmp_path / "C0527M01.XML").write_text("<not valid xml", encoding="utf-8")
        assert bird_sweep.sidecar_marks(str(clip), 29.97) == ([], 0)

    def test_the_marks_module_is_absent_from_this_repository(self):
        """Documents the gap rather than hiding it. If `marks.py` is published,
        this test starts failing, which is the reminder to write real coverage
        for the force-keep path instead of leaving this note behind."""
        assert importlib.util.find_spec("marks") is None, (
            "marks.py is now importable - replace this with real tests of the "
            "Catalyst shot-mark force-keep path")

    def test_mark_padding_is_generous_enough_to_be_useful(self):
        assert bird_sweep.MARK_PRE > 0 and bird_sweep.MARK_POST > 0
