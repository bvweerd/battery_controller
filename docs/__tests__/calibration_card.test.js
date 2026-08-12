/**
 * @jest-environment jsdom
 */

/**
 * What the analyzer says about the learned calibration.
 *
 * The numbers themselves are unambiguous only in one direction: a correction
 * of 1.0 means "measured, and on target" when samples exist and "never
 * measured anything" when they do not, and the second case is by far the more
 * common one. Rendering both as a confident 100% is how a user concludes the
 * integration has calibrated itself when in fact it never can — on a
 * DC-coupled system, or on a PV array with no production meter, the sample
 * count stays at zero forever.
 *
 * This drives the page's own render path against a synthetic diagnostics
 * payload. The CDN bundles are stripped and Chart.js is stubbed: the assertion
 * is about the text the card produces, not about drawing anything.
 */

const fs = require('fs');
const path = require('path');

const PAGE = path.join(__dirname, '..', 'analyzer', 'index.html');

const DIAGNOSTICS = {
  config_entry: { entry_id: 'e1', title: 't', data: {}, options: {}, subentries: {} },
  battery_config: {
    capacity_kwh: 10, usable_capacity_kwh: 8,
    max_charge_power_kw: 5, max_discharge_power_kw: 5,
    round_trip_efficiency: 0.9, charge_efficiency: 0.9487, discharge_efficiency: 0.9487,
    min_soc_percent: 10, max_soc_percent: 90, min_soc_kwh: 1, max_soc_kwh: 9,
    pv_dc_coupled: false, pv_dc_peak_power_kwp: 0, pv_dc_efficiency: 0.97,
  },
  weather: {},
  forecast: {
    current_pv_kw: 1.2, current_dc_pv_kw: 0,
    current_consumption_kw: 0.5, current_net_load_kw: -0.7,
    pv_calibration: {
      // Learned and in use.
      'South Array': { correction: 0.87, samples: 64, applied: true, last_result: 'sampled' },
      // Can never learn: no production meter configured.
      'East Array': { correction: 1.0, samples: 0, applied: false, last_result: 'no_measured_production_sensor' },
    },
  },
  optimization: {
    control_mode: 'hybrid', optimal_mode: 'idle', optimal_power_kw: 0,
    current_price: 0.24, current_feed_in_price: 0.07, shadow_price_eur_kwh: 0.11,
    // Learned and in use.
    charge_eff_correction: 0.94, charge_eff_samples: 12,
    charge_eff_applied: true, charge_eff_last_result: 'sampled',
    // Can never learn: DC-coupled PV makes the SoC delta unusable.
    discharge_eff_correction: 1.0, discharge_eff_samples: 0,
    discharge_eff_applied: false, discharge_eff_last_result: 'dc_coupled_pv',
    battery_state: { soc_kwh: 5, soc_percent: 50, power_kw: 0, mode: 'idle' },
    schedule: {
      price_forecast: [0.2, 0.3], power_schedule_kw: [0, 0],
      mode_schedule: ['idle', 'idle'], soc_schedule_kwh: [5, 5], price_interval: 60,
    },
  },
  entities: [],
};

describe('analyzer learned-calibration card', () => {
  let card;

  beforeAll(() => {
    const markup = fs
      .readFileSync(PAGE, 'utf8')
      .replace(/<script\b[^>]*src=["']https?:[^"']*["'][^>]*><\/script>/g, '');
    document.open();
    document.write(markup);
    document.close();

    // Stand in for the CDN bundles the page loads in a browser.
    window.Chart = function () { return { destroy() {}, update() {} }; };
    window.Chart.register = () => {};
    HTMLCanvasElement.prototype.getContext = () => ({});

    window.parseAndLoad(JSON.stringify(DIAGNOSTICS));
    card = document.getElementById('cfg-calibration').textContent;
  });

  test('shows a learned correction as applied', () => {
    expect(card).toContain('Battery charge efficiency');
    expect(card).toContain('94.0%');
    expect(card).toContain('n=12');
  });

  test('does not present an unmeasured correction as 100%', () => {
    // The discharge side has never sampled; "100.0% applied" would be a lie.
    expect(card).toContain('not learned');
    expect(card).not.toMatch(/100\.0% \(n=0\) applied/);
  });

  test('names the reason a correction cannot be learned', () => {
    expect(card).toContain('dc coupled pv');
    expect(card).toContain('no measured production sensor');
  });

  test('reports every PV array, including one that is not calibrated', () => {
    expect(card).toContain('South Array');
    expect(card).toContain('87.0%');
    // An array left out of the report is exactly what hides the problem.
    expect(card).toContain('East Array');
  });

  test('the efficiency card no longer claims 100% for an unmeasured correction', () => {
    const eff = document.getElementById('cfg-eff').textContent;
    expect(eff).toContain('Discharge eff correction');
    expect(eff).toContain('not learned yet');
  });
});
