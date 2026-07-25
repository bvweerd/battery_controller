"""Standalone subprocess worker for the DP optimizer.

Run by the coordinator as::

    python -m custom_components.battery_controller._optimizer_worker

with ``cwd`` set to the HA config directory.  Python then adds the config
directory to ``sys.path[0]``, making ``custom_components`` natively
importable without any ``PYTHONPATH`` manipulation — and keeping the
``battery_controller`` subdirectory *off* ``sys.path`` so its files (e.g.
``select.py``) cannot shadow stdlib modules.

Reads a JSON request from stdin, runs the DP optimizer, writes a JSON
response to stdout.  No pickling — all data crosses the process boundary
as plain text.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import traceback

from custom_components.battery_controller.battery_model import BatteryConfig
from custom_components.battery_controller.optimizer import (
    OptimizationResult,
    optimize_battery_schedule,
)

_BATTERY_CONFIG_INIT_FIELDS = frozenset(
    f.name for f in dataclasses.fields(BatteryConfig) if f.init
)


def _main() -> None:
    """Read JSON request from stdin, run optimizer, write JSON result to stdout."""
    try:
        request = json.loads(sys.stdin.read())
    except Exception:
        json.dump({"error": True, "message": "Failed to parse JSON request"}, sys.stdout)
        sys.stdout.flush()
        sys.exit(1)

    try:
        result = _run(request)
    except Exception:
        result = {"error": True, "traceback": traceback.format_exc()}

    json.dump(result, sys.stdout)
    sys.stdout.flush()


def _run(request: dict) -> dict:
    """Execute the DP optimizer from a JSON request dict."""
    battery_config = BatteryConfig(
        **{k: v for k, v in request["battery_config"].items() if k in _BATTERY_CONFIG_INIT_FIELDS}
    )

    result: OptimizationResult = optimize_battery_schedule(
        battery_config=battery_config,
        current_soc_kwh=request["current_soc_kwh"],
        price_forecast=request["price_forecast"],
        feed_in_forecast=request.get("feed_in_forecast"),
        pv_forecast=request["pv_forecast"],
        consumption_forecast=request["consumption_forecast"],
        step_durations_hours=request.get("step_durations_hours"),
        degradation_cost_per_kwh=request["degradation_cost_per_kwh"],
        min_price_spread=request["min_price_spread"],
        pv_dc_forecast=request.get("pv_dc_forecast"),
        charge_eff_override=request.get("charge_eff_override"),
        discharge_eff_override=request.get("discharge_eff_override"),
    )

    return dataclasses.asdict(result)


if __name__ == "__main__":
    _main()
