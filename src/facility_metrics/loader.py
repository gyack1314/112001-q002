"""Load raw submissions into normalized observations.

The loader is deliberately lossless on the way in:

* a malformed row is not dropped silently -- it becomes a :class:`Rejection`
  carrying the original payload and a reason;
* a well-formed row becomes an :class:`Observation` whose ``exclusions`` say
  whether it may enter a ratio or a monthly sum, and why not;
* calibration rows and later revisions are matched to the window they correct
  and marked effective, without overwriting the original version.

Nothing here computes a metric.  It only decides, per reading, what the row
*means* and whether it is comparable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .models import MetricPolicy, Observation, ObservationStatus, Reading
from .units import UnitError, capacity_to_eflops, energy_to_kwh
from .windows import Window, WindowError, parse_instant


class InputError(ValueError):
    """Raised when a single input record is unusable."""


@dataclass(frozen=True)
class Rejection:
    """A submission that could not become an observation."""

    source_file: str
    index: int
    observation_ref: str
    reason: str
    detail: str
    raw: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "index": self.index,
            "observation_ref": self.observation_ref,
            "reason": self.reason,
            "detail": self.detail,
            "raw": dict(self.raw),
        }


@dataclass
class LoadResult:
    observations: list[Observation] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)

    def reject(
        self,
        source_file: str,
        index: int,
        reason: str,
        detail: str,
        raw: Mapping[str, Any],
    ) -> None:
        self.rejections.append(
            Rejection(
                source_file=source_file,
                index=index,
                observation_ref=f"{source_file}#{index}",
                reason=reason,
                detail=detail,
                raw=raw,
            )
        )

    def by_id(self) -> dict[str, Observation]:
        return {obs.observation_id: obs for obs in self.observations}

    def by_reading_id(self) -> dict[str, Observation]:
        return {obs.reading_id: obs for obs in self.observations}


def _number(raw: Mapping[str, Any], key: str) -> float | None:
    value = raw.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InputError(f"{key} must be a number, got {type(value).__name__}")
    return float(value)


def _optional_text(raw: Mapping[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InputError(f"{key} must be a string")
    value = value.strip()
    return value or None


def _required_text(raw: Mapping[str, Any], key: str) -> str:
    value = _optional_text(raw, key)
    if value is None:
        raise InputError(f"missing {key}")
    return value


def _parse_optional_instant(raw: Mapping[str, Any], key: str) -> datetime | None:
    value = _optional_text(raw, key)
    return parse_instant(value) if value else None


def _derive_status(declared: str | None, kind: str) -> ObservationStatus:
    if kind == "calibration":
        return "calibration"
    if declared is None or declared in ("complete", "normal"):
        return "complete"
    if declared in ("partial", "downtime"):
        return declared  # type: ignore[return-value]
    raise InputError(f"unknown status: {declared!r}")


def parse_reading(raw: Mapping[str, Any], source_file: str, index: int) -> tuple[Reading, Window]:
    if not isinstance(raw, Mapping):
        raise InputError("record is not an object")
    window = Window.from_reported(raw.get("window_start"), raw.get("window_end"))
    kind_value = (_optional_text(raw, "kind") or "reading").lower()
    if kind_value not in ("reading", "calibration"):
        raise InputError(f"unknown kind: {kind_value!r}")
    revision_raw = raw.get("revision", 1)
    if isinstance(revision_raw, bool) or not isinstance(revision_raw, int) or revision_raw < 1:
        raise InputError("revision must be a positive integer")
    declared = (_optional_text(raw, "status") or "").lower() or None
    reading = Reading(
        reading_id=_optional_text(raw, "reading_id") or f"{source_file}#{index}",
        source_file=source_file,
        sequence=index,
        park=_required_text(raw, "park"),
        facility=_optional_text(raw, "facility"),
        window_start=window.start,
        window_end=window.end,
        energy=_number(raw, "energy"),
        energy_unit=_optional_text(raw, "unit") or "",
        it_energy=_number(raw, "it_energy"),
        it_energy_unit=_optional_text(raw, "it_energy_unit") or "",
        capacity=_number(raw, "capacity"),
        capacity_unit=_optional_text(raw, "capacity_unit") or "",
        kind=kind_value,  # type: ignore[arg-type]
        declared_status=declared,
        submitted_at=_parse_optional_instant(raw, "submitted_at"),
        revision=revision_raw,
        supersedes=_optional_text(raw, "supersedes"),
        note=_optional_text(raw, "note"),
    )
    # Fail on an unknown declared status during parsing rather than later.
    _derive_status(declared, reading.kind)
    return reading, window


def version_order(reading: Reading) -> tuple[datetime, int, int, str]:
    """Sort key for choosing an effective version.

    Later submission wins; without timestamps, the revision number decides so
    a file with no timestamps is still deterministic.  Source sequence is the
    tiebreak before the id, so "last row in the file" is meaningful, and the
    id is the final stable tiebreak.
    """

    submitted = reading.submitted_at or datetime.min.replace(tzinfo=timezone.utc)
    return (submitted, reading.revision, reading.sequence, reading.reading_id)


def _slot(reading: Reading) -> tuple[str, str, str, str, str]:
    """Identity of the fact a reading states.

    Downtime notices are their own fact family: a monthly reading and an
    outage notice for the same window do not supersede each other, so both
    survive and the outage keeps its business meaning.
    """

    family = "downtime" if reading.declared_status == "downtime" else "measurement"
    return (
        reading.park,
        reading.facility or "",
        reading.window_start.isoformat(),
        reading.window_end.isoformat(),
        family,
    )


def choose_effective(
    readings: Iterable[Reading],
) -> dict[tuple[str, str, str, str, str], Reading]:
    """Pick one effective reading per (park, facility, window, fact family).

    A calibration anywhere in a measurement slot beats plain readings; among
    calibrations (or among plains), the latest version wins.  The explicit
    ``supersedes`` pointer is recorded on the observation but does not
    override the version ordering, so a mis-typed pointer cannot hide a row.
    """

    groups: dict[tuple[str, str, str, str, str], list[Reading]] = {}
    for reading in readings:
        groups.setdefault(_slot(reading), []).append(reading)
    chosen: dict[tuple[str, str, str, str, str], Reading] = {}
    for key, rows in groups.items():
        calibrations = [row for row in rows if row.kind == "calibration"]
        pool = calibrations or rows
        chosen[key] = max(pool, key=version_order)
    return chosen


def build_observation(reading: Reading, window: Window, policy: MetricPolicy) -> Observation:
    status = _derive_status(reading.declared_status, reading.kind)
    exclusions: list[str] = []

    energy_kwh: float | None = None
    if reading.energy is None:
        exclusions.append("missing_energy")
    else:
        try:
            energy_kwh = energy_to_kwh(reading.energy, reading.energy_unit)
        except UnitError:
            exclusions.append("invalid_energy_unit")
        else:
            if energy_kwh < 0:
                exclusions.append("negative_energy")

    it_energy_kwh: float | None = None
    if reading.it_energy is None:
        exclusions.append("missing_it_energy")
    else:
        try:
            it_energy_kwh = energy_to_kwh(reading.it_energy, reading.it_energy_unit)
        except UnitError:
            exclusions.append("invalid_it_energy_unit")
        else:
            if it_energy_kwh < 0:
                exclusions.append("negative_energy")

    capacity_eflops: float | None = None
    if reading.capacity is None:
        exclusions.append("missing_capacity")
    else:
        try:
            capacity_eflops = capacity_to_eflops(reading.capacity, reading.capacity_unit)
        except UnitError:
            exclusions.append("invalid_capacity_unit")
        else:
            if capacity_eflops <= 0:
                exclusions.append("nonpositive_capacity")

    if window.crosses_month:
        exclusions.append("crosses_month")
    # The policy may accept a slightly-short window as comparable.
    if window.coverage < policy.complete_coverage:
        exclusions.append("partial_window")
    if status == "partial" and "partial_window" not in exclusions:
        # An explicitly declared partial period is never comparable, even
        # when its stamped window happens to look complete.
        exclusions.append("partial_window")
    if status == "downtime" and policy.downtime_excluded:
        exclusions.append("downtime")

    return Observation(
        observation_id=reading.observation_id,
        reading_id=reading.reading_id,
        park=reading.park,
        facility=reading.facility,
        window_start=reading.window_start,
        window_end=reading.window_end,
        duration_hours=window.duration_hours,
        month=window.month,
        coverage=window.coverage,
        energy_kwh=energy_kwh,
        energy_unit_original=reading.energy_unit,
        it_energy_kwh=it_energy_kwh,
        it_energy_unit_original=reading.it_energy_unit,
        capacity_eflops=capacity_eflops,
        capacity_unit_original=reading.capacity_unit,
        status=status,
        kind=reading.kind,
        revision=reading.revision,
        submitted_at=reading.submitted_at,
        supersedes=reading.supersedes,
        note=reading.note,
        source_file=reading.source_file,
        sequence=reading.sequence,
        effective=True,
        replaced_by=None,
        replaces=None,
        exclusions=tuple(dict.fromkeys(exclusions)),
    )


def _replace(obs: Observation, **changes: Any) -> Observation:
    data = obs.__dict__.copy()
    data.update(changes)
    return Observation(**data)


def load_readings(
    records: Iterable[Mapping[str, Any]],
    policy: MetricPolicy,
    *,
    source_file: str = "<inline>",
    cutoff: datetime | None = None,
) -> LoadResult:
    """Turn raw records into observations, preserving every rejection reason.

    ``cutoff`` reproduces a historical reporting batch: readings submitted
    after it are kept for audit but marked ``after_cutoff`` and excluded, so
    re-running an old batch does not silently absorb later calibrations.
    """

    result = LoadResult()
    parsed: list[tuple[Reading, Window]] = []
    for index, raw in enumerate(records):
        try:
            parsed.append(parse_reading(raw, source_file, index))
        except (InputError, WindowError) as exc:
            reason = "invalid_window" if isinstance(exc, WindowError) else "invalid_record"
            result.reject(source_file, index, reason, str(exc), raw)

    if cutoff is not None and cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)

    def in_batch(reading: Reading) -> bool:
        return cutoff is None or reading.submitted_at is None or reading.submitted_at <= cutoff

    observations = [build_observation(reading, window, policy) for reading, window in parsed]

    # Version selection only sees readings the batch could have known about,
    # so a calibration submitted later never rewrites a closed batch.
    effective = choose_effective(reading for reading, _ in parsed if in_batch(reading))
    effective_ids = {reading.observation_id for reading in effective.values()}

    for idx, (reading, _) in enumerate(parsed):
        obs = observations[idx]
        if not in_batch(reading):
            # A later fact is kept and marked, not treated as superseding an
            # in-batch version.
            observations[idx] = _replace(
                obs,
                effective=False,
                exclusions=tuple(dict.fromkeys(obs.exclusions + ("after_cutoff",))),
            )
            continue
        if obs.observation_id in effective_ids:
            continue
        winner = effective.get(_slot(reading))
        observations[idx] = _replace(
            obs,
            effective=False,
            replaced_by=winner.observation_id if winner else None,
            exclusions=tuple(dict.fromkeys(obs.exclusions + ("superseded",))),
        )

    # Point each effective calibration at the plain reading it corrects.
    for idx, (reading, _) in enumerate(parsed):
        obs = observations[idx]
        if not (obs.effective and obs.kind == "calibration"):
            continue
        replaced_rows = [
            candidate
            for candidate, _ in parsed
            if candidate.kind == "reading"
            and candidate.park == reading.park
            and (candidate.facility or "") == (reading.facility or "")
            and candidate.window_start == reading.window_start
            and candidate.window_end == reading.window_end
            and in_batch(candidate)
        ]
        if replaced_rows:
            replaced = max(replaced_rows, key=version_order)
            observations[idx] = _replace(obs, replaces=replaced.observation_id)

    result.observations = observations
    return result
