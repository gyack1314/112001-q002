"""Synthetic records shared by the tests.

All values are made up for exercising the pipeline; they are not readings
from any real park and say nothing about real energy use.
"""

from __future__ import annotations


def jan(**overrides):
    record = {
        "park": "P01",
        "facility": "F1",
        "window_start": "2026-01-01T00:00:00",
        "window_end": "2026-01-31T23:59:59",
        "energy": 1000,
        "unit": "MWh",
        "it_energy": 800,
        "it_energy_unit": "MWh",
        "capacity": 2.0,
        "capacity_unit": "EFLOPS",
        "submitted_at": "2026-02-02T00:00:00Z",
    }
    record.update(overrides)
    return record


def partial_feb(**overrides):
    record = {
        "park": "P01",
        "facility": "F1",
        "window_start": "2026-02-10T00:00:00",
        "window_end": "2026-02-28T23:59:59",
        "energy": 600,
        "unit": "MWh",
        "it_energy": 480,
        "it_energy_unit": "MWh",
        "capacity": 2.0,
        "capacity_unit": "EFLOPS",
        "submitted_at": "2026-03-02T00:00:00Z",
    }
    record.update(overrides)
    return record
