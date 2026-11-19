"""Audit and lineage helpers: why a summary has the value it has.

These functions answer the reviewer's questions directly:

* ``explain_cell`` -- which observations constitute one metric row, which
  were excluded and why;
* ``trace_observation`` -- what a single reading became, including whether it
  was superseded, by what, and what it replaced;
* ``policy_diff`` -- which definition changed between two batches.

Everything works from a serialized batch (or a built :class:`Batch`), so an
already reported result can be explained without recomputing it.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

EXCLUSION_DETAILS = {
    "partial_window": "窗口未覆盖完整自然月，能耗不可与整月比较",
    "crosses_month": "窗口跨越月份边界",
    "downtime": "设备在窗口内停运，数值不作为正常能耗",
    "invalid_window": "窗口无法解析",
    "invalid_energy_unit": "能耗单位无法换算",
    "invalid_it_energy_unit": "IT 能耗单位无法换算",
    "invalid_capacity_unit": "算力单位无法换算",
    "missing_energy": "缺少能耗读数",
    "missing_it_energy": "缺少 IT 能耗读数，无法计算 PUE",
    "missing_capacity": "缺少算力容量读数",
    "nonpositive_capacity": "算力容量为零或负值",
    "negative_energy": "能耗为负值",
    "superseded": "同一窗口存在更新的有效版本",
    "after_cutoff": "提交时间晚于该报送批次截止时间",
    "park_level_overridden": "该园区当月已有设施级读数，园区级读数不重复计入",
    "park_meter_preferred": "按口径优先使用园区级电表，设施级读数不重复计入",
    "invalid_record": "记录结构不合法",
}


def _batch_payload(batch: Any) -> Mapping[str, Any]:
    if isinstance(batch, Mapping):
        return batch
    if hasattr(batch, "as_dict"):
        return batch.as_dict()
    raise TypeError("expected a serialized batch mapping or a Batch object")


def explain_cell(
    batch: Any,
    *,
    month: str,
    park: str,
    facility: str | None = None,
    level: str = "facility",
) -> dict[str, Any]:
    """Explain exactly one summary cell."""

    payload = _batch_payload(batch)
    target = None
    for row in payload.get("metrics", []):
        if (
            row["month"] == month
            and row["park"] == park
            and row["facility"] == facility
            and row["level"] == level
        ):
            target = row
            break
    if target is None:
        raise KeyError(f"no metric row for {month}/{park}/{facility}/{level}")

    observations = {
        obs["observation_id"]: obs for obs in payload.get("observations", [])
    }

    def describe(ids: Iterable[str]) -> list[dict[str, Any]]:
        described = []
        for obs_id in ids:
            obs = observations.get(obs_id)
            if obs is None:
                described.append({"observation_id": obs_id, "missing_from_batch": True})
                continue
            described.append(
                {
                    "observation_id": obs_id,
                    "reading_id": obs["reading_id"],
                    "source_file": obs["source_file"],
                    "window_start": obs["window_start"],
                    "window_end": obs["window_end"],
                    "energy_kwh": obs["energy_kwh"],
                    "it_energy_kwh": obs["it_energy_kwh"],
                    "capacity_eflops": obs["capacity_eflops"],
                    "status": obs["status"],
                    "kind": obs["kind"],
                    "revision": obs["revision"],
                }
            )
        return described

    return {
        "cell": {
            "month": month,
            "park": park,
            "facility": facility,
            "level": level,
            "policy_version": payload.get("policy_version"),
            "batch_id": payload.get("batch_id"),
        },
        "values": {
            key: target.get(key)
            for key in (
                "energy_kwh",
                "energy_status",
                "it_energy_kwh",
                "pue",
                "pue_status",
                "pue_reason",
                "capacity_eflops",
                "capacity_status",
                "energy_per_eflops_kwh",
                "partial_energy_kwh",
                "capacity_missing_facilities",
                "level_fallback",
            )
        },
        "included": {
            "energy": describe(target.get("energy_observations", [])),
            "it_energy": describe(target.get("it_energy_observations", [])),
            "capacity": describe(target.get("capacity_observations", [])),
            "partial_energy": describe(target.get("partial_energy_observations", [])),
        },
        "excluded": [
            {
                "observation_id": item["observation_id"],
                "source_file": item.get("source_file"),
                "reasons": item.get("reasons", []),
                "explanation": [
                    EXCLUSION_DETAILS.get(reason, reason) for reason in item.get("reasons", [])
                ],
                "detail": item.get("detail", ""),
            }
            for item in target.get("excluded", [])
        ],
    }


def trace_observation(batch: Any, observation_id: str) -> dict[str, Any]:
    """Follow one observation through supersession and calibration."""

    payload = _batch_payload(batch)
    observations = {obs["observation_id"]: obs for obs in payload.get("observations", [])}
    obs = observations.get(observation_id)
    if obs is None:
        raise KeyError(f"no observation {observation_id!r} in batch {payload.get('batch_id')}")

    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    current = obs
    while current is not None and current["observation_id"] not in seen:
        seen.add(current["observation_id"])
        chain.append(
            {
                "observation_id": current["observation_id"],
                "reading_id": current["reading_id"],
                "effective": current["effective"],
                "kind": current["kind"],
                "revision": current["revision"],
                "submitted_at": current.get("submitted_at"),
                "energy_kwh": current["energy_kwh"],
                "it_energy_kwh": current["it_energy_kwh"],
                "capacity_eflops": current["capacity_eflops"],
                "replaced_by": current.get("replaced_by"),
                "replaces": current.get("replaces"),
            }
        )
        next_id = current.get("replaced_by")
        current = observations.get(next_id) if next_id else None

    superseded_by = obs.get("replaced_by")
    replaces = obs.get("replaces")
    return {
        "observation_id": observation_id,
        "chain": chain,
        "is_effective": obs.get("effective"),
        "superseded_by": observations.get(superseded_by) if superseded_by else None,
        "replaces": observations.get(replaces) if replaces else None,
        "exclusions": obs.get("exclusions", []),
        "source_file": obs.get("source_file"),
    }


def policy_diff(old: Any, new: Any) -> dict[str, Any]:
    """Which definition inputs changed, and how that changes the meaning."""

    old_doc = _batch_payload(old).get("policy", {})
    new_doc = _batch_payload(new).get("policy", {})
    changes: dict[str, dict[str, Any]] = {}
    for key in sorted(set(old_doc) | set(new_doc)):
        if old_doc.get(key) != new_doc.get(key):
            changes[key] = {"old": old_doc.get(key), "new": new_doc.get(key)}
    return {
        "old_policy_version": _batch_payload(old).get("policy_version"),
        "new_policy_version": _batch_payload(new).get("policy_version"),
        "changes": changes,
        "changed": bool(changes),
    }
