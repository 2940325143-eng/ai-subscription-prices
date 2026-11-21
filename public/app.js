/* Unified ChatGPT (7-tier) & Google AI Ultra (2-tier) cloud price explorer */

export const PRODUCT_CONFIG = Object.freeze({
  chatgpt: Object.freeze({
    key: 'chatgpt',
    title: 'ChatGPT',
    description: '个人 5 档（GO / Plus / Pro 100 / Pro 200 / Pro 500）与团队 2 档（Business Standard / Premium）。官网优先采用不含税直出月付，官方仅提供含税价时标注「含税」直接入表并参与排名；App Store 自动隔离年付并按新开用户取最高月价。',
    note: 'ChatGPT 官网优先取 tax="exclusive" 不含税直出月付字段；若某国某档官方仅返回 tax="inclusive"，则贴「含税」标签入表参与排名（绝不推算扣税）。同币种 App Store 低于官网 65% 将标疑移出看板。',
    plans: Object.freeze([
      { key: 'go', label: 'ChatGPT GO', badge: 'GO', badgeClass: '', channels: ['web', 'apple'], group: 'personal' },
      { key: 'plus', label: 'ChatGPT Plus', badge: 'Plus', badgeClass: '', channels: ['web', 'apple'], group: 'personal' },
      { key: 'businessStandard', label: 'Business Std', badge: '团队标准', badgeClass: 'tier-team', channels: ['web'], group: 'team' },
      { key: 'pro100', label: 'Pro 100', badge: 'Pro 100', badgeClass: 'tier-purple', channels: ['web', 'apple'], group: 'personal' },
      { key: 'businessPremium', label: 'Business Prem', badge: '团队高级', badgeClass: 'tier-team', channels: ['web'], group: 'team' },
      { key: 'pro200', label: 'Pro 200', badge: 'Pro 200', badgeClass: 'tier-purple', channels: ['web', 'apple'], group: 'personal' },
      { key: 'pro500', label: 'Pro 500', badge: 'Pro 500', badgeClass: 'tier-purple', channels: ['web', 'apple'], group: 'personal' },
    ]),
    filters: Object.freeze([
      { key: 'all', label: '全部 7 档' },
      { key: 'personal', label: '个人 5 档' },
      { key: 'team', label: '团队 2 档' },
      { key: 'web', label: '仅官网' },
      { key: 'apple', label: '仅 App Store' },
    ]),
  }),
  google_ultra: Object.freeze({
    key: 'google_ultra',
    title: 'Google AI Ultra',
    description: 'Google AI Ultra 5×（20 TB）与 20×（30 TB）全球月价。官网通过住宅代理同会话核验当地 IP、本币与数字；App Store 扫描 Google 全部 16 款含内购软件并按涨价后新开用户取最高有效月价。',
    note: '官网仅采用 Google 官方地区正常月付标价（确系仅收美元地区按美元折算）。App Store 每日 04:00 直连巡检，同币种低于官网 65%（如仅老用户旧续订价进 Top 10）将标黄并移出最低价看板。',
    plans: Object.freeze([
      { key: 'ultra5x', label: 'Ultra 5× (20 TB)', badge: '5×', badgeClass: '', channels: ['web', 'apple'], group: 'personal' },
      { key: 'ultra20x', label: 'Ultra 20× (30 TB)', badge: '20×', badgeClass: 'tier-purple', channels: ['web', 'apple'], group: 'personal' },
    ]),
    filters: Object.freeze([
      { key: 'all', label: '全部 2 档' },
      { key: 'web', label: '仅官网' },
      { key: 'apple', label: '仅 App Store' },
    ]),
  }),
});

export const state = {
  published: null,
  overrides: { schemaVersion: 1, updatedAt: null, entries: {} },
  fx: null,
  activeProduct: 'chatgpt',
  tierFilter: 'all',
  sort: null,
  search: '',
  verifiedOnly: false,
  suspectOnly: false,
  admin: { authenticated: false, adminConfigured: false, githubConfigured: false, repo: null, branch: 'main', status: null, jobs: [] },
  toastTimer: null,
};

export function escapeHTML(value) {
  return String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

export function safeSourceUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password) return null;
    return url.href;
  } catch {
    return null;
  }
}

export function productFromHash(hash = typeof location !== 'undefined' ? location.hash : '') {
  const h = String(hash || '').toLowerCase();
  if (h.includes('google') || h.includes('ultra')) return 'google_ultra';
  return 'chatgpt';
}

export function buildColumns(productKey, filterKey = 'all') {
  const cfg = PRODUCT_CONFIG[productKey];
  if (!cfg) return [];
  const cols = [];
  for (const plan of cfg.plans) {
    if (filterKey === 'personal' && plan.group !== 'personal') continue;
    if (filterKey === 'team' && plan.group !== 'team') continue;
    for (const ch of plan.channels) {
      if (filterKey === 'web' && ch !== 'web') continue;
      if (filterKey === 'apple' && ch !== 'apple') continue;
      cols.push({
        key: `${plan.key}-${ch}`,
        plan: plan.key,
        planLabel: plan.label,
        badge: plan.badge,
        badgeClass: plan.badgeClass,
        source: ch,
        sourceLabel: ch === 'web' ? '官网' : 'App Store',
      });
    }
  }
  return cols;
}

export function applyOverridesAndGuardToProduct(productKey, rawProd, overridesDoc) {
  const prod = structuredClone(rawProd);
  const cfg = PRODUCT_CONFIG[productKey];
  const entries = overridesDoc?.entries || {};
  const applePlans = prod.applePlans || cfg.plans.filter((p) => p.channels.includes('apple')).map((p) => p.key);

  for (const row of prod.countries || []) {
    const cc = row.code;
    for (const plan of cfg.plans) {
      const ovWeb = entries[`${productKey}:${cc}:web:${plan.key}`];
      if (ovWeb && row.web?.cells) {
        row.web.cells[plan.key] = applyOverrideToCell(row.web.cells[plan.key], ovWeb);
      }
      if (plan.channels.includes('apple') && row.apple?.cells) {
        const ovApple = entries[`${productKey}:${cc}:apple:${plan.key}`];
        if (ovApple) {
          row.apple.cells[plan.key] = applyOverrideToCell(row.apple.cells[plan.key], ovApple);
        }
      }
    }
    applySameCurrencyGuard(row, applePlans);
  }
  return prod;
}

function applyOverrideToCell(cell, entry) {
  const base = cell ? structuredClone(cell) : {};
  const amount = Number(entry?.amount);
  const currency = String(entry?.currency || '').toUpperCase();
  if (!Number.isFinite(amount) || amount <= 0 || !/^[A-Z]{3}$/.test(currency)) return base;
  if (base.amount != null && !base.manualOverride) {
    base.rawObserved = {
      amount: base.amount,
      currency: base.currency,
      displayPrice: base.displayPrice,
      observedAt: base.observedAt,
      reason: base.reason,
    };
  }
  return {
    ...base,
    status: 'ok',
    amount,
    currency,
    period: 'month',
    identityVerified: true,
    periodVerified: true,
    regionVerified: true,
    manualOverride: true,
    manualOverrideNote: entry.note || '管理员实测校准价',
    manualOverrideAt: entry.updatedAt || base.observedAt,
    observedAt: entry.updatedAt || base.observedAt,
    suspectGrandfathered: false,
    excludeFromMinCard: false,
    reason: `实测校准价：${entry.note || '已人工核验新开月付价'}`,
  };
}

export function applySameCurrencyGuard(row, applePlans, ratioThreshold = 0.65) {
  for (const planKey of applePlans) {
    const w = row.web?.cells?.[planKey];
    const a = row.apple?.cells?.[planKey];
    if (!a || typeof a !== 'object') continue;
    const staleExcluded = Boolean(a.historyStale);
    if (a.manualOverride) {
      a.suspectGrandfathered = false;
      a.excludeFromMinCard = false;
      delete a.suspectRatio;
      delete a.suspectReason;
      continue;
    }
    const wOk = w?.status === 'ok' && Number.isFinite(w.amount) && w.amount > 0 && /^[A-Z]{3}$/.test(w.currency || '');
    const aOk = a.status === 'ok' && Number.isFinite(a.amount) && a.amount > 0 && /^[A-Z]{3}$/.test(a.currency || '');
    if (!wOk || !aOk || w.currency !== a.currency) {
      a.suspectGrandfathered = false;
      a.excludeFromMinCard = staleExcluded;
      delete a.suspectRatio;
      delete a.suspectReason;
      continue;
    }
    const ratio = a.amount / w.amount;
    if (ratio < ratioThreshold) {
      const pct = Math.round(ratio * 100);
      a.suspectGrandfathered = true;
      a.suspectRatio = Number(ratio.toFixed(4));
      a.excludeFromMinCard = true;
      a.suspectReason = `同币种（${a.currency}）下 App Store 公开价仅约为官网价的 ${pct}%（< 65%），疑似老用户旧续订价霸榜，已移出最低价看板`;
    } else {
      a.suspectGrandfathered = false;
      a.excludeFromMinCard = staleExcluded;
      delete a.suspectRatio;
      delete a.suspectReason;
    }
  }
}

export function cellIsDisplayValid(cell) {
  return Boolean(
    cell &&
    cell.status === 'ok' &&
    typeof cell.amount === 'number' &&
    Number.isFinite(cell.amount) &&
    cell.amount > 0 &&
    /^[A-Z]{3}$/.test(cell.currency || ''),
  );
}

export function cellEligibleForMinCard(cell) {
  return (
    cellIsDisplayValid(cell) &&
    !cell.excludeFromMinCard &&
    !cell.suspectGrandfathered &&
    !cell.historyStale
  );
}

export function euroAmount(cell, fx) {
  if (!cellIsDisplayValid(cell)) return null;
  const rate = fx?.rates?.[cell.currency]?.rate;
  return typeof rate === 'number' && Number.isFinite(rate) && rate > 0 ? cell.amount / rate : null;
}

export function minimumPricesForProduct(prod, columns, fx) {
  return columns.map((column) => {
    let winner = null;
    for (const country of prod?.countries || []) {
      if (country.excluded) continue;
      const cell = country[column.source]?.cells?.[column.plan];
      if (!cellEligibleForMinCard(cell)) continue;
      const euro = euroAmount(cell, fx);
      if (euro !== null && (!winner || euro < winner.euro || (euro === winner.euro && country.code < winner.country.code))) {
        winner = { country, cell, euro };
      }
    }
    return { column, winner };
  });
}

export function sortCountriesForColumn(countries, column, fx) {
  return [...countries].sort((a, b) => {
    if (column) {
      const ac = a[column.source]?.cells?.[column.plan];
      const bc = b[column.source]?.cells?.[column.plan];
      const av = euroAmount(ac, fx);
      const bv = euroAmount(bc, fx);
      if (av === null && bv !== null) return 1;
      if (av !== null && bv === null) return -1;
      if (av !== null && bv !== null && av !== bv) return av - bv;
    }
    return String(a.code).localeCompare(String(b.code));
  });
}

const ZERO_DECIMAL_CURRENCIES = new Set([
  'BIF', 'CLP', 'DJF', 'GNF', 'ISK', 'JPY', 'KMF', 'KRW',
  'PYG', 'RWF', 'UGX', 'VND', 'VUV', 'XAF', 'XOF', 'XPF',
]);
const THREE_DECIMAL_CURRENCIES = new Set(['BHD', 'IQD', 'JOD', 'KWD', 'LYD', 'OMR', 'TND']);

export function formatMoney(amount, currency) {
  if (typeof amount !== 'number' || !Number.isFinite(amount)) return '—';
  const digits = ZERO_DECIMAL_CURRENCIES.has(currency) ? 0 : THREE_DECIMAL_CURRENCIES.has(currency) ? 3 : 2;
  return `${currency} ${amount.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })}`;
}

export function formatEuro(eur) {
  if (typeof eur !== 'number' || !Number.isFinite(eur)) return '待更新汇率';
  return `≈ €${eur.toFixed(2)} / 月`;
}

export function formatTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}

export function formatDateOnly(iso) {
  if (!iso) return '—';
  return String(iso).slice(0, 10);
}

function showToast(msg) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { el.hidden = true; }, 4200);
}

function activeProductRaw() {
  const raw = state.published?.products?.[state.activeProduct];
  if (!raw) return null;
  return applyOverridesAndGuardToProduct(state.activeProduct, raw, state.overrides);
}

function visibleColumns() {
  return buildColumns(state.activeProduct, state.tierFilter);
}

function renderHeaderAndStats(prod) {
  const cfg = PRODUCT_CONFIG[state.activeProduct];
  document.getElementById('heroDescription').textContent = cfg.description;
  document.getElementById('sectionNote').textContent = cfg.note;

  document.querySelectorAll('.product-tab').forEach((btn) => {
    const active = btn.dataset.product === state.activeProduct;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', String(active));
  });

  const countries = prod?.countries || [];
  let validRegions = 0;
  let suspectCells = 0;
  let historyRetainedCells = 0;
  let overrideCells = 0;
  for (const row of countries) {
    if (row.excluded) continue;
    let hasValid = false;
    for (const plan of cfg.plans) {
      if (cellIsDisplayValid(row.web?.cells?.[plan.key])) hasValid = true;
      const ac = row.apple?.cells?.[plan.key];
      if (cellIsDisplayValid(ac)) {
        hasValid = true;
        if (ac.suspectGrandfathered) suspectCells += 1;
        if (ac.historyRetained) historyRetainedCells += 1;
        if (ac.manualOverride) overrideCells += 1;
      }
    }
    if (hasValid) validRegions += 1;
  }

  document.getElementById('candidateCount').textContent = String(countries.length);
  document.getElementById('validCount').textContent = String(validRegions);
  document.getElementById('suspectCount').textContent = String(suspectCells);
  document.getElementById('historyRetainedCount').textContent = String(historyRetainedCells);

  const webAt = prod?.webUpdatedAt ? `官网 ${formatTime(prod.webUpdatedAt)}` : '';
  const appleAt = prod?.appleUpdatedAt ? `Apple ${formatTime(prod.appleUpdatedAt)}` : '';
  document.getElementById('channelCoverage').textContent = [webAt, appleAt, overrideCells ? `校准 ${overrideCells}` : ''].filter(Boolean).join(' · ') || '已加载';
  document.getElementById('lastUpdated').textContent = prod?.updatedAt ? `快照更新：${formatTime(prod.updatedAt)}` : '已就绪';

  const guardNotice = document.getElementById('guardNotice');
  if (suspectCells > 0) {
    guardNotice.innerHTML = `<span>检测到 <strong>${suspectCells}</strong> 个同币种 App Store 公开价明显低于官网（&lt; 65%，疑似老用户旧续订价占住 Top 10），已标黄警示并自动排除出最低价看板。</span><button id="jumpSuspectBtn" type="button" class="guard-link-btn">${state.suspectOnly ? '恢复查看全部地区' : '仅看这些标疑地区'}</button>`;
    guardNotice.hidden = false;
    document.getElementById('jumpSuspectBtn').addEventListener('click', () => {
      state.suspectOnly = !state.suspectOnly;
      document.getElementById('suspectOnlyInput').checked = state.suspectOnly;
      renderAll();
    });
  } else {
    guardNotice.hidden = true;
  }
}

function renderFilterPills() {
  const cfg = PRODUCT_CONFIG[state.activeProduct];
  const host = document.getElementById('tierFilters');
  host.innerHTML = cfg.filters
    .map((f) => `<button type="button" class="tier-pill ${state.tierFilter === f.key ? 'active' : ''}" data-filter="${f.key}">${escapeHTML(f.label)}</button>`)
    .join('');
  host.querySelectorAll('.tier-pill').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.tierFilter = btn.dataset.filter;
      const visibleCols = visibleColumns();
      if (state.sort && !visibleCols.some((c) => c.key === state.sort)) {
        state.sort = null;
      }
      renderAll();
    });
  });
}

function renderMinimumGrid(prod, cols) {
  const grid = document.getElementById('minimumGrid');
  const mins = minimumPricesForProduct(prod, cols, state.fx);
  grid.innerHTML = mins.map(({ column, winner }) => {
    const toneClass = column.badgeClass === 'tier-purple' ? 'purple' : column.badgeClass === 'tier-team' ? 'teal' : '';
    const selected = state.sort === column.key ? 'selected' : '';
    if (!winner) {
      return `<button type="button" class="minimum-card ${toneClass} ${selected}" data-sort-col="${column.key}"><div class="card-heading"><span>${escapeHTML(column.planLabel)}</span><span class="source-label">${escapeHTML(column.sourceLabel)}</span></div><div class="empty-minimum">暂无有效合格低价</div></button>`;
    }
    const { country, cell, euro } = winner;
    const tags = [];
    if (cell.manualOverride) {
      tags.push(`<span class="card-tag teal">实测校准</span>`);
    } else if (cell.historyRetained) {
      tags.push(`<span class="card-tag">历史新价 · ${escapeHTML(formatDateOnly(cell.historyLastSeen || cell.observedAt))}</span>`);
    }
    if (column.source === 'web' && (cell.tax === 'inclusive' || cell.taxInclusive)) {
      tags.push(`<span class="card-tag tax">含税</span>`);
    }
    return `<button type="button" class="minimum-card ${toneClass} ${selected}" data-sort-col="${column.key}">
      <div class="card-heading"><span>${escapeHTML(column.planLabel)}</span><span class="source-label">${escapeHTML(column.sourceLabel)}</span></div>
      <div class="minimum-country">${escapeHTML(country.flag || '')} ${escapeHTML(country.name)} (${escapeHTML(country.code)})</div>
      <div class="minimum-local">${escapeHTML(formatMoney(cell.amount, cell.currency))}</div>
      <div class="minimum-eur">${escapeHTML(formatEuro(euro))}</div>
      ${tags.join('')}
    </button>`;
  }).join('');

  grid.querySelectorAll('[data-sort-col]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const col = btn.dataset.sortCol;
      state.sort = state.sort === col ? null : col;
      renderAll();
    });
  });
  document.getElementById('minimumSubtle').textContent = `按欧元比较 · 当前显示 ${cols.length} 个购买渠道卡片`;
}

function buildTableHeader(cols) {
  const thead = document.getElementById('tableHead');
  const table = document.getElementById('priceTable');
  table.className = state.activeProduct === 'chatgpt' && cols.length >= 9
    ? 'wide-chatgpt'
    : state.activeProduct === 'google_ultra' && cols.length === 4
    ? 'compact-ultra'
    : 'filtered-narrow';

  const cfg = PRODUCT_CONFIG[state.activeProduct];
  const groups = [];
  for (const plan of cfg.plans) {
    const planCols = cols.filter((c) => c.plan === plan.key);
    if (planCols.length) groups.push({ plan, cols: planCols });
  }

  const row1 = `
    <tr class="plan-head">
      <th scope="col" rowspan="2" class="rank-head">#</th>
      <th scope="col" rowspan="2" class="country-head">国家 / 地区</th>
      ${groups.map((g) => `<th scope="colgroup" data-plan-group="${escapeHTML(g.plan.key)}" colspan="${g.cols.length}"><span class="tier-badge ${escapeHTML(g.plan.badgeClass)}">${escapeHTML(g.plan.badge)}</span>${escapeHTML(g.plan.label)}</th>`).join('')}
    </tr>
  `;
  const row2 = `
    <tr class="sub-head">
      ${cols.map((c, idx) => {
        const isGroupStart = idx === 0 || cols[idx - 1].plan !== c.plan;
        const sorted = state.sort === c.key ? 'sorted' : '';
        return `<th scope="col" class="channel-head ${isGroupStart ? 'group-start' : 'sub-col'} ${sorted}" data-col-key="${c.key}">
          <button type="button" data-sort-btn="${c.key}">${escapeHTML(c.sourceLabel)}</button>
        </th>`;
      }).join('')}
    </tr>
  `;
  thead.innerHTML = row1 + row2;
  thead.querySelectorAll('[data-sort-btn]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const k = btn.dataset.sortBtn;
      state.sort = state.sort === k ? null : k;
      renderAll();
    });
  });

  const jumpsHost = document.getElementById('tierJumpPills');
  if (jumpsHost) {
    jumpsHost.innerHTML = groups
      .map((g) => `<button type="button" class="jump-pill" data-jump-plan="${escapeHTML(g.plan.key)}">${escapeHTML(g.plan.badge || g.plan.label)}</button>`)
      .join('');
    jumpsHost.querySelectorAll('[data-jump-plan]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const scrollBox = document.getElementById('priceTableScroll');
        const targetTh = document.querySelector(`#tableHead th[data-plan-group="${btn.dataset.jumpPlan}"]`);
        const countryTh = document.querySelector('#tableHead th.country-head');
        const rankTh = document.querySelector('#tableHead th.rank-head');
        if (scrollBox && targetTh) {
          const stickyWidth = (rankTh?.offsetWidth || 34) + (countryTh?.offsetWidth || 124);
          scrollBox.scrollTo({ left: Math.max(0, targetTh.offsetLeft - stickyWidth), behavior: 'smooth' });
        }
      });
    });
  }
}

function renderCellHtml(row, col, idx, cols) {
  const isGroupStart = idx === 0 || cols[idx - 1].plan !== col.plan;
  const sorted = state.sort === col.key ? 'sorted' : '';
  const cell = row[col.source]?.cells?.[col.plan];
  const valid = cellIsDisplayValid(cell);
  const eur = valid ? euroAmount(cell, state.fx) : null;

  let inner = '';
  let extraBtnClass = '';
  if (valid) {
    const badges = [];
    if (cell.manualOverride) {
      badges.push(`<span class="cell-badge badge-override">实测校准</span>`);
    } else if (cell.suspectGrandfathered) {
      extraBtnClass += ' cell-suspect';
      const pct = typeof cell.suspectRatio === 'number' ? ` · 官网 ${Math.round(cell.suspectRatio * 100)}%` : '';
      badges.push(`<span class="cell-badge badge-suspect">疑似旧续订价${pct}</span>`);
    } else if (cell.historyRetained && cell.historyStale) {
      extraBtnClass += ' cell-stale';
      badges.push(`<span class="cell-badge badge-stale">历史待复核 &gt;90天</span>`);
    } else if (cell.historyRetained) {
      badges.push(`<span class="cell-badge badge-history">历史新价 ${escapeHTML(formatDateOnly(cell.historyLastSeen || cell.observedAt))}</span>`);
    }
    if (col.source === 'web' && (cell.tax === 'inclusive' || cell.taxInclusive)) {
      badges.push(`<span class="cell-badge badge-tax">含税</span>`);
    }

    let sourceTag = '';
    if (col.source === 'apple' && cell.appName) {
      sourceTag = `<div class="price-source">${escapeHTML(cell.appName)}</div>`;
    } else if (col.source === 'web' && cell.evidence?.currencyMode === 'usd_regional_billing') {
      sourceTag = `<div class="price-source">该区仅收 USD</div>`;
    }

    inner = `
      <div class="local-price">${escapeHTML(formatMoney(cell.amount, cell.currency))}</div>
      <div class="eur-price">${escapeHTML(formatEuro(eur))}</div>
      ${sourceTag}
      ${badges.join('')}
    `;
  } else {
    const st = cell?.status || 'pending';
    const label = st === 'unavailable'
      ? '不支持'
      : st === 'pending_basis'
      ? '含税/待核'
      : st === 'error' || st === 'failed'
      ? '失败'
      : '待核实';
    inner = `<span class="unverified-label">${label}</span>`;
  }

  return `<td class="price-cell ${isGroupStart ? 'group-start' : 'sub-col'} ${sorted}">
    <button type="button" class="price-button${extraBtnClass}" data-status="${escapeHTML(cell?.status || 'pending')}" data-country="${escapeHTML(row.code)}" data-channel="${escapeHTML(col.source)}" data-plan="${escapeHTML(col.plan)}">
      ${inner}
    </button>
  </td>`;
}

function renderTable(prod, cols) {
  buildTableHeader(cols);
  const tbody = document.getElementById('tableBody');
  const excluded = [];
  const allRows = (prod?.countries || []).filter((r) => {
    if (r.excluded) {
      excluded.push(r);
      return false;
    }
    return true;
  });

  const q = state.search.trim().toLowerCase();
  const filtered = allRows.filter((row) => {
    if (q && !String(row.code).toLowerCase().includes(q) && !String(row.name || '').toLowerCase().includes(q)) {
      return false;
    }
    if (state.verifiedOnly) {
      const anyOk = cols.some((c) => cellIsDisplayValid(row[c.source]?.cells?.[c.plan]));
      if (!anyOk) return false;
    }
    if (state.suspectOnly) {
      const anySuspect = cols.some((c) => row[c.source]?.cells?.[c.plan]?.suspectGrandfathered);
      if (!anySuspect) return false;
    }
    return true;
  });

  const sortCol = cols.find((c) => c.key === state.sort) || null;
  const sortedRows = sortCountriesForColumn(filtered, sortCol, state.fx);

  if (!sortedRows.length) {
    tbody.innerHTML = `<tr class="empty-table"><td colspan="${2 + cols.length}">没有匹配当前筛选条件的地区。</td></tr>`;
  } else {
    tbody.innerHTML = sortedRows.map((row, idx) => {
      const pst = row.proxy?.status || 'pending';
      const proxyText = pst === 'ok'
        ? (row.proxy?.googleIpCountry && row.proxy.googleIpCountry !== row.code ? `代理·${row.proxy.googleIpCountry}` : '代理✓')
        : pst === 'not_in_proxy_catalog'
        ? '仅Apple'
        : pst === 'skipped'
        ? '无需代理'
        : pst === 'error' || pst === 'unreachable' || pst === 'mismatch'
        ? '代理失败'
        : '待测';
      return `<tr>
        <td class="rank-cell">${idx + 1}</td>
        <td class="country-cell"><div class="country-name" title="${escapeHTML(row.name)}"><span class="country-flag">${escapeHTML(row.flag || '')}</span>${escapeHTML(row.name)}</div><div class="country-meta"><span class="country-code">${escapeHTML(row.code)}</span><span class="proxy-badge" data-status="${escapeHTML(pst)}">${escapeHTML(proxyText)}</span></div></td>
        ${cols.map((c, i) => renderCellHtml(row, c, i, cols)).join('')}
      </tr>`;
    }).join('');
  }

  document.getElementById('tableStats').textContent =
    `显示 ${sortedRows.length} / ${allRows.length} 个地区（${PRODUCT_CONFIG[state.activeProduct].title}` +
    `${sortCol ? ` · 已按「${sortCol.planLabel} · ${sortCol.sourceLabel}」欧元升序` : ' · 按地区代码排序'}）`;

  document.getElementById('excludedSummary').textContent = `双渠道均确认不支持的地区（${excluded.length}）`;
  document.getElementById('excludedList').textContent = excluded.length
    ? excluded.map((r) => `${r.flag || ''} ${r.name} (${r.code})`).join('、')
    : '暂无确认双渠道均不支持的地区。';

  tbody.querySelectorAll('.price-button').forEach((btn) => {
    btn.addEventListener('click', () => {
      openDetailDialog(btn.dataset.country, btn.dataset.channel, btn.dataset.plan);
    });
  });

  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(syncScrollAssist);
  }
}

export function syncScrollAssist() {
  const scrollBox = document.getElementById('priceTableScroll');
  const assist = document.getElementById('tableScrollAssist');
  const slider = document.getElementById('tableScrollSlider');
  const leftBtn = document.getElementById('scrollLeftBtn');
  const rightBtn = document.getElementById('scrollRightBtn');
  if (!scrollBox || !assist) return;
  const maxScroll = Math.max(0, scrollBox.scrollWidth - scrollBox.clientWidth);
  assist.hidden = maxScroll <= 4;
  if (assist.hidden) return;
  if (slider) {
    slider.max = String(maxScroll);
    slider.value = String(Math.round(scrollBox.scrollLeft));
  }
  if (leftBtn) leftBtn.disabled = scrollBox.scrollLeft <= 2;
  if (rightBtn) rightBtn.disabled = scrollBox.scrollLeft >= maxScroll - 2;
}

function openDetailDialog(countryCode, channel, planKey) {
  const prod = activeProductRaw();
  const row = (prod?.countries || []).find((r) => r.code === countryCode);
  if (!row) return;
  const colCfg = PRODUCT_CONFIG[state.activeProduct].plans.find((p) => p.key === planKey);
  const cell = row[channel]?.cells?.[planKey] || {};
  const valid = cellIsDisplayValid(cell);
  const eur = valid ? euroAmount(cell, state.fx) : null;

  document.getElementById('detailTitle').textContent =
    `${row.flag || ''} ${row.name} (${row.code}) · ${colCfg?.label || planKey} · ${channel === 'web' ? '官网' : 'App Store'}`;

  const srcUrl = safeSourceUrl(cell.sourceUrl || row[channel]?.sourceUrl);
  const srcLink = srcUrl
    ? `<a href="${escapeHTML(srcUrl)}" target="_blank" rel="noreferrer">${escapeHTML(srcUrl)} ↗</a>`
    : '—';

  const suspectHtml = cell.suspectGrandfathered
    ? `<div class="detail-reason warning">${escapeHTML(cell.suspectReason)}</div>`
    : `<div class="detail-reason">${escapeHTML(cell.reason || '暂无额外说明')}</div>`;

  let rawObsHtml = '';
  if (cell.manualOverride && cell.rawObserved) {
    rawObsHtml = `<dt>原始公开观测</dt><dd>${escapeHTML(formatMoney(cell.rawObserved.amount, cell.rawObserved.currency))}（${escapeHTML(formatTime(cell.rawObserved.observedAt))}）</dd>`;
  }

  let extraFieldsHtml = '';
  if (channel === 'web' && state.activeProduct === 'chatgpt' && (cell.tax === 'exclusive' || cell.tax === 'inclusive' || cell.taxInclusive)) {
    const isInc = cell.tax === 'inclusive' || cell.taxInclusive;
    extraFieldsHtml += `<dt>计税口径</dt><dd>${isInc ? '含税直出（tax: inclusive，官方该地区未提供不含税节点，已标「含税」纳入排名）' : '不含税直出（tax: exclusive）'}</dd>`;
  }
  if (cell.appName) {
    extraFieldsHtml += `<dt>来源 App</dt><dd>${escapeHTML(cell.appName)} (ID ${escapeHTML(cell.appId || '—')})</dd>`;
  }
  if (cell.sourceProductName) {
    extraFieldsHtml += `<dt>商店商品名称</dt><dd>${escapeHTML(cell.sourceProductName)}</dd>`;
  }
  if (cell.evidence?.supersededGrandfatheredPrices?.length) {
    extraFieldsHtml += `<dt>已排除旧低价</dt><dd>${cell.evidence.supersededGrandfatheredPrices.map((a) => escapeHTML(formatMoney(a, cell.currency))).join('、')}</dd>`;
  }
  if (cell.evidence?.excludedAnnualPrices?.length) {
    extraFieldsHtml += `<dt>已隔离年付价</dt><dd>${cell.evidence.excludedAnnualPrices.map((a) => escapeHTML(formatMoney(a, cell.currency))).join('、')}</dd>`;
  }
  if (cell.historyRetained) {
    extraFieldsHtml += `<dt>历史账本状态</dt><dd>${cell.historyStale ? '历史保留（超 90 天未复现，已标灰且移出最低价看板）' : '历史账本保留（近 90 天最高有效新价）'}；首次露出 ${escapeHTML(formatDateOnly(cell.historyFirstSeen))}，最近露出 ${escapeHTML(formatDateOnly(cell.historyLastSeen))}</dd>`;
  }

  let historyHtml = '';
  if (Array.isArray(cell.historyTimeline) && cell.historyTimeline.length) {
    historyHtml = `<div class="evidence-section"><h3>App Store 历史价格轨迹（每日 04:00 巡检记录）</h3><ul class="candidate-list">${cell.historyTimeline.map((h) => `<li><strong>${escapeHTML(formatMoney(h.amount, h.currency))}</strong> · ${escapeHTML(h.appName || '')} · 首次 ${escapeHTML(formatDateOnly(h.firstSeenAt))} / 最近 ${escapeHTML(formatDateOnly(h.lastSeenAt))}（出现 ${escapeHTML(h.seenCount)} 次）</li>`).join('')}</ul></div>`;
  }

  let candidatesHtml = '';
  if (Array.isArray(cell.candidates) && cell.candidates.length) {
    candidatesHtml = `<div class="evidence-section"><h3>公开列表候选项目（${cell.candidates.length}）</h3><ul class="candidate-list">${cell.candidates.map((c) => {
      const tag = c.excludedAsAnnual ? ' [已隔离年付]' : c.excludedAsGrandfathered ? ' [老用户旧续订价·已排除]' : c.isPromotional ? ' [试用/促销·已排除]' : '';
      const amtText = c.amount ? formatMoney(c.amount, c.currency || cell.currency || 'USD') : (c.displayPrice || '—');
      return `<li><strong>${escapeHTML(c.sourceProductName || c.appName || '候选')}</strong>：${escapeHTML(amtText)}${escapeHTML(tag)} <span class="subtle">${escapeHTML(c.appName || '')}</span></li>`;
    }).join('')}</ul></div>`;
  }

  let appsHtml = '';
  if (state.activeProduct === 'google_ultra' && channel === 'apple' && Array.isArray(row.apple?.apps) && row.apple.apps.length) {
    appsHtml = `<details class="other-apps-details"><summary>查看已巡检的 ${row.apple.apps.length} 款 Google 含内购 App 状态</summary><ul class="candidate-list">${row.apple.apps.map((a) => `<li><strong>${escapeHTML(a.appName)}</strong> (${escapeHTML(a.appId)}) · HTTP ${escapeHTML(a.httpStatus ?? '0')} · ${escapeHTML(a.finalStorefront || row.code)}${a.skippedStorefrontRedirect ? ' [核心入口已确认跨区，跳过]' : ''}</li>`).join('')}</ul></details>`;
  }

  let adminCalibrateHtml = '';
  if (state.admin.authenticated) {
    adminCalibrateHtml = `<div class="evidence-section"><h3>管理员 · 实测新开月价校准</h3>
      <form id="cellOverrideForm" class="form-grid">
        <label class="form-field">币种（3位ISO）<input name="currency" required maxlength="3" value="${escapeHTML(cell.currency || row.web?.currency || '')}" placeholder="例如 USD"></label>
        <label class="form-field">实测月付金额<input name="amount" type="number" step="0.01" min="0.01" required value="${escapeHTML(cell.amount || '')}" placeholder="例如 19.99"></label>
        <label class="form-field" style="grid-column:1 / -1">校准备注<input name="note" maxlength="180" value="${escapeHTML(cell.manualOverrideNote || '')}" placeholder="例如：iOS 土区 App Store 实测新开月价"></label>
        <div class="button-row" style="grid-column:1 / -1">
          <button type="submit" class="button button-primary">保存实测校准价</button>
          ${cell.manualOverride ? `<button type="button" id="deleteCellOverrideBtn" class="button button-danger">清除当前校准</button>` : ''}
        </div>
      </form>
    </div>`;
  }

  document.getElementById('detailBody').innerHTML = `
    <div class="detail-price">${valid ? escapeHTML(formatMoney(cell.amount, cell.currency)) : '暂无可核验月价'}</div>
    <div class="detail-eur">${valid ? escapeHTML(formatEuro(eur)) : `状态：${escapeHTML(cell.status || 'pending')}`}</div>
    ${suspectHtml}
    <dl class="detail-grid">
      <dt>采集时间</dt><dd>${escapeHTML(formatTime(cell.observedAt))}</dd>
      ${rawObsHtml}
      ${extraFieldsHtml}
      <dt>官方来源链接</dt><dd>${srcLink}</dd>
    </dl>
    ${historyHtml}
    ${candidatesHtml}
    ${appsHtml}
    ${adminCalibrateHtml}
  `;

  const form = document.getElementById('cellOverrideForm');
  if (form) {
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const fd = new FormData(form);
      await saveOverride({
        product: state.activeProduct,
        country: countryCode,
        channel,
        plan: planKey,
        currency: String(fd.get('currency') || '').toUpperCase().trim(),
        amount: Number(fd.get('amount')),
        note: String(fd.get('note') || '').trim(),
      });
      openDetailDialog(countryCode, channel, planKey);
    });
    const delBtn = document.getElementById('deleteCellOverrideBtn');
    if (delBtn) {
      delBtn.addEventListener('click', async () => {
        await deleteOverride({ product: state.activeProduct, country: countryCode, channel, plan: planKey });
        openDetailDialog(countryCode, channel, planKey);
      });
    }
  }

  document.getElementById('priceDetails').showModal();
}

async function saveOverride(payload) {
  const res = await fetch('/api/admin/overrides', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) {
    showToast(data.error || '保存校准失败');
    return;
  }
  state.overrides = data.overrides;
  renderAll();
  showToast('已保存实测校准价并同步至 GitHub 仓库');
}

async function deleteOverride(payload) {
  const res = await fetch('/api/admin/overrides', {
    method: 'DELETE',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) {
    showToast(data.error || '清除校准失败');
    return;
  }
  state.overrides = data.overrides;
  renderAll();
  showToast('已清除该条实测校准');
}

function renderAdminBody() {
  const body = document.getElementById('adminBody');
  const entryBtn = document.getElementById('openAdminBtn');
  entryBtn.classList.toggle('authed', state.admin.authenticated);
  entryBtn.textContent = state.admin.authenticated ? '管理员（已登录）' : '管理员';

  if (!state.admin.authenticated) {
    body.innerHTML = `
      <div class="admin-section">
        <p class="hero-description" style="margin-top:0">当前网站对所有访客公开只读。输入云端环境变量 <code>ADMIN_PASSWORD</code> 对应的管理员口令后，可派发 GitHub Actions 抓价任务、查看每日 Apple 巡检记录与录入实测校准价。</p>
        <form id="adminLoginForm" class="button-row" style="margin-top:16px">
          <input id="adminPasswordInput" type="password" required placeholder="输入管理员口令" style="flex:1;min-width:180px;border:1px solid #d8e1ee;border-radius:8px;padding:9px 11px;">
          <button type="submit" class="button button-primary">登录管理员</button>
        </form>
      </div>
    `;
    document.getElementById('adminLoginForm').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const pw = document.getElementById('adminPasswordInput').value;
      const res = await fetch('/api/admin/login', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ password: pw }),
      });
      const data = await res.json();
      if (!res.ok) {
        showToast(data.error || '登录失败');
        return;
      }
      await refreshAdminSessionAndStatus();
      showToast('管理员登录成功');
    });
    return;
  }

  const secrets = state.admin.status?.proxySecrets || {};
  const secretPills = ['IPROYAL_PROXY_USER', 'IPROYAL_PROXY_PASS', 'IPROYAL_PROXY_ENDPOINT'].map((name) => {
    const info = secrets[name] || {};
    return `<span class="secret-pill ${info.configured ? 'ok' : 'missing'}">${info.configured ? '✓' : '!'} ${escapeHTML(name)}${info.updatedAt ? ` (${escapeHTML(formatDateOnly(info.updatedAt))})` : ''}</span>`;
  }).join('');

  const overridesList = Object.entries(state.overrides?.entries || {});
  const runs = state.admin.jobs || [];

  body.innerHTML = `
    <div class="admin-section">
      <h3>1. GitHub Actions 住宅代理 Secrets 状态（凭据不回传浏览器）</h3>
      <div>${secretPills}</div>
      <p class="subtle" style="margin-top:4px">${state.admin.status?.githubConfigured ? `仓库：${escapeHTML(state.admin.status.repo)} · 仅服务端核对是否存在` : '未配置 GITHUB_TOKEN / GITHUB_REPO'}</p>
      <div class="button-row">
        ${state.admin.status?.secretsSettingsUrl ? `<a class="button" href="${escapeHTML(state.admin.status.secretsSettingsUrl)}" target="_blank" rel="noreferrer">在 GitHub Secrets 修改代理配置 ↗</a>` : ''}
        <button type="button" id="refreshAdminStatusBtn" class="button">刷新状态</button>
        <button type="button" id="adminLogoutBtn" class="button button-danger" style="margin-left:auto">退出登录</button>
      </div>
    </div>

    <div class="admin-section">
      <h3>2. 派发云端采集任务（GitHub Actions）</h3>
      <form id="dispatchRefreshForm" class="form-grid">
        <label class="form-field">产品
          <select name="product">
            <option value="all">全部（ChatGPT + Google AI Ultra）</option>
            <option value="chatgpt">仅 ChatGPT（7 档）</option>
            <option value="google_ultra">仅 Google AI Ultra（2 档）</option>
          </select>
        </label>
        <label class="form-field">渠道范围
          <select name="scope">
            <option value="apple">仅 App Store（直连，零代理）</option>
            <option value="web">仅官网（走住宅代理）</option>
            <option value="all">全部（官网 + App Store）</option>
          </select>
        </label>
        <label class="form-field">执行模式
          <select name="mode">
            <option value="full">全量刷新</option>
            <option value="retry">仅补抓失败 / 待核实项</option>
          </select>
        </label>
        <label class="form-field">指定地区（可选）
          <input name="countries" placeholder="留空全量，或填如 TR,US,JP">
        </label>
        <div class="button-row" style="grid-column:1 / -1">
          <button type="submit" class="button button-primary">立即触发云端抓价</button>
        </div>
      </form>
    </div>

    <div class="admin-section">
      <h3>3. 最近云端任务（含每日 04:00 Apple 巡检）</h3>
      ${runs.length ? `<ul class="candidate-list">${runs.map((r) => `<li><strong>${escapeHTML(r.name)}</strong> · ${escapeHTML(r.status)} ${r.conclusion ? `(${escapeHTML(r.conclusion)})` : ''} · ${escapeHTML(formatTime(r.updatedAt))} <a href="${escapeHTML(r.htmlUrl)}" target="_blank" rel="noreferrer">日志 ↗</a></li>`).join('')}</ul>` : '<p class="subtle">暂无最近运行记录。</p>'}
    </div>

    <div class="admin-section">
      <h3>4. 已有实测校准（${overridesList.length} 条 · 可在任意价格弹窗中新增）</h3>
      ${overridesList.length ? `<ul class="candidate-list">${overridesList.map(([k, v]) => `<li><strong>${escapeHTML(k)}</strong> → ${escapeHTML(formatMoney(v.amount, v.currency))} (${escapeHTML(v.note || '')}) <button type="button" class="text-btn-del" data-del-ov="${escapeHTML(k)}">删除</button></li>`).join('')}</ul>` : '<p class="subtle">当前无人工实测校准条目。</p>'}
    </div>
  `;

  document.getElementById('refreshAdminStatusBtn').addEventListener('click', () => refreshAdminSessionAndStatus());
  document.getElementById('adminLogoutBtn').addEventListener('click', async () => {
    await fetch('/api/admin/logout', { method: 'POST' });
    state.admin = { authenticated: false, adminConfigured: state.admin.adminConfigured, status: null, jobs: [] };
    renderAdminBody();
    showToast('已退出管理员登录');
  });
  document.getElementById('dispatchRefreshForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const fd = new FormData(ev.currentTarget);
    const res = await fetch('/api/admin/refresh', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        product: fd.get('product'),
        scope: fd.get('scope'),
        mode: fd.get('mode'),
        countries: fd.get('countries'),
      }),
    });
    const data = await res.json();
    if (!res.ok) {
      showToast(data.error || '派发任务失败');
      return;
    }
    showToast('已派发 GitHub Actions 采集任务；完成后页面部署会自动更新');
    await refreshAdminSessionAndStatus();
  });
  body.querySelectorAll('[data-del-ov]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const [product, country, channel, plan] = btn.dataset.delOv.split(':');
      await deleteOverride({ product, country, channel, plan });
      renderAdminBody();
    });
  });
}

async function refreshAdminSessionAndStatus() {
  try {
    const sess = await fetch('/api/admin/session').then((r) => r.json());
    state.admin.authenticated = Boolean(sess.authenticated);
    state.admin.adminConfigured = Boolean(sess.adminConfigured);
    if (state.admin.authenticated) {
      const [statusRes, jobsRes] = await Promise.all([
        fetch('/api/admin/status').then((r) => r.json()).catch(() => null),
        fetch('/api/admin/jobs').then((r) => r.json()).catch(() => ({ runs: [] })),
      ]);
      state.admin.status = statusRes;
      state.admin.jobs = jobsRes?.runs || [];
    }
  } catch {
    state.admin.authenticated = false;
  }
  renderAdminBody();
}

function renderFxState() {
  const fx = state.fx || state.published?.fx;
  const el = document.getElementById('fxState');
  if (!el) return;
  el.textContent = fx?.referenceDate
    ? `参考汇率日期：${fx.referenceDate} · 已加载 ${Object.keys(fx.rates || {}).length} 个币种对欧元汇率`
    : '等待央行汇率';
}

function renderAll() {
  const prod = activeProductRaw();
  if (!prod) return;
  const cols = visibleColumns();
  renderHeaderAndStats(prod);
  renderFilterPills();
  renderMinimumGrid(prod, cols);
  renderTable(prod, cols);
  renderFxState();
}

async function refreshLiveFx() {
  try {
    const res = await fetch('/api/fx');
    if (res.ok) {
      const data = await res.json();
      if (data?.rates?.USD) {
        state.fx = data;
        renderAll();
      }
    }
  } catch {
    // Keep published snapshot FX silently on offline fallback
  }
}

async function boot() {
  state.activeProduct = productFromHash();

  document.querySelectorAll('.product-tab').forEach((btn) => {
    btn.addEventListener('click', () => {
      const next = btn.dataset.product;
      if (next === state.activeProduct) return;
      state.activeProduct = next;
      state.tierFilter = 'all';
      state.sort = null;
      history.replaceState(null, '', next === 'google_ultra' ? '#google-ultra' : '#chatgpt');
      renderAll();
    });
  });

  document.getElementById('searchInput').addEventListener('input', (ev) => {
    state.search = ev.target.value;
    renderAll();
  });
  document.getElementById('verifiedOnlyInput').addEventListener('change', (ev) => {
    state.verifiedOnly = ev.target.checked;
    renderAll();
  });
  document.getElementById('suspectOnlyInput').addEventListener('change', (ev) => {
    state.suspectOnly = ev.target.checked;
    renderAll();
  });
  document.getElementById('closeDetailsBtn').addEventListener('click', () => {
    document.getElementById('priceDetails').close();
  });
  document.getElementById('openAdminBtn').addEventListener('click', async () => {
    await refreshAdminSessionAndStatus();
    document.getElementById('adminDialog').showModal();
  });
  document.getElementById('closeAdminBtn').addEventListener('click', () => {
    document.getElementById('adminDialog').close();
  });

  const scrollBox = document.getElementById('priceTableScroll');
  const slider = document.getElementById('tableScrollSlider');
  document.getElementById('scrollLeftBtn')?.addEventListener('click', () => {
    scrollBox?.scrollBy({ left: -320, behavior: 'smooth' });
  });
  document.getElementById('scrollRightBtn')?.addEventListener('click', () => {
    scrollBox?.scrollBy({ left: 320, behavior: 'smooth' });
  });
  slider?.addEventListener('input', () => {
    if (scrollBox) scrollBox.scrollLeft = Number(slider.value);
    syncScrollAssist();
  });
  scrollBox?.addEventListener('scroll', syncScrollAssist, { passive: true });
  scrollBox?.addEventListener('wheel', (ev) => {
    if (ev.shiftKey && Math.abs(ev.deltaY) > Math.abs(ev.deltaX)) {
      ev.preventDefault();
      scrollBox.scrollLeft += ev.deltaY;
    }
  }, { passive: false });
  window.addEventListener('resize', syncScrollAssist);

  const statusPill = document.getElementById('serviceState');
  try {
    const [pubRes, ovRes] = await Promise.all([
      fetch('/data/published.json', { cache: 'no-store' }),
      fetch('/data/overrides.json', { cache: 'no-store' }).catch(() => null),
    ]);
    if (!pubRes.ok) throw new Error(`HTTP ${pubRes.status}`);
    state.published = await pubRes.json();
    state.fx = state.published.fx || null;
    if (ovRes && ovRes.ok) {
      state.overrides = await ovRes.json();
    }
    statusPill.dataset.state = 'ready';
    statusPill.textContent = '云端快照已就绪';
    renderAll();
    refreshAdminSessionAndStatus();
    refreshLiveFx();
    setInterval(refreshLiveFx, 30 * 60 * 1000);
  } catch (err) {
    statusPill.dataset.state = 'error';
    statusPill.textContent = '数据加载失败';
    document.getElementById('tableBody').innerHTML =
      `<tr class="empty-table"><td colspan="10">无法读取 /data/published.json（${escapeHTML(err.message)}）</td></tr>`;
  }
}

if (typeof document !== 'undefined') {
  window.addEventListener('DOMContentLoaded', boot);
}
