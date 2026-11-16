/* PIXEL TRADING FLOOR — [고래·일정] 탭
   왼쪽: 이번 주 미국 주요 경제 일정 · 오른쪽: 실시간 TRC20 고래 입출금.

   1) 실시간 고래 입출금 (TRC20 USDT 50만 달러 이상 · 30초 갱신)
      · 이체 목록 — TronGrid 공개 API(api.trongrid.io, 키 없이 1초에 1회)
        /v1/contracts/{USDT}/events?event_name=Transfer&limit=200 — 한 번에 200건을 주므로
        TronScan(한 번에 50건)보다 4배 넓게 훑는다. 주소는 hex로 오므로 Base58(T...)로 바꿔 쓴다.
      · 거래소 이름표 — TronScan 공개 API(/api/token_trc20/transfers)를 90초에 한 장씩 받아
        '주소 → Binance-Hot 16, Bybit, Bitfinex…' 표를 쌓아 둔다. TronScan은 익명 호출을 자주 막아(429)
        실패하면 4분 쉬었다 다시 시도하고, 그동안에도 목록은 TronGrid로 계속 흐른다.
      · 50건·200건은 각각 1~3초 분량이라 한 번에 흐름 전체를 볼 수는 없다. 그래서 훑은 것 중
        50만 USDT 이상만 골라 최근 3시간·26건까지 쌓아 두고, 화면의 합계·순유입은 '관측된 것' 기준이라고 밝힌다.
      · 행을 누르면 tronscan.org 의 그 거래로 새 탭이 열린다.
   2) 이번 주 경제 일정
      · data/calendar.json — scripts/update-global.py가 ForexFactory 주간 캘린더에서 미국 High·Medium만
        골라 KST·한국어로 담아 둔 파일(GitHub Actions가 갱신). 사이트가 CORS를 열지 않아 브라우저가 직접 못 받는다.
      · 화면에서는 지금 시각과 비교해 오늘 강조 · 다가오는 일정 카운트다운 · 지난 일정 흐리게(완료)를 스스로 갱신한다.
   실주문·투자 조언 아님. 값이 없으면 '—'로 두고 지어내지 않는다. */
(function () {
  'use strict';

  const $ = (selector, root = document) => (root || document).querySelector(selector);
  const $$ = (selector, root = document) => [...(root || document).querySelectorAll(selector)];
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  const GRID = 'https://api.trongrid.io/v1/contracts/TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t/events';
  const SCAN = 'https://apilist.tronscanapi.com/api/token_trc20/transfers';
  const CONTRACT = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t';   // Tether USD (TRC20)
  const EXPLORER = 'https://tronscan.org/#/transaction/';
  const CAL_URL = 'data/calendar.json';
  const GLOBAL_URL = 'data/global.json';

  const MIN_USDT = 500000;          // 50만 USDT 이상만 '고래'
  const POLL_MS = 30000;            // 30초마다 새로 훑는다
  const DEEP_PAGES = 6;             // 처음 열 때는 200건×6장(≈18초 분량)까지 거슬러 올라간다
  const GRID_GAP_MS = 1100;         // TronGrid는 키 없이 1초에 1회 — 장 사이를 띄운다
  const TAG_MS = 90000;             // 거래소 이름표 갱신 간격
  const TAG_COOL_MS = 240000;       // TronScan이 429를 내면 이만큼 쉰다
  const MAX_ROWS = 26;              // 화면에 남기는 최대 건수
  const KEEP_MS = 3 * 3600 * 1000;  // 3시간 지난 이체는 버린다
  const STAT_MS = 15 * 60000;       // 요약(합계·순유입)은 최근 15분 관측분
  const AGE_MS = 5000;
  const CLOCK_MS = 1000;
  const NEXT_MS = 10000;
  const FX_MS = 5 * 60000;

  const WEEKDAY = ['일', '월', '화', '수', '목', '금', '토'];
  const B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
  const TAG_KEY = 'pixel-whale-tags-v1';   // 주소 → 거래소 이름표 (다음 방문에도 쓰게 브라우저에 남긴다)
  const TAG_MAX = 400;

  // 태그에 들어 있는 거래소·서비스 이름 → 화면에 쓸 짧은 이름 (긴 것부터 확인)
  const TAG_KEYS = [
    ['binance.us', 'Binance.US'], ['binance', 'Binance'], ['bybit', 'Bybit'], ['okx', 'OKX'],
    ['huobi', 'HTX'], ['htx', 'HTX'], ['bitfinex', 'Bitfinex'], ['kraken', 'Kraken'], ['kucoin', 'KuCoin'],
    ['gate', 'Gate'], ['mexc', 'MEXC'], ['bitget', 'Bitget'], ['coinbase', 'Coinbase'],
    ['crypto.com', 'Crypto.com'], ['upbit', 'Upbit'], ['bithumb', 'Bithumb'], ['poloniex', 'Poloniex'],
    ['bitstamp', 'Bitstamp'], ['gemini', 'Gemini'], ['bitmart', 'BitMart'], ['lbank', 'LBank'],
    ['bitrue', 'Bitrue'], ['phemex', 'Phemex'], ['coinex', 'CoinEx'], ['deribit', 'Deribit'],
    ['bitvavo', 'Bitvavo'], ['wintermute', 'Wintermute'], ['tether', 'Tether'], ['tronlink', 'TronLink'],
    ['justlend', 'JustLend'], ['sunswap', 'SunSwap'], ['fireblocks', 'Fireblocks'], ['circle', 'Circle'],
  ];

  const state = {
    active: false, built: false,
    rows: new Map(), order: [], tags: new Map(),
    fetching: false, error: '', lastOkAt: 0, nextAt: 0, tagAt: 0,
    calendar: null, calError: '', calAt: 0, stale: false,
    fx: null, fxAt: 0,
    clockTimer: 0, ageTimer: 0, pollTimer: 0, nextTimer: 0, fxTimer: 0,
  };

  // 태그 표 저장/복원 — 한 번 배운 이름표는 다음 방문에도 그대로 쓴다
  function loadTags() {
    try {
      const raw = localStorage.getItem(TAG_KEY);
      if (!raw) return;
      const obj = JSON.parse(raw);
      for (const [addr, name] of Object.entries(obj || {})) {
        if (typeof addr === 'string' && typeof name === 'string') state.tags.set(addr, name);
      }
    } catch (e) { /* 저장된 값이 없거나 깨졌으면 그냥 새로 배운다 */ }
  }

  function saveTags() {
    try {
      const obj = {};
      let n = 0;
      for (const [addr, name] of state.tags) {
        obj[addr] = name;
        if (++n >= TAG_MAX) break;
      }
      localStorage.setItem(TAG_KEY, JSON.stringify(obj));
    } catch (e) { /* 저장 못 해도 화면은 그대로 */ }
  }

  /* ---------------------------------------------------------------- 숫자·시간 표기 */

  function ageText(ts) {
    const s = Math.max(0, Math.floor((Date.now() - ts) / 1000));
    if (s < 5) return '방금';
    if (s < 60) return `${s}초 전`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}분 전`;
    const h = Math.floor(m / 60);
    return h < 24 ? `${h}시간 ${m % 60}분 전` : `${Math.floor(h / 24)}일 전`;
  }

  function usdText(v) {
    if (!Number.isFinite(v)) return '—';
    if (v >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
    if (v >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
    if (v >= 1e3) return `$${Math.round(v / 1e3)}K`;
    return `$${Math.round(v)}`;
  }

  // 원화는 한국식으로 끊는다 — 7억 8,757만원
  function krwText(usd, rate) {
    if (!Number.isFinite(usd) || !Number.isFinite(rate) || rate <= 0) return '';
    const krw = usd * rate;
    if (krw >= 1e8) {
      let eok = Math.floor(krw / 1e8);
      let man = Math.round((krw - eok * 1e8) / 1e4);
      if (man >= 10000) { eok += 1; man -= 10000; }
      return man ? `${eok}억 ${man.toLocaleString('en-US')}만원` : `${eok}억원`;
    }
    const man = Math.round(krw / 1e4);
    if (man >= 1) return `${man.toLocaleString('en-US')}만원`;
    return `${Math.round(krw).toLocaleString('en-US')}원`;
  }

  function untilText(ms) {
    if (!Number.isFinite(ms) || ms <= 0) return '완료';
    const s = Math.floor(ms / 1000);
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d) return `${d}일 ${h}시간 뒤`;
    if (h) return `${h}시간 ${m}분 뒤`;
    if (m) return `${m}분 뒤`;
    return `${s}초 뒤`;
  }

  function clockText(d = new Date()) {
    try {
      return new Intl.DateTimeFormat('ko-KR', {
        timeZone: 'Asia/Seoul', hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit',
      }).format(d);
    } catch (e) {
      return d.toISOString().slice(11, 19);
    }
  }

  function kstTodayKey(d = new Date()) {
    try {
      return new Intl.DateTimeFormat('en-CA', {
        timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit',
      }).format(d);
    } catch (e) {
      return new Date(d.getTime() + 9 * 3600 * 1000).toISOString().slice(0, 10);
    }
  }

  function dayLabel(key) {
    const [y, m, d] = String(key).split('-').map(Number);
    if (!y || !m || !d) return key;
    const wd = WEEKDAY[new Date(Date.UTC(y, m - 1, d)).getUTCDay()];
    return `${m}월 ${d}일 (${wd})`;
  }

  function shortAddr(addr) {
    const a = String(addr || '');
    return a.length > 10 ? `${a.slice(0, 4)}…${a.slice(-3)}` : a;
  }

  /* ---------------------------------------------------------------- Tron 주소(hex → Base58) */

  async function sha256(bytes) {
    const buf = await crypto.subtle.digest('SHA-256', bytes);
    return new Uint8Array(buf);
  }

  // TronGrid는 주소를 0x…(20바이트)로 준다 — 화면과 tronscan 링크는 T…(Base58)를 쓴다
  async function tronAddress(hex) {
    const raw = String(hex || '').replace(/^0x/i, '');
    if (raw.length !== 40) return '';
    if (!(window.crypto && crypto.subtle && crypto.subtle.digest)) return '';
    const payload = new Uint8Array(21);
    payload[0] = 0x41;                       // Tron 주소 앞자리 0x41
    for (let i = 0; i < 20; i += 1) payload[i + 1] = parseInt(raw.slice(i * 2, i * 2 + 2), 16);
    const checksum = (await sha256(await sha256(payload))).slice(0, 4);
    const buf = new Uint8Array(25);
    buf.set(payload, 0);
    buf.set(checksum, 21);
    const digits = [];
    for (const byte of buf) {
      let carry = byte;
      for (let i = 0; i < digits.length; i += 1) {
        carry += digits[i] * 256;
        digits[i] = carry % 58;
        carry = Math.floor(carry / 58);
      }
      while (carry > 0) { digits.push(carry % 58); carry = Math.floor(carry / 58); }
    }
    let out = '';
    for (const byte of buf) { if (byte === 0) out += '1'; else break; }
    for (let i = digits.length - 1; i >= 0; i -= 1) out += B58[digits[i]];
    return out;
  }

  /* ---------------------------------------------------------------- 주소 → 이름표 */

  function party(addr) {
    if (!addr) return { kind: 'person', name: '알 수 없음', detail: '' };
    const raw = String(state.tags.get(addr) || '').trim();
    if (raw) {
      const lower = raw.toLowerCase();
      const hit = TAG_KEYS.find(([key]) => lower.includes(key));
      if (hit) {
        let detail = raw.replace(new RegExp(hit[0].replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i'), '');
        detail = detail.replace(/^[\s\-:·|#]+/, '').replace(/\s+/g, ' ').trim();
        return { kind: 'ex', name: hit[1], detail: detail.slice(0, 12) };
      }
      return { kind: 'svc', name: raw.slice(0, 14), detail: '' };
    }
    return { kind: 'person', name: '개인지갑', detail: shortAddr(addr) };
  }

  /* ---------------------------------------------------------------- 환율 */

  // 글로벌 시황이 이미 받아 둔 원/달러를 먼저 쓰고, 없으면 data/global.json 을 직접 읽는다.
  function usdKrw() {
    const g = window.LiteGlobal;
    if (g && typeof g.rows === 'function') {
      try {
        const row = g.rows().find((r) => r && r.key === 'fx');
        const v = Number(row && row.value);
        if (Number.isFinite(v) && v > 500 && v < 5000) return v;
      } catch (e) { /* 글로벌 시황이 없으면 아래로 */ }
    }
    return Number.isFinite(state.fx) && state.fx > 500 ? state.fx : null;
  }

  async function loadFx() {
    try {
      const res = await fetch(`${GLOBAL_URL}?_=${Date.now()}`, { cache: 'no-store', credentials: 'omit' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      const v = Number(json && json.crypto && json.crypto.usdKrw);
      if (Number.isFinite(v) && v > 500 && v < 5000) { state.fx = v; state.fxAt = Date.now(); }
    } catch (e) { /* 환율이 없으면 원화 칸을 비워 둔다 — 지어내지 않는다 */ } finally {
      paintAges();
    }
  }

  /* ---------------------------------------------------------------- 화면 뼈대 */

  function build() {
    if (state.built) return;
    state.built = true;

    const page = document.createElement('section');
    page.id = 'whale-page';
    page.setAttribute('aria-label', '고래 입출금과 경제 일정');
    page.innerHTML = `
      <header class="wh-head">
        <div class="wh-title"><b>◆ 고래·일정 ◆</b><span>실시간 TRC20 고래 입출금 · 이번 주 미국 경제 일정</span></div>
        <div class="wh-clock-wrap">
          <span class="wh-live" id="wh-live"><i></i><span id="wh-live-text">LIVE</span></span>
          <span class="wh-clock" id="wh-clock">--:--:--</span>
        </div>
      </header>
      <div class="wh-grid">
        <section class="wh-panel wh-cal" aria-label="이번 주 경제 일정">
          <div class="wh-p-head">
            <h2>▣ 이번 주 경제 일정</h2>
            <span class="wh-chip" id="wh-cal-chip">—</span>
          </div>
          <p class="wh-p-sub">미국 주요 지표 · 한국 시간(KST) · 중요도 ★★ 이상</p>
          <div class="wh-legend">
            <span class="st high">★★★</span> 매우 중요
            <span class="st medium">★★</span> 중요
          </div>
          <div class="wh-next" id="wh-cal-next">다가오는 일정을 계산하는 중…</div>
          <div class="wh-cal-body" id="wh-cal-body"><div class="wh-msg">이번 주 일정을 불러오는 중…</div></div>
          <div class="wh-p-foot" id="wh-cal-foot">출처 ForexFactory · 한국 시간(KST)</div>
        </section>
        <section class="wh-panel wh-whales" aria-label="실시간 고래 입출금">
          <div class="wh-p-head">
            <h2>◈ 실시간 고래 입출금</h2>
            <span class="wh-chip live"><i></i>TRON LIVE</span>
          </div>
          <p class="wh-p-sub">TRC20 USDT · 50만 달러 이상 · 30초마다 갱신</p>
          <div class="wh-wstat" id="wh-wstat"><span>고래 이체를 찾는 중…</span></div>
          <div class="wh-rows" id="wh-rows"><div class="wh-msg">50만 달러 이상 이체를 찾는 중…</div></div>
          <div class="wh-p-foot" id="wh-wfoot">
            <span>TronGrid·TronScan 공개 API · 관측된 이체만 표시</span>
            <a href="https://tronscan.org/#/tokens/TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t" target="_blank" rel="noopener noreferrer">트론스캔에서 보기 ↗</a>
          </div>
        </section>
      </div>`;
    document.body.appendChild(page);

    page.addEventListener('click', (event) => {
      const button = event.target.closest('[data-act]');
      if (!button) return;
      const act = button.dataset.act;
      if (act === 'retry-cal') loadCalendar(true);
      if (act === 'retry-whale') poll(true);
    });
  }

  /* ---------------------------------------------------------------- 경제 일정 */

  async function loadCalendar(force) {
    if (state.calAt && !force && Date.now() - state.calAt < 10 * 60000) return;
    try {
      const res = await fetch(`${CAL_URL}?_=${Date.now()}`, { cache: 'no-store', credentials: 'omit' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const json = await res.json();
      if (!json || !Array.isArray(json.events)) throw new Error('일정 형식이 아닙니다');
      state.calendar = json;
      state.calError = '';
      state.calAt = Date.now();
    } catch (err) {
      state.calError = String((err && err.message) || err);
      state.calAt = Date.now();
    }
    renderCalendar();
  }

  function renderCalendar() {
    const body = $('#wh-cal-body');
    if (!body) return;
    const chip = $('#wh-cal-chip');
    const foot = $('#wh-cal-foot');
    const cal = state.calendar;

    if (!cal) {
      if (state.calError) {
        body.innerHTML = `<div class="wh-msg err"><b>일정표를 불러오지 못했습니다.</b><br>${esc(state.calError)}<br><button type="button" data-act="retry-cal">다시 시도</button></div>`;
        if (chip) { chip.textContent = '불러오기 실패'; chip.className = 'wh-chip warn'; }
      }
      return;
    }

    const today = kstTodayKey();
    const events = cal.events.slice().sort((a, b) => (a.atMs || 0) - (b.atMs || 0));
    const stale = !(String(cal.weekStart) <= today && today <= String(cal.weekEnd));
    state.stale = stale;

    const groups = [];
    let current = null;
    for (const ev of events) {
      if (!current || current.date !== ev.date) { current = { date: ev.date, items: [] }; groups.push(current); }
      current.items.push(ev);
    }

    const now = Date.now();
    const nextEvent = events.find((ev) => (ev.atMs || 0) > now) || null;

    if (chip) {
      if (stale) {
        chip.textContent = `지난 주 일정표 (${String(cal.weekStart).slice(5)}~${String(cal.weekEnd).slice(5)})`;
        chip.className = 'wh-chip warn';
      } else {
        chip.textContent = `오늘 ${dayLabel(today)}`;
        chip.className = 'wh-chip on';
      }
    }

    body.innerHTML = groups.map((group) => {
      const isToday = group.date === today;
      const rows = group.items.map((ev) => {
        const at = Number(ev.atMs) || Date.parse(ev.at) || 0;
        const past = at <= now;
        const isNext = nextEvent && ev === nextEvent;
        const bits = [];
        if (ev.forecast) bits.push(`<i>예상</i> ${esc(ev.forecast)}`);
        if (ev.previous) bits.push(`<i>이전</i> ${esc(ev.previous)}`);
        const stars = ev.impact === 'high' ? '★★★' : '★★';
        return `<div class="wh-ev${past ? ' is-past' : ''}${isNext ? ' is-next' : ''}" data-at="${at}" title="${esc(ev.titleEn || ev.title)}">
          <span class="wh-time">${esc(ev.time)}</span>
          <span class="wh-stars ${ev.impact === 'high' ? 'high' : 'medium'}">${stars}</span>
          <span class="wh-ev-main">
            <span class="wh-ev-title">${esc(ev.title)}</span>
            ${bits.length ? `<span class="wh-ev-sub">${bits.join(' · ')}</span>` : ''}
          </span>
          <span class="wh-when ${past ? 'done' : 'soon'}">${past ? '완료' : untilText(at - now)}</span>
        </div>`;
      }).join('');
      return `<div class="wh-day${isToday ? ' is-today' : ''}" data-date="${esc(group.date)}">
        <div class="wh-day-head${isToday ? ' today' : ''}">
          <span>${dayLabel(group.date)}</span>
          ${isToday ? '<span class="today-tag">오늘</span>' : ''}
          <span class="cnt">${group.items.length}건</span>
        </div>
        ${rows}
      </div>`;
    }).join('') || '<div class="wh-msg">이번 주에 잡힌 미국 주요 지표가 없습니다.</div>';

    if (foot) {
      const stamp = cal.updatedAt ? String(cal.updatedAt).replace('T', ' ').slice(5, 16) : '—';
      foot.innerHTML = `<span>출처 ForexFactory · 주차 ${esc(String(cal.weekStart).slice(5))}~${esc(String(cal.weekEnd).slice(5))} · 갱신 ${esc(stamp)}</span>`
        + (stale ? '<span style="color:#ffcf6b">이번 주가 아닙니다 — 일정표를 새로 받아야 합니다</span>' : '');
    }
    paintNext();
  }

  // 다가오는 일정 띠 + 각 줄의 남은 시간 (10초마다)
  function paintNext() {
    const box = $('#wh-cal-next');
    if (!box) return;
    const cal = state.calendar;
    if (!cal || !Array.isArray(cal.events) || !cal.events.length) {
      box.className = 'wh-next empty';
      box.textContent = cal ? '이번 주 남은 일정이 없습니다.' : '일정표를 불러오는 중…';
      return;
    }
    const now = Date.now();
    const events = cal.events.slice().sort((a, b) => (a.atMs || 0) - (b.atMs || 0));
    const next = events.find((ev) => (ev.atMs || 0) > now);
    if (!next) {
      box.className = 'wh-next empty';
      box.textContent = '이번 주 일정은 모두 지났습니다.';
      return;
    }
    box.className = 'wh-next';
    const stars = next.impact === 'high' ? '★★★' : '★★';
    box.innerHTML = '<span class="k">다음 일정</span>'
      + `<span class="wh-stars ${next.impact === 'high' ? 'high' : 'medium'}">${stars}</span>`
      + `<b>${esc(next.title)}</b><span>${esc(next.time)} KST · ${dayLabel(next.date)}</span>`
      + `<span class="cd">${untilText(Number(next.atMs) - now)}</span>`;

    for (const row of $$('.wh-ev', $('#wh-cal-body') || document)) {
      const at = Number(row.dataset.at) || 0;
      const when = $('.wh-when', row);
      if (!when) continue;
      const past = at <= now;
      when.textContent = past ? '완료' : untilText(at - now);
      when.className = `wh-when ${past ? 'done' : (at - now < 30 * 60000 ? 'soon' : '')}`.trim();
      row.classList.toggle('is-past', past);
    }
  }

  /* ---------------------------------------------------------------- 고래 입출금 */

  function keep(id, ts, usd, fromAddr, toAddr) {
    if (!id || !Number.isFinite(usd) || usd < MIN_USDT) return false;
    if (state.rows.has(id)) return false;
    if (Date.now() - ts > KEEP_MS) return false;
    state.rows.set(id, { id, ts, usd, from: fromAddr, to: toAddr, fresh: true });
    state.order.push(id);
    return true;
  }

  // TronGrid: 200건 · result{from,to,2=수량} hex 주소
  async function ingestGrid(events) {
    let added = 0;
    for (const ev of events) {
      const res = ev && ev.result;
      if (!res) continue;
      const usd = Number(res['2']) / 1e6;
      if (!Number.isFinite(usd) || usd < MIN_USDT) continue;
      if (state.rows.has(ev.transaction_id)) continue;
      const from = await tronAddress(res.from);
      const to = await tronAddress(res.to);
      if (keep(ev.transaction_id, Number(ev.block_timestamp) || Date.now(), usd, from || String(res.from || ''), to || String(res.to || ''))) added += 1;
    }
    return added;
  }

  // TronScan: 50건 · base58 주소 + 거래소 이름표
  function ingestScan(list) {
    let added = 0;
    for (const t of list) {
      const fromTag = (t.from_address_tag && t.from_address_tag.from_address_tag) || '';
      const toTag = (t.to_address_tag && t.to_address_tag.to_address_tag) || '';
      if (fromTag && t.from_address && !state.tags.has(t.from_address)) state.tags.set(t.from_address, fromTag);
      if (toTag && t.to_address && !state.tags.has(t.to_address)) state.tags.set(t.to_address, toTag);
      if (t.contractRet && t.contractRet !== 'SUCCESS') continue;
      const decimals = Number(t.tokenInfo && t.tokenInfo.tokenDecimal);
      const usd = Number(t.quant) / Math.pow(10, Number.isFinite(decimals) ? decimals : 6);
      if (keep(t.transaction_id, Number(t.block_ts) || Date.now(), usd, t.from_address || '', t.to_address || '')) added += 1;
    }
    return added;
  }

  async function gridPage(fingerprint) {
    const base = `${GRID}?event_name=Transfer&limit=200&order_by=block_timestamp,desc`;
    const url = fingerprint ? `${base}&fingerprint=${encodeURIComponent(fingerprint)}` : base;
    const res = await fetch(url, { cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer' });
    if (!res.ok) throw new Error(`TronGrid HTTP ${res.status}`);
    const json = await res.json();
    const meta = json.meta || {};
    return { data: json.data || [], fingerprint: meta.fingerprint || '' };
  }

  // 거래소 이름표 — TronScan 한 장. 429면 4분 쉰다(그동안에도 목록은 TronGrid로 흐른다).
  async function learnTags(found) {
    const now = Date.now();
    if (now < state.tagAt) return 0;
    try {
      const url = `${SCAN}?limit=50&start=0&sort=-timestamp&count=false&contract_address=${CONTRACT}`;
      const res = await fetch(url, { cache: 'no-store', credentials: 'omit', referrerPolicy: 'no-referrer' });
      if (!res.ok) throw new Error(`TronScan HTTP ${res.status}`);
      const json = await res.json();
      const list = (json && json.token_transfers) || [];
      const added = ingestScan(list);
      state.tagAt = Date.now() + TAG_MS;
      if (state.tags.size) saveTags();
      return added;
    } catch (err) {
      state.tagAt = Date.now() + TAG_COOL_MS;   // 호출 제한이면 조용히 물러난다
      return 0;
    }
  }

  function trim() {
    const kept = [];
    for (const id of state.order) {
      const row = state.rows.get(id);
      if (!row) continue;
      if (Date.now() - row.ts > KEEP_MS) { state.rows.delete(id); continue; }
      kept.push(row);
    }
    kept.sort((a, b) => b.ts - a.ts);
    const view = kept.slice(0, MAX_ROWS);
    const ids = new Set(view.map((r) => r.id));
    for (const id of [...state.rows.keys()]) if (!ids.has(id)) state.rows.delete(id);
    state.order = view.map((r) => r.id);
  }

  async function poll(deep) {
    if (state.fetching) return;
    state.fetching = true;
    paintWhaleState();
    const pages = deep && !state.order.length ? DEEP_PAGES : 1;
    let found = 0;
    try {
      let fingerprint = '';
      for (let page = 0; page < pages; page += 1) {
        const result = await gridPage(fingerprint);
        found += await ingestGrid(result.data);
        state.error = '';
        state.lastOkAt = Date.now();
        fingerprint = result.fingerprint;
        if (found) { trim(); renderWhales(true); }   // 찾는 대로 바로 보여 준다(깊게 훑는 동안 기다리지 않게)
        if (!result.data.length || !fingerprint) break;
        if (!state.active || document.visibilityState === 'hidden') break;
        if (page < pages - 1) await sleep(GRID_GAP_MS);   // 키 없이 1초에 1회 — 장 사이를 띄운다
      }
    } catch (err) {
      state.error = String((err && err.message) || err);
    }
    try {
      found += await learnTags();     // 거래소 이름표(실패해도 목록은 그대로)
    } catch (e) { /* 무시 */ }
    state.fetching = false;
    state.nextAt = Date.now() + POLL_MS;
    trim();
    renderWhales(found > 0);
  }

  function stats() {
    const now = Date.now();
    const rows = state.order.map((id) => state.rows.get(id)).filter((r) => r && now - r.ts <= STAT_MS);
    const total = rows.reduce((sum, r) => sum + r.usd, 0);
    const isEx = (addr) => party(addr).kind === 'ex';
    const inflow = rows.filter((r) => isEx(r.to)).reduce((sum, r) => sum + r.usd, 0);
    const outflow = rows.filter((r) => isEx(r.from)).reduce((sum, r) => sum + r.usd, 0);
    const tagged = rows.some((r) => isEx(r.to) || isEx(r.from));
    return { count: rows.length, total, net: inflow - outflow, tagged };
  }

  function paintWhaleState() {
    const box = $('#wh-wstat');
    const live = $('#wh-live');
    const liveText = $('#wh-live-text');
    if (live) live.classList.toggle('off', !!state.error);
    if (liveText) liveText.textContent = state.error ? '지연' : (state.fetching ? 'SYNC' : 'LIVE');
    if (!box) return;

    const s = stats();
    const rate = usdKrw();
    const pieces = [];
    pieces.push(`<span>↻ <b>${state.lastOkAt ? clockText(new Date(state.lastOkAt)) : '—'}</b> 확인</span>`);
    pieces.push(`<span>최근 15분 <b>${s.count}</b>건${s.count ? ` · 합계 <b>${usdText(s.total)}</b>${rate ? ` (≈${krwText(s.total, rate)})` : ''}` : ''}</span>`);
    if (s.tagged) {
      const cls = s.net > 0 ? 'up' : s.net < 0 ? 'down' : 'flat';
      const sign = s.net > 0 ? '+' : s.net < 0 ? '−' : '';
      pieces.push(`<span class="net ${cls}">거래소 순유입 ${sign}${usdText(Math.abs(s.net))}</span>`);
    }
    box.innerHTML = pieces.join('');
  }

  function renderWhales(flash) {
    const box = $('#wh-rows');
    if (!box) return;
    paintWhaleState();

    if (!state.order.length) {
      if (state.error) {
        box.innerHTML = `<div class="wh-msg err"><b>고래 이체를 받지 못했습니다.</b><br>${esc(state.error)}<br>잠시 뒤 저절로 다시 시도합니다.<br><button type="button" data-act="retry-whale">지금 다시</button></div>`;
      } else if (state.fetching) {
        box.innerHTML = '<div class="wh-msg">50만 달러 이상 이체를 찾는 중…</div>';
      } else {
        box.innerHTML = '<div class="wh-msg">지금은 관측된 대형 이체가 없습니다.<br>30초마다 다시 확인합니다.</div>';
      }
      return;
    }

    const rate = usdKrw();
    box.innerHTML = state.order.map((id) => {
      const r = state.rows.get(id);
      if (!r) return '';
      const tier = r.usd >= 2e6 ? ' tier3' : r.usd >= 1e6 ? ' tier2' : ' tier1';
      const krw = krwText(r.usd, rate);
      const pill = (addr) => {
        const p = party(addr);
        return `<span class="wh-pill ${p.kind}"><b>${esc(p.name)}</b>${p.detail ? `<small>${esc(p.detail)}</small>` : ''}</span>`;
      };
      return `<a class="wh-row${tier}${r.fresh && flash ? ' is-new' : ''}" href="${EXPLORER}${esc(r.id)}" target="_blank" rel="noopener noreferrer" data-id="${esc(r.id)}">
        <span class="wh-tok" aria-hidden="true">USDT</span>
        <span class="wh-mid">
          <span class="wh-pills">${pill(r.from)}<span class="wh-arrow" aria-hidden="true">→</span>${pill(r.to)}</span>
          <span class="wh-meta">TRC20 · <span class="wh-age">${ageText(r.ts)}</span></span>
        </span>
        <span class="wh-amt">
          <b>${krw || usdText(r.usd)}</b>
          <small>${krw ? usdText(r.usd) : '환율 정보 없음'}</small>
        </span>
      </a>`;
    }).join('');

    for (const row of state.rows.values()) row.fresh = false;
    paintFooter();
  }

  function paintAges() {
    const box = $('#wh-rows');
    if (!box) return;
    for (const el of $$('.wh-row', box)) {
      const r = state.rows.get(el.dataset.id);
      if (!r) continue;
      const age = $('.wh-age', el);
      if (age) age.textContent = ageText(r.ts);
      const krw = krwText(r.usd, usdKrw());
      const big = $('.wh-amt b', el);
      const small = $('.wh-amt small', el);
      if (krw && big) { big.textContent = krw; if (small) small.textContent = usdText(r.usd); }
    }
    paintWhaleState();
    paintFooter();
  }

  function paintFooter() {
    const foot = $('#wh-wfoot');
    if (!foot) return;
    const secs = state.nextAt ? Math.max(0, Math.round((state.nextAt - Date.now()) / 1000)) : null;
    foot.firstElementChild.textContent = 'TronGrid·TronScan 공개 API · 관측된 이체만 표시'
      + (state.fetching ? ' · 새 이체 확인 중' : secs != null ? ` · 다음 확인 ${secs}초 후` : '');
  }

  /* ---------------------------------------------------------------- 타이머·활성화 */

  function startTimers() {
    stopTimers();
    state.clockTimer = setInterval(() => {
      const el = $('#wh-clock');
      if (el) el.textContent = clockText();
      paintFooter();
    }, CLOCK_MS);
    state.ageTimer = setInterval(paintAges, AGE_MS);
    state.nextTimer = setInterval(paintNext, NEXT_MS);
    state.pollTimer = setInterval(() => poll(false), POLL_MS);
    state.fxTimer = setInterval(loadFx, FX_MS);
  }

  function stopTimers() {
    for (const key of ['clockTimer', 'ageTimer', 'nextTimer', 'pollTimer', 'fxTimer']) {
      if (state[key]) clearInterval(state[key]);
      state[key] = 0;
    }
  }

  function refresh() {
    if (!state.calendar || Date.now() - state.calAt > 10 * 60000) loadCalendar();
    if (!state.fx) loadFx();
    if (!state.lastOkAt || Date.now() - state.lastOkAt > POLL_MS) poll(!state.order.length);
    const clock = $('#wh-clock');
    if (clock) clock.textContent = clockText();
    renderCalendar();
    renderWhales(false);
  }

  function setActive(active) {
    build();
    const next = !!active;
    if (state.active === next) { if (next) refresh(); return; }
    state.active = next;
    if (!state.active) { stopTimers(); return; }
    startTimers();
    refresh();
  }

  document.addEventListener('visibilitychange', () => {
    if (!state.active) return;
    if (document.visibilityState === 'visible') {
      startTimers();
      if (!state.lastOkAt || Date.now() - state.lastOkAt > POLL_MS) poll(false);
      paintAges();
      paintNext();
    } else {
      stopTimers();
    }
  });

  window.addEventListener('online', () => { if (state.active) poll(false); });

  loadTags();

  window.LiteWhale = {
    setActive,
    refresh: () => { loadCalendar(true); poll(true); },
    debug: () => ({
      active: state.active,
      rows: state.order.length,
      tags: state.tags.size,
      lastOkAt: state.lastOkAt,
      nextAt: state.nextAt,
      error: state.error,
      fx: usdKrw(),
      calendar: state.calendar ? { week: [state.calendar.weekStart, state.calendar.weekEnd], events: state.calendar.events.length } : null,
      calError: state.calError,
      top: state.order.slice(0, 4).map((id) => {
        const r = state.rows.get(id);
        return r && { usd: r.usd, ts: r.ts, from: party(r.from).name, to: party(r.to).name, addr: r.from };
      }),
    }),
  };
})();
