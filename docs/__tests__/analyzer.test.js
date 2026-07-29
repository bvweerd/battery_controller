'use strict';

const {
  SOC_RES_WH,
  DC_TO_AC_EFF,
  calculateStepCost,
  totalPvSeries,
  netPvSeries,
  findNearestSocIdx,
  runDP,
  forwardPass,
  computeShadowPrice,
  computeTotalCost,
  runOptimizer,
  generateTips,
} = require('../analyzer');

// ─── helpers ────────────────────────────────────────────────────────────────

/** Minimal battery config for runDP / runOptimizer. */
function makeCfg(overrides = {}) {
  return {
    rte: 0.9,
    minSocKwh: 1.0,
    maxSocKwh: 10.0,
    maxChargeKw: 5.0,
    maxDischargeKw: 5.0,
    pvDcCoupled: false,
    pvDcEfficiency: 0.97,
    maxGridPowerKw: 0,
    ...overrides,
  };
}

/** Run the optimizer over a simple N-step scenario. */
function runSimple(prices, feedIn, pv, consump, socKwh = 5, cfg = makeCfg(), degradCost = 0.004) {
  const n = prices.length;
  return runOptimizer(cfg, socKwh, {
    priceFc: prices,
    feedInFc: feedIn || prices.map(() => 0.07),
    pvFc: pv || prices.map(() => 0),
    consumFc: consump || prices.map(() => 1.0),
    stepDurations: prices.map(() => 0.25),
    degradCost,
    minPriceSpread: 0.05,
    pvDcFc: null,
    terminalShadowPrice: null,
  });
}

// ─── calculateStepCost ──────────────────────────────────────────────────────

describe('calculateStepCost', () => {
  const baseArgs = (actionW) => [
    0.25,          // stepH
    5000,          // socWh
    actionW,       // actionW
    0.15,          // gridPrice
    0.07,          // feedInPrice
    0,             // pvW
    1000,          // consumW  (1 kW load)
    0.9,           // rte
    0.004,         // degradCostPerKwh
    false,         // pvDcCoupled
    0,             // pvDcW
    0.97,          // pvDcEfficiency
    0,             // maxGridPowerKw
    10000,         // maxSocWh
  ];

  test('idle: cost = consumption × price × stepH', () => {
    // 1 kW load, 0.25 h, €0.15 → 0.25 × 0.15 = 0.0375 €
    const cost = calculateStepCost(...baseArgs(0));
    expect(cost).toBeCloseTo(0.0375, 5);
  });

  test('charging from grid increases grid cost', () => {
    // Charging 2000 W + 1000 W load = 3000 W from grid (no PV)
    const costCharge = calculateStepCost(...baseArgs(2000));
    const costIdle   = calculateStepCost(...baseArgs(0));
    expect(costCharge).toBeGreaterThan(costIdle);
  });

  test('discharging reduces grid cost', () => {
    // Discharging 1000 W exactly covers the load → near-zero grid
    const costDischarge = calculateStepCost(...baseArgs(-1000));
    const costIdle      = calculateStepCost(...baseArgs(0));
    expect(costDischarge).toBeLessThan(costIdle);
  });

  test('charging adds degradation cost', () => {
    // With degradation > 0, charging costs more than without
    const args = baseArgs(2000);
    const withDeg    = calculateStepCost(...args);
    const noDeg      = calculateStepCost(args[0], args[1], args[2], args[3], args[4],
      args[5], args[6], args[7], 0, ...args.slice(9));
    expect(withDeg).toBeGreaterThan(noDeg);
  });

  test('PV surplus (feed-in > consumption) produces negative cost', () => {
    // 5 kW PV, 1 kW load → 4 kW net export at feed-in price €0.07
    const cost = calculateStepCost(
      0.25, 5000, 0, 0.15, 0.07, 5000, 1000, 0.9, 0.004,
      false, 0, 0.97, 0, 10000
    );
    expect(cost).toBeLessThan(0);
  });

  test('maxGridPowerKw caps grid export income', () => {
    // Huge PV surplus, cap at 3 kW → less revenue than uncapped
    const uncapped = calculateStepCost(0.25, 5000, 0, 0.15, 0.07, 10000, 1000, 0.9, 0, false, 0, 0.97, 0, 10000);
    const capped   = calculateStepCost(0.25, 5000, 0, 0.15, 0.07, 10000, 1000, 0.9, 0, false, 0, 0.97, 3, 10000);
    expect(capped).toBeGreaterThan(uncapped);
  });

  test('DC-coupled idle passive charging: throughputKwh > 0 adds degradation', () => {
    // DC PV 2 kW, idle → passive charging → degradation cost added
    const noDc = calculateStepCost(0.25, 5000, 0, 0.15, 0.07, 0, 1000, 0.9, 0.004, false, 2000, 0.97, 0, 10000);
    const dcIdle = calculateStepCost(0.25, 5000, 0, 0.15, 0.07, 0, 1000, 0.9, 0.004, true, 2000, 0.97, 0, 10000);
    // DC passive charging reduces grid draw (pvExcess → AC) and adds degradation
    // Net should differ from non-DC case
    expect(dcIdle).not.toBeCloseTo(noDc, 4);
  });
});

// ─── findNearestSocIdx ──────────────────────────────────────────────────────

describe('findNearestSocIdx', () => {
  const states = [1000, 1025, 1050, 1075, 1100];

  test('exact match returns correct index', () => {
    expect(findNearestSocIdx(1050, states)).toBe(2);
  });

  test('rounds to nearest state', () => {
    expect(findNearestSocIdx(1038, states)).toBe(2); // closer to 1050 than 1025
    expect(findNearestSocIdx(1036, states)).toBe(1); // closer to 1025
  });

  test('clamps below minimum', () => {
    expect(findNearestSocIdx(0, states)).toBe(0);
  });

  test('clamps above maximum', () => {
    expect(findNearestSocIdx(99999, states)).toBe(states.length - 1);
  });

  test('single-element array returns 0', () => {
    expect(findNearestSocIdx(5000, [5000])).toBe(0);
  });
});

// ─── runOptimizer — schedule decisions ──────────────────────────────────────

describe('runOptimizer', () => {
  test('charges when price is low (battery starts at min SoC so discharge is blocked)', () => {
    // Start at minSoc (1 kWh) — can only charge or idle.
    // Step 0: €0.10 cheap → charge is profitable vs terminal lambda ≈ 0.07
    // Using explicit terminalShadowPrice=0.25 to make charging attractive at 0.10.
    const cfg = makeCfg({ minSocKwh: 1.0, maxSocKwh: 10.0 });
    const result = runOptimizer(cfg, 1.0, {
      priceFc: [0.10, 0.40],
      feedInFc: [0.07, 0.07],
      pvFc: [0, 0],
      consumFc: [1.0, 1.0],
      stepDurations: [0.25, 0.25],
      degradCost: 0.004,
      minPriceSpread: 0.05,
      pvDcFc: null,
      terminalShadowPrice: 0.25,  // high terminal value makes charging at 0.10 profitable
    });
    // With terminal=0.25: charge profit = 0.25 - 0.10/sqrt(0.9) - 0.004 ≈ 0.25 - 0.105 - 0.004 > 0
    expect(result.modes[0]).toBe('charging');
  });

  test('charge efficiency correction reduces charged energy in the optimizer result', () => {
    const baseInputs = {
      priceFc: [0.10, 0.40],
      feedInFc: [0.07, 0.07],
      pvFc: [0, 0],
      consumFc: [1.0, 1.0],
      stepDurations: [0.25, 0.25],
      degradCost: 0.004,
      minPriceSpread: 0.05,
      pvDcFc: null,
      terminalShadowPrice: 0.25,
    };
    const nominal = runOptimizer(makeCfg(), 1.0, baseInputs);
    const corrected = runOptimizer(makeCfg(), 1.0, {
      ...baseInputs,
      chargeEffCorrection: 0.7,
    });

    expect(corrected.socKwh[1]).toBeLessThan(nominal.socKwh[1]);
  });

  test('discharges when price is high after cheap charging', () => {
    // Start at high SoC so discharge is possible at the expensive step
    const result = runSimple([0.10, 0.40], [0.07, 0.07], null, null, 5);
    expect(result.modes.some(m => m === 'discharging')).toBe(true);
  });

  test('stays idle when buy price equals terminal and feed-in (no arbitrage possible)', () => {
    // buy = feed-in = terminal = 0.15: any charge/discharge loses money to degradation only
    const cfg = makeCfg();
    const result = runOptimizer(cfg, 5, {
      priceFc: [0.15, 0.15],
      feedInFc: [0.15, 0.15],
      pvFc: [0, 0],
      consumFc: [1.0, 1.0],
      stepDurations: [0.25, 0.25],
      degradCost: 0.01,          // high degrad makes any cycling clearly unprofitable
      minPriceSpread: 0.05,
      pvDcFc: null,
      terminalShadowPrice: 0.15,
    });
    expect(result.modes.every(m => m === 'idle')).toBe(true);
  });

  test('savings are positive for profitable arbitrage', () => {
    const result = runSimple([0.10, 0.40], [0.07, 0.07], null, null, 5);
    expect(result.savings).toBeGreaterThan(0);
  });

  test('savings are near zero when no arbitrage is possible (flat prices, high degrad)', () => {
    const cfg = makeCfg();
    const result = runOptimizer(cfg, 5, {
      priceFc: [0.15, 0.15],
      feedInFc: [0.15, 0.15],
      pvFc: [0, 0],
      consumFc: [1.0, 1.0],
      stepDurations: [0.25, 0.25],
      degradCost: 0.01,
      minPriceSpread: 0.05,
      pvDcFc: null,
      terminalShadowPrice: 0.15,
    });
    expect(result.savings).toBeCloseTo(0, 3);
  });

  test('does not exceed max charge power', () => {
    const cfg = makeCfg({ maxChargeKw: 2.0 });
    const result = runSimple([0.10, 0.40], null, null, null, 5, cfg);
    // powerKw is negative=charge in forwardPass output convention (inverted sign)
    const maxChargeKw = Math.max(...result.powerKw.map(p => -p));
    expect(maxChargeKw).toBeLessThanOrEqual(2.0 + 0.01); // small float tolerance
  });

  test('respects min SoC boundary (does not discharge below min)', () => {
    // Start at min SoC, try to discharge
    const cfg = makeCfg({ minSocKwh: 1.0, maxSocKwh: 10.0 });
    const result = runSimple([0.10, 0.40], null, null, null, 1.0, cfg);
    expect(result.socKwh.every(s => s >= 0.99)).toBe(true);
  });

  test('respects max SoC boundary (does not charge above max)', () => {
    const cfg = makeCfg({ minSocKwh: 1.0, maxSocKwh: 5.0 });
    const result = runSimple([0.10, 0.40], null, null, null, 5.0, cfg);
    expect(result.socKwh.every(s => s <= 5.01)).toBe(true);
  });

  test('terminal shadow price influences final-step behavior', () => {
    // High terminal price → hold energy for end-of-horizon; low → discharge more
    const priceFc = [0.30];
    const base = { priceFc, feedInFc: [0.07], pvFc: [0], consumFc: [1.0],
                   stepDurations: [0.25], degradCost: 0.004, minPriceSpread: 0.05,
                   pvDcFc: null };
    const highTerminal = runOptimizer(makeCfg(), 5, { ...base, terminalShadowPrice: 0.50 });
    const lowTerminal  = runOptimizer(makeCfg(), 5, { ...base, terminalShadowPrice: 0.01 });
    // With high terminal price, energy is more valuable to hold → less or no discharge
    // With low terminal price, €0.30 sell price is attractive → more discharge
    // At minimum: discharge should be equal or greater under low terminal
    const dischargeHigh = highTerminal.modes.filter(m => m === 'discharging').length;
    const dischargeLow  = lowTerminal.modes.filter(m  => m === 'discharging').length;
    expect(dischargeLow).toBeGreaterThanOrEqual(dischargeHigh);
  });

  test('shadow price is a finite number', () => {
    // The shadow price sign depends on whether V increases or decreases with SoC.
    // In practice it should be near the feed-in price; just verify it is finite.
    const result = runSimple([0.10, 0.40], null, null, null, 5);
    expect(Number.isFinite(result.shadowPrice)).toBe(true);
  });

  test('SoC schedule has length = steps + 1', () => {
    const result = runSimple([0.10, 0.30, 0.20, 0.40], null, null, null, 5);
    expect(result.socKwh.length).toBe(result.modes.length + 1);
  });
});

// ─── computeShadowPrice ─────────────────────────────────────────────────────

describe('computeShadowPrice', () => {
  test('returns 0 for too few SoC states', () => {
    const V = [new Float64Array([1, 2])];
    expect(computeShadowPrice(V, [1000, 2000], 1.5)).toBe(0);
  });

  test('returns a finite number reflecting the marginal value of stored energy', () => {
    const dp = runDP(
      makeCfg(), 5,
      [0.10, 0.40], null, [0, 0], [1.0, 1.0],
      [0.25, 0.25], 0.004, 0.05, null, null
    );
    const shadow = computeShadowPrice(dp.V, dp.socStates, 5);
    expect(Number.isFinite(shadow)).toBe(true);
    // Shadow price should be in the ballpark of the feed-in / buy price range
    expect(Math.abs(shadow)).toBeLessThan(1.0);
  });
});

// ─── totalPvSeries / netPvSeries ────────────────────────────────────────────

describe('totalPvSeries / netPvSeries', () => {
  test('AC-only system is unchanged', () => {
    expect(totalPvSeries([1, 2, 3], [])).toEqual([1, 2, 3]);
  });

  test('DC PV is counted through the inverter', () => {
    const out = totalPvSeries([0, 0], [2, 4]);
    expect(out[0]).toBeCloseTo(2 * DC_TO_AC_EFF);
    expect(out[1]).toBeCloseTo(4 * DC_TO_AC_EFF);
  });

  test('AC and DC are summed', () => {
    const out = totalPvSeries([1, 1], [2, 0]);
    expect(out[0]).toBeCloseTo(1 + 2 * DC_TO_AC_EFF);
    expect(out[1]).toBeCloseTo(1);
  });

  test('handles missing/undefined series and unequal lengths', () => {
    expect(totalPvSeries(undefined, undefined)).toEqual([]);
    expect(totalPvSeries([1], [1, 1]).length).toBe(2);
  });

  test('net line reflects DC PV instead of showing no solar', () => {
    // DC-coupled system: AC series all zeros, DC carries production
    const net = netPvSeries([0, 0], [4, 4], [1, 1]);
    expect(net[0]).toBeCloseTo(4 * DC_TO_AC_EFF - 1);
    expect(net[0]).toBeGreaterThan(0); // surplus, not a deficit
  });

  test('net line is a deficit when consumption exceeds PV', () => {
    const net = netPvSeries([0], [1], [3]);
    expect(net[0]).toBeLessThan(0);
  });
});

// ─── generateTips ───────────────────────────────────────────────────────────

describe('generateTips', () => {
  /** Build minimal diagnostics object. */
  function makeDiag(overrides = {}) {
    return {
      battery_config: {
        round_trip_efficiency: 0.92,
        max_soc_kwh: 9.0,
        min_soc_kwh: 1.0,
        pv_dc_coupled: false,
      },
      optimization: {
        control_mode: 'follow_schedule',
        schedule: {
          price_forecast: [0.10, 0.15, 0.30, 0.12],
          mode_schedule: ['charging', 'idle', 'discharging', 'idle'],
          soc_schedule_kwh: [3, 4, 4, 3.5, 3],
        },
        battery_state: { soc_kwh: 3, soc_percent: 30 },
        savings: 0.005,
        baseline_cost: 0.05,
        timestamp: new Date(Date.now() - 5 * 60000).toISOString(), // 5 min ago
      },
      forecast: {},
      config_entry: {
        options: {
          degradation_cost_per_cycle: 0.04,
          min_price_spread: 0.05,
          fixed_feed_in_price: 0.07,
        },
      },
      ...overrides,
    };
  }

  test('returns an array of tip objects', () => {
    const tips = generateTips(makeDiag());
    expect(Array.isArray(tips)).toBe(true);
    expect(tips.length).toBeGreaterThan(0);
    tips.forEach(tip => {
      expect(tip).toHaveProperty('t');
      expect(tip).toHaveProperty('title');
      expect(tip).toHaveProperty('text');
      expect(['ok', 'info', 'warn', 'err']).toContain(tip.t);
    });
  });

  test('reports failure_reason as err tip', () => {
    const d = makeDiag();
    d.optimization.failure_reason = 'No forecast data available';
    const tips = generateTips(d);
    expect(tips.some(t => t.t === 'err' && t.title.includes('failed'))).toBe(true);
  });

  test('warns for very low RTE', () => {
    const d = makeDiag();
    d.battery_config.round_trip_efficiency = 0.75;
    const tips = generateTips(d);
    expect(tips.some(t => t.t === 'err' && t.title.toLowerCase().includes('efficiency'))).toBe(true);
  });

  test('warns for unrealistically high RTE', () => {
    const d = makeDiag();
    d.battery_config.round_trip_efficiency = 0.99;
    const tips = generateTips(d);
    expect(tips.some(t => t.t === 'warn' && t.title.toLowerCase().includes('high rte'))).toBe(true);
  });

  test('warns when entire schedule is idle', () => {
    const d = makeDiag();
    d.optimization.schedule.mode_schedule = ['idle', 'idle', 'idle', 'idle'];
    const tips = generateTips(d);
    expect(tips.some(t => t.t === 'warn' && t.title.toLowerCase().includes('idle'))).toBe(true);
  });

  test('errors on manual mode', () => {
    const d = makeDiag();
    d.optimization.control_mode = 'manual';
    const tips = generateTips(d);
    expect(tips.some(t => t.t === 'err' && t.title.toLowerCase().includes('manual'))).toBe(true);
  });

  test('errors on current_only price source', () => {
    const d = makeDiag();
    d.optimization.price_forecast_source = 'current_only';
    const tips = generateTips(d);
    expect(tips.some(t => t.t === 'err' && t.title.toLowerCase().includes('current price only'))).toBe(true);
  });

  test('warns when price spread is insufficient for arbitrage', () => {
    const d = makeDiag();
    // Very flat prices → spread < minArb
    d.optimization.schedule.price_forecast = [0.15, 0.152, 0.151, 0.149];
    const tips = generateTips(d);
    expect(tips.some(t => t.t === 'warn' && t.title.toLowerCase().includes('spread'))).toBe(true);
  });

  test('ok tip when price spread is sufficient', () => {
    const d = makeDiag();
    // Prices span 0.10–0.40 → spread = 0.30, well above threshold
    d.optimization.schedule.price_forecast = [0.10, 0.40, 0.25, 0.30];
    const tips = generateTips(d);
    expect(tips.some(t => t.t === 'ok' && t.title.toLowerCase().includes('spread'))).toBe(true);
  });

  test('warns for SoC-limited optimizer runs', () => {
    const d = makeDiag();
    d.optimization.optimizer_run_log = [
      { effective_power_kw: 1.2, setpoint_kw: 0.0, commitment_locked: false,
        timestamp: '2026-03-23T10:00:00', soc_kwh: 9.0 },
      { effective_power_kw: 1.2, setpoint_kw: 0.0, commitment_locked: false,
        timestamp: '2026-03-23T10:15:00', soc_kwh: 9.0 },
      { effective_power_kw: 0.5, setpoint_kw: 0.5, commitment_locked: false,
        timestamp: '2026-03-23T10:30:00', soc_kwh: 7.0 },
    ];
    const tips = generateTips(d);
    expect(tips.some(t => t.t === 'warn' && t.title.toLowerCase().includes('soc/power limited'))).toBe(true);
  });

  test('does not warn for zero_grid runs where effective_power_kw is 0 by design', () => {
    const d = makeDiag();
    d.optimization.optimizer_run_log = [
      { effective_power_kw: 0.0, setpoint_kw: 1.8, commitment_locked: false,
        effective_mode: 'zero_grid', timestamp: '2026-03-23T10:00:00', soc_kwh: 5.0 },
      { effective_power_kw: 0.0, setpoint_kw: 2.1, commitment_locked: false,
        effective_mode: 'zero_grid', timestamp: '2026-03-23T10:15:00', soc_kwh: 5.5 },
      { effective_power_kw: 0.5, setpoint_kw: 0.5, commitment_locked: false,
        effective_mode: 'charging', timestamp: '2026-03-23T10:30:00', soc_kwh: 6.0 },
    ];
    const tips = generateTips(d);
    expect(tips.some(t => t.t === 'warn' && t.title.toLowerCase().includes('soc/power limited'))).toBe(false);
  });

  test('info tip when commitment filter is active', () => {
    const d = makeDiag();
    d.optimization.optimizer_run_log = [
      { effective_power_kw: 1.2, setpoint_kw: 1.2, commitment_locked: true,
        commitment_reason: 'power_locked', timestamp: '2026-03-23T10:00:00', soc_kwh: 5.0 },
      { effective_power_kw: 1.2, setpoint_kw: 1.2, commitment_locked: true,
        commitment_reason: 'power_locked', timestamp: '2026-03-23T10:15:00', soc_kwh: 5.5 },
      { effective_power_kw: 0.5, setpoint_kw: 0.5, commitment_locked: false,
        timestamp: '2026-03-23T10:30:00', soc_kwh: 6.0 },
    ];
    const tips = generateTips(d);
    expect(tips.some(t => t.t === 'info' && t.title.toLowerCase().includes('commitment'))).toBe(true);
  });

  test('warns for SoC-limited real-time setpoints', () => {
    const d = makeDiag();
    d.optimization.setpoint_log = [
      { soc_limited: true,  schedule_kw: 1.5, setpoint_kw: 0.0,
        timestamp: '2026-03-23T10:01:00' },
      { soc_limited: false, schedule_kw: 0.5, setpoint_kw: 0.5,
        timestamp: '2026-03-23T10:01:05' },
    ];
    const tips = generateTips(d);
    expect(tips.some(t => t.t === 'warn' && t.title.toLowerCase().includes('soc limit blocked'))).toBe(true);
  });

  test('no soc_limited tip when setpoint_log is empty', () => {
    const d = makeDiag();
    d.optimization.setpoint_log = [];
    const tips = generateTips(d);
    expect(tips.some(t => t.title.toLowerCase().includes('soc limit blocked'))).toBe(false);
  });

  test('err tip when consumption pattern is empty', () => {
    const d = makeDiag();
    d.forecast.consumption_hourly_pattern = {};
    d.forecast.current_consumption_kw = 0.5;
    const tips = generateTips(d);
    const tip = tips.find(t => t.title.toLowerCase().includes('no consumption pattern learned'));
    expect(tip).toBeDefined();
    expect(tip.t).toBe('err');
    // Should tell the user that waiting does not help
    expect(tip.text.toLowerCase()).toContain('not');
    expect(tip.text).toContain('0.5 kW');
  });

  test('warn tip when consumption pattern is only partly learned', () => {
    const d = makeDiag();
    d.forecast.consumption_hourly_pattern = { '08_0': 0.4, '09_0': 0.5, '10_0': 0.6 };
    const tips = generateTips(d);
    const tip = tips.find(t => t.title.toLowerCase().includes('partly learned'));
    expect(tip).toBeDefined();
    expect(tip.t).toBe('warn');
    expect(tip.title).toContain('3 of 168');
  });

  test('no consumption pattern tip when the pattern is well populated', () => {
    const d = makeDiag();
    const pattern = {};
    for (let h = 0; h < 24; h++) {
      for (let dow = 0; dow < 7; dow++) {
        pattern[`${String(h).padStart(2, '0')}_${dow}`] = 1.2;
      }
    }
    d.forecast.consumption_hourly_pattern = pattern;
    const tips = generateTips(d);
    expect(tips.some(t => t.title.toLowerCase().includes('consumption pattern'))).toBe(false);
  });

  test('no consumption pattern tip for diagnostics without the key', () => {
    // Older diagnostics files have no consumption_hourly_pattern at all
    const d = makeDiag();
    const tips = generateTips(d);
    expect(tips.some(t => t.title.toLowerCase().includes('consumption pattern'))).toBe(false);
  });

  test('info tip reports an all-DC PV forecast', () => {
    const d = makeDiag();
    d.forecast.pv_forecast_kw = [0, 0, 0, 0];
    d.forecast.pv_dc_forecast_kw = [2, 2, 2, 2];
    d.forecast.forecast_interval_minutes = 15;
    const tips = generateTips(d);
    const tip = tips.find(t => t.title.includes('all DC-coupled'));
    expect(tip).toBeDefined();
    expect(tip.t).toBe('info');
    // 4 steps x 2 kW x 0.25 h = 2.0 kWh
    expect(tip.title).toContain('2.0 kWh');
  });

  test('warn tip when the PV forecast is zero everywhere', () => {
    const d = makeDiag();
    d.forecast.pv_forecast_kw = [0, 0];
    d.forecast.pv_dc_forecast_kw = [0, 0];
    const tips = generateTips(d);
    const tip = tips.find(t => t.title.toLowerCase().includes('pv forecast is zero'));
    expect(tip).toBeDefined();
    expect(tip.t).toBe('warn');
  });

  test('info tip reports the AC/DC split when both produce', () => {
    const d = makeDiag();
    d.forecast.pv_forecast_kw = [1, 1];
    d.forecast.pv_dc_forecast_kw = [1, 1];
    d.forecast.forecast_interval_minutes = 60;
    const tips = generateTips(d);
    const tip = tips.find(t => t.title.includes('over the horizon'));
    expect(tip).toBeDefined();
    expect(tip.text).toContain('DC-coupled');
  });

  test('no PV forecast tip when the arrays are absent', () => {
    const d = makeDiag();
    const tips = generateTips(d);
    expect(tips.some(t => t.title.toLowerCase().includes('pv forecast'))).toBe(false);
  });

  test('SoC boundary tips name the planned horizon, not history', () => {
    const d = makeDiag();
    // Battery pinned at max for the whole planned schedule
    d.battery_config.max_soc_kwh = 9.0;
    d.battery_config.min_soc_kwh = 1.0;
    d.optimization.schedule.soc_schedule_kwh = [9, 9, 9, 9, 9];
    const tips = generateTips(d);
    const tip = tips.find(t => t.title.toLowerCase().includes('max soc'));
    expect(tip).toBeDefined();
    // Must not read as a claim about measured history
    expect(tip.title.toLowerCase()).toContain('planned');
    expect(tip.title.toLowerCase()).toContain('horizon');
    expect(tip.title.toLowerCase()).not.toContain('of the time');
    expect(tip.text.toLowerCase()).toContain('not what the battery did in the past');
  });

  test('minimum SoC tip is worded the same way', () => {
    const d = makeDiag();
    d.battery_config.max_soc_kwh = 9.0;
    d.battery_config.min_soc_kwh = 1.0;
    d.optimization.schedule.soc_schedule_kwh = [1, 1, 1, 1, 1];
    const tips = generateTips(d);
    const tip = tips.find(t => t.title.toLowerCase().includes('minimum soc'));
    expect(tip).toBeDefined();
    expect(tip.title.toLowerCase()).toContain('planned');
    expect(tip.text.toLowerCase()).toContain('not what the battery did in the past');
  });

  test('ok tip included in clean configuration', () => {
    // Only ok and info tips should result in a "well-tuned" message
    // Remove any triggers for warn/err
    const d = makeDiag();
    d.optimization.price_forecast_source = 'live';
    d.optimization.schedule.price_forecast = [0.10, 0.40]; // good spread
    d.optimization.schedule.mode_schedule = ['charging', 'discharging'];
    const tips = generateTips(d);
    // Either explicit savings ok-tip or well-tuned tip should be present
    expect(tips.some(t => t.t === 'ok')).toBe(true);
  });
});
