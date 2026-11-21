import test from 'node:test';
import assert from 'node:assert/strict';

import worker, { triggerDailyPatrol } from '../workers/patrol-cron/src/index.js';

test('triggerDailyPatrol dispatches workflow_dispatch with Bearer token and main ref', async () => {
  const calls = [];
  const mockFetch = async (url, opts) => {
    calls.push({ url, opts });
    return new Response(null, { status: 204 });
  };
  const env = {
    GITHUB_TOKEN: 'github_pat_secret_123',
    GITHUB_REPO: '2940325143-eng/ai-subscription-prices',
    GITHUB_BRANCH: 'main',
    WORKFLOW_FILE: 'daily-apple-patrol.yml',
  };

  const result = await triggerDailyPatrol(env, { cron: '5 21 * * *', scheduledTime: 1795300000000 }, mockFetch);
  assert.equal(calls.length, 1);
  assert.equal(
    calls[0].url,
    'https://api.github.com/repos/2940325143-eng/ai-subscription-prices/actions/workflows/daily-apple-patrol.yml/dispatches',
  );
  assert.equal(calls[0].opts.method, 'POST');
  assert.equal(calls[0].opts.headers.authorization, 'Bearer github_pat_secret_123');
  assert.equal(calls[0].opts.headers['x-github-api-version'], '2022-11-28');
  assert.deepEqual(JSON.parse(calls[0].opts.body), { ref: 'main' });
  assert.deepEqual(result, {
    ok: true,
    status: 204,
    repo: '2940325143-eng/ai-subscription-prices',
    workflowFile: 'daily-apple-patrol.yml',
    branch: 'main',
  });
});

test('triggerDailyPatrol throws clearly when GITHUB_TOKEN is missing', async () => {
  await assert.rejects(
    () => triggerDailyPatrol({ GITHUB_REPO: '2940325143-eng/ai-subscription-prices' }, {}, async () => {
      throw new Error('fetch should not be called');
    }),
    /Missing GITHUB_TOKEN Worker secret/,
  );
});

test('triggerDailyPatrol rejects on non-2xx GitHub API error without leaking secret', async () => {
  const secret = 'github_pat_never_leak_me';
  const mockFetch = async () => new Response('{"message":"Resource not accessible by personal access token"}', { status: 403 });
  let err;
  try {
    await triggerDailyPatrol({ GITHUB_TOKEN: secret }, {}, mockFetch);
  } catch (e) {
    err = e;
  }
  assert.ok(err instanceof Error);
  assert.match(err.message, /HTTP 403/);
  assert.equal(err.message.includes(secret), false);
});

test('worker.fetch serves read-only health check and blocks mutations/unknown paths', async () => {
  const secret = 'github_pat_health_secret';
  const env = {
    GITHUB_TOKEN: secret,
    GITHUB_REPO: '2940325143-eng/ai-subscription-prices',
    GITHUB_BRANCH: 'main',
    WORKFLOW_FILE: 'daily-apple-patrol.yml',
  };

  const okRes = await worker.fetch(new Request('https://patrol.example.workers.dev/health'), env);
  assert.equal(okRes.status, 200);
  const bodyText = await okRes.text();
  assert.equal(bodyText.includes(secret), false);
  const data = JSON.parse(bodyText);
  assert.equal(data.ok, true);
  assert.equal(data.scheduleUtc, '5 21 * * *');
  assert.equal(data.scheduleCst, '05:05');
  assert.equal(data.tokenConfigured, true);

  const postRes = await worker.fetch(new Request('https://patrol.example.workers.dev/', { method: 'POST' }), env);
  assert.equal(postRes.status, 405);

  const notFoundRes = await worker.fetch(new Request('https://patrol.example.workers.dev/dispatch'), env);
  assert.equal(notFoundRes.status, 404);
});

test('worker.scheduled registers patrol dispatch on ctx.waitUntil without destructuring ctx', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response(null, { status: 204 });
  const waited = [];
  const ctx = {
    tag: 'cf-ctx',
    waitUntil(promise) {
      assert.equal(this.tag, 'cf-ctx');
      waited.push(promise);
    },
  };
  try {
    await worker.scheduled(
      { cron: '5 21 * * *', scheduledTime: Date.now() },
      { GITHUB_TOKEN: 'github_pat_ok' },
      ctx,
    );
    assert.equal(waited.length, 1);
    const out = await waited[0];
    assert.equal(out.ok, true);
    assert.equal(out.status, 204);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
