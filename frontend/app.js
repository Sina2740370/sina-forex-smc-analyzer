/* ============================================================
   Sina Quant - Frontend Logic
   ============================================================ */

const API_BASE = '';

let currentTab = 'live';
let currentFilter = 'all';
let allSignals = [];
let refreshInterval = null;
let pricesInterval = null;

const SYMBOL_FLAGS = {
  'EURUSD=X': '🇪🇺🇺🇸',
  'GBPUSD=X': '🇬🇧🇺🇸',
  'USDJPY=X': '🇺🇸🇯🇵',
  'AUDUSD=X': '🇦🇺🇺🇸',
  'USDCAD=X': '🇺🇸🇨🇦',
  'NZDUSD=X': '🇳🇿🇺🇸',
  'USDCHF=X': '🇺🇸🇨🇭',
  'EURGBP=X': '🇪🇺🇬🇧',
  'EURJPY=X': '🇪🇺🇯🇵',
  'GBPJPY=X': '🇬🇧🇯🇵',
  'GC=F': '🥇',
};

/* ============================================================
   Utilities
   ============================================================ */

function $(sel) { return document.querySelector(sel); }
function $$(sel) { return document.querySelectorAll(sel); }

function showToast(msg, type = 'success') {
  let toast = $('#toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'toast';
    toast.className = 'toast';
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.className = `toast ${type}`;
  setTimeout(() => toast.classList.add('show'), 10);
  setTimeout(() => toast.classList.remove('show'), 3000);
}

function fmtPrice(price, symbol) {
  if (price === null || price === undefined) return '-';
  if (symbol === 'GC=F') return price.toFixed(2);
  return price.toFixed(5);
}

function fmtScore(score) {
  if (score === null || score === undefined) return '0.00';
  const s = parseFloat(score);
  return (s >= 0 ? '+' : '') + s.toFixed(2);
}

function scoreClass(score) {
  if (score > 0) return 'pos';
  if (score < 0) return 'neg';
  return 'zero';
}

function trendClass(trend) {
  if (trend === 'BULLISH') return 'trend-bull';
  if (trend === 'BEARISH') return 'trend-bear';
  return 'trend-range';
}

function trendLabel(trend) {
  const map = {
    'BULLISH': 'صعودی',
    'BEARISH': 'نزولی',
    'RANGING': 'رنج',
    'UNCLEAR': 'نامشخص',
    'CONFLICT': 'تضاد',
    'NEUTRAL': 'خنثی',
  };
  return map[trend] || trend || '-';
}

function zoneLabel(zone) {
  if (zone === 'PREMIUM') return 'گرون';
  if (zone === 'DISCOUNT') return 'ارزون';
  return '-';
}

function timeAgo(isoStr) {
  try {
    const t = new Date(isoStr);
    const diff = (Date.now() - t.getTime()) / 1000;
    if (diff < 60) return 'همین الان';
    if (diff < 3600) return `${Math.floor(diff / 60)} دقیقه پیش`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} ساعت پیش`;
    return `${Math.floor(diff / 86400)} روز پیش`;
  } catch {
    return '-';
  }
}

/* ============================================================
   📊 Ticker
   ============================================================ */

async function loadPrices() {
  const el = $('#tickerContent');
  if (!el) return;

  try {
    const r = await fetch(`${API_BASE}/api/prices`);
    const data = await r.json();

    if (!data.data || data.data.length === 0) {
      el.innerHTML = '<div class="ticker-loading">قیمتی موجود نیست</div>';
      return;
    }

    renderTicker(data.data);
  } catch (e) {
    console.error('خطا در قیمت‌ها:', e);
  }
}

function renderTicker(prices) {
  const el = $('#tickerContent');
  if (!el) return;

  el.innerHTML = prices.map(p => {
    const flag = SYMBOL_FLAGS[p.symbol] || '💱';
    const up = p.direction === 'up';
    const arrow = up ? '▲' : '▼';
    const cls = up ? 'up' : 'down';
    const sign = p.change_pct >= 0 ? '+' : '';

    return `
      <div class="ticker-item" data-symbol="${p.symbol}">
        <span class="ticker-flag">${flag}</span>
        <span class="ticker-symbol">${p.name}</span>
        <span class="ticker-price">${p.price}</span>
        <span class="ticker-change ${cls}">${arrow} ${sign}${p.change_pct}%</span>
      </div>
    `;
  }).join('');

  el.querySelectorAll('.ticker-item').forEach(item => {
    item.addEventListener('click', () => {
      const symbol = item.dataset.symbol;
      const card = document.querySelector(`[data-card-symbol="${symbol}"]`);
      if (card) {
        card.scrollIntoView({ behavior: 'smooth', block: 'center' });
        card.style.transition = 'box-shadow 0.5s';
        card.style.boxShadow = '0 0 20px rgba(59, 130, 246, 0.5)';
        setTimeout(() => { card.style.boxShadow = ''; }, 1500);
      }
    });
  });
}

/* ============================================================
   Tabs
   ============================================================ */

function setupTabs() {
  $$('.tab').forEach(tab => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.tab;
      currentTab = target;

      $$('.tab').forEach(t => t.classList.remove('active'));
      $$('.tab-content').forEach(c => c.classList.remove('active'));

      tab.classList.add('active');
      $(`#tab-${target}`).classList.add('active');

      if (target === 'signals') loadSignals();
      if (target === 'stats') loadStats();
    });
  });
}

/* ============================================================
   Live Scan
   ============================================================ */

async function loadLive(showSpinner = true) {
  const loading = $('#loading');
  const cards = $('#cards');

  if (showSpinner && cards.children.length === 0) {
    loading.style.display = 'block';
  }

  try {
    const r = await fetch(`${API_BASE}/api/scan`);
    const data = await r.json();

    loading.style.display = 'none';

    if (data.status === 'waiting' || !data.data) {
      cards.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">⏳</div>
          <div class="empty-state-text">اولین اسکن در حال اجراست...</div>
        </div>`;
      return;
    }

    renderCards(data.data);
    renderScanInfo(data);

    const lastUpdate = $('#lastUpdate');
    if (lastUpdate) {
      lastUpdate.textContent = `بروزرسانی: ${new Date().toLocaleTimeString('fa-IR')}`;
    }
  } catch (e) {
    console.error('خطا در بارگذاری:', e);
    loading.style.display = 'none';
    if (cards.children.length === 0) {
      cards.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">⚠️</div>
          <div class="empty-state-text">خطا در دریافت داده</div>
        </div>`;
    }
  }
}

function renderScanInfo(data) {
  const el = $('#scanInfo');
  if (!el) return;
  const count = data.count || 0;
  el.innerHTML = `
    <span class="badge-live">زنده</span>
    <span>${count} جفت‌ارز در حال رصد</span>
  `;
}

function renderCards(scans) {
  const el = $('#cards');
  if (!scans || scans.length === 0) {
    el.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">📭</div>
        <div class="empty-state-text">داده‌ای موجود نیست</div>
      </div>`;
    return;
  }

  const sorted = [...scans].sort((a, b) => {
    const aHas = a.direction ? 1 : 0;
    const bHas = b.direction ? 1 : 0;
    if (aHas !== bHas) return bHas - aHas;
    return Math.abs(b.score || 0) - Math.abs(a.score || 0);
  });

  el.innerHTML = sorted.map(s => renderCard(s)).join('');
}

function renderCard(s) {
  const flag = SYMBOL_FLAGS[s.symbol] || '💱';
  const hasSignal = !!s.direction;

  let badge = '<span class="badge wait">⏳ انتظار</span>';
  let cardClass = '';
  if (s.direction === 'BUY') {
    badge = '<span class="badge buy">▲ خرید</span>';
    cardClass = 'signal-buy';
  } else if (s.direction === 'SELL') {
    badge = '<span class="badge sell">▼ فروش</span>';
    cardClass = 'signal-sell';
  }

  const trend = s.htf_trend || '-';
  const zone = s.zone || '-';
  const zoneClass = zone === 'PREMIUM' ? 'premium' : 'discount';

  let slTpBlock = '';
  if (hasSignal && s.suggested_sl && s.suggested_tp) {
    slTpBlock = `
      <div class="sl-tp-grid">
        <div class="sl-tp-box sl">
          <div class="sl-tp-label">🛑 حد ضرر</div>
          <div class="sl-tp-value">${fmtPrice(s.suggested_sl, s.symbol)}</div>
        </div>
        <div class="sl-tp-box tp">
          <div class="sl-tp-label">🎯 حد سود</div>
          <div class="sl-tp-value">${fmtPrice(s.suggested_tp, s.symbol)}</div>
        </div>
      </div>`;
  }

  // ✅ فیکس: reasons ممکنه آرایه یا رشته باشه
  let reasonsArr = [];
  if (Array.isArray(s.reasons)) {
    reasonsArr = s.reasons;
  } else if (typeof s.reasons === 'string') {
    reasonsArr = s.reasons.split('|').filter(Boolean);
  }

  let reasonsBlock = '';
  if (reasonsArr.length > 0) {
    reasonsBlock = `
      <div class="reasons">
        <div class="reasons-title">دلایل تحلیل:</div>
        <ul>
          ${reasonsArr.map(r => `<li>${r}</li>`).join('')}
        </ul>
      </div>`;
  }

  return `
    <div class="card ${cardClass}" data-card-symbol="${s.symbol}">
      <div class="card-head">
        <div class="card-symbol">
          <span class="symbol-flag">${flag}</span>
          <div>
            <div class="symbol-name">${s.name || s.symbol}</div>
            <div class="symbol-code">${s.symbol}</div>
          </div>
        </div>
        ${badge}
      </div>

      <div class="card-row">
        <span class="label">💰 قیمت فعلی</span>
        <span class="value">${fmtPrice(s.current_price, s.symbol)}</span>
      </div>

      <div class="card-row">
        <span class="label">📈 روند</span>
        <span class="${trendClass(trend)}">${trendLabel(trend)}</span>
      </div>

      <div class="card-row">
        <span class="label">📍 ناحیه</span>
        <span class="zone-badge ${zoneClass}">${zoneLabel(zone)}</span>
      </div>

      <div class="card-row">
        <span class="label">🎯 امتیاز</span>
        <span class="score-value ${scoreClass(s.score)}">${fmtScore(s.score)}</span>
      </div>

      ${slTpBlock}
      ${reasonsBlock}
    </div>
  `;
}

/* ============================================================
   Signals
   ============================================================ */

function setupFilters() {
  $$('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      $$('.filter-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentFilter = btn.dataset.filter;
      renderSignals();
    });
  });
}

async function loadSignals() {
  const el = $('#signalsList');
  el.innerHTML = `<div class="spinner" style="display:block"><p>در حال بارگذاری...</p></div>`;

  try {
    const r = await fetch(`${API_BASE}/api/signals`);
    allSignals = await r.json();
    renderSignals();
  } catch (e) {
    console.error('خطا در سیگنال‌ها:', e);
    el.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">⚠️</div>
        <div class="empty-state-text">خطا در دریافت سیگنال‌ها</div>
      </div>`;
  }
}

function renderSignals() {
  const el = $('#signalsList');

  let filtered = allSignals;
  if (currentFilter !== 'all') {
    filtered = allSignals.filter(s => s.status === currentFilter);
  }

  if (filtered.length === 0) {
    el.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">🎯</div>
        <div class="empty-state-text">
          ${currentFilter === 'all'
            ? 'هنوز سیگنالی ثبت نشده'
            : 'سیگنالی با این فیلتر وجود ندارد'}
        </div>
      </div>`;
    return;
  }

  el.innerHTML = filtered.map(s => renderSignalCard(s)).join('');
}

function renderSignalCard(s) {
  const flag = SYMBOL_FLAGS[s.symbol] || '💱';
  const direction = s.direction;

  let statusBadge = '<span class="badge wait">باز</span>';
  if (s.status === 'WIN') statusBadge = '<span class="badge win">✓ سود</span>';
  if (s.status === 'LOSS') statusBadge = '<span class="badge loss">✗ ضرر</span>';
  if (s.status === 'EXPIRED') statusBadge = '<span class="badge wait">⏰ منقضی</span>';

  const dirClass = direction === 'BUY' ? 'buy' : 'sell';
  const dirLabel = direction === 'BUY' ? '▲ خرید' : '▼ فروش';

  const pnlRow = s.pnl_pips !== null && s.pnl_pips !== undefined
    ? `<div class="card-row">
         <span class="label">📊 نتیجه</span>
         <span class="value ${s.pnl_pips >= 0 ? 'green' : 'red'}">${s.pnl_pips > 0 ? '+' : ''}${parseFloat(s.pnl_pips).toFixed(1)} پیپ</span>
       </div>` : '';

  // ✅ فیکس اصلی: reasons ممکنه آرایه باشه
  let reasonsArr = [];
  if (Array.isArray(s.reasons)) {
    reasonsArr = s.reasons;
  } else if (typeof s.reasons === 'string') {
    reasonsArr = s.reasons.split('|').filter(Boolean);
  }

  let reasons = '';
  if (reasonsArr.length > 0) {
    reasons = `<div class="reasons"><div class="reasons-title">دلایل:</div><ul>${reasonsArr.map(r => `<li>${r}</li>`).join('')}</ul></div>`;
  }

  return `
    <div class="card">
      <div class="card-head">
        <div class="card-symbol">
          <span class="symbol-flag">${flag}</span>
          <div>
            <div class="symbol-name">${s.name || s.symbol}</div>
            <span class="badge ${dirClass}" style="margin-top:4px">${dirLabel}</span>
          </div>
        </div>
        ${statusBadge}
      </div>

      <div class="card-row">
        <span class="label">💰 ورود</span>
        <span class="value">${fmtPrice(s.entry, s.symbol)}</span>
      </div>
      <div class="card-row">
        <span class="label">🛑 حد ضرر</span>
        <span class="value red">${fmtPrice(s.sl, s.symbol)}</span>
      </div>
      <div class="card-row">
        <span class="label">🎯 حد سود</span>
        <span class="value green">${fmtPrice(s.tp, s.symbol)}</span>
      </div>
      <div class="card-row">
        <span class="label">📊 امتیاز</span>
        <span class="value ${scoreClass(s.score)}">${fmtScore(s.score)}</span>
      </div>
      ${pnlRow}
      <div class="card-row">
        <span class="label">🕐 زمان</span>
        <span class="value">${timeAgo(s.created_at)}</span>
      </div>
      ${reasons}
    </div>
  `;
}

/* ============================================================
   Stats
   ============================================================ */

async function loadStats() {
  const el = $('#statsContent');
  el.innerHTML = `<div class="spinner" style="display:block"><p>در حال بارگذاری...</p></div>`;

  try {
    const r = await fetch(`${API_BASE}/api/stats`);
    const s = await r.json();
    renderStats(s);
  } catch (e) {
    console.error('خطا در آمار:', e);
    el.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">⚠️</div>
        <div class="empty-state-text">خطا در دریافت آمار</div>
      </div>`;
  }
}

function renderStats(s) {
  const el = $('#statsContent');

  const winRate = (s.win_rate || 0) * 100;
  const winRateGood = winRate >= (s.min_win_rate * 100);

  const totalProgress = Math.min(100, (s.total / s.min_trades) * 100);
  const winRateProgress = Math.min(100, (winRate / (s.min_win_rate * 100)) * 100);
  const pfProgress = Math.min(100, (s.profit_factor / s.min_profit_factor) * 100);

  const readyClass = s.ready_for_live ? 'yes' : 'no';
  const readyText = s.ready_for_live
    ? '✅ آماده برای حساب واقعی!'
    : '⏳ هنوز معیارها برآورده نشده';

  el.innerHTML = `
    <div class="stats-hero">
      <div class="stats-hero-label">وین ریت</div>
      <div class="stats-hero-value ${winRateGood ? 'good' : 'bad'}">
        ${winRate.toFixed(1)}%
      </div>
    </div>

    <div class="stats-grid">
      <div class="stat-box">
        <div class="stat-value accent">${s.total}</div>
        <div class="stat-label">کل معاملات</div>
      </div>
      <div class="stat-box">
        <div class="stat-value green">${s.wins}</div>
        <div class="stat-label">برد</div>
      </div>
      <div class="stat-box">
        <div class="stat-value red">${s.losses}</div>
        <div class="stat-label">باخت</div>
      </div>
      <div class="stat-box">
        <div class="stat-value ${s.profit_factor >= s.min_profit_factor ? 'green' : 'yellow'}">
          ${parseFloat(s.profit_factor).toFixed(2)}
        </div>
        <div class="stat-label">Profit Factor</div>
      </div>
      <div class="stat-box">
        <div class="stat-value ${s.total_pips >= 0 ? 'green' : 'red'}">
          ${s.total_pips > 0 ? '+' : ''}${s.total_pips}
        </div>
        <div class="stat-label">پیپ کل</div>
      </div>
      <div class="stat-box">
        <div class="stat-value accent">${s.open_signals}</div>
        <div class="stat-label">سیگنال باز</div>
      </div>
    </div>

    <div class="progress-section">
      <div class="progress-title">
        <span>پیشرفت معیارها</span>
        <span style="color:var(--muted);font-size:0.75rem">${s.test_duration_days} روز</span>
      </div>

      <div class="progress-row">
        <span>تعداد معاملات (${s.total}/${s.min_trades})</span>
        <span class="val">${totalProgress.toFixed(0)}%</span>
      </div>
      <div class="progress-bar-bg">
        <div class="progress-bar-fill ${totalProgress >= 100 ? 'good' : ''}" style="width:${totalProgress}%"></div>
      </div>

      <div class="progress-row">
        <span>وین ریت (${winRate.toFixed(1)}% / ${(s.min_win_rate * 100).toFixed(0)}%)</span>
        <span class="val">${winRateProgress.toFixed(0)}%</span>
      </div>
      <div class="progress-bar-bg">
        <div class="progress-bar-fill ${winRateProgress >= 100 ? 'good' : ''}" style="width:${winRateProgress}%"></div>
      </div>

      <div class="progress-row">
        <span>Profit Factor (${parseFloat(s.profit_factor).toFixed(2)} / ${s.min_profit_factor})</span>
        <span class="val">${pfProgress.toFixed(0)}%</span>
      </div>
      <div class="progress-bar-bg">
        <div class="progress-bar-fill ${pfProgress >= 100 ? 'good' : ''}" style="width:${pfProgress}%"></div>
      </div>
    </div>

    <div class="ready-badge ${readyClass}">
      ${readyText}
    </div>

    <div style="margin-top:20px;padding:14px;background:var(--card);border:1px solid var(--border);border-radius:var(--radius);font-size:0.75rem;color:var(--muted);line-height:2">
      <div>🎯 حداقل معاملات: <b style="color:var(--text)">${s.min_trades}</b></div>
      <div>🎯 حداقل وین ریت: <b style="color:var(--text)">${(s.min_win_rate * 100).toFixed(0)}%</b></div>
      <div>🎯 حداقل Profit Factor: <b style="color:var(--text)">${s.min_profit_factor}</b></div>
      <div>🎯 حداکثر دراودان: <b style="color:var(--text)">${(s.max_drawdown * 100).toFixed(0)}%</b></div>
    </div>
  `;
}

/* ============================================================
   Refresh
   ============================================================ */

function setupRefresh() {
  const btn = $('#refreshBtn');
  if (!btn) return;

  btn.addEventListener('click', async () => {
    btn.classList.add('spinning');
    showToast('در حال اسکن مجدد...', 'success');

    try {
      await fetch(`${API_BASE}/api/scan/force`, { method: 'POST' });
      await loadLive();
      await loadPrices();
      showToast('✅ اسکن انجام شد', 'success');
    } catch (e) {
      showToast('⚠️ خطا در اسکن', 'error');
    }

    setTimeout(() => btn.classList.remove('spinning'), 800);
  });
}

/* ============================================================
   Auto Refresh
   ============================================================ */

function startAutoRefresh() {
  if (refreshInterval) clearInterval(refreshInterval);
  refreshInterval = setInterval(() => {
    if (currentTab === 'live') loadLive(false);
    else if (currentTab === 'signals') loadSignals();
    else if (currentTab === 'stats') loadStats();
  }, 60000);
}

function startPricesRefresh() {
  if (pricesInterval) clearInterval(pricesInterval);
  pricesInterval = setInterval(loadPrices, 30000);
}

/* ============================================================
   Init
   ============================================================ */

function init() {
  console.log('🚀 Sina Quant - شروع');
  setupTabs();
  setupFilters();
  setupRefresh();
  loadPrices();
  loadLive();
  startAutoRefresh();
  startPricesRefresh();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
