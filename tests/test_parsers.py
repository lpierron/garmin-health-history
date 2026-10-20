#!/usr/bin/env python3
"""
Tests unitaires (bibliothèque standard uniquement).

Couvrent :
  - le parsing des réponses de l'API Garmin (pas, sommeil, prédictions) ;
  - la fusion dans le cache (les valeurs déjà connues ne sont pas écrasées) ;
  - la robustesse des scripts quand un indicateur est absent des données
    (c'était la cause du « KeyError: Column not found: pred_5k »).

Usage :
    python -m unittest discover -s tests -v
"""
import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analysis_fine  # noqa: E402
from history import (  # noqa: E402
    _chunks,
    _merge,
    _needed_chunks,
    _parse_race_rows,
    _parse_sleep_rows,
    _parse_steps_rows,
    _rows,
    display_table,
    fetch_day,
    monthly,
    synthese_semaine,
)
from units import fmt_hm, fmt_hms, parse_hm, parse_hms  # noqa: E402


# ----------------------------------------------------------- réponses API
STEPS_ROWS = [
    {"calendarDate": "2024-03-01", "totalSteps": 9567,
     "totalDistance": 7587, "stepGoal": 6640},
    {"calendarDate": "2024-03-02", "totalSteps": 7267,
     "totalDistance": 5598, "stepGoal": 6640},
]

SLEEP_ROWS = [
    {"calendarDate": "2026-10-15", "values": {
        "sleepScore": 80, "sleepScoreQuality": "GOOD",
        "totalSleepTimeInSeconds": 28800, "restingHeartRate": 50}},
    {"calendarDate": "2026-10-16", "values": {
        "sleepScore": None, "sleepScoreQuality": None,
        "totalSleepTimeInSeconds": 25200}},
]

RACE_ROWS = [
    {"userId": 12345678, "fromCalendarDate": "2026-10-01",
     "toCalendarDate": "2026-10-02", "calendarDate": "2026-10-01",
     "time5K": 1860, "time10K": 4016, "timeHalfMarathon": 9150,
     "timeMarathon": 20837},
    {"calendarDate": "2026-10-02", "time5K": 1875,
     "timeHalfMarathon": 9210},
]


class TestParsers(unittest.TestCase):
    def test_parse_steps_rows(self):
        out = _parse_steps_rows(STEPS_ROWS)
        self.assertEqual(out, {"2024-03-01": 9567, "2024-03-02": 7267})

    def test_parse_steps_rows_ignores_incomplete(self):
        self.assertEqual(_parse_steps_rows([{"calendarDate": "2024-03-01"}]), {})
        self.assertEqual(_parse_steps_rows(None), {})

    def test_parse_sleep_rows(self):
        out = _parse_sleep_rows(SLEEP_ROWS)
        self.assertEqual(out["2026-10-15"]["sleep_score"], 80)
        self.assertEqual(out["2026-10-15"]["sleep_hours"], 8.0)    # 28800 s
        self.assertIsNone(out["2026-10-16"]["sleep_score"])
        self.assertEqual(out["2026-10-16"]["sleep_hours"], 7.0)    # 25200 s

    def test_parse_race_rows(self):
        out = _parse_race_rows(RACE_ROWS)
        self.assertEqual(out["2026-10-01"]["pred_5k"], 31.0)       # 1860 s
        self.assertEqual(out["2026-10-01"]["pred_half"], 152.5)    # 9150 s
        self.assertEqual(out["2026-10-02"]["pred_5k"], 31.2)       # 1875 s

    def test_parse_race_rows_ignores_empty(self):
        self.assertEqual(_parse_race_rows([{"calendarDate": "2026-10-03"}]), {})

    def test_rows_accepts_both_shapes(self):
        self.assertEqual(_rows(STEPS_ROWS), STEPS_ROWS)
        self.assertEqual(_rows({"userPredictionVos": RACE_ROWS}), RACE_ROWS)
        self.assertEqual(_rows({"individualStats": SLEEP_ROWS}), SLEEP_ROWS)
        self.assertEqual(_rows(None), [])
        self.assertEqual(_rows({"unexpected": 1}), [])


class TestChunks(unittest.TestCase):
    def test_chunks_cover_range(self):
        cs = list(_chunks(date(2020, 1, 1), date(2020, 1, 10), 28))
        self.assertEqual(cs, [(date(2020, 1, 1), date(2020, 1, 9))])

    def test_chunks_split_long_range(self):
        cs = list(_chunks(date(2020, 1, 1), date(2021, 1, 1), 28))
        self.assertEqual(len(cs), 14)
        self.assertEqual(cs[0], (date(2020, 1, 1), date(2020, 1, 28)))
        self.assertEqual(cs[-1], (date(2020, 12, 30), date(2020, 12, 31)))

    def test_chunks_empty_range(self):
        self.assertEqual(list(_chunks(date(2020, 1, 1), date(2020, 1, 1), 28)), [])

    def test_needed_chunks_skips_complete_tranches(self):
        start, end = date(2020, 1, 1), date(2020, 3, 1)
        missing = {"2020-02-10"}
        got = list(_needed_chunks(start, end, 28, missing))
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0], (date(2020, 1, 29), date(2020, 2, 25)))

    def test_needed_chunks_none_when_complete(self):
        got = list(_needed_chunks(date(2020, 1, 1), date(2020, 3, 1), 28, set()))
        self.assertEqual(got, [])


class TestCacheSafety(unittest.TestCase):
    def test_merge_fills_missing_only(self):
        cache = {"2024-03-01": {"steps": 100, "pred_5k": None}}
        data = {"2024-03-01": {"steps": 999, "pred_5k": 30.0},
                "2024-03-02": {"steps": 500}}
        n = _merge(cache, data, ("steps", "pred_5k"))
        self.assertEqual(n, 1)                       # seulement le champ manquant
        self.assertEqual(cache["2024-03-01"]["steps"], 100)   # pas écrasé
        self.assertEqual(cache["2024-03-01"]["pred_5k"], 30.0)
        self.assertNotIn("2024-03-02", cache)       # jour absent du cache : ignoré

    def test_merge_ignores_null_values(self):
        cache = {"2024-03-01": {"sleep_score": 80, "sleep_hours": None}}
        data = {"2024-03-01": {"sleep_score": None, "sleep_hours": 7.5}}
        n = _merge(cache, data, ("sleep_score", "sleep_hours"))
        self.assertEqual(n, 1)
        self.assertEqual(cache["2024-03-01"]["sleep_score"], 80)
        self.assertEqual(cache["2024-03-01"]["sleep_hours"], 7.5)

    def test_merge_accepts_scalar_values(self):
        # le nombre de pas est un scalaire, pas un dict
        cache = {"2024-03-01": {"steps": None}}
        n = _merge(cache, {"2024-03-01": 9567}, ("steps",))
        self.assertEqual(n, 1)
        self.assertEqual(cache["2024-03-01"]["steps"], 9567)


class FakeApi:
    """Fausse API Garmin : tout échoue, sauf la réponse « stats » fournie."""

    def __init__(self, stats=None):
        self.stats = stats or {}

    def get_stats(self, cdate):
        if isinstance(self.stats, Exception):
            raise self.stats
        return self.stats

    def get_max_metrics(self, cdate):
        raise RuntimeError("indisponible")

    def get_body_composition(self, start, end):
        raise RuntimeError("indisponible")


class TestFetchDay(unittest.TestCase):
    def test_steps_come_from_stats(self):
        api = FakeApi({"restingHeartRate": 50, "totalSteps": 9447,
                       "activeKilocalories": 472.0})
        row = fetch_day(api, "2026-10-15")
        self.assertEqual(row["steps"], 9447)
        self.assertEqual(row["rhr"], 50)

    def test_failed_call_keeps_cached_values(self):
        api = FakeApi(RuntimeError("429"))
        existing = {"rhr": 50, "stress": 39, "steps": 9447}
        row = fetch_day(api, "2026-10-15", existing=existing)
        self.assertEqual(row["rhr"], 50)
        self.assertEqual(row["stress"], 39)
        self.assertEqual(row["steps"], 9447)

    def test_missing_field_does_not_erase_cached_value(self):
        api = FakeApi({"restingHeartRate": 50})   # pas de totalSteps
        row = fetch_day(api, "2026-10-15", existing={"steps": 9447})
        self.assertEqual(row["steps"], 9447)
        self.assertEqual(row["rhr"], 50)


# ------------------------------------------------- robustesse des analyses
PARTIAL_CACHE = {
    "2024-03-01": {"rhr": 50, "hr_max": 150, "stress": 30, "bb_max": 70,
                   "calories": 500.0, "moderate": 20, "intense": 15,
                   "weight": 85.0, "VO2max": 45.0},
    "2024-03-02": {"rhr": 51, "hr_max": 151, "stress": 31, "bb_max": 71,
                   "calories": 520.0, "moderate": 22, "intense": 16,
                   "weight": 85.1, "VO2max": 45.1},
}


class TestUnits(unittest.TestCase):
    """Mise en forme et relecture des durées."""

    def test_fmt_hms(self):
        self.assertEqual(fmt_hms(30.9), "00:30:54")
        self.assertEqual(fmt_hms(152.3), "02:32:18")
        self.assertEqual(fmt_hms(0), "00:00:00")
        self.assertEqual(fmt_hms(-0.5), "-00:00:30")

    def test_fmt_hms_signed(self):
        self.assertEqual(fmt_hms(-0.5, signed=True), "-00:00:30")
        self.assertEqual(fmt_hms(0.5, signed=True), "+00:00:30")

    def test_fmt_hm(self):
        self.assertEqual(fmt_hm(8.05), "08:03")
        self.assertEqual(fmt_hm(7.5), "07:30")
        self.assertEqual(fmt_hm(-0.25, signed=True), "-00:15")

    def test_fmt_empty(self):
        self.assertEqual(fmt_hms(float("nan")), "")
        self.assertEqual(fmt_hm(None), "")

    def test_parse_round_trip(self):
        self.assertAlmostEqual(parse_hms(fmt_hms(30.9)), 30.9, places=2)
        self.assertAlmostEqual(parse_hm(fmt_hm(8.05)), 8.05, places=2)
        self.assertAlmostEqual(parse_hms("00:30:54"), 30.9, places=2)
        self.assertAlmostEqual(parse_hm("08:03"), 8.05, places=2)

    def test_parse_numbers_and_missing(self):
        self.assertAlmostEqual(parse_hms(30.9), 30.9)
        self.assertAlmostEqual(parse_hm(8.05), 8.05)
        self.assertTrue(np.isnan(parse_hms("")))
        self.assertTrue(np.isnan(parse_hm(None)))


class TestDisplayTable(unittest.TestCase):
    """Le tableau mensuel écrit sur disque : pas/j, « hh:mm », « hh:mm:ss »."""

    def setUp(self):
        cache = {
            "2024-03-01": {"rhr": 50, "hr_max": 150, "stress": 30, "bb_max": 70,
                           "calories": 500.0, "moderate": 20, "intense": 15,
                           "weight": 85.0, "VO2max": 45.0, "steps": 9000,
                           "sleep_hours": 8.05, "pred_5k": 30.9, "pred_half": 152.3},
            "2024-03-02": {"rhr": 51, "hr_max": 151, "stress": 31, "bb_max": 71,
                           "calories": 520.0, "moderate": 22, "intense": 16,
                           "weight": 85.1, "VO2max": 45.1, "steps": 8000,
                           "sleep_hours": 7.5, "pred_5k": 31.0, "pred_half": 153.0},
        }
        self.table = monthly(cache)
        self.out = display_table(self.table)

    def test_steps_are_per_day(self):
        # moyenne journalière du mois, et non la somme
        self.assertAlmostEqual(float(self.table["steps"].iloc[0]), 8500.0)
        self.assertEqual(int(self.out["steps"].iloc[0]), 8500)

    def test_durations_are_formatted(self):
        self.assertEqual(self.out["sleep_hours"].iloc[0], "07:47")   # moyenne de 8.05 et 7.5 h
        self.assertEqual(self.out["pred_5k"].iloc[0], "00:30:57")    # moyenne de 30.9 et 31.0 min
        self.assertEqual(self.out["pred_half"].iloc[0], "02:32:39")

    def test_missing_values_stay_empty(self):
        out = display_table(monthly({"2024-03-01": {"rhr": 50}}))
        self.assertEqual(out["pred_5k"].iloc[0], "")
        self.assertEqual(out["sleep_hours"].iloc[0], "")
        self.assertTrue(pd.isna(out["steps"].iloc[0]))


class TestMissingColumns(unittest.TestCase):
    """Un cache sans indicateurs optionnels ne doit faire échouer aucun script."""

    def test_monthly_tolerates_missing_columns(self):
        table = monthly(PARTIAL_CACHE)
        for c in ("steps", "sleep_score", "sleep_hours", "pred_5k", "pred_half"):
            self.assertIn(c, table.columns)
            self.assertTrue(table[c].isna().all())

    def test_monthly_on_empty_cache(self):
        table = monthly({})
        self.assertIn("pred_5k", table.columns)
        self.assertEqual(len(table), 0)

    def test_synthese_semaine_tolerates_partial_cache(self):
        out = synthese_semaine(PARTIAL_CACHE)
        self.assertIn("SYNTHÈSE DE LA SEMAINE", out)
        self.assertIn("Prédiction 5K", out)

    def test_load_daily_tolerates_partial_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cache.json"
            p.write_text(json.dumps(PARTIAL_CACHE), encoding="utf-8")
            df, n_off = analysis_fine.load_daily(p, 50.0)
        for c in ("steps", "sleep_score", "sleep_hours", "pred_5k", "pred_half"):
            self.assertIn(c, df.columns)
            self.assertTrue(df[c].isna().all())
        self.assertIn("total", df.columns)

    def test_weekly_keeps_only_present_indicators(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cache.json"
            p.write_text(json.dumps(PARTIAL_CACHE), encoding="utf-8")
            df, _ = analysis_fine.load_daily(p, 50.0)
        weekly = analysis_fine.to_weekly(df, 4)
        # aucune colonne optionnelle ne doit avoir été reconstruite d'un coup
        for c in ("pred_5k", "pred_half", "sleep_score"):
            if c in weekly.columns:
                self.assertTrue(weekly[c].isna().all())


if __name__ == "__main__":
    unittest.main()
