"""Domain types for facility metering observations.

Two record families share one shape:

``Reading``
    A raw submission exactly as it was received from a park.  Readings are
    never mutated; a corrected meter value is a *new* reading.

``Observation``
    A normalized, auditable view of one reading.  It keeps the source
    provenance, a half-open time window in UTC, energy converted to kWh and
    capacity converted to EFLOPS, and the full business meaning of the
    submission (``complete`` / ``partial`` / ``downtime`` / ``calibration``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping

ReadingKind = Literal["reading", "calibration"]
ObservationStatus = Literal["complete", "partial", "downtime", "calibration"]

# Machine-readable reasons an observation may not contribute to a metric.
# A row can have several (for example a partial window with a missing IT
# reading).  They are recorded instead of dropping or zero-filling the row.
EXCLUSION_REASONS: dict[str, str] = {
    "partial_window": "window does not cover a full calendar month",
    "crosses_month": "window crosses a month boundary",
    "downtime": "equipment reported out of service",
    "invalid_window": "window cannot be parsed",
    "invalid_energy_unit": "energy unit is unsupported",
    "invalid_it_energy_unit": "IT energy unit is unsupported",
    "invalid_capacity_unit": "capacity unit is unsupported",
    "missing_energy": "no energy reading",
    "missing_it_energy": "no IT energy reading (needed for PUE)",
    "missing_capacity": "no capacity reading",
    "nonpositive_capacity": "capacity is zero or negative",
    "negative_energy": "energy is negative",
    "superseded": "a later version is effective for this window",
    "after_cutoff": "submitted after the batch cutoff",
    "park_level_overridden": "facility-level rows exist for this park/month and take precedence",
    "park_meter_preferred": "the park-level meter is preferred for the park total",
}


def format_utc(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Reading:
    """One raw submission, preserved in normalized form."""

    reading_id: str
    source_file: str
    sequence: int
    park: str
    facility: str | None
    window_start: datetime
    window_end: datetime
    energy: float | None
    energy_unit: str
    it_energy: float | None
    it_energy_unit: str
    capacity: float | None
    capacity_unit: str
    kind: ReadingKind
    declared_status: str | None
    submitted_at: datetime | None
    revision: int
    supersedes: str | None
    note: str | None

    @property
    def observation_id(self) -> str:
        # Unique even when two files reuse the same explicit reading_id.
        return f"{self.source_file}#{self.sequence}"

    @property
    def start(self) -> str:
        return format_utc(self.window_start)

    @property
    def end(self) -> str:
        return format_utc(self.window_end)


@dataclass(frozen=True)
class Observation:
    """A normalized reading plus the provenance needed to audit it."""

    observation_id: str
    reading_id: str
    park: str
    facility: str | None
    window_start: datetime
    window_end: datetime
    duration_hours: float
    month: str
    coverage: float
    energy_kwh: float | None
    energy_unit_original: str
    it_energy_kwh: float | None
    it_energy_unit_original: str
    capacity_eflops: float | None
    capacity_unit_original: str
    status: ObservationStatus
    kind: ReadingKind
    revision: int
    submitted_at: datetime | None
    supersedes: str | None
    note: str | None
    source_file: str
    sequence: int
    # True when this reading is the effective version for its window after
    # applying revisions, calibrations and the batch cutoff.
    effective: bool
    # The observation id that replaced this one, if any.
    replaced_by: str | None
    # If this reading is itself a replacement, the observation id it replaces.
    replaces: str | None
    # Machine-readable reasons this row is not usable for some metric.
    exclusions: tuple[str, ...] = ()

    @property
    def start(self) -> str:
        return format_utc(self.window_start)

    @property
    def end(self) -> str:
        return format_utc(self.window_end)

    @property
    def usable(self) -> bool:
        """Effective, in time, parseable, and not stopped equipment.

        The loader records the ``downtime`` exclusion only when the policy
        excludes outages; with ``downtime_excluded=False`` the row stays
        usable and its status still travels on the observation.
        """

        return self.effective and not {
            "superseded",
            "after_cutoff",
            "invalid_window",
            "downtime",
        }.intersection(self.exclusions)

    @property
    def comparable_window(self) -> bool:
        return not {"partial_window", "crosses_month"}.intersection(self.exclusions)

    @property
    def has_valid_energy(self) -> bool:
        return self.energy_kwh is not None and not {
            "invalid_energy_unit",
            "missing_energy",
            "negative_energy",
        }.intersection(self.exclusions)

    @property
    def has_valid_it_energy(self) -> bool:
        return self.it_energy_kwh is not None and not {
            "invalid_it_energy_unit",
            "missing_it_energy",
            "negative_energy",
        }.intersection(self.exclusions)

    @property
    def has_valid_capacity(self) -> bool:
        return self.capacity_eflops is not None and not {
            "invalid_capacity_unit",
            "missing_capacity",
            "nonpositive_capacity",
        }.intersection(self.exclusions)

    def exclusion_details(self) -> list[dict[str, str]]:
        return [
            {"reason": reason, "detail": EXCLUSION_REASONS.get(reason, "")}
            for reason in self.exclusions
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "reading_id": self.reading_id,
            "park": self.park,
            "facility": self.facility,
            "window_start": self.start,
            "window_end": self.end,
            "duration_hours": round(self.duration_hours, 6),
            "month": self.month,
            "coverage": round(self.coverage, 6),
            "energy_kwh": self.energy_kwh,
            "energy_unit_original": self.energy_unit_original,
            "it_energy_kwh": self.it_energy_kwh,
            "it_energy_unit_original": self.it_energy_unit_original,
            "capacity_eflops": self.capacity_eflops,
            "capacity_unit_original": self.capacity_unit_original,
            "status": self.status,
            "kind": self.kind,
            "revision": self.revision,
            "submitted_at": format_utc(self.submitted_at) if self.submitted_at else None,
            "supersedes": self.supersedes,
            "note": self.note,
            "source_file": self.source_file,
            "effective": self.effective,
            "replaced_by": self.replaced_by,
            "replaces": self.replaces,
            "exclusions": self.exclusion_details(),
        }


@dataclass(frozen=True)
class MetricPolicy:
    """Named, versioned definition of how the summary metrics are computed.

    Changing any field changes the meaning of the reported numbers, so the
    policy version is stored with every batch and shown by ``compare``.
    """

    version: str
    # A window must cover at least this share of the calendar month to count
    # as comparable.  1.0 means exact coverage of the month.
    complete_coverage: float = 1.0
    # "duration": weight capacity by observation hours; "simple": mean.
    capacity_weighting: str = "duration"
    # If True, a capacity reading is only used when its window is comparable.
    # Capacity is a level, not a quantity, so the default accepts a
    # partial-window capacity reading and marks the coverage as partial.
    capacity_requires_comparable_window: bool = False
    # "facility": sum facility submeters for the park total; "park": use the
    # park-level meter when one exists.  The two must never be added together.
    level_precedence: str = "facility"
    # Exclude stopped-equipment rows from energy and capacity metrics.  They
    # are still reported with their outage duration and exclusion reason.
    downtime_excluded: bool = True
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "complete_coverage": self.complete_coverage,
            "capacity_weighting": self.capacity_weighting,
            "capacity_requires_comparable_window": self.capacity_requires_comparable_window,
            "level_precedence": self.level_precedence,
            "downtime_excluded": self.downtime_excluded,
            "description": self.description,
        }


RawRecord = Mapping[str, Any]
