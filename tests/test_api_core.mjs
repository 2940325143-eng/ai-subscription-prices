import test from 'node:test';
import assert from 'node:assert/strict';

import {
  createAdminToken,
  handleApiRequest,
  normalizeBankOfItalyRates,
  verifyAdminToken,
} from '../shared/api-core.js';

const ENV = {
  ADMIN_PASSWORD: 'super-secret-pass',
  GITHUB_TOKEN: 'ghp_testtoken123',
  GITHUB_REPO: 'owner/ai-subscription-prices',
  GITHUB_BRANCH: 'main',
};

function makeCookieHeader(res) {
  const setCookie = res.headers.get('set-cookie') || '';
  return setCookie.split(';')[0];
}

test('health endpoint reports status without secrets', async () => {
  const res = await handleApiRequest(new Request('https://example.com/api/health'), ENV);
  assert.equal(res.status, 200);
  const body = await res.json();
  assert.equal(body.ok, true);
  assert.equal(body.adminConfigured, true);
  assert.equal(body.githubConfigured, true);
  assert.equal(JSON.stringify(body).includes('super-secret'), false);
  assert.equal(JSON.stringify(body).includes('ghp_'), false);
});

test('admin login rejects wrong password and sets HttpOnly Strict cookie on success', async () => {
  const bad = await handleApiRequest(
    new Request('https://example.com/api/admin/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ password: 'wrong' }),
    }),
    ENV,
  );
  assert.equal(bad.status, 401);

  const ok = await handleApiRequest(
    new Request('https://example.com/api/admin/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ password: ENV.ADMIN_PASSWORD }),
    }),
    ENV,
  );
  assert.equal(ok.status, 200);
  const sc = ok.headers.get('set-cookie') || '';
  assert.match(sc, /ai_prices_admin=/);
  assert.match(sc, /HttpOnly/i);
  assert.match(sc, /SameSite=Strict/i);
  assert.match(sc, /Secure/i);

  const cookie = makeCookieHeader(ok);
  const sess = await handleApiRequest(
    new Request('https://example.com/api/admin/session', { headers: { cookie } }),
    ENV,
  );
  const sessJson = await sess.json();
  assert.equal(sessJson.authenticated, true);
  assert.equal(sessJson.repo, 'owner/ai-subscription-prices');
});

test('unauthenticated requests to protected admin routes return 401', async () => {
  for (const [method, path] of [
    ['GET', '/api/admin/status'],
    ['GET', '/api/admin/jobs'],
    ['POST', '/api/admin/refresh'],
    ['POST', '/api/admin/overrides'],
    ['DELETE', '/api/admin/overrides'],
  ]) {
    const res = await handleApiRequest(new Request(`https://example.com${path}`, { method }), ENV);
    assert.equal(res.status, 401, `${method} ${path} should require auth`);
  }
});

test('token verification rejects tampered or expired tokens', async () => {
  const tok = await createAdminToken(ENV, 1000);
  assert.equal(await verifyAdminToken(tok, ENV, 1010), true);
  assert.equal(await verifyAdminToken(tok, ENV, 1000 + 50000), false);
  const [b, s] = tok.split('.');
  const tampered = `${b}.${s.slice(0, -1)}${s.endsWith('A') ? 'B' : 'A'}`;
  assert.equal(await verifyAdminToken(tampered, ENV, 1010), false);
});

test('authenticated refresh dispatches GitHub Actions workflow and validates inputs', async () => {
  const loginRes = await handleApiRequest(
    new Request('https://example.com/api/admin/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ password: ENV.ADMIN_PASSWORD }),
    }),
    ENV,
  );
  const cookie = makeCookieHeader(loginRes);

  const badCountries = await handleApiRequest(
    new Request('https://example.com/api/admin/refresh', {
      method: 'POST',
      headers: { cookie, 'content-type': 'application/json' },
      body: JSON.stringify({ product: 'all', scope: 'web', mode: 'full', countries: 'invalid-123' }),
    }),
    ENV,
  );
  assert.equal(badCountries.status, 400);

  const dispatchedCalls = [];
  const mockFetch = async (url, init) => {
    dispatchedCalls.push({ url, init });
    return new Response(null, { status: 204 });
  };
  const okRes = await handleApiRequest(
    new Request('https://example.com/api/admin/refresh', {
      method: 'POST',
      headers: { cookie, 'content-type': 'application/json' },
      body: JSON.stringify({ product: 'google_ultra', scope: 'web', mode: 'retry', countries: 'TR, US' }),
    }),
    ENV,
    { fetch: mockFetch },
  );
  assert.equal(okRes.status, 202);
  assert.equal(dispatchedCalls.length, 1);
  assert.match(dispatchedCalls[0].url, /\/actions\/workflows\/refresh-prices\.yml\/dispatches$/);
  const sentBody = JSON.parse(dispatchedCalls[0].init.body);
  assert.deepEqual(sentBody.inputs, { product: 'google_ultra', scope: 'web', mode: 'retry', countries: 'TR,US' });
});

test('admin status returns GitHub secret presence metadata only and never leaks values', async () => {
  const loginRes = await handleApiRequest(
    new Request('https://example.com/api/admin/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ password: ENV.ADMIN_PASSWORD }),
    }),
    ENV,
  );
  const cookie = makeCookieHeader(loginRes);
  const mockFetch = async () =>
    new Response(
      JSON.stringify({
        total_count: 2,
        secrets: [
          { name: 'IPROYAL_PROXY_USER', updated_at: '2026-11-20T12:00:00Z' },
          { name: 'IPROYAL_PROXY_ENDPOINT', updated_at: '2026-11-20T12:00:00Z' },
        ],
      }),
      { status: 200 },
    );
  const res = await handleApiRequest(
    new Request('https://example.com/api/admin/status', { headers: { cookie } }),
    ENV,
    { fetch: mockFetch },
  );
  assert.equal(res.status, 200);
  const data = await res.json();
  assert.equal(data.proxySecrets.IPROYAL_PROXY_USER.configured, true);
  assert.equal(data.proxySecrets.IPROYAL_PROXY_PASS.configured, false);
  assert.equal(data.proxySecrets.IPROYAL_PROXY_ENDPOINT.configured, true);
});

test('overrides POST & DELETE update overrides.json via GitHub Contents API with 409 SHA retry', async () => {
  const loginRes = await handleApiRequest(
    new Request('https://example.com/api/admin/login', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ password: ENV.ADMIN_PASSWORD }),
    }),
    ENV,
  );
  const cookie = makeCookieHeader(loginRes);

  let currentDoc = { schemaVersion: 1, updatedAt: null, entries: {} };
  let shaCounter = 1;
  let putAttempt = 0;

  const mockFetch = async (url, init = {}) => {
    if (!init.method || init.method === 'GET') {
      return new Response(
        JSON.stringify({
          sha: `sha-${shaCounter}`,
          content: Buffer.from(JSON.stringify(currentDoc), 'utf-8').toString('base64'),
        }),
        { status: 200 },
      );
    }
    if (init.method === 'PUT') {
      putAttempt += 1;
      if (putAttempt === 1) {
        shaCounter += 1;
        return new Response(JSON.stringify({ message: 'SHA conflict' }), { status: 409 });
      }
      const parsed = JSON.parse(init.body);
      currentDoc = JSON.parse(Buffer.from(parsed.content, 'base64').toString('utf-8'));
      shaCounter += 1;
      return new Response(JSON.stringify({ content: { sha: `sha-${shaCounter}` } }), { status: 200 });
    }
    return new Response('', { status: 404 });
  };

  const saveRes = await handleApiRequest(
    new Request('https://example.com/api/admin/overrides', {
      method: 'POST',
      headers: { cookie, 'content-type': 'application/json' },
      body: JSON.stringify({
        product: 'google_ultra',
        country: 'tr',
        channel: 'apple',
        plan: 'ultra5x',
        currency: 'try',
        amount: 4749.99,
        note: 'Verified iOS new price',
      }),
    }),
    ENV,
    { fetch: mockFetch },
  );
  assert.equal(saveRes.status, 200);
  const saved = await saveRes.json();
  assert.equal(saved.key, 'google_ultra:TR:apple:ultra5x');
  assert.equal(saved.overrides.entries['google_ultra:TR:apple:ultra5x'].amount, 4749.99);
  assert.equal(putAttempt, 2);

  const delRes = await handleApiRequest(
    new Request('https://example.com/api/admin/overrides', {
      method: 'DELETE',
      headers: { cookie, 'content-type': 'application/json' },
      body: JSON.stringify({ product: 'google_ultra', country: 'TR', channel: 'apple', plan: 'ultra5x' }),
    }),
    ENV,
    { fetch: mockFetch },
  );
  assert.equal(delRes.status, 200);
  const deleted = await delRes.json();
  assert.equal(deleted.overrides.entries['google_ultra:TR:apple:ultra5x'], undefined);
});

test('normalizeBankOfItalyRates builds valid EUR rate map', () => {
  const latestRates = [
    { isoCode: 'USD', referenceDate: '2026-11-20', eurRate: '1.10' },
    { isoCode: 'TRY', referenceDate: '2026-11-20', eurRate: '55.5' },
  ];
  for (let i = 0; i < 100; i += 1) {
    const a = String.fromCharCode(65 + Math.floor(i / 26));
    const b = String.fromCharCode(65 + (i % 26));
    latestRates.push({ isoCode: `X${a}${b}`, referenceDate: '2026-11-20', eurRate: '2.0' });
  }
  const out = normalizeBankOfItalyRates({ latestRates });
  assert.equal(out.referenceDate, '2026-11-20');
  assert.equal(out.rates.EUR.rate, 1);
  assert.equal(out.rates.USD.rate, 1.1);
});
