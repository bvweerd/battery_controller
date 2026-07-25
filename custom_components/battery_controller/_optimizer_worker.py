"""Standalone subprocess worker for the DP optimizer.

Invoked by the OptimizationCoordinator via :func:`subprocess.run`.  Reads
a JSON request from stdin, runs the dynamic-programming optimizer, and
writes a JSON response to stdout.

NO PICKLING — all data crosses the process boundary as plain JSON, so the
subprocess interpreter does not need custom_components to be importable at
startup.  It only needs ``sys.path`` to include the parent directory of
``custom_components``, which the coordinator guarantees by setting the
``PYTHONPATH`` environment variable before spawning.
"""

from __future__ import annotations

import json
import sys
import traceback


def _main() -> None:
    """Read JSON request from stdin, run optimizer, write JSON result to stdout."""
    try:
        request_raw = sys.stdin.read()
        request = json.loads(request_raw)
    except Exception:
        _fail("Failed to parse JSON request")

    try:
        result = _run(request)
    except Exception:
        result = {
            "error": True,
            "traceback": traceback.format_exc(),
        }

    json.dump(result, sys.stdout)
    sys.stdout.flush()


def _fail(message: str) -> None:
    """Write an error response and exit."""
    json.dump({"error": True, "message": message}, sys.stdout)
    sys.stdout.flush()
    sys.exit(1)


def _run(request: dict) -> dict:
    """Execute the DP optimizer from a JSON request dict.

    Imports are done here (not at module level) so that the script can
    start and parse its request even if custom_components is not yet on
    sys.path.  The coordinator guarantees PYTHONPATH is set before
    spawning, so by the time we reach this function the path is available.
    """
    from custom_components.battery_controller.battery_model import BatteryConfig
    from custom_components.battery_controller.optimizer import (
        OptimizationResult,
        optimize_battery_schedule,
    )
    import dataclasses

    # Reconstruct BatteryConfig, passing only init=True fields.
    init_fields = {f.name for f in dataclasses.fields(BatteryConfig) if f.init}
    battery_config = BatteryConfig(
        **{k: v for k, v in request["battery_config"].items() if k in init_fields}
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
    # The script lives in …/battery_controller/, which contains files like
    # ``select.py`` and ``__init__.py``.  Python prepends this directory to
    # sys.path, and it may also leak in via PYTHONPATH or .pth files.
    # ``import select`` (triggered transitively by homeassistant →
    # asyncio → subprocess) must find the stdlib module, not our select.py.
    # Strip *all* sys.path entries that resolve to this script's directory.
    import os as __os
    _bad = __os.path.realpath(__os.path.dirname(__file__))
    sys.path = [p for p in sys.path if __os.path.realpath(p) != _bad]
    del __os
    _main()
