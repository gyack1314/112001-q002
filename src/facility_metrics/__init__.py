"""Facility metering pipeline: raw submissions -> observations -> metrics.

Public entry points::

    from facility_metrics import MetricPolicy, load_readings, compute_metrics

    result = load_readings(rows, MetricPolicy(version="m-2026.01"), source_file="jan.json")
    for row in compute_metrics(result.observations, policy):
        print(row.as_dict())

The package uses only the Python standard library.  See ``README.md`` for the
policy semantics and the CLI.
"""

from .audit import EXCLUSION_DETAILS, explain_cell, policy_diff, trace_observation
from .batch import Batch, build_batch, compare_batches, read_batch
from .loader import LoadResult, Rejection, load_readings, version_order
from .metrics import MetricRow, compute_metrics, rows_as_dicts
from .models import MetricPolicy, Observation, Reading
from .units import capacity_to_eflops, energy_to_kwh
from .windows import Window, month_bounds, parse_instant

__all__ = [
    "Batch",
    "EXCLUSION_DETAILS",
    "LoadResult",
    "MetricPolicy",
    "MetricRow",
    "Observation",
    "Reading",
    "Rejection",
    "Window",
    "build_batch",
    "capacity_to_eflops",
    "compare_batches",
    "compute_metrics",
    "energy_to_kwh",
    "explain_cell",
    "load_readings",
    "month_bounds",
    "parse_instant",
    "policy_diff",
    "read_batch",
    "rows_as_dicts",
    "trace_observation",
    "version_order",
]
