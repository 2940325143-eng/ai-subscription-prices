/**
 * Portable Web-standard API core shared by Cloudflare Pages Functions,
 * Vercel Serverless Functions, and Node unit tests. Zero external npm deps.
 */

const FX_URL =
  'https://tassidicambio.bancaditalia.it/terzevalute-wf-web/rest/v1.0/latestRates?lang=en';
const COOKIE_NAME = 'ai_prices_admin';
const SESSION_TTL_SECONDS = 12 * 3600;
const PROXY_SECRET_NAMES = Object.freeze([
  'IPROYAL_PROXY_USER',
  'IPROYAL_PROXY_PASS',
  'IPROYAL_PROXY_ENDPOINT',
]);

const VALID_PLANS = Object.freeze({
  chatgpt: Object.freeze({
    web: Object.freeze(['go', 'plus', 'businessStandard', 'pro100', 'businessPremium', 'pro200', 'pro500']),
    apple: Object.freeze(['go', 'plus', 'pro100', 'pro200', 'pro500']),
  }),
  google_ultra: Object.freeze({
    web: Object.freeze(['ultra5x', 'ultra20x']),
    apple: Object.freeze(['ultra5x', 'ultra20x']),
  }),
});

let fxCache = null;

function textEncoder() {
  return new TextEncoder();
}

function toBase64Url(bytes) {
  let binary = '';
  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function fromBase64Url(str) {
  const padded = str.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((str.length + 3) % 4);
  const binary = atob(padded);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    out[i] = binary.charCodeAt(i);
  }
  return out;
}

function constantTimeEqualBytes(a, b) {
  if (!(a instanceof Uint8Array) || !(b instanceof Uint8Array) || a.length !== b.length) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a[i] ^ b[i];
  }
  return diff === 0;
}

async function sha256Bytes(value) {
  const digest = await crypto.subtle.digest('SHA-256', textEncoder().encode(String(value)));
  return new Uint8Array(digest);
}

async function verifyPasswordConstantTime(candidate, expected) {
  if (typeof candidate !== 'string' || typeof expected !== 'string' || !expected) {
    return false;
  }
  const [a, b] = await Promise.all([sha256Bytes(candidate), sha256Bytes(expected)]);
  return constantTimeEqualBytes(a, b);
}

async function signingKey(env) {
  const secretSource = env.ADMIN_COOKIE_SECRET || env.ADMIN_PASSWORD || '';
  if (!secretSource) {
    throw new Error('ADMIN_PASSWORD is not configured');
  }
  const raw = await sha256Bytes(`ai-subscription-prices-cookie:${secretSource}`);
  return crypto.subtle.importKey('raw', raw, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign', 'verify']);
}

export async function createAdminToken(env, nowSec = Math.floor(Date.now() / 1000)) {
  const key = await signingKey(env);
  const payload = { v: 1, role: 'admin', iat: nowSec, exp: nowSec + SESSION_TTL_SECONDS };
  const body = toBase64Url(textEncoder().encode(JSON.stringify(payload)));
  const sig = await crypto.subtle.sign('HMAC', key, textEncoder().encode(body));
  return `${body}.${toBase64Url(new Uint8Array(sig))}`;
}

export async function verifyAdminToken(token, env, nowSec = Math.floor(Date.now() / 1000)) {
  if (typeof token !== 'string' || !token.includes('.') || !(env.ADMIN_COOKIE_SECRET || env.ADMIN_PASSWORD)) {
    return false;
  }
  const [body, sigB64] = token.split('.', 2);
  if (!body || !sigB64) return false;
  try {
    const key = await signingKey(env);
    const sigBytes = fromBase64Url(sigB64);
    const valid = await crypto.subtle.verify('HMAC', key, sigBytes, textEncoder().encode(body));
    if (!valid) return false;
    const payload = JSON.parse(new TextDecoder().decode(fromBase64Url(body)));
    return payload?.v === 1 && payload?.role === 'admin' && Number.isFinite(payload?.exp) && payload.exp > nowSec;
  } catch {
    return false;
  }
}

function parseCookies(headerValue) {
  const map = {};
  for (const part of String(headerValue || '').split(';')) {
    const idx = part.indexOf('=');
    if (idx <= 0) continue;
    const name = part.slice(0, idx).trim();
    const val = part.slice(idx + 1).trim();
    if (name) map[name] = val;
  }
  return map;
}

function jsonResponse(payload, status = 200, headers = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
      'x-content-type-options': 'nosniff',
      ...headers,
    },
  });
}

function isHttpsRequest(request) {
  if (request.url.startsWith('https://')) return true;
  const proto = request.headers.get('x-forwarded-proto') || '';
  return proto.split(',')[0].trim().toLowerCase() === 'https';
}

function buildSessionCookie(request, value, maxAgeSeconds) {
  const parts = [
    `${COOKIE_NAME}=${value}`,
    'Path=/',
    'HttpOnly',
    'SameSite=Strict',
    `Max-Age=${maxAgeSeconds}`,
  ];
  if (isHttpsRequest(request)) parts.push('Secure');
  return parts.join('; ');
}

function sameOriginAllowed(request) {
  const origin = request.headers.get('origin');
  if (!origin) return true;
  try {
    return new URL(origin).origin === new URL(request.url).origin;
  } catch {
    return false;
  }
}

async function readJsonBody(request) {
  try {
    const text = await request.text();
    if (!text) return {};
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export function normalizeBankOfItalyRates(payload) {
  const entries = payload?.latestRates;
  if (!Array.isArray(entries) || entries.length < 100) {
    throw new Error('Incomplete official exchange rates');
  }
  const rates = { EUR: { rate: 1, date: null } };
  for (const item of entries) {
    const code = item?.isoCode;
    const date = item?.referenceDate;
    const val = Number(item?.eurRate);
    if (/^[A-Z]{3}$/.test(String(code)) && date && Number.isFinite(val) && val > 0) {
      if (!rates[code] || !rates[code].date || String(date) > String(rates[code].date)) {
        rates[code] = { rate: val, date: String(date) };
      }
    }
  }
  const dates = Object.values(rates)
    .map((r) => r.date)
    .filter(Boolean)
    .sort();
  if (Object.keys(rates).length < 100 || !dates.length) {
    throw new Error('Incomplete official exchange rates');
  }
  const latestDate = dates[dates.length - 1];
  rates.EUR.date = latestDate;
  return {
    rates,
    sourceUrl: FX_URL,
    referenceDate: latestDate,
    updatedAt: new Date().toISOString(),
  };
}

function parseRepoSlug(raw) {
  const repo = String(raw || '').trim().replace(/^https:\/\/github\.com\//i, '').replace(/\.git$/i, '').replace(/^\/+|\/+$/g, '');
  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)) return null;
  return repo;
}

async function githubFetch(env, path, options = {}, fetchImpl = fetch) {
  const token = String(env.GITHUB_TOKEN || '').trim();
  const repo = parseRepoSlug(env.GITHUB_REPO);
  if (!token || !repo) {
    return { configured: false, ok: false, status: 503, data: null };
  }
  const url = `https://api.github.com/repos/${repo}${path}`;
  const resp = await fetchImpl(url, {
    ...options,
    headers: {
      accept: 'application/vnd.github+json',
      authorization: `Bearer ${token}`,
      'user-agent': 'ai-subscription-prices-cloud/1.0',
      'x-github-api-version': '2022-11-28',
      ...(options.body ? { 'content-type': 'application/json' } : {}),
      ...(options.headers || {}),
    },
  });
  let data = null;
  const text = await resp.text();
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { message: text.slice(0, 300) };
    }
  }
  return { configured: true, ok: resp.ok, status: resp.status, data, repo };
}

function validateOverrideInput(body, requireAmount = true) {
  if (!body || typeof body !== 'object') return { error: '请求体必须是 JSON 对象' };
  const product = String(body.product || '').trim();
  const country = String(body.country || '').trim().toUpperCase();
  const channel = String(body.channel || '').trim();
  const plan = String(body.plan || '').trim();
  if (!VALID_PLANS[product]) return { error: '不支持的产品标识' };
  if (!/^[A-Z]{2}$/.test(country)) return { error: '国家/地区代码必须是 2 位大写字母' };
  if (!VALID_PLANS[product][channel]) return { error: '渠道必须是 web 或 apple' };
  if (!VALID_PLANS[product][channel].includes(plan)) return { error: `渠道 ${channel} 不支持档位 ${plan}` };

  if (!requireAmount) {
    return { value: { product, country, channel, plan } };
  }
  const currency = String(body.currency || '').trim().toUpperCase();
  const amount = Number(body.amount);
  const note = String(body.note || '').trim().slice(0, 200);
  if (!/^[A-Z]{3}$/.test(currency)) return { error: '币种必须是 3 位 ISO 大写字母' };
  if (!Number.isFinite(amount) || amount <= 0) return { error: '月付金额必须是大于 0 的数字' };
  return { value: { product, country, channel, plan, currency, amount, note } };
}

function encodeUtf8Base64(text) {
  const bytes = textEncoder().encode(text);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

function decodeUtf8Base64(b64) {
  const cleaned = String(b64 || '').replace(/\s+/g, '');
  const binary = atob(cleaned);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return new TextDecoder().decode(bytes);
}

async function requireAdmin(request, env) {
  if (!sameOriginAllowed(request) && request.method !== 'GET' && request.method !== 'HEAD') {
    return { ok: false, response: jsonResponse({ ok: false, error: '禁止跨站提交写操作' }, 403) };
  }
  const cookies = parseCookies(request.headers.get('cookie'));
  const token = cookies[COOKIE_NAME];
  const valid = await verifyAdminToken(token, env);
  if (!valid) {
    return { ok: false, response: jsonResponse({ ok: false, error: '未登录管理员或会话已过期' }, 401) };
  }
  return { ok: true };
}

export async function handleApiRequest(request, env = {}, deps = {}) {
  const fetchImpl = deps.fetch || fetch;
  const nowMs = deps.nowMs || (() => Date.now());
  const url = new URL(request.url);
  const path = url.pathname.replace(/\/+$/, '') || '/';
  const method = request.method.toUpperCase();

  if (method === 'GET' && path === '/api/health') {
    const repo = parseRepoSlug(env.GITHUB_REPO);
    return jsonResponse({
      ok: true,
      service: 'ai-subscription-prices',
      adminConfigured: Boolean(env.ADMIN_PASSWORD),
      githubConfigured: Boolean(env.GITHUB_TOKEN && repo),
      now: new Date(nowMs()).toISOString(),
    });
  }

  if (method === 'GET' && path === '/api/fx') {
    if (fxCache && nowMs() - fxCache.fetchedAt < 1800 * 1000) {
      return jsonResponse(fxCache.data, 200, { 'cache-control': 'public, max-age=1800' });
    }
    try {
      const resp = await fetchImpl(FX_URL, { headers: { accept: 'application/json' } });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = normalizeBankOfItalyRates(await resp.json());
      fxCache = { fetchedAt: nowMs(), data };
      return jsonResponse(data, 200, { 'cache-control': 'public, max-age=1800' });
    } catch (err) {
      if (fxCache) {
        return jsonResponse({ ...fxCache.data, stale: true, error: String(err.message || err) }, 200);
      }
      return jsonResponse({ ok: false, error: '央行汇率获取失败，页面继续使用已随表发布的汇率' }, 502);
    }
  }

  if (method === 'GET' && path === '/api/admin/session') {
    const cookies = parseCookies(request.headers.get('cookie'));
    const authenticated = await verifyAdminToken(cookies[COOKIE_NAME], env);
    if (!authenticated) {
      return jsonResponse({ authenticated: false, adminConfigured: Boolean(env.ADMIN_PASSWORD) });
    }
    const repo = parseRepoSlug(env.GITHUB_REPO);
    return jsonResponse({
      authenticated: true,
      adminConfigured: true,
      githubConfigured: Boolean(env.GITHUB_TOKEN && repo),
      repo,
      branch: String(env.GITHUB_BRANCH || 'main'),
    });
  }

  if (method === 'POST' && path === '/api/admin/login') {
    if (!sameOriginAllowed(request)) {
      return jsonResponse({ ok: false, error: '禁止跨站登录请求' }, 403);
    }
    if (!env.ADMIN_PASSWORD) {
      return jsonResponse({ ok: false, error: '服务端未配置 ADMIN_PASSWORD 环境变量' }, 503);
    }
    const body = await readJsonBody(request);
    if (!body || typeof body.password !== 'string') {
      return jsonResponse({ ok: false, error: '请提供管理员口令' }, 400);
    }
    const ok = await verifyPasswordConstantTime(body.password, String(env.ADMIN_PASSWORD));
    if (!ok) {
      return jsonResponse({ ok: false, error: '管理员口令不正确' }, 401);
    }
    const token = await createAdminToken(env, Math.floor(nowMs() / 1000));
    return jsonResponse(
      { ok: true, authenticated: true },
      200,
      { 'set-cookie': buildSessionCookie(request, token, SESSION_TTL_SECONDS) },
    );
  }

  if (method === 'POST' && path === '/api/admin/logout') {
    if (!sameOriginAllowed(request)) {
      return jsonResponse({ ok: false, error: '禁止跨站退出请求' }, 403);
    }
    return jsonResponse(
      { ok: true, authenticated: false },
      200,
      { 'set-cookie': buildSessionCookie(request, '', 0) },
    );
  }

  if (method === 'GET' && path === '/api/admin/status') {
    const auth = await requireAdmin(request, env);
    if (!auth.ok) return auth.response;
    const repo = parseRepoSlug(env.GITHUB_REPO);
    if (!env.GITHUB_TOKEN || !repo) {
      return jsonResponse({
        ok: true,
        githubConfigured: false,
        repo: null,
        proxySecrets: Object.fromEntries(PROXY_SECRET_NAMES.map((n) => [n, { configured: false }])),
        message: '尚未在云端环境变量配置 GITHUB_TOKEN 与 GITHUB_REPO',
      });
    }
    const gh = await githubFetch(env, '/actions/secrets?per_page=100', {}, fetchImpl);
    if (!gh.ok) {
      return jsonResponse(
        {
          ok: false,
          githubConfigured: true,
          repo,
          error: `读取 GitHub Actions Secrets 元信息失败（HTTP ${gh.status}）`,
        },
        gh.status >= 500 ? 502 : gh.status,
      );
    }
    const listed = new Map((gh.data?.secrets || []).map((s) => [s.name, s]));
    const proxySecrets = {};
    for (const name of PROXY_SECRET_NAMES) {
      const hit = listed.get(name);
      proxySecrets[name] = hit ? { configured: true, updatedAt: hit.updated_at || null } : { configured: false };
    }
    return jsonResponse({
      ok: true,
      githubConfigured: true,
      repo,
      branch: String(env.GITHUB_BRANCH || 'main'),
      secretsSettingsUrl: `https://github.com/${repo}/settings/secrets/actions`,
      actionsUrl: `https://github.com/${repo}/actions`,
      proxySecrets,
    });
  }

  if (method === 'POST' && path === '/api/admin/refresh') {
    const auth = await requireAdmin(request, env);
    if (!auth.ok) return auth.response;
    const body = await readJsonBody(request);
    if (!body) return jsonResponse({ ok: false, error: '无效的 JSON 请求体' }, 400);

    const product = String(body.product || 'all');
    const scope = String(body.scope || 'apple');
    const mode = String(body.mode || 'full');
    const rawCountries = String(body.countries || '').trim().toUpperCase();

    if (!['all', 'chatgpt', 'google_ultra'].includes(product)) {
      return jsonResponse({ ok: false, error: 'product 参数必须是 all、chatgpt 或 google_ultra' }, 400);
    }
    if (!['apple', 'web', 'all'].includes(scope)) {
      return jsonResponse({ ok: false, error: 'scope 参数必须是 apple、web 或 all' }, 400);
    }
    if (!['full', 'retry'].includes(mode)) {
      return jsonResponse({ ok: false, error: 'mode 参数必须是 full 或 retry' }, 400);
    }
    const countries = rawCountries
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
      .join(',');
    if (countries && !/^[A-Z]{2}(?:,[A-Z]{2})*$/.test(countries)) {
      return jsonResponse({ ok: false, error: 'countries 必须是逗号分隔的 2 位国家代码，如 TR,US' }, 400);
    }

    const repo = parseRepoSlug(env.GITHUB_REPO);
    if (!env.GITHUB_TOKEN || !repo) {
      return jsonResponse({ ok: false, error: '未配置 GITHUB_TOKEN 或 GITHUB_REPO，无法派发云端采集任务' }, 503);
    }
    const branch = String(env.GITHUB_BRANCH || 'main');
    const gh = await githubFetch(
      env,
      '/actions/workflows/refresh-prices.yml/dispatches',
      {
        method: 'POST',
        body: JSON.stringify({
          ref: branch,
          inputs: { product, scope, mode, countries },
        }),
      },
      fetchImpl,
    );
    if (!gh.ok) {
      return jsonResponse(
        { ok: false, error: gh.data?.message || `GitHub Actions 派发失败（HTTP ${gh.status}）` },
        gh.status >= 500 ? 502 : gh.status,
      );
    }
    return jsonResponse(
      {
        ok: true,
        dispatched: true,
        workflow: 'refresh-prices.yml',
        inputs: { product, scope, mode, countries },
        actionsUrl: `https://github.com/${repo}/actions/workflows/refresh-prices.yml`,
      },
      202,
    );
  }

  if (method === 'GET' && path === '/api/admin/jobs') {
    const auth = await requireAdmin(request, env);
    if (!auth.ok) return auth.response;
    const repo = parseRepoSlug(env.GITHUB_REPO);
    if (!env.GITHUB_TOKEN || !repo) {
      return jsonResponse({ ok: true, githubConfigured: false, runs: [] });
    }
    const gh = await githubFetch(env, '/actions/runs?per_page=15', {}, fetchImpl);
    if (!gh.ok) {
      return jsonResponse(
        { ok: false, error: `读取 GitHub Actions 运行记录失败（HTTP ${gh.status}）` },
        gh.status >= 500 ? 502 : gh.status,
      );
    }
    const runs = (gh.data?.workflow_runs || [])
      .filter((r) => !r.path || r.path.endsWith('refresh-prices.yml') || r.path.endsWith('daily-apple-patrol.yml'))
      .slice(0, 12)
      .map((r) => ({
        id: r.id,
        name: r.name,
        path: r.path,
        status: r.status,
        conclusion: r.conclusion,
        event: r.event,
        createdAt: r.created_at,
        updatedAt: r.updated_at,
        htmlUrl: r.html_url,
      }));
    return jsonResponse({ ok: true, githubConfigured: true, runs });
  }

  if ((method === 'POST' || method === 'DELETE') && path === '/api/admin/overrides') {
    const auth = await requireAdmin(request, env);
    if (!auth.ok) return auth.response;
    const body = await readJsonBody(request);
    if (!body) return jsonResponse({ ok: false, error: '无效的 JSON 请求体' }, 400);

    const parsed = validateOverrideInput(body, method === 'POST');
    if (parsed.error) return jsonResponse({ ok: false, error: parsed.error }, 400);

    const repo = parseRepoSlug(env.GITHUB_REPO);
    if (!env.GITHUB_TOKEN || !repo) {
      return jsonResponse({ ok: false, error: '未配置 GITHUB_TOKEN 或 GITHUB_REPO，无法写入 overrides.json' }, 503);
    }
    const branch = String(env.GITHUB_BRANCH || 'main');
    const { product, country, channel, plan } = parsed.value;
    const key = `${product}:${country}:${channel}:${plan}`;

    for (let attempt = 0; attempt < 2; attempt += 1) {
      const getRes = await githubFetch(
        env,
        `/contents/public/data/overrides.json?ref=${encodeURIComponent(branch)}`,
        {},
        fetchImpl,
      );
      let doc = { schemaVersion: 1, updatedAt: null, entries: {} };
      let sha = null;
      if (getRes.ok && getRes.data?.content) {
        try {
          const decoded = JSON.parse(decodeUtf8Base64(getRes.data.content));
          if (decoded && typeof decoded.entries === 'object') doc = decoded;
          sha = getRes.data.sha;
        } catch {
          return jsonResponse({ ok: false, error: '仓库中的 overrides.json 格式异常' }, 500);
        }
      } else if (getRes.status !== 404) {
        return jsonResponse({ ok: false, error: `读取 overrides.json 失败（HTTP ${getRes.status}）` }, 502);
      }

      const timestamp = new Date(nowMs()).toISOString();
      if (method === 'POST') {
        doc.entries[key] = { ...parsed.value, updatedAt: timestamp };
      } else {
        delete doc.entries[key];
      }
      doc.updatedAt = timestamp;

      const putRes = await githubFetch(
        env,
        '/contents/public/data/overrides.json',
        {
          method: 'PUT',
          body: JSON.stringify({
            message: method === 'POST' ? `chore(override): calibrate ${key}` : `chore(override): remove ${key}`,
            content: encodeUtf8Base64(JSON.stringify(doc, null, 2) + '\n'),
            branch,
            ...(sha ? { sha } : {}),
          }),
        },
        fetchImpl,
      );
      if (putRes.ok) {
        return jsonResponse({ ok: true, key, overrides: doc });
      }
      if (putRes.status !== 409) {
        return jsonResponse(
          { ok: false, error: putRes.data?.message || `保存 overrides.json 失败（HTTP ${putRes.status}）` },
          putRes.status >= 500 ? 502 : putRes.status,
        );
      }
    }
    return jsonResponse({ ok: false, error: '写入冲突，请重试' }, 409);
  }

  return jsonResponse({ ok: false, error: 'Not found' }, 404);
}
