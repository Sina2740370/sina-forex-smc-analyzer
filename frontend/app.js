/* ============================================================
   Sina Quant - Frontend Logic v3.1
   - دکمه رفرش دیگه /api/scan/force رو صدا نمی‌زنه (بدون کلید ادمین)
   - خروجی سرور escape می‌شه
   - تجزیه امتیاز (تکنیکال / فاندامنتال / خبر / حافظه) نمایش داده می‌شه
   ============================================================ */

const API_BASE = '';
const FETCH_TIMEOUT = 60000;      // 60 ثانیه
const LIVE_REFRESH_MS = 60000;    // رفرش خودکار اسکن
const PRICES_REFRESH_MS = 30000;  // رفرش خودکار قیمت‌ها

let currentTab = 'live';
let currentFilter = 'all';
let allSignals = [];
let refreshInterval = null;
let pricesInterval = null;
let liveLoading = false;   // جلوگیری از درخواست‌های هم‌زمان
let liveRetries = 0;       // تلاش مجدد وقتی سرور Render خوابه

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

// escape برای هر متنی که از سرور میاد و تو innerHTML می‌ره
function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// fetch با timeout
async function fetchWithTimeout(url, options = {}, timeout = FETCH_TIMEOUT) {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    clearTimeout(id);
    return response;
  } catch (e) {
    clearTimeout(id);
    throw e;
  }
}

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
  const p = Number(price);
  if (Number.isNaN(p)) return '-';
  if (symbol === 'GC=F') return p.toFixed(2);
  return p.toFixed(5);
}

function fmtScore(score) {
  if (score === null || score === undefined) return '0.00';
  const s = parseFloat(score);
  if (Number.isNaN(s)) return '0.00';
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
    if (Number.isNaN(diff)) return '-';
    if (diff < 60) return 'همین الان';
    if (diff < 3600) return `${Math.floor(diff / 60)} دقیقه پیش`;
    if (diff < 86400) return `${Math.floor(diff / 3600)} ساعت پیش`;
    return `${Math.floor(diff / 86400)} روز پیش`;
  } catch {
    return '-';
  }
}

function toReasonsArray(reasons) {
  if (Array.isArray(reasons)) return reasons;
  if (typeof reasons === 'string') return reasons.split('|').filter(Boolean);
  return [];
}

/* ============================================================
   Ticker
   ============================================================ */

async function loadPrices() {
  const el = $('#tickerContent');
  if (!el) return;

  try {
    const r = await fetchWithTimeout(`${API_BASE}/api/prices`, {}, 30000);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();

    if (!data.data || data.data.length === 0) {
      el.innerHTML = '<div class="ticker-loading">قیمتی موجود نیست</div>';
      return;
    }

    renderTicker(data.data);
  } catch (e) {
    console.error('خطا در قیمت‌ها:', e);
    // اگه قبلاً داده داشتیم، پاک نکن
    if (el.children.length === 0 || el.querySelector('.ticker-loading')) {
      el.innerHTML = '<div class="ticker-loading">خطا در دریافت قیمت</div>';
    }
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
      <div class="ticker-item" data-symbol="${esc(p.symbol)}">
        <span class="ticker-flag">${flag}</span>
        <span class="ticker-symbol">${esc(p.name)}</span>
        <span class="ticker-price">${esc(p.price)}</span>
        <span class="ticker-change ${cls}">${arrow} ${sign}${esc(p.change_pct)}%</span>
      </div>
    `;
  }).join('');

  el.querySelectorAll('.ticker-item').forEach(item => {
    item.addEventListener('click', () => {
      const symbol = item.dataset.symbol;
      const card = Array.from($$('[data-card-symbol]'))
        .find(c => c.dataset.cardSymbol === symbol);
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
      const content = $(`#tab-${target}`);
      if (content) content.classList.add('active');

      if (target === 'signals') loadSignals();
      if (target === 'stats') loadStats();
    });
  });
}

/* ============================================================
   Live Scan
   ============================================================ */

function scheduleLiveRetry() {
  if (liveRetries >= 6) return;
  liveRetries += 1;
  setTimeout(() => loadLive(false), 8000);
}

async function loadLive(showSpinner = true) {
  const loading = $('#loading');
  const cards = $('#cards');
  if (!cards) return;
  if (liveLoading) return;
  liveLoading = true;

  if (showSpinner && loading && cards.children.length === 0) {
    loading.style.display = 'block';
  }

  try {
    const r = await fetchWithTimeout(`${API_BASE}/api/scan`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();

    if (loading) loading.style.display = 'none';

    if (data.status === 'waiting' || !data.data) {
      cards.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">⏳</div>
          <div class="empty-state-text">اولین اسکن در حال اجراست...</div>
        </div>`;
      scheduleLiveRetry();
      return;
    }

    liveRetries = 0;
    renderCards(data.data);
    renderScanInfo(data);

    const lastUpdate = $('#lastUpdate');
    if (lastUpdate) {
      lastUpdate.textContent = `بروزرسانی: ${new Date().toLocaleTimeString('fa-IR')}`;
    }
  } catch (e) {
    console.error('خطا در بارگذاری:', e);
    if (loading) loading.style.display = 'none';

    // فقط اگه واقعاً خالی بود، خطا نشون بده
    if (cards.children.length === 0) {
      cards.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">⏳</div>
          <div class="empty-state-text">
            سرور در حال بیدار شدن...<br>
            <small>چند لحظه صبر کن و Refresh کن</small>
          </div>
        </div>`;
      scheduleLiveRetry();
    }
  } finally {
    liveLoading = false;
  }
}

function renderScanInfo(data) {
  const el = $('#scanInfo');
  if (!el) return;
  const count = data.count || 0;

  // جدیدترین زمان اسکن بین همه جفت‌ارزها
  let latest = null;
  (data.data || []).forEach(s => {
    const t = s.scanned_at ? new Date(s.scanned_at).getTime() : NaN;
    if (!Number.isNaN(t) && (latest === null || t > latest)) latest = t;
  });
  const ago = latest ? ` · آخرین اسکن ${timeAgo(new Date(latest).toISOString())}` : '';

  el.innerHTML = `
    <span class="badge-live">زنده</span>
    <span>${count} جفت‌ارز در حال رصد${ago}</span>
  `;
}

function renderCards(scans) {
  const el = $('#cards');
  if (!el) return;

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

// تجزیه امتیاز: تکنیکال / فاندامنتال / خبر / حافظه (فقط اگه سرور فرستاده باشه)
function renderScoreBreakdown(s) {
  const parts = [
    ['تکنیکال', s.technical_score],
    ['فاندامنتال', s.fundamental_score],
    ['خبر', s.news_score],
    ['حافظه', s.memory_score],
  ].filter(([, v]) => v !== undefined && v !== null);

  if (parts.length === 0) return '';

  const text = parts.map(([label, v]) => `${label} ${fmtScore(v)}`).join(' · ');
  return `
    <div class="card-row">
      <span class="label">🧮 تجزیه امتیاز</span>
      <span class="value" style="font-size:0.75rem">${esc(text)}</span>
    </div>`;
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

  const reasonsArr = toReasonsArray(s.reasons);
  let reasonsBlock = '';
  if (reasonsArr.length > 0) {
    reasonsBlock = `
      <div class="reasons">
        <div class="reasons-title">دلایل تحلیل:</div>
        <ul>
          ${reasonsArr.map(r => `<li>${esc(r)}</li>`).join('')}
        </ul>
      </div>`;
  }

  const scannedRow = s.scanned_at ? `
      <div class="card-row">
        <span class="label">🕐 زمان اسکن</span>
        <span class="value">${timeAgo(s.scanned_at)}</span>
      </div>` : '';

  return `
    <div class="card ${cardClass}" data-card-symbol="${esc(s.symbol)}">
      <div class="card-head">
        <div class="card-symbol">
          <span class="symbol-flag">${flag}</span>
          <div>
            <div class="symbol-name">${esc(s.name || s.symbol)}</div>
            <div class="symbol-code">${esc(s.symbol)}</div>
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

      ${renderScoreBreakdown(s)}
      ${scannedRow}
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
  if (!el) return;
  el.innerHTML = `<div class="spinner" style="display:block"><p>در حال بارگذاری...</p></div>`;

  try {
    const r = await fetchWithTimeout(`${API_BASE}/api/signals?limit=100`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    allSignals = Array.isArray(data) ? data : [];
    renderSignals();
  } catch (e) {
    console.error('خطا در سیگنال‌ها:', e);
    el.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">⏳</div>
        <div class="empty-state-text">سرور در حال بیدار شدن...<br><small>دوباره تلاش کن</small></div>
      </div>`;
  }
}

function renderSignals() {
  const el = $('#signalsList');
  if (!el) return;

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

  const reasonsArr = toReasonsArray(s.reasons);
  let reasons = '';
  if (reasonsArr.length > 0) {
    reasons = `<div class="reasons"><div class="reasons-title">دلایل:</div><ul>${reasonsArr.map(r => `<li>${esc(r)}</li>`).join('')}</ul></div>`;
  }

  return `
    <div class="card">
      <div class="card-head">
        <div class="card-symbol">
          <span class="symbol-flag">${flag}</span>
          <div>
            <div class="symbol-name">${esc(s.name || s.symbol)}</div>
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
  if (!el) return;
  el.innerHTML = `<div class="spinner" style="display:block"><p>در حال بارگذاری...</p></div>`;

  try {
    const r = await fetchWithTimeout(`${API_BASE}/api/stats`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const s = await r.json();
    renderStats(s);
  } catch (e) {
    console.error('خطا در آمار:', e);
    el.innerHTML = `
      <div class="empty-state">
        <div class="empty-state-icon">⏳</div>
        <div class="empty-state-text">سرور در حال بیدار شدن...<br><small>دوباره تلاش کن</small></div>
      </div>`;
  }
}

function renderStats(s) {
  const el = $('#statsContent');
  if (!el) return;

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
   Refresh (دستی): فقط داده جدید رو می‌خونه، اسکن اجباری نمی‌زنه
   ============================================================ */

function setupRefresh() {
  const btn = $('#refreshBtn');
  if (!btn) return;

  btn.addEventListener('click', async () => {
    btn.classList.add('spinning');
    showToast('در حال بروزرسانی...', 'success');

    try {
      await loadLive();
      await loadPrices();
      showToast('✅ بروزرسانی شد', 'success');
    } catch (e) {
      showToast('⚠️ خطا در بروزرسانی', 'error');
    }

    setTimeout(() => btn.classList.remove('spinning'), 800);
  });
}

/* ============================================================
   Auto Refresh
   ============================================================ */

function stopAutoRefresh() {
  if (refreshInterval) clearInterval(refreshInterval);
  if (pricesInterval) clearInterval(pricesInterval);
  refreshInterval = null;
  pricesInterval = null;
}

function startAutoRefresh() {
  stopAutoRefresh();

  refreshInterval = setInterval(() => {
    if (document.hidden) return;
    if (currentTab === 'live') loadLive(false);
  }, LIVE_REFRESH_MS);

  pricesInterval = setInterval(() => {
    if (document.hidden) return;
    loadPrices();
  }, PRICES_REFRESH_MS);
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  loadPrices();
  if (currentTab === 'live') loadLive(false);
});

/* ============================================================
   Init
   ============================================================ */

function init() {
  setupTabs();
  setupFilters();
  setupRefresh();

  loadLive();
  loadPrices();
  startAutoRefresh();

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
