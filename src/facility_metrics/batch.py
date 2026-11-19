"""Reporting batches: a snapshot of definitions and results that can be
re-explained months later.

A batch is immutable once written.  It records

* the policy document (every definition input, including its version),
* the exact raw input files and their SHA-256 digests,
* the cutoff that bounds which submissions were visible,
* the resulting observations and metrics, plus every rejection and exclusion.

Comparing two batches shows what changed in the definitions and what changed
in the numbers, without recomputing or "fixing" the earlier one.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .loader import LoadResult, load_readings
from .metrics import MetricRow, compute_metrics
from .models import MetricPolicy, Observation

BATCH_SCHEMA_VERSION = 1


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _digest(obj: Any) -> str:
    return sha256_bytes(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8"))


@dataclass(frozen=True)
class InputFile:
    name: str
    sha256: str
    record_count: int

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "sha256": self.sha256, "record_count": self.record_count}


@dataclass
class Batch:
    batch_id: str
    created_at: str
    policy: MetricPolicy
    cutoff: str | None
    inputs: list[InputFile]
    observations: list[Observation]
    metrics: list[MetricRow]
    rejections: list[dict[str, Any]]
    notes: str | None = None

    def as_dict(self) -> dict[str, Any]:
        policy_doc = self.policy.as_dict()
        return {
            "schema_version": BATCH_SCHEMA_VERSION,
            "batch_id": self.batch_id,
            "created_at": self.created_at,
            "policy": policy_doc,
            "policy_version": self.policy.version,
            "policy_digest": _digest(policy_doc),
            "cutoff": self.cutoff,
            "inputs": [item.as_dict() for item in self.inputs],
            "metrics": [row.as_dict() for row in self.metrics],
            "observations": [obs.as_dict() for obs in self.observations],
            "rejections": list(self.rejections),
            "notes": self.notes,
        }

    def write(self, path: Path) -> None:
        if path.exists():
            raise FileExistsError(
                f"{path} already exists; a reporting batch is immutable -- "
                "write the new result under a new batch id"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.as_dict(), ensure_ascii=False, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )


def read_batch(path: Path) -> Mapping[str, Any]:
    """Read a serialized batch for audit or comparison (no recomputation)."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != BATCH_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported batch schema {payload.get('schema_version')!r}; expected {BATCH_SCHEMA_VERSION}"
        )
    for required in ("batch_id", "policy", "metrics", "observations"):
        if required not in payload:
            raise ValueError(f"batch file is missing {required!r}")
    return payload


def build_batch(
    inputs: Mapping[str, bytes],
    policy: MetricPolicy,
    *,
    batch_id: str,
    created_at: datetime | None = None,
    cutoff: datetime | None = None,
    notes: str | None = None,
) -> Batch:
    """Build a batch from named raw JSON payloads (one list of records each)."""

    if not batch_id.strip():
        raise ValueError("batch_id is required")
    observations: list[Observation] = []
    rejections: list[dict[str, Any]] = []
    input_files: list[InputFile] = []

    for name in sorted(inputs):
        payload = inputs[name]
        rows = json.loads(payload.decode("utf-8"))
        if not isinstance(rows, list):
            raise ValueError(f"{name}: expected a JSON array of records")
        result: LoadResult = load_readings(
            rows, policy, source_file=name, cutoff=cutoff
        )
        observations.extend(result.observations)
        rejections.extend(item.as_dict() for item in result.rejections)
        input_files.append(
            InputFile(name=name, sha256=sha256_bytes(payload), record_count=len(rows))
        )

    metrics = compute_metrics(observations, policy)
    created = created_at or datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return Batch(
        batch_id=batch_id,
        created_at=created.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        policy=policy,
        cutoff=cutoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if cutoff else None,
        inputs=input_files,
        observations=observations,
        metrics=metrics,
        rejections=rejections,
        notes=notes,
    )


def _metric_index(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str, str | None, str], Mapping[str, Any]]:
    return {
        (row["month"], row["park"], row["facility"], row["level"]): row for row in rows
    }


def compare_batches(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """Explain the difference between two serialized batches.

    Returns definition changes, input changes and per-cell metric movements.
    A metric cell is a (month, park, facility, level) tuple; movements are
    reported as old value, new value and delta, with an explicit note when a
    value was unavailable in one batch.
    """

    old_rows = _metric_index(old.get("metrics", []))
    new_rows = _metric_index(new.get("metrics", []))
    cells = sorted(set(old_rows) | set(new_rows), key=lambda key: tuple(str(part) for part in key))

    fields = ("energy_kwh", "it_energy_kwh", "capacity_eflops", "pue", "energy_per_eflops_kwh")
    movements: list[dict[str, Any]] = []
    for key in cells:
        before = old_rows.get(key)
        after = new_rows.get(key)
        entry: dict[str, Any] = {
            "month": key[0],
            "park": key[1],
            "facility": key[2],
            "level": key[3],
        }
        if before is None:
            entry["status"] = "added"
            movements.append(entry)
            continue
        if after is None:
            entry["status"] = "removed"
            movements.append(entry)
            continue
        changed: dict[str, Any] = {}
        for field in fields:
            old_value = before.get(field)
            new_value = after.get(field)
            if old_value == new_value:
                continue
            changed[field] = {
                "old": old_value,
                "new": new_value,
                "delta": (
                    round(new_value - old_value, 9)
                    if isinstance(old_value, (int, float))
                    and isinstance(new_value, (int, float))
                    else None
                ),
            }
        if changed:
            entry["status"] = "changed"
            entry["fields"] = changed
            movements.append(entry)

    policy_changes: dict[str, dict[str, Any]] = {}
    old_policy = old.get("policy", {})
    new_policy = new.get("policy", {})
    for key in sorted(set(old_policy) | set(new_policy)):
        if old_policy.get(key) != new_policy.get(key):
            policy_changes[key] = {"old": old_policy.get(key), "new": new_policy.get(key)}

    input_changes: list[dict[str, Any]] = []
    old_inputs = {item["name"]: item for item in old.get("inputs", [])}
    new_inputs = {item["name"]: item for item in new.get("inputs", [])}
    for name in sorted(set(old_inputs) | set(new_inputs)):
        before = old_inputs.get(name)
        after = new_inputs.get(name)
        if before == after:
            continue
        input_changes.append(
            {
                "name": name,
                "old_sha256": before.get("sha256") if before else None,
                "new_sha256": after.get("sha256") if after else None,
                "old_record_count": before.get("record_count") if before else None,
                "new_record_count": after.get("record_count") if after else None,
            }
        )

    return {
        "old_batch_id": old.get("batch_id"),
        "new_batch_id": new.get("batch_id"),
        "policy_version_old": old.get("policy_version"),
        "policy_version_new": new.get("policy_version"),
        "policy_changes": policy_changes,
        "input_changes": input_changes,
        "metric_movements": movements,
    }
