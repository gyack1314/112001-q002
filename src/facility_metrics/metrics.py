"""Monthly energy / capacity / PUE metrics with a full audit trail.

Rules follow from the input contract:

* Only **effective** observations are considered.  Superseded versions and
  post-cutoff rows stay in the observation list, never in a total.
* Energy is a quantity, so only a full, comparable month enters the monthly
  sum.  Partial-window energy is reported separately as
  ``partial_energy_kwh`` and is never mixed into the comparable total, nor
  zero-filled.  If a month has *both* comparable and partial rows for the
  same group, the energy total is flagged ``partial`` so a reader does not
  mistake it for a complete month.
* Capacity is a level, not a quantity: duration-weighted within the month
  and summed across distinct facilities for a park.  A partial window can
  supply a capacity level (unless the policy forbids it) but is flagged as
  partial.
* PUE is computed only when the **same** observations supply both energy and
  IT energy; otherwise it is ``None`` with an explanatory status, never 0.
* Park-level meter rows and facility sub-meter rows are alternatives, not
  addends: one level is chosen per park/month and the other is recorded as
  excluded with its reason.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from .models import MetricPolicy, Observation
from .windows import month_bounds, parse_instant

ENERGY_COMPLETE = "complete"
CAPACITY_COMPLETE = "complete"
CAPACITY_PARTIAL = "partial"
STATUS_UNAVAILABLE = "unavailable"

PUE_COMPLETE = "complete"
PUE_UNAVAILABLE = "unavailable"


@dataclass
class MetricRow:
    month: str
    park: str
    facility: str | None
    level: str  # "park" | "facility"

    energy_kwh: float | None = None
    energy_status: str = STATUS_UNAVAILABLE
    energy_observations: list[str] = field(default_factory=list)

    it_energy_kwh: float | None = None
    it_energy_observations: list[str] = field(default_factory=list)

    pue: float | None = None
    pue_status: str = PUE_UNAVAILABLE
    pue_reason: str | None = None

    capacity_eflops: float | None = None
    capacity_status: str = STATUS_UNAVAILABLE
    capacity_observations: list[str] = field(default_factory=list)
    capacity_weight_hours: float = 0.0
    capacity_partial: bool = False
    # Facilities that supplied energy but no usable capacity: the park
    # capacity total is then incomplete and is flagged, not silently smaller.
    capacity_missing_facilities: list[str] = field(default_factory=list)
    # Set when the park total had to fall back to a different metering level.
    level_fallback: str | None = None

    energy_per_eflops_kwh: float | None = None

    partial_energy_kwh: float | None = None
    partial_energy_observations: list[str] = field(default_factory=list)

    excluded: list[dict[str, str]] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str | None, str]:
        return (self.month, self.park, self.facility, self.level)

    def as_dict(self) -> dict[str, Any]:
        return {
            "month": self.month,
            "park": self.park,
            "facility": self.facility,
            "level": self.level,
            "energy_kwh": self.energy_kwh,
            "energy_status": self.energy_status,
            "energy_observations": list(self.energy_observations),
            "it_energy_kwh": self.it_energy_kwh,
            "it_energy_observations": list(self.it_energy_observations),
            "pue": self.pue,
            "pue_status": self.pue_status,
            "pue_reason": self.pue_reason,
            "capacity_eflops": self.capacity_eflops,
            "capacity_status": self.capacity_status,
            "capacity_observations": list(self.capacity_observations),
            "capacity_weight_hours": round(self.capacity_weight_hours, 6),
            "capacity_partial": self.capacity_partial,
            "capacity_missing_facilities": list(self.capacity_missing_facilities),
            "level_fallback": self.level_fallback,
            "energy_per_eflops_kwh": self.energy_per_eflops_kwh,
            "partial_energy_kwh": self.partial_energy_kwh,
            "partial_energy_observations": list(self.partial_energy_observations),
            "excluded": list(self.excluded),
        }


def _overlap_hours(obs: Observation, month_start: datetime, month_end: datetime) -> float:
    start = max(obs.window_start, month_start)
    end = min(obs.window_end, month_end)
    return max(0.0, (end - start).total_seconds() / 3600.0)


def _excluded_entry(
    obs: Observation, extra_reason: str | None = None, detail: str | None = None
) -> dict[str, str]:
    reasons = list(obs.exclusions)
    if extra_reason and extra_reason not in reasons:
        reasons.append(extra_reason)
    return {
        "observation_id": obs.observation_id,
        "source_file": obs.source_file,
        "park": obs.park,
        "facility": obs.facility,
        "window_start": obs.start,
        "window_end": obs.end,
        "reasons": reasons or ["not_applicable"],
        "detail": detail or "",
    }


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    return month_bounds(parse_instant(f"{month}-01T00:00:00Z"))


def _sum_energy(
    rows: Iterable[Observation], month_start: datetime, month_end: datetime
) -> tuple[float | None, list[str], float | None, list[str]]:
    """Sum comparable energy; keep partial-window energy separately."""

    contributing: list[str] = []
    total = 0.0
    partial_ids: list[str] = []
    partial_total = 0.0
    for obs in rows:
        if not (obs.usable and obs.has_valid_energy):
            continue
        if not obs.comparable_window:
            partial_ids.append(obs.observation_id)
            partial_total += obs.energy_kwh or 0.0
            continue
        total += obs.energy_kwh or 0.0
        contributing.append(obs.observation_id)
    return (
        total if contributing else None,
        contributing,
        partial_total if partial_ids else None,
        partial_ids,
    )


def _sum_it_energy(rows: Iterable[Observation]) -> tuple[float | None, list[str]]:
    contributing: list[str] = []
    total = 0.0
    for obs in rows:
        if not (obs.usable and obs.has_valid_it_energy):
            continue
        if not obs.comparable_window:
            continue
        total += obs.it_energy_kwh or 0.0
        contributing.append(obs.observation_id)
    return (total if contributing else None, contributing)


@dataclass
class _Capacity:
    value: float | None
    ids: list[str]
    weight_hours: float
    partial: bool


def _weighted_capacity(
    rows: Iterable[Observation],
    month_start: datetime,
    month_end: datetime,
    policy: MetricPolicy,
) -> _Capacity:
    usable: list[tuple[Observation, float]] = []
    for obs in rows:
        if not (obs.usable and obs.has_valid_capacity):
            continue
        if policy.capacity_requires_comparable_window and not obs.comparable_window:
            continue
        hours = _overlap_hours(obs, month_start, month_end)
        if hours <= 0:
            continue
        usable.append((obs, hours))
    if not usable:
        return _Capacity(None, [], 0.0, False)

    ids = [obs.observation_id for obs, _ in usable]
    partial = any(not obs.comparable_window for obs, _ in usable)
    if policy.capacity_weighting == "simple":
        value = sum((obs.capacity_eflops or 0.0) for obs, _ in usable) / len(usable)
        weight = float(len(usable))
    elif policy.capacity_weighting == "duration":
        weight = sum(hours for _, hours in usable)
        value = sum((obs.capacity_eflops or 0.0) * hours for obs, hours in usable) / weight
    else:
        raise ValueError(f"unknown capacity_weighting: {policy.capacity_weighting!r}")
    return _Capacity(value, ids, weight, partial)


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator


def _apply_pue(row: MetricRow, energy_ids: list[str], it_ids: list[str], it_total: float) -> None:
    if row.energy_kwh is None:
        row.pue_status = PUE_UNAVAILABLE
        row.pue_reason = "missing_energy"
        return
    if not it_ids:
        row.pue_status = PUE_UNAVAILABLE
        row.pue_reason = "missing_it_energy"
        return
    if sorted(energy_ids) != sorted(it_ids):
        row.pue_status = PUE_UNAVAILABLE
        row.pue_reason = "incomplete_it_energy_coverage"
        return
    row.it_energy_kwh = it_total
    row.it_energy_observations = list(it_ids)
    row.pue = _ratio(row.energy_kwh, it_total)
    if row.pue is None:
        row.pue_status = PUE_UNAVAILABLE
        row.pue_reason = "it_energy_not_positive"
    else:
        row.pue_status = PUE_COMPLETE


def _build_group_row(
    *,
    month: str,
    park: str,
    facility: str | None,
    level: str,
    observations: list[Observation],
    attributed: list[Observation],
    policy: MetricPolicy,
) -> MetricRow:
    month_start, month_end = _month_bounds(month)
    row = MetricRow(month=month, park=park, facility=facility, level=level)

    energy_kwh, energy_ids, partial_kwh, partial_ids = _sum_energy(
        observations, month_start, month_end
    )
    if energy_kwh is not None:
        row.energy_kwh = energy_kwh
        row.energy_status = ENERGY_COMPLETE
        row.energy_observations = energy_ids
    row.partial_energy_kwh = partial_kwh
    row.partial_energy_observations = partial_ids
    if partial_ids and energy_kwh is not None:
        # Comparable and partial rows coexist in this group: the total is
        # still a valid comparable total, but the month is not fully covered.
        row.energy_status = "partial_coverage"

    it_kwh, it_ids = _sum_it_energy(observations)
    _apply_pue(row, energy_ids, it_ids, it_kwh or 0.0)

    capacity = _weighted_capacity(observations, month_start, month_end, policy)
    if capacity.value is not None:
        row.capacity_eflops = capacity.value
        row.capacity_observations = capacity.ids
        row.capacity_weight_hours = capacity.weight_hours
        row.capacity_partial = capacity.partial
        row.capacity_status = CAPACITY_PARTIAL if capacity.partial else CAPACITY_COMPLETE

    if (
        row.energy_kwh is not None
        and row.capacity_eflops is not None
        and row.capacity_status == CAPACITY_COMPLETE
    ):
        row.energy_per_eflops_kwh = row.energy_kwh / row.capacity_eflops

    contributing = set(energy_ids) | set(it_ids) | set(capacity.ids)
    for obs in attributed:
        if obs.observation_id in contributing:
            continue
        row.excluded.append(_excluded_entry(obs))
    return row


def compute_metrics(observations: Iterable[Observation], policy: MetricPolicy) -> list[MetricRow]:
    """Compute facility rows and park totals for every (month, park) with data."""

    observations = list(observations)
    grouped: dict[tuple[str, str], list[Observation]] = {}
    for obs in observations:
        grouped.setdefault((obs.month, obs.park), []).append(obs)

    rows: list[MetricRow] = []
    for (month, park), group in sorted(grouped.items()):
        facility_obs = [obs for obs in group if obs.facility]
        park_obs = [obs for obs in group if not obs.facility]
        effective_facility = [obs for obs in facility_obs if obs.effective]
        effective_park = [obs for obs in park_obs if obs.effective]

        # The two levels are alternatives, never addends.  Honour the
        # policy's preference, and fall back to whichever level exists.
        # "Exists" includes an all-excluded level: its row still documents
        # why nothing entered the total.
        prefer_facility = policy.level_precedence == "facility"
        if prefer_facility:
            use_facility = bool(facility_obs) or not park_obs
        else:
            use_facility = not effective_park and bool(facility_obs)

        if use_facility:
            facility_rows = _facility_rows(month, park, facility_obs, policy)
            rows.extend(facility_rows)
            park_row = _park_total(
                month=month,
                park=park,
                facility_rows=facility_rows,
                unused_other_level=park_obs,
                reason="park_level_overridden",
                detail="facility-level observations exist for this park/month",
            )
            if not prefer_facility:
                park_row.level_fallback = "park meter absent; facility sub-meters used"
            rows.append(park_row)
        elif effective_park:
            park_row = _build_group_row(
                month=month,
                park=park,
                facility=None,
                level="park",
                observations=effective_park,
                attributed=park_obs,
                policy=policy,
            )
            if facility_obs:
                park_row.excluded.extend(
                    _excluded_entry(
                        obs,
                        "park_meter_preferred",
                        "the park-level meter is preferred for this park/month",
                    )
                    for obs in facility_obs
                )
            rows.append(park_row)
        else:
            # There are observations for the month but none is effective.
            row = MetricRow(month=month, park=park, facility=None, level="park")
            row.pue_reason = "missing_energy"
            for obs in group:
                row.excluded.append(_excluded_entry(obs))
            rows.append(row)

    return sorted(
        rows,
        key=lambda row: (row.month, row.park, row.level != "facility", row.facility or ""),
    )


def _facility_rows(
    month: str, park: str, facility_obs: list[Observation], policy: MetricPolicy
) -> list[MetricRow]:
    rows: list[MetricRow] = []
    # Include facilities whose only rows are superseded: the row is empty,
    # but it still records what was excluded and why.
    facilities = sorted({obs.facility for obs in facility_obs if obs.facility})
    for facility in facilities:
        this_facility = [obs for obs in facility_obs if obs.facility == facility]
        rows.append(
            _build_group_row(
                month=month,
                park=park,
                facility=facility,
                level="facility",
                observations=[obs for obs in this_facility if obs.effective],
                attributed=this_facility,
                policy=policy,
            )
        )
    return rows


def _park_total(
    *,
    month: str,
    park: str,
    facility_rows: list[MetricRow],
    unused_other_level: list[Observation],
    reason: str,
    detail: str,
) -> MetricRow:
    """Sum comparable facility rows into a park total.

    Quantities add (energy).  Capacity levels of distinct facilities add too,
    because each facility contributes its own capacity.  Ratios are recomputed
    from the pooled inputs so the park PUE is not an average of ratios.
    """

    row = MetricRow(month=month, park=park, facility=None, level="park")
    energy_total = 0.0
    it_total = 0.0
    capacity_total = 0.0
    energy_ids: list[str] = []
    it_ids: list[str] = []
    capacity_ids: list[str] = []
    partial_energy = 0.0
    partial_ids: list[str] = []
    all_capacity_complete = True
    any_capacity = False

    for sub in facility_rows:
        if sub.energy_kwh is not None:
            energy_total += sub.energy_kwh
            energy_ids.extend(sub.energy_observations)
        if sub.it_energy_kwh is not None:
            it_total += sub.it_energy_kwh
            it_ids.extend(sub.it_energy_observations)
        if sub.capacity_eflops is not None:
            any_capacity = True
            capacity_total += sub.capacity_eflops
            capacity_ids.extend(sub.capacity_observations)
            row.capacity_weight_hours += sub.capacity_weight_hours
            if sub.capacity_status != CAPACITY_COMPLETE:
                row.capacity_partial = True
                all_capacity_complete = False
        elif sub.energy_kwh is not None:
            # This facility consumed energy but contributes no capacity, so a
            # park-level energy/capacity ratio would overstate utilisation.
            all_capacity_complete = False
            row.capacity_missing_facilities.append(sub.facility or sub.park)
        if sub.partial_energy_kwh is not None:
            partial_energy += sub.partial_energy_kwh
            partial_ids.extend(sub.partial_energy_observations)
        row.excluded.extend(sub.excluded)

    if energy_ids:
        row.energy_kwh = energy_total
        row.energy_status = ENERGY_COMPLETE
        row.energy_observations = energy_ids
    if partial_ids:
        row.partial_energy_kwh = partial_energy
        row.partial_energy_observations = partial_ids
        if energy_ids:
            row.energy_status = "partial_coverage"
    if any_capacity:
        row.capacity_eflops = capacity_total
        row.capacity_observations = capacity_ids
        row.capacity_status = CAPACITY_COMPLETE if all_capacity_complete else CAPACITY_PARTIAL

    _apply_pue(row, energy_ids, it_ids, it_total)
    if (
        row.energy_kwh is not None
        and row.capacity_eflops is not None
        and row.capacity_status == CAPACITY_COMPLETE
    ):
        row.energy_per_eflops_kwh = row.energy_kwh / row.capacity_eflops

    for obs in unused_other_level:
        row.excluded.append(_excluded_entry(obs, reason, detail))
    return row


def rows_as_dicts(rows: Iterable[MetricRow]) -> list[dict[str, Any]]:
    return [row.as_dict() for row in rows]
