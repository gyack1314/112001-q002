"""Metrics: ratios only between comparable readings; no zero-filling."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

import unittest

from fixtures import jan, partial_feb
from facility_metrics import MetricPolicy, compute_metrics, load_readings


POLICY = MetricPolicy(version="test")


def metrics_for(rows, policy=POLICY):
    result = load_readings(rows, policy, source_file="f")
    return {(row.month, row.park, row.facility, row.level): row for row in compute_metrics(result.observations, policy)}


class ComparableRatioTest(unittest.TestCase):
    def test_full_month_produces_pue_and_capacity(self):
        rows = {key: value for key, value in metrics_for([jan()]).items()}
        facility = rows[("2026-01", "P01", "F1", "facility")]
        self.assertAlmostEqual(facility.energy_kwh, 1_000_000)
        self.assertAlmostEqual(facility.pue, 1.25)
        self.assertEqual(facility.pue_status, "complete")
        self.assertAlmostEqual(facility.capacity_eflops, 2.0)
        self.assertAlmostEqual(facility.energy_per_eflops_kwh, 500_000)

    def test_partial_month_energy_is_kept_apart_from_the_total(self):
        rows = metrics_for([partial_feb()])
        facility = rows[("2026-02", "P01", "F1", "facility")]
        self.assertIsNone(facility.energy_kwh)
        self.assertEqual(facility.energy_status, "unavailable")
        self.assertAlmostEqual(facility.partial_energy_kwh, 600_000)
        self.assertIsNone(facility.pue)
        self.assertIsNone(facility.energy_per_eflops_kwh)

    def test_partial_capacity_is_flagged(self):
        rows = metrics_for([partial_feb()])
        facility = rows[("2026-02", "P01", "F1", "facility")]
        self.assertAlmostEqual(facility.capacity_eflops, 2.0)
        self.assertEqual(facility.capacity_status, "partial")
        self.assertTrue(facility.capacity_partial)

    def test_pue_requires_the_same_observations_for_energy_and_it_energy(self):
        rows = metrics_for([jan(), partial_feb()])
        # February energy is excluded (partial) but its IT energy would be
        # excluded too; the cell must not borrow January's IT energy.
        facility = rows[("2026-02", "P01", "F1", "facility")]
        self.assertIsNone(facility.pue)
        self.assertEqual(facility.pue_reason, "missing_energy")

    def test_missing_it_energy_yields_unavailable_pue_not_zero(self):
        rows = metrics_for([dict(jan(), it_energy=None)])
        facility = rows[("2026-01", "P01", "F1", "facility")]
        self.assertIsNone(facility.pue)
        self.assertEqual(facility.pue_status, "unavailable")
        self.assertEqual(facility.pue_reason, "missing_it_energy")

    def test_zero_capacity_is_excluded_not_dividing_by_zero(self):
        rows = metrics_for([jan(capacity=0)])
        facility = rows[("2026-01", "P01", "F1", "facility")]
        self.assertIsNone(facility.capacity_eflops)
        self.assertIn(
            "nonpositive_capacity",
            {reason["reason"] for obs in load_readings([jan(capacity=0)], POLICY).observations for reason in obs.exclusion_details()},
        )


class DowntimeTest(unittest.TestCase):
    def test_downtime_energy_does_not_enter_the_total(self):
        rows = metrics_for([jan(), jan(facility="F2", status="downtime", energy=9999, it_energy=9999)])
        park = rows[("2026-01", "P01", None, "park")]
        self.assertAlmostEqual(park.energy_kwh, 1_000_000)
        facility_f2 = rows[("2026-01", "P01", "F2", "facility")]
        self.assertIsNone(facility_f2.energy_kwh)
        self.assertTrue(any(item["reasons"].count("downtime") for item in facility_f2.excluded))

    def test_downtime_policy_can_keep_outage_rows(self):
        policy = MetricPolicy(version="keep-outages", downtime_excluded=False)
        result = load_readings([jan(status="downtime")], policy, source_file="f")
        self.assertNotIn("downtime", result.observations[0].exclusions)
        rows = compute_metrics(result.observations, policy)
        self.assertAlmostEqual(rows[0].energy_kwh, 1_000_000)


class LevelTest(unittest.TestCase):
    def test_park_and_facility_rows_are_not_added_together(self):
        rows = metrics_for(
            [
                jan(facility=None, energy=1200, it_energy=960, capacity=3.0),
                jan(facility="F1", energy=1000, it_energy=800, capacity=2.0),
            ]
        )
        park = rows[("2026-01", "P01", None, "park")]
        # Facility sub-meters win; the park meter is evidence, not an addend.
        self.assertAlmostEqual(park.energy_kwh, 1_000_000)
        self.assertAlmostEqual(park.capacity_eflops, 2.0)
        self.assertTrue(any(item["reasons"] == ["park_level_overridden"] for item in park.excluded))

    def test_policy_can_prefer_the_park_meter(self):
        policy = MetricPolicy(version="park", level_precedence="park")
        rows = metrics_for(
            [
                jan(facility=None, energy=1200, it_energy=960, capacity=3.0),
                jan(facility="F1", energy=1000, it_energy=800, capacity=2.0),
            ],
            policy,
        )
        park = rows[("2026-01", "P01", None, "park")]
        self.assertAlmostEqual(park.energy_kwh, 1_200_000)
        self.assertAlmostEqual(park.pue, 1.25)

    def test_multi_facility_park_sums_energy_and_capacity(self):
        rows = metrics_for(
            [
                jan(facility="F1", energy=1000, it_energy=800, capacity=2.0),
                jan(facility="F2", energy=500, it_energy=400, capacity=1.0),
            ]
        )
        park = rows[("2026-01", "P01", None, "park")]
        self.assertAlmostEqual(park.energy_kwh, 1_500_000)
        self.assertAlmostEqual(park.capacity_eflops, 3.0)
        self.assertAlmostEqual(park.pue, 1.25)

    def test_park_capacity_flagged_when_a_facility_lacks_capacity(self):
        rows = metrics_for(
            [
                jan(facility="F1", energy=1000, it_energy=800, capacity=2.0),
                jan(facility="F2", energy=500, it_energy=400, capacity=None),
            ]
        )
        park = rows[("2026-01", "P01", None, "park")]
        self.assertEqual(park.capacity_status, "partial")
        self.assertEqual(park.capacity_missing_facilities, ["F2"])
        self.assertIsNone(park.energy_per_eflops_kwh)


if __name__ == "__main__":
    unittest.main()
