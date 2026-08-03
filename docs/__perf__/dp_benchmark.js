#!/usr/bin/env node
/**
 * Runtime guard for the browser-side DP.
 *
 * cross_impl.test.js proves analyzer.js still computes what optimizer.py
 * computes. Nothing proved it still does so in reasonable time — a change that
 * keeps every result correct while making the in-browser solve an order of
 * magnitude slower would pass every existing test, and the analyzer would
 * simply feel broken to whoever uploaded a diagnostics file.
 *
 * Deliberately NOT a jest test. Jest runs module code in a vm context, which
 * costs about 16x on this hot numeric loop (0.7 s becomes 11.3 s) — so a jest
 * benchmark mostly measures jest. Coverage instrumentation is worse again
 * (18 s, and it dragged the whole suite from 6 s to 111 s). A plain Node
 * script measures what the browser actually does with this file.
 *
 * This is a coarse guard, not a benchmark. Hosted runners vary in speed and
 * are noisy, so the ceiling carries wide headroom: it exists to catch
 * order-of-magnitude regressions, not a 20% slowdown. The measured figure is
 * printed on every run so the trend stays visible while the check passes.
 */

const { runOptimizer } = require('../analyzer/analyzer.js');

// A full 36-hour horizon at 15-minute resolution: the longest solve the
// integration actually asks for.
const STEPS = 144;

// Uninstrumented baseline on the machine this was written on: ~0.7 s median.
// 5 s leaves roughly 7x headroom, which absorbs a slower hosted runner while
// still catching an order-of-magnitude regression.
const CEILING_MS = 5000;

const REPEATS = 5;

function buildScenario(steps) {
  const config = {
    capacityKwh: 10,
    minSocKwh: 1,
    maxSocKwh: 9,
    maxChargeKw: 5,
    maxDischargeKw: 5,
    chargeCurve: [[0.0, 0.9487], [5.0, 0.9487]],
    dischargeCurve: [[0.0, 0.9487], [5.0, 0.9487]],
    pvDcCoupled: false,
    pvDcEfficiency: 0.97,
    maxGridPowerKw: 0,
  };

  // A day-shaped price curve with a PV bump, so the DP has real trade-offs to
  // resolve rather than collapsing to all-idle.
  const priceFc = Array.from({ length: steps }, (_, i) => 0.20 + 0.15 * Math.sin(i / 12));

  const options = {
    priceFc,
    feedInFc: priceFc.map((p) => p * 0.3),
    pvFc: Array.from({ length: steps }, (_, i) => Math.max(0, 3 * Math.sin((i - 24) / 30))),
    consumFc: Array.from({ length: steps }, () => 0.5),
    stepDurations: Array.from({ length: steps }, () => 0.25),
    degradCost: 0.02,
    minPriceSpread: 0.05,
    pvDcFc: Array.from({ length: steps }, () => 0),
  };

  return { config, options };
}

function main() {
  const { config, options } = buildScenario(STEPS);

  // Warm-up: the first call pays JIT and allocation costs that say nothing
  // about the algorithm.
  runOptimizer(config, 5, options);

  const timings = [];
  for (let i = 0; i < REPEATS; i += 1) {
    const started = Date.now();
    const result = runOptimizer(config, 5, options);
    timings.push(Date.now() - started);

    // Guard against the timing becoming meaningless if the solve quietly
    // starts returning nothing.
    if (!result || !Array.isArray(result.powerKw) || result.powerKw.length !== STEPS) {
      console.error(
        `FAIL: expected a ${STEPS}-step schedule, got ` +
        `${result && result.powerKw ? result.powerKw.length : 'nothing'}`,
      );
      process.exit(1);
    }
  }

  const sorted = [...timings].sort((a, b) => a - b);
  const median = sorted[Math.floor(REPEATS / 2)];

  console.log(
    `DP runtime: ${STEPS} steps, median ${median} ms ` +
    `(runs: ${timings.join(', ')} ms, ceiling ${CEILING_MS} ms)`,
  );

  if (median >= CEILING_MS) {
    console.error(
      `FAIL: median ${median} ms is at or over the ${CEILING_MS} ms ceiling. ` +
      'Either the DP regressed, or the baseline moved and the ceiling needs ' +
      'recalibrating — say which in the commit that changes it.',
    );
    process.exit(1);
  }

  console.log('OK');
}

main();
