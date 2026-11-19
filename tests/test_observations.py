"""Input contract: normalization without silent loss."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import unittest

from fixtures import jan
from facility_metrics import MetricPolicy, load_readings
from facility_metrics.windows import Window


POLICY = MetricPolicy(version="test")


class WindowTest(unittest.TestCase):
    def test_inclusive_end_of_day_becomes_half_open_boundary(self):
        window = Window.from_reported("2026-01-01T00:00:00", "2026-01-31T23:59:59")
        self.assertEqual(window.end.isoformat(), "2026-02-01T00:00:00+00:00")
        self.assertEqual(window.duration_hours, 744.0)
        self.assertEqual(window.coverage, 1.0)
        self.assertFalse(window.crosses_month)

    def test_exclusive_midnight_end_is_not_advanced(self):
        window = Window.from_reported("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
        self.assertEqual(window.duration_hours, 744.0)

    def test_two_week_window_is_partial(self):
        window = Window.from_reported("2026-01-10T00:00:00", "2026-01-31T23:59:59")
        self.assertLess(window.coverage, 1.0)
        self.assertGreater(window.coverage, 0.5)

    def test_window_crossing_month_is_flagged(self):
        window = Window.from_reported("2026-01-15T00:00:00", "2026-02-14T23:59:59")
        self.assertTrue(window.crosses_month)


class NormalizationTest(unittest.TestCase):
    def test_units_are_converted_and_originals_kept(self):
        rows = [jan(unit="kWh", energy=1_000_000, capacity=2000, capacity_unit="PFLOPS")]
        obs = load_readings(rows, POLICY, source_file="f").observations[0]
        self.assertAlmostEqual(obs.energy_kwh, 1_000_000)
        self.assertAlmostEqual(obs.capacity_eflops, 2.0)
        self.assertEqual(obs.energy_unit_original, "kWh")
        self.assertEqual(obs.capacity_unit_original, "PFLOPS")

    def test_unknown_unit_is_excluded_not_coerced(self):
        rows = [jan(unit="buckets")]
        result = load_readings(rows, POLICY, source_file="f")
        obs = result.observations[0]
        self.assertIn("invalid_energy_unit", obs.exclusions)
        self.assertIsNone(obs.energy_kwh)
        self.assertFalse(obs.has_valid_energy)

    def test_malformed_record_is_rejected_with_payload(self):
        rows = [jan(window_end="not-a-time")]
        result = load_readings(rows, POLICY, source_file="f")
        self.assertEqual(result.observations, [])
        self.assertEqual(len(result.rejections), 1)
        self.assertEqual(result.rejections[0].reason, "invalid_window")
        self.assertEqual(result.rejections[0].raw["window_end"], "not-a-time")

    def test_missing_energy_stays_missing_and_is_flagged(self):
        rows = [dict(jan(), energy=None)]
        obs = load_readings(rows, POLICY, source_file="f").observations[0]
        self.assertIsNone(obs.energy_kwh)
        self.assertIn("missing_energy", obs.exclusions)


class SupersessionTest(unittest.TestCase):
    def test_calibration_replaces_without_erasing_the_original(self):
        rows = [
            jan(reading_id="r1"),
            jan(reading_id="r2", kind="calibration", energy=1100),
        ]
        result = load_readings(rows, POLICY, source_file="f")
        by_id = result.by_id()
        self.assertFalse(by_id["f#0"].effective)
        self.assertEqual(by_id["f#0"].replaced_by, by_id["f#1"].observation_id)
        self.assertTrue(by_id["f#1"].effective)
        self.assertEqual(by_id["f#1"].replaces, by_id["f#0"].observation_id)
        self.assertAlmostEqual(by_id["f#1"].energy_kwh, 1_100_000)

    def test_later_revision_wins_within_same_kind(self):
        rows = [
            jan(submitted_at="2026-02-02T00:00:00Z", energy=1000),
            jan(submitted_at="2026-03-02T00:00:00Z", energy=1200, revision=2),
        ]
        result = load_readings(rows, POLICY, source_file="f")
        effective = [obs for obs in result.observations if obs.effective]
        self.assertEqual(len(effective), 1)
        self.assertAlmostEqual(effective[0].energy_kwh, 1_200_000)

    def test_downtime_notice_does_not_supersede_the_reading(self):
        rows = [jan(), jan(status="downtime", energy=None, it_energy=None)]
        result = load_readings(rows, POLICY, source_file="f")
        self.assertTrue(result.by_id()["f#0"].effective)
        self.assertTrue(result.by_id()["f#1"].effective)
        self.assertIn("downtime", result.by_id()["f#1"].exclusions)

    def test_cutoff_keeps_historical_batch_stable(self):
        rows = [
            jan(reading_id="base", submitted_at="2026-02-02T00:00:00Z", energy=1000),
            jan(reading_id="late", kind="calibration", submitted_at="2026-06-01T00:00:00Z", energy=1400),
        ]
        from facility_metrics import parse_instant

        historical = load_readings(
            rows, POLICY, source_file="f", cutoff=parse_instant("2026-03-01T00:00:00Z")
        )
        by_id = historical.by_id()
        self.assertTrue(by_id["f#0"].effective)
        self.assertFalse(by_id["f#1"].effective)
        self.assertIn("after_cutoff", by_id["f#1"].exclusions)
        self.assertAlmostEqual(by_id["f#0"].energy_kwh, 1_000_000)

        current = load_readings(rows, POLICY, source_file="f")
        self.assertAlmostEqual(current.by_id()["f#1"].energy_kwh, 1_400_000)


if __name__ == "__main__":
    unittest.main()
