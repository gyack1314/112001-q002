"""Batches, audit trails and policy comparison."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from fixtures import jan, partial_feb
from facility_metrics import MetricPolicy, build_batch, compare_batches, explain_cell, policy_diff, trace_observation
from facility_metrics.batch import read_batch

POLICY = MetricPolicy(version="v1")


def _batch(rows, policy=POLICY, batch_id="b1", name="jan.json", **kwargs):
    payloads = {name: json.dumps(rows).encode("utf-8")}
    return build_batch(
        payloads,
        policy,
        batch_id=batch_id,
        created_at=datetime(2026, 3, 1, tzinfo=timezone.utc),
        **kwargs,
    )


class BatchTest(unittest.TestCase):
    def test_batch_records_definition_inputs_and_digests(self):
        batch = _batch([jan()]).as_dict()
        self.assertEqual(batch["policy_version"], "v1")
        self.assertEqual(batch["policy_digest"], batch["policy_digest"])
        self.assertEqual(len(batch["inputs"][0]["sha256"]), 64)
        self.assertEqual(batch["inputs"][0]["record_count"], 1)
        self.assertEqual(batch["created_at"], "2026-03-01T00:00:00Z")

    def test_written_batch_is_immutable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b1.json"
            batch = _batch([jan()])
            batch.write(path)
            with self.assertRaises(FileExistsError):
                batch.write(path)
            loaded = read_batch(path)
            self.assertEqual(loaded["batch_id"], "b1")

    def test_rejections_are_preserved_in_the_batch(self):
        batch = _batch([jan(window_start=None)]).as_dict()
        self.assertEqual(len(batch["rejections"]), 1)
        self.assertEqual(batch["rejections"][0]["reason"], "invalid_window")
        self.assertIsNone(batch["rejections"][0]["raw"]["window_start"])


class ExplainTest(unittest.TestCase):
    def test_explain_lists_inputs_and_exclusions(self):
        batch = _batch(
            [
                jan(reading_id="base"),
                jan(reading_id="corrected", kind="calibration", energy=1100),
                jan(facility="F2", status="downtime", energy=5, it_energy=4),
            ]
        ).as_dict()
        cell = explain_cell(batch, month="2026-01", park="P01", facility=None, level="park")
        self.assertAlmostEqual(cell["values"]["energy_kwh"], 1_100_000)
        included_ids = [item["observation_id"] for item in cell["included"]["energy"]]
        self.assertEqual(included_ids, ["jan.json#1"])
        reasons = {
            item["observation_id"]: item["reasons"] for item in cell["excluded"]
        }
        self.assertIn("superseded", reasons["jan.json#0"])
        self.assertIn("downtime", reasons["jan.json#2"])
        self.assertTrue(all(item["explanation"] for item in cell["excluded"]))

    def test_explain_unknown_cell_raises(self):
        batch = _batch([jan()]).as_dict()
        with self.assertRaises(KeyError):
            explain_cell(batch, month="2026-12", park="P01", facility=None, level="park")

    def test_trace_follows_calibration_chain(self):
        batch = _batch([jan(reading_id="old"), jan(reading_id="new", kind="calibration", energy=1100)]).as_dict()
        trace = trace_observation(batch, "jan.json#0")
        self.assertFalse(trace["is_effective"])
        self.assertEqual(trace["superseded_by"]["observation_id"], "jan.json#1")
        self.assertEqual([step["observation_id"] for step in trace["chain"]], ["jan.json#0", "jan.json#1"])


class CompareTest(unittest.TestCase):
    def test_policy_change_is_reported_alongside_metric_movement(self):
        old = _batch([partial_feb()], policy=MetricPolicy(version="v1")).as_dict()
        new_policy = MetricPolicy(version="v2", capacity_weighting="simple")
        new = _batch([partial_feb()], policy=new_policy, batch_id="b2").as_dict()

        diff = policy_diff(old, new)
        self.assertTrue(diff["changed"])
        self.assertEqual(diff["changes"]["capacity_weighting"], {"old": "duration", "new": "simple"})

        comparison = compare_batches(old, new)
        self.assertEqual(comparison["policy_version_new"], "v2")
        self.assertIn("capacity_weighting", comparison["policy_changes"])

    def test_input_change_is_reported(self):
        old = _batch([jan(energy=1000)], batch_id="b1").as_dict()
        new = _batch([jan(energy=1200)], batch_id="b2", name="jan.json").as_dict()
        comparison = compare_batches(old, new)
        self.assertEqual(len(comparison["input_changes"]), 1)
        movement = comparison["metric_movements"][0]
        self.assertEqual(movement["status"], "changed")
        self.assertEqual(movement["fields"]["energy_kwh"]["new"], 1_200_000)
        self.assertAlmostEqual(movement["fields"]["energy_kwh"]["delta"], 200_000)

    def test_unchanged_batches_have_no_movements(self):
        old = _batch([jan()], batch_id="b1").as_dict()
        comparison = compare_batches(old, old)
        self.assertEqual(comparison["metric_movements"], [])
        self.assertEqual(comparison["input_changes"], [])


if __name__ == "__main__":
    unittest.main()
