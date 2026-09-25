# -*- coding: utf-8 -*-
"""What the cut throws away.

This is the most important arithmetic in the repository, and the reason is in
the README: checking a detector's output only catches errors of commission.
Everything the sweep KEPT will look correct on a contact sheet even when the
settings are far too aggressive, because a page of hits cannot show you the bird
that was dropped. Once the cut is made that footage is gone from the edit and
nothing will ever mention it was there.

dropped_ranges computes the complement of the kept events. If it is wrong, the
verification sheet looks at the wrong seconds and the whole safety net is
decorative.
"""
import pytest

from sweep_verify import dropped_ranges, hhmmss, sample_times


class TestDroppedRanges:
    def test_gap_before_between_and_after(self, make_event):
        events = [make_event(10, 20), make_event(40, 50)]
        assert dropped_ranges(events, 60) == [(0.0, 10.0), (20.0, 40.0), (50.0, 60.0)]

    def test_unsorted_input_is_handled(self, make_event):
        """Events arrive in detection order, which is not always time order."""
        events = [make_event(40, 50), make_event(10, 20)]
        assert dropped_ranges(events, 60) == [(0.0, 10.0), (20.0, 40.0), (50.0, 60.0)]

    def test_overlapping_events_do_not_invent_a_gap(self, make_event):
        """Two overlapping detections are one continuous kept stretch. Treating
        the second start as a gap boundary would report kept footage as dropped
        and send a reviewer to look at nothing."""
        events = [make_event(10, 30), make_event(20, 40)]
        assert dropped_ranges(events, 50) == [(0.0, 10.0), (40.0, 50.0)]

    def test_an_event_fully_inside_another_is_absorbed(self, make_event):
        events = [make_event(10, 60), make_event(20, 30)]
        assert dropped_ranges(events, 70) == [(0.0, 10.0), (60.0, 70.0)]

    def test_adjacent_events_leave_no_gap(self, make_event):
        events = [make_event(0, 10), make_event(10, 20)]
        assert dropped_ranges(events, 20) == []

    def test_no_events_means_the_whole_clip_is_dropped(self):
        """The dangerous case. A detector that found nothing must report the
        entire file as discarded, not an empty list that reads like 'all clear'."""
        assert dropped_ranges([], 300) == [(0.0, 300.0)]

    def test_event_covering_everything_drops_nothing(self, make_event):
        assert dropped_ranges([make_event(0, 120)], 120) == []

    def test_sub_half_second_gaps_are_ignored(self, make_event):
        """A 0.3s sliver between two events is a detector flutter, not footage
        worth sampling. Keeping it would flood the sheet with useless frames."""
        events = [make_event(0, 10), make_event(10.3, 20)]
        assert dropped_ranges(events, 20) == []

    def test_a_gap_just_over_the_floor_is_kept(self, make_event):
        events = [make_event(0, 10), make_event(10.6, 20)]
        assert dropped_ranges(events, 20) == [(10.0, 10.6)]

    def test_events_running_past_the_stated_duration(self, make_event):
        """Duration comes from a container that sometimes disagrees with the
        decoded stream. This must not produce a negative-length gap."""
        gaps = dropped_ranges([make_event(0, 130)], 120)
        assert all(b > a for a, b in gaps)
        assert gaps == []


class TestSampleTimes:
    def test_every_sample_lands_inside_a_gap(self):
        gaps = [(0.0, 10.0), (50.0, 90.0)]
        for t in sample_times(gaps, 12):
            assert any(a <= t <= b for a, b in gaps), t

    def test_longer_gaps_get_more_attention(self):
        """A forty second dead stretch deserves more looks than a two second one,
        or a long silent miss hides behind a single sampled frame."""
        gaps = [(0.0, 2.0), (10.0, 50.0)]
        times = sample_times(gaps, 20)
        short = len([t for t in times if t <= 2.0])
        long_ = len([t for t in times if t >= 10.0])
        assert long_ > short

    def test_every_gap_gets_at_least_one_sample(self):
        """Proportional allocation rounds a tiny gap to zero. It must still be
        looked at - a two second miss is still a missed bird."""
        gaps = [(0.0, 0.6), (10.0, 600.0)]
        times = sample_times(gaps, 10)
        assert any(t <= 0.6 for t in times), "the short gap was never sampled"

    def test_no_gaps_means_no_samples(self):
        assert sample_times([], 10) == []

    def test_zero_length_gaps_do_not_divide_by_zero(self):
        assert sample_times([(5.0, 5.0)], 10) == []

    def test_samples_are_ordered(self):
        times = sample_times([(0.0, 10.0), (50.0, 90.0)], 12)
        assert times == sorted(times)


class TestHhmmss:
    @pytest.mark.parametrize("seconds,expected", [
        (0, "00:00:00"),
        (59.4, "00:00:59"),
        (60, "00:01:00"),
        (3600, "01:00:00"),
        (3661, "01:01:01"),
        (86399, "23:59:59"),
    ])
    def test_formatting(self, seconds, expected):
        assert hhmmss(seconds) == expected

    def test_negative_is_clamped_not_rendered_as_minus(self):
        assert hhmmss(-5) == "00:00:00"

    def test_past_a_day_keeps_counting_hours(self):
        """Long-form source files run for hours; the hour field must not wrap."""
        assert hhmmss(90000) == "25:00:00"
