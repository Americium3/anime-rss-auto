#!/usr/bin/env python3
"""Tests: an air date must land in the cour the show is actually listed under.

Run:  python test_season.py              (stdlib unittest, no network, no qB)

The cour string 'YYYY.MM' is not cosmetic — it is the season badge, the timetable
grouping, and the qB save path (X:\\Bangumi\\<cour>\\) plus tag a new subscription
is filed under. A plain calendar-quarter mapping files a show premiering on
2026-09-25 as a summer show even though ten of its eleven episodes air in the
autumn cour, so the rollover window (COUR_ROLLOVER_DAYS) moves an end-of-cour
premiere to the next cour. Each case here pins one property of that rule:

  * a premiere inside the window rolls forward, one outside it does not;
  * the window is measured in days to the next cour, so it behaves the same in a
    30- and a 31-day month;
  * December rolls across the year boundary;
  * season_of and current_season agree, and undated/month-only input still
    answers the conservative calendar quarter;
  * and — the load-bearing one — the SKIP_BEFORE_SEASON hands-off cutoff reads
    calendar_season_of, so the protected set of old shows does not move when the
    display rule does.
"""
from __future__ import annotations

import datetime
import time
import unittest
import unittest.mock as mock

import anime_rss as core


class SeasonOfCase(unittest.TestCase):
    def test_mid_cour_premiere_keeps_its_quarter(self):
        self.assertEqual(core.season_of("2026-07-02"), "2026.07")
        self.assertEqual(core.season_of("2026-08-12"), "2026.07")
        self.assertEqual(core.season_of("2026-04-08"), "2026.04")
        self.assertEqual(core.season_of("2026-01-05"), "2026.01")

    def test_end_of_cour_premiere_rolls_to_next_cour(self):
        # JOJO SBR 2nd&3rd: bgm date 2026-09-25, finale 2026-12-04 = autumn.
        self.assertEqual(core.season_of("2026-09-25"), "2026.10")
        self.assertEqual(core.season_of("2026-09-30"), "2026.10")
        self.assertEqual(core.season_of("2026-06-28"), "2026.07")
        self.assertEqual(core.season_of("2026-03-22"), "2026.04")

    def test_december_rolls_into_the_next_year(self):
        self.assertEqual(core.season_of("2026-12-26"), "2027.01")
        self.assertEqual(core.season_of("2026-12-10"), "2026.10")

    def test_window_is_days_to_next_cour_not_a_day_of_month(self):
        # September has 30 days, March 31: the same day-of-month is 11 vs 12 days
        # out, and both are inside the default 14-day window.
        self.assertEqual(core.season_of("2026-09-20"), "2026.10")
        self.assertEqual(core.season_of("2026-03-20"), "2026.04")
        # 15 days out is outside the window in either month.
        self.assertEqual(core.season_of("2026-09-16"), "2026.07")
        self.assertEqual(core.season_of("2026-03-17"), "2026.01")

    def test_boundary_is_inclusive_and_configurable(self):
        with mock.patch.object(core, "COUR_ROLLOVER_DAYS", 14):
            self.assertEqual(core.season_of("2026-09-17"), "2026.10")  # 14 days
            self.assertEqual(core.season_of("2026-09-16"), "2026.07")  # 15 days
        with mock.patch.object(core, "COUR_ROLLOVER_DAYS", 0):         # rule off
            self.assertEqual(core.season_of("2026-09-25"), "2026.07")
            self.assertEqual(core.season_of("2026-12-31"), "2026.10")

    def test_unusable_dates_stay_conservative(self):
        self.assertIsNone(core.season_of(""))
        self.assertIsNone(core.season_of(None))
        self.assertIsNone(core.season_of("TBA"))
        # Month-only: no day to test, so the plain quarter mapping stands.
        self.assertEqual(core.season_of("2026-09"), "2026.07")
        # Out-of-range values must not raise on a hand-entered bgm date.
        self.assertIsNone(core.season_of("2026-13-01"))
        self.assertEqual(core.season_of("2026-09-31"), "2026.07")

    def test_current_season_matches_season_of(self):
        for ymd in ("2026-08-21", "2026-09-25", "2026-12-26", "2027-01-01"):
            d = datetime.date.fromisoformat(ymd)
            self.assertEqual(core.current_season(d), core.season_of(ymd), ymd)


class CutoffIsUnaffectedCase(unittest.TestCase):
    """The rollover is a display/filing rule. The hands-off cutoff must not move
    with it, or a show that has been read-only since it was downloaded becomes
    eligible for teardown the day the rule ships."""

    def test_calendar_season_never_rolls(self):
        self.assertEqual(core.calendar_season_of("2026-09-25"), "2026.07")
        self.assertEqual(core.calendar_season_of("2026-03-19"), "2026.01")
        self.assertEqual(core.calendar_season_of("2026-12-31"), "2026.10")
        self.assertEqual(core.calendar_season_of("2026-09"), "2026.07")
        self.assertIsNone(core.calendar_season_of(""))

    def test_calendar_cour_is_never_later_than_the_display_cour(self):
        d = datetime.date(2025, 12, 1)
        for _ in range(800):
            ymd = d.isoformat()
            self.assertLessEqual(core.calendar_season_of(ymd), core.season_of(ymd), ymd)
            d += datetime.timedelta(days=1)

    def test_a_pre_cutoff_show_stays_hands_off_after_rolling(self):
        # 2026-03-19 displays as 2026.04 (= the cutoff) but is a 2026.01 show and
        # must remain manual-only.
        with mock.patch.object(core, "SKIP_BEFORE_SEASON", "2026.04"):
            self.assertEqual(core.season_of("2026-03-19"), "2026.04")
            self.assertTrue(core.is_manual_old_show("2026-03-19"))
            self.assertFalse(core.is_manual_old_show("2026-09-25"))

    def test_old_cour_long_runner_stays_exempt(self):
        # cour_still_airing only looks up shows below the cutoff — a rolled cour
        # would short-circuit it to False and silently freeze a running long show.
        span = {1: {"first": "2026-03-19", "last": "2099-01-01",
                    "count": 24, "_ts": time.time()}}   # fresh: no network
        with mock.patch.object(core, "SKIP_BEFORE_SEASON", "2026.04"):
            self.assertTrue(core.cour_still_airing(1, core.calendar_season_of("2026-03-19"), span))
            self.assertFalse(core.cour_still_airing(1, core.season_of("2026-03-19"), span))


if __name__ == "__main__":
    unittest.main(verbosity=2)
