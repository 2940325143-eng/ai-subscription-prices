/**
 * Dedicated $0 Cloudflare Worker Cron Trigger for Daily Apple Patrol (05:05 CST / 21:05 UTC).
 * Dispatches GitHub Actions `daily-apple-patrol.yml` via workflow_dispatch using an encrypted
 * `GITHUB_TOKEN` Worker secret so schedules never drop on GitHub's native cron queue.
 */

const REPO_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const WORKFLOW_RE = /^[A-Za-z0-9_.-]+\.ya?ml$/;
const SCHEDULE_UTC = '5 21 * * *';
const SCHEDULE_CST = '05:05';

function normalizeRepo(raw) {
  const repo = String(raw || '')
    .trim()
    .replace(/^https:\/\/github\.com\//i, '')
    .replace(/\.git$/i, '')
    .replace(/^\/+|\/+$/g, '');
  if (!REPO_RE.test(repo)) {
    throw new Error('Invalid GITHUB_REPO configuration');
  }
  return repo;
}

function normalizeWorkflowFile(raw) {
  const name = String(raw || 'daily-apple-patrol.yml').trim();
  if (!WORKFLOW_RE.test(name)) {
    throw new Error('Invalid WORKFLOW_FILE configuration');
  }
  return name;
}

function normalizeBranch(raw) {
  const branch = String(raw || 'main').trim();
  if (!branch || branch.length > 128 || /[\r\n\s]/.test(branch)) {
    throw new Error('Invalid GITHUB_BRANCH configuration');
  }
  return branch;
}

async function readErrorSnippet(response, maxBytes = 512) {
  const body = response?.body;
  if (!body || typeof body.getReader !== 'function') {
    return '';
  }
  const reader = body.getReader();
  const chunks = [];
  let total = 0;
  try {
    while (total < maxBytes) {
      const { value, done } = await reader.read();
      if (done || !value) break;
      const take = Math.min(value.byteLength, maxBytes - total);
      chunks.push(value.subarray(0, take));
      total += take;
      if (total >= maxBytes) break;
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
  const merged = new Uint8Array(total);
  let offset = 0;
  for (const part of chunks) {
    merged.set(part, offset);
    offset += part.byteLength;
  }
  return new TextDecoder().decode(merged);
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
      'x-content-type-options': 'nosniff',
    },
  });
}

export async function triggerDailyPatrol(env, meta = {}, fetchImpl = fetch) {
  const token = String(env?.GITHUB_TOKEN || '').trim();
  if (!token) {
    const err = new Error('Missing GITHUB_TOKEN Worker secret');
    console.error(JSON.stringify({ event: 'patrol_dispatch_failed', reason: 'missing_token' }));
    throw err;
  }

  const repo = normalizeRepo(env?.GITHUB_REPO || '2940325143-eng/ai-subscription-prices');
  const branch = normalizeBranch(env?.GITHUB_BRANCH || 'main');
  const workflowFile = normalizeWorkflowFile(env?.WORKFLOW_FILE || 'daily-apple-patrol.yml');

  const url = `https://api.github.com/repos/${repo}/actions/workflows/${workflowFile}/dispatches`;
  const resp = await fetchImpl(url, {
    method: 'POST',
    headers: {
      accept: 'application/vnd.github+json',
      authorization: `Bearer ${token}`,
      'content-type': 'application/json',
      'user-agent': 'ai-prices-patrol-cron/1.0',
      'x-github-api-version': '2022-11-28',
    },
    body: JSON.stringify({ ref: branch }),
  });

  if (resp.ok) {
    await resp.body?.cancel?.().catch(() => {});
    console.log(
      JSON.stringify({
        event: 'patrol_dispatched',
        status: resp.status,
        repo,
        workflowFile,
        branch,
        cron: meta.cron || null,
        scheduledTime: meta.scheduledTime || null,
      }),
    );
    return { ok: true, status: resp.status, repo, workflowFile, branch };
  }

  const snippet = await readErrorSnippet(resp, 512);
  console.error(
    JSON.stringify({
      event: 'patrol_dispatch_failed',
      status: resp.status,
      repo,
      workflowFile,
      branch,
      snippet,
    }),
  );
  throw new Error(`GitHub workflow_dispatch failed with HTTP ${resp.status}`);
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(
      triggerDailyPatrol(env, {
        cron: controller?.cron || SCHEDULE_UTC,
        scheduledTime: controller?.scheduledTime || Date.now(),
      }),
    );
  },

  async fetch(request, env) {
    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return jsonResponse({ ok: false, error: 'Method Not Allowed' }, 405);
    }
    const url = new URL(request.url);
    if (url.pathname !== '/' && url.pathname !== '/health') {
      return jsonResponse({ ok: false, error: 'Not Found' }, 404);
    }
    return jsonResponse({
      ok: true,
      service: 'ai-prices-patrol-cron',
      scheduleUtc: SCHEDULE_UTC,
      scheduleCst: SCHEDULE_CST,
      repo: String(env?.GITHUB_REPO || '2940325143-eng/ai-subscription-prices'),
      branch: String(env?.GITHUB_BRANCH || 'main'),
      workflowFile: String(env?.WORKFLOW_FILE || 'daily-apple-patrol.yml'),
      tokenConfigured: Boolean(String(env?.GITHUB_TOKEN || '').trim()),
    });
  },
};
