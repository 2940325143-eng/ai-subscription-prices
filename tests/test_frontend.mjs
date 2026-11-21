import test from 'node:test';
import assert from 'node:assert/strict';

import {
  PRODUCT_CONFIG,
  applyOverridesAndGuardToProduct,
  buildColumns,
  formatFxUpdatedDisplay,
  formatProductFreshness,
  formatTime,
  minimumPricesForProduct,
} from '../public/app.js';

test('ChatGPT 7-tier column filters produce expected column counts', () => {
  assert.equal(PRODUCT_CONFIG.chatgpt.plans.length, 7);
  assert.equal(buildColumns('chatgpt', 'all').length, 12); // 5*2 + 2 team web-only
  assert.equal(buildColumns('chatgpt', 'personal').length, 10);
  assert.equal(buildColumns('chatgpt', 'team').length, 2);
  assert.equal(buildColumns('chatgpt', 'web').length, 7);
  assert.equal(buildColumns('chatgpt', 'apple').length, 5);
  assert.equal(buildColumns('google_ultra', 'all').length, 4);
});

test('minimumPricesForProduct excludes suspectGrandfathered and historyStale cells', () => {
  const fx = { rates: { EUR: { rate: 1 }, TRY: { rate: 55.5 }, USD: { rate: 1.1 }, NGN: { rate: 1600 } } };
  const rawProd = {
    applePlans: ['ultra5x', 'ultra20x'],
    countries: [
      {
        code: 'TR',
        name: '土耳其',
        excluded: false,
        web: {
          cells: {
            ultra5x: { status: 'ok', currency: 'TRY', amount: 4749.99 },
            ultra20x: { status: 'ok', currency: 'TRY', amount: 8999.99 },
          },
        },
        apple: {
          cells: {
            // 1989.99 / 4749.99 = 0.4189 (< 0.65 -> suspectGrandfathered)
            ultra5x: { status: 'ok', currency: 'TRY', amount: 1989.99 },
            ultra20x: { status: 'ok', currency: 'TRY', amount: 2949.99 },
          },
        },
      },
      {
        code: 'NG',
        name: '尼日利亚',
        excluded: false,
        web: {
          cells: {
            ultra5x: { status: 'ok', currency: 'NGN', amount: 140000 }, // €87.50
            ultra20x: { status: 'pending', amount: null, currency: null },
          },
        },
        apple: {
          cells: {
            ultra5x: { status: 'ok', currency: 'NGN', amount: 150000, historyRetained: true, historyStale: true },
            ultra20x: { status: 'ok', currency: 'USD', amount: 199.99 },
          },
        },
      },
      {
        code: 'US',
        name: '美国',
        excluded: false,
        web: {
          cells: {
            ultra5x: { status: 'ok', currency: 'USD', amount: 124.99 },
            ultra20x: { status: 'ok', currency: 'USD', amount: 249.99 },
          },
        },
        apple: {
          cells: {
            ultra5x: { status: 'ok', currency: 'USD', amount: 124.99 },
            ultra20x: { status: 'ok', currency: 'USD', amount: 249.99 },
          },
        },
      },
    ],
  };

  const processed = applyOverridesAndGuardToProduct('google_ultra', rawProd, { entries: {} });
  const trRow = processed.countries.find((c) => c.code === 'TR');
  assert.equal(trRow.apple.cells.ultra5x.suspectGrandfathered, true);
  assert.equal(trRow.apple.cells.ultra5x.excludeFromMinCard, true);

  const cols = buildColumns('google_ultra', 'all');
  const mins = Object.fromEntries(minimumPricesForProduct(processed, cols, fx).map((m) => [m.column.key, m.winner]));

  // ultra5x-apple: TR excluded (suspect) and NG excluded (stale) -> must pick US!
  assert.equal(mins['ultra5x-apple'].country.code, 'US');
  // ultra20x-apple: TR excluded (suspect) -> must pick NG (USD 199.99)!
  assert.equal(mins['ultra20x-apple'].country.code, 'NG');
});

test('applyOverridesAndGuardToProduct promotes manual override and clears suspect flag', () => {
  const fx = { rates: { EUR: { rate: 1 }, TRY: { rate: 55.5 }, USD: { rate: 1.1 } } };
  const rawProd = {
    applePlans: ['ultra5x', 'ultra20x'],
    countries: [
      {
        code: 'TR',
        name: '土耳其',
        excluded: false,
        web: {
          cells: {
            ultra5x: { status: 'ok', currency: 'TRY', amount: 4749.99 },
            ultra20x: { status: 'ok', currency: 'TRY', amount: 8999.99 },
          },
        },
        apple: {
          cells: {
            ultra5x: { status: 'ok', currency: 'TRY', amount: 1989.99 },
            ultra20x: { status: 'ok', currency: 'TRY', amount: 2949.99 },
          },
        },
      },
    ],
  };
  const overrides = {
    entries: {
      'google_ultra:TR:apple:ultra5x': {
        product: 'google_ultra',
        country: 'TR',
        channel: 'apple',
        plan: 'ultra5x',
        currency: 'TRY',
        amount: 4749.99,
        note: 'Manual iOS verify',
      },
    },
  };
  const processed = applyOverridesAndGuardToProduct('google_ultra', rawProd, overrides);
  const tr5x = processed.countries[0].apple.cells.ultra5x;
  assert.equal(tr5x.manualOverride, true);
  assert.equal(tr5x.amount, 4749.99);
  assert.equal(tr5x.suspectGrandfathered, false);
  const mins = minimumPricesForProduct(processed, buildColumns('google_ultra', 'all'), fx);
  const a5 = mins.find((m) => m.column.key === 'ultra5x-apple');
  assert.equal(a5.winner.country.code, 'TR');
});

test('minimumPricesForProduct includes tax-inclusive ChatGPT web cells in ranking', () => {
  const fx = { rates: { EUR: { rate: 1 }, USD: { rate: 1.1 } } };
  const rawProd = {
    applePlans: ['go', 'plus', 'pro100', 'pro200', 'pro500'],
    countries: [
      {
        code: 'BS',
        name: '巴哈马',
        excluded: false,
        web: { cells: { go: { status: 'ok', currency: 'USD', amount: 7.25, tax: 'inclusive', taxInclusive: true } } },
        apple: { cells: {} },
      },
      {
        code: 'US',
        name: '美国',
        excluded: false,
        web: { cells: { go: { status: 'ok', currency: 'USD', amount: 8.0, tax: 'exclusive', taxInclusive: false } } },
        apple: { cells: {} },
      },
    ],
  };
  const mins = minimumPricesForProduct(rawProd, buildColumns('chatgpt', 'web'), fx);
  const goMin = mins.find((m) => m.column.key === 'go-web');
  assert.equal(goMin.winner.country.code, 'BS');
  assert.equal(goMin.winner.cell.tax, 'inclusive');
});

test('formatTime formats ISO timestamps as YYYY-MM-DD HH:mm and handles empty/invalid input', () => {
  assert.match(formatTime('2026-11-13T05:03:13+00:00'), /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);
  assert.equal(formatTime(null), '—');
  assert.equal(formatTime(''), '—');
  assert.equal(formatTime('not-a-date'), 'not-a-date');
});

test('formatFxUpdatedDisplay prefers official referenceDate and falls back to updatedAt', () => {
  const withRef = formatFxUpdatedDisplay({
    referenceDate: '2026-11-20',
    updatedAt: '2026-11-21T15:15:53+00:00',
  });
  assert.equal(withRef.text, '2026-11-20');
  assert.equal(withRef.datetime, '2026-11-20');

  const withoutRef = formatFxUpdatedDisplay({
    updatedAt: '2026-11-21T15:15:53+00:00',
  });
  assert.match(withoutRef.text, /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/);

  const empty = formatFxUpdatedDisplay(null);
  assert.equal(empty.text, '—');
});

test('formatProductFreshness uses webUpdatedAt and appleUpdatedAt instead of snapshot updatedAt', () => {
  const prod = {
    webUpdatedAt: '2026-11-13T05:03:13+00:00',
    appleUpdatedAt: '2026-11-13T05:04:48+00:00',
    updatedAt: '2026-11-21T16:48:02+00:00',
  };
  const fx = { referenceDate: '2026-11-20', updatedAt: '2026-11-21T15:15:53+00:00' };
  const expectedWeb = formatTime(prod.webUpdatedAt);
  const expectedApple = formatTime(prod.appleUpdatedAt);
  const snapshotTime = formatTime(prod.updatedAt);

  const freshness = formatProductFreshness(prod, fx);
  assert.equal(freshness.web.text, expectedWeb);
  assert.equal(freshness.apple.text, expectedApple);
  assert.notEqual(freshness.web.text, snapshotTime);
  assert.notEqual(freshness.apple.text, snapshotTime);
  assert.equal(freshness.fx.text, '2026-11-20');
});
