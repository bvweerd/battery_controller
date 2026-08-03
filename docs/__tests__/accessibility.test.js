/**
 * @jest-environment jsdom
 */

/**
 * Accessibility regression guard for the analyzer page.
 *
 * The analyzer is the only real application in this repository — a file
 * upload, six canvases and a pile of generated tables — and it shipped with no
 * ARIA attributes at all. Worse than the missing labels, the upload control
 * was a plain <div> with a click listener wrapping a display:none input, so
 * there was no focusable element for uploading a file: with a keyboard the
 * page could not be used at all.
 *
 * This runs axe-core over the static markup. The page's own scripts are not
 * executed and the Tailwind and Chart.js CDN bundles are not fetched: the
 * point is to check the structure that ships in index.html, not to reproduce a
 * browser. Rules that need real layout or computed styles cannot mean anything
 * under those conditions and are turned off explicitly rather than left to
 * produce noise.
 *
 * Dynamically generated content (report tables, tips) is out of scope — that
 * would need a real browser and a loaded diagnostics file.
 */

const fs = require('fs');
const path = require('path');
const axe = require('axe-core');

const PAGE = path.join(__dirname, '..', 'analyzer', 'index.html');

// Meaningless without real CSS: no Tailwind is applied here, so every computed
// colour is a default and every element is zero-sized.
const DISABLED_RULES = ['color-contrast', 'target-size'];

jest.setTimeout(60000);

describe('analyzer accessibility', () => {
  const source = fs.readFileSync(PAGE, 'utf8');
  let results;

  beforeAll(async () => {
    // Strip the CDN script tags so jsdom does not try to reach the network,
    // and the inline page script so nothing runs against a half-built DOM.
    const staticMarkup = source
      .replace(/<script\b[^>]*src=["']https?:[^"']*["'][^>]*><\/script>/g, '')
      .replace(/<script\b(?![^>]*\bsrc=)[^>]*>[\s\S]*?<\/script>/g, '');

    document.open();
    document.write(staticMarkup);
    document.close();

    results = await axe.run(document, {
      rules: Object.fromEntries(DISABLED_RULES.map((id) => [id, { enabled: false }])),
    });
  });

  test('has no axe violations in the static markup', () => {
    if (results.violations.length > 0) {
      const detail = results.violations
        .map((v) => {
          const nodes = v.nodes.map((n) => `      ${n.html.slice(0, 160)}`).join('\n');
          return `  [${v.impact}] ${v.id}: ${v.help}\n${nodes}`;
        })
        .join('\n');
      throw new Error(`axe found ${results.violations.length} violation(s):\n${detail}`);
    }
    expect(results.violations).toHaveLength(0);
  });

  test('every chart canvas carries a text alternative', () => {
    const canvases = [...document.querySelectorAll('canvas')];
    expect(canvases.length).toBeGreaterThan(0);
    canvases.forEach((canvas) => {
      expect(canvas.getAttribute('role')).toBe('img');
      expect(canvas.getAttribute('aria-label')).toBeTruthy();
    });
  });

  test('the upload control is reachable and operable without a mouse', () => {
    const dropZone = document.getElementById('drop-zone');
    expect(dropZone).not.toBeNull();
    // A display:none input cannot receive focus, so the wrapper has to be the
    // focusable, labelled control.
    expect(dropZone.getAttribute('tabindex')).toBe('0');
    expect(dropZone.getAttribute('role')).toBe('button');
    expect(dropZone.getAttribute('aria-label')).toBeTruthy();

    // The keyboard handler lives in the inline script this test strips out —
    // assert it is wired up in the shipped file rather than that it fires.
    expect(source).toMatch(/dz\.addEventListener\('keydown'/);
  });
});
