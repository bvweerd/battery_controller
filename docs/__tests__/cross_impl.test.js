/**
 * Cross-implementation DP fixtures: analyzer.js vs optimizer.py.
 *
 * tests/fixtures/dp_cross_impl.json is generated from optimizer.py (the
 * source of truth) and is also checked against simulate_diagnostics.py by
 * tests/test_cross_impl.py. This suite runs the identical cases through the
 * JS re-implementation so any algorithmic drift between the three DP
 * implementations fails CI.
 *
 * Sign convention: Python power_schedule_kw uses positive = charge;
 * analyzer.js runOptimizer returns powerKw with positive = discharge,
 * so the comparison negates the Python values.
 */

const fs = require('fs');
const path = require('path');
const { runOptimizer } = require('../analyzer.js');

const FIXTURE = path.join(__dirname, '..', '..', 'tests', 'fixtures', 'dp_cross_impl.json');
const cases = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));

const TOL = 1e-6;

function toJsCfg(cfg) {
  return {
    capacityKwh: cfg.capacity_kwh,
    minSocKwh: cfg.capacity_kwh * cfg.min_soc_percent / 100,
    maxSocKwh: cfg.capacity_kwh * cfg.max_soc_percent / 100,
    maxChargeKw: cfg.max_charge_power_kw,
    maxDischargeKw: cfg.max_discharge_power_kw,
    chargeCurve: cfg.charge_curve,
    dischargeCurve: cfg.discharge_curve,
    pvDcCoupled: cfg.pv_dc_coupled,
    pvDcEfficiency: cfg.pv_dc_efficiency,
    maxGridPowerKw: cfg.max_grid_power_kw,
  };
}

describe.each(cases.map(c => [c.name, c]))('cross-impl %s', (name, c) => {
  const result = runOptimizer(toJsCfg(c.config), c.inputs.soc_kwh, {
    priceFc: c.inputs.prices,
    feedInFc: c.inputs.feed_in,
    pvFc: c.inputs.pv,
    consumFc: c.inputs.consumption,
    stepDurations: c.inputs.step_hours,
    degradCost: c.inputs.degradation_cost_per_kwh,
    minPriceSpread: c.inputs.min_price_spread,
    pvDcFc: c.inputs.pv_dc,
  });

  test('power schedule matches optimizer.py', () => {
    expect(result.powerKw.length).toBe(c.expected.power_schedule_kw.length);
    c.expected.power_schedule_kw.forEach((p, i) => {
      // analyzer.js sign convention is inverted (positive = discharge)
      expect(result.powerKw[i]).toBeCloseTo(-p, 6);
    });
  });

  test('SoC schedule matches optimizer.py', () => {
    expect(result.socKwh.length).toBe(c.expected.soc_schedule_kwh.length);
    c.expected.soc_schedule_kwh.forEach((s, i) => {
      expect(Math.abs(result.socKwh[i] - s)).toBeLessThan(TOL);
    });
  });

  test('costs match optimizer.py', () => {
    expect(Math.abs(result.totalCost - c.expected.total_cost)).toBeLessThan(TOL);
    expect(Math.abs(result.baselineCost - c.expected.baseline_cost)).toBeLessThan(TOL);
  });
});
