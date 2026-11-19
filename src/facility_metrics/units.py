"""Unit normalization.

Every physical quantity is converted to one canonical unit before any
arithmetic happens, so a park reporting MWh and a park reporting kWh can
never be silently added together:

* energy   -> kWh
* capacity -> EFLOPS

The original unit string is kept on the observation for audit.  A present
value with a missing or unsupported unit is an error, never a guess.
"""

from __future__ import annotations

ENERGY_FACTORS_TO_KWH = {
    "WH": 1e-3,
    "KWH": 1.0,
    "MWH": 1e3,
    "GWH": 1e6,
    "TWH": 1e9,
}

CAPACITY_FACTORS_TO_EFLOPS = {
    # Calculated capacity is conventionally expressed in EFLOPS; the
    # synthetic input also uses PFLOPS and TFLOPS.
    "EFLOPS": 1.0,
    "PFLOPS": 1e-3,
    "TFLOPS": 1e-6,
    "GFLOPS": 1e-9,
    "MFLOPS": 1e-12,
    "FLOPS": 1e-18,
}


class UnitError(ValueError):
    """Raised when a unit cannot be normalized."""


def _unit_key(unit: object) -> str:
    if not isinstance(unit, str) or not unit.strip():
        raise UnitError("missing unit")
    return unit.strip().upper()


def energy_to_kwh(value: float, unit: str) -> float:
    key = _unit_key(unit)
    try:
        factor = ENERGY_FACTORS_TO_KWH[key]
    except KeyError as exc:
        raise UnitError(f"unsupported energy unit: {unit!r}") from exc
    return float(value) * factor


def capacity_to_eflops(value: float, unit: str) -> float:
    key = _unit_key(unit)
    try:
        factor = CAPACITY_FACTORS_TO_EFLOPS[key]
    except KeyError as exc:
        raise UnitError(f"unsupported capacity unit: {unit!r}") from exc
    return float(value) * factor
