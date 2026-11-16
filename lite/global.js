/* PIXEL TRADING FLOOR — 글로벌 시황 (1안 + 2안 하이브리드 · 실데이터)
   · 1안: 상단 전광판 바(#gbar) — [사무실]·[트레이딩] 탭에서 보이는 한 줄 티커
   · 2안: 스크리너 탭 상단 요약 카드(#scr-global) — 7칸 카드 + 8비트 스파크라인 + 도미넌스 막대 + 공포탐욕 게이지
   두 벌의 옷을 각자의 탭 톤에 맞춘다: 오피스는 검정+금색 픽셀 전광판, 스크리너는 거래소 톤(#0b0e13/#10151e).

   어디서 값을 받는가 (서버 없음)
     · 미 선물 NQ·ES, 달러 인덱스, 원/달러 : data/global.json — GitHub Actions가 10분마다 야후 파이낸스에서
       받아 저장소에 커밋한다(브라우저는 야후에 CORS로 직접 붙을 수 없다). 24시간 스파크도 그 파일에 들어 있다.
     · 김프 : 업비트 KRW-BTC + 코인베이스 BTC-USD + 원/달러 환율 → 브라우저가 직접 계산(30초)
     · BTC 도미넌스·ETH·총시총 : CoinGecko /api/v3/global (3분 캐싱)
     · 공포탐욕 : alternative.me/fng (10분 캐싱)
   실시간 호출이 실패하면 data/global.json의 값으로 물러난다. 값이 30분보다 낡으면 SYNC 표시가 노랗게 바뀐다.
   투자 조언이 아니다. */

(function () {
  'use strict';

  const JSON_URL = 'data/global.json';
  const TICK_MS = 5000;
  // 업비트 REST는 Origin(사이트 주소) 단위로 1분에 10번 정도만 허용한다 — 모든 방문자가 같은 Origin을 쓰므로
  // 평소에는 웹소켓으로 받고, 웹소켓이 막힌 환경에서만 REST로 드문드문 받는다.
  const DUE = { json: 5 * 60000, prices: 60000, krw: 60000, cg: 3 * 60000, fng: 10 * 60000, fx: 30 * 60000 };
  const STALE_MS = 30 * 60000;

  const SPEC = [
    { key: 'nq', short: 'NQ 선물', label: '나스닥100 선물', tag: 'CME', digits: 2, kind: 'market' },
    { key: 'es', short: 'ES 선물', label: 'S&P500 선물', tag: 'CME', digits: 2, kind: 'market' },
    { key: 'dxy', short: 'DXY', label: '달러 인덱스', tag: 'ICE', digits: 2, kind: 'market' },
    { key: 'fx', short: '원/달러', label: '원/달러', tag: 'FX', digits: 2, unit: '원', kind: 'market' },
    { key: 'kimchi', short: '김프', label: '김프', tag: '업비트', digits: 2, signed: true, kind: 'kimchi' },
    { key: 'btcd', short: 'BTC.D', label: 'BTC 도미넌스', tag: 'ETH', digits: 2, unit: '%', kind: 'dominance' },
    { key: 'fng', short: 'F&G', label: '공포·탐욕', tag: 'F&G', digits: 0, kind: 'fng' },
  ];

  // 좁은 화면(≤820px)에서 7칸 카드를 다 펼치면 코인 목록이 두 줄만 남는다 — 접어 둔 상태를 기억한다
  const CARDS_KEY = 'pixel-global-cards-open-v1';
  const CARDS_MINI = ['kimchi', 'btcd', 'fng', 'nq'];

  // CoinGecko가 호출 제한을 내면 한동안 아예 시도하지 않는다(콘솔·요청 소음 방지) — 그동안은 Coinlore가 값를 낸다
  const CG_PAUSE_KEY = 'pixel-global-cg-pause-v1';
  const CG_PAUSE_MS = 30 * 60000;
  const cgPause = {
    get until() {
      try { return Number(localStorage.getItem(CG_PAUSE_KEY) || 0) || 0; } catch (e) { return 0; }
    },
    set(mins) {
      try { localStorage.setItem(CG_PAUSE_KEY, String(Date.now() + mins)); } catch (e) { /* 저장 못 해도 동작 */ }
    },
    clear() {
      try { localStorage.removeItem(CG_PAUSE_KEY); } catch (e) { /* 무시 */ }
    },
  };
  let cgFails = 0;

  const FG = {
    'Extreme Fear': '극단적 공포',
    Fear: '공포',
    Neutral: '중립',
    Greed: '탐욕',
    'Extreme Greed': '극단적 탐욕',
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const num = (v) => (v === null || v === undefined || v === '' || !Number.isFinite(Number(v)) ? null : Number(v));
  const fmt = (v, d) => Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  const arrow = (p) => (p > 0 ? '▲' : p < 0 ? '▼' : '—');
  const dirOf = (p) => (p > 0 ? 'up' : p < 0 ? 'down' : 'flat');

  const state = {
    markets: {},
    cryptoJson: {},
    live: { btcUsd: null, ethUsd: null, btcKrw: null, krwSource: null, fx: null, cg: null, fng: null, fngLabel: null },
    jsonAt: 0,
    liveAt: 0,
    due: { json: 0, prices: 0, krw: 0, cg: 0, fng: 0, fx: 0 },
    ready: false,
    busy: {},
  };

  // ---------- 값 읽기 ----------
  async function getJSON(url, timeout = 9000) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    try {
      const res = await fetch(url, { signal: ctrl.signal, cache: 'no-store', credentials: 'omit' });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  async function guard(name, fn) {
    if (state.busy[name]) return;
    state.busy[name] = true;
    try {
      await fn();
    } catch (error) {
      // 값이 없다는 뜻일 뿐이다 — 화면은 지난 값이나 global.json으로 계속 돈다
      console.warn('[글로벌 시황] ' + name + ' 갱신 실패:', error && error.message ? error.message : error);
    } finally {
      state.busy[name] = false;
    }
  }

  async function loadJson() {
    const data = await getJSON(JSON_URL + '?t=' + Date.now());
    if (data && data.markets) {
      state.markets = data.markets;
      state.cryptoJson = data.crypto || {};
      const at = Date.parse(data.updatedAt || '');
      state.jsonAt = Number.isFinite(at) ? at : Date.now();
      state.ready = true;
      paint();
    }
    state.due.json = Date.now() + DUE.json;
  }

  // ---- 김프 재료: 코인베이스(달러) · 업비트(원) · 환율 ----
  // 업비트는 웹소켓으로 실시간 체결가를 받는다(공개 웹소켓, Origin 제한 없음).
  // 웹소켓이 막히면 REST로 5분에 한 번만 받는다 — REST는 Origin당 분당 호출 수가 작아 자주 부르면
  // 사이트 전체가 429를 맞는다.
  const upbit = { ws: null, state: 'idle', retry: 0, timer: 0, paintTimer: 0 };
  const KRW_NAME = { upbit: '업비트', bithumb: '빗썸' };

  async function loadPrices() {
    const [btcUsd, ethUsd] = await Promise.allSettled([
      getJSON('https://api.exchange.coinbase.com/products/BTC-USD/ticker', 7000),
      getJSON('https://api.exchange.coinbase.com/products/ETH-USD/ticker', 7000),
    ]);
    let touched = false;
    if (btcUsd.status === 'fulfilled') {
      const price = num(btcUsd.value && btcUsd.value.price);
      if (price) { state.live.btcUsd = price; touched = true; }
    }
    if (ethUsd.status === 'fulfilled') {
      const price = num(ethUsd.value && ethUsd.value.price);
      if (price) { state.live.ethUsd = price; touched = true; }
    }
    // 환율은 원/달러 카드와 같은 값을 쓴다 (global.json의 KRW=X → 없으면 er-api)
    if (!state.live.fx && !num(state.cryptoJson.usdKrw)) await loadFx();
    if (touched) {
      state.liveAt = Date.now();
      paint();
    }
    state.due.prices = Date.now() + DUE.prices;
  }

  function krwPrice(price, source) {
    if (!price) return;
    state.live.btcKrw = price;
    state.live.krwSource = source;
    state.liveAt = Date.now();
    if (upbit.paintTimer) return;   // 체결이 초당 여러 번 와도 화면은 1초에 한 번만 고친다
    upbit.paintTimer = setTimeout(() => {
      upbit.paintTimer = 0;
      paint();
    }, 900);
  }

  async function onUpbitMessage(event) {
    try {
      const raw = typeof event.data === 'string' ? event.data
        : (event.data && event.data.text ? await event.data.text() : '');
      const data = JSON.parse(raw);
      const row = Array.isArray(data) ? data[0] : data;
      if (row && row.type === 'ticker' && row.code === 'KRW-BTC') {
        const price = num(row.trade_price);
        if (price) krwPrice(price, 'upbit');
      }
    } catch (error) {
      // 이상한 메시지는 버린다 — 소켓은 그대로 둔다
    }
  }

  function scheduleUpbitRetry() {
    upbit.retry += 1;
    // 업비트도 접속(핸드셰이크)에 제한이 있다 — 실패하면 1분부터 천천히, 최대 10분 간격으로만 다시 붙는다
    const delay = Math.min(10 * 60000, 60000 * Math.pow(2, Math.min(3, upbit.retry)));
    clearTimeout(upbit.timer);
    upbit.timer = setTimeout(openUpbit, delay);
  }

  function openUpbit() {
    if (upbit.ws) return;
    if (typeof WebSocket === 'undefined') { upbit.state = 'down'; return; }
    let ws;
    try {
      ws = new WebSocket('wss://api.upbit.com/websocket/v1');
    } catch (error) {
      upbit.state = 'down';
      scheduleUpbitRetry();
      return;
    }
    upbit.ws = ws;
    upbit.state = 'connecting';
    const failTimer = setTimeout(() => {
      if (upbit.state === 'connecting') {
        upbit.state = 'down';
        try { ws.close(); } catch (error) { /* 이미 닫힘 */ }
      }
    }, 9000);
    ws.onopen = () => {
      upbit.state = 'live';
      upbit.retry = 0;
      clearTimeout(failTimer);
      try {
        ws.send(JSON.stringify([{ ticket: 'pixel-trading-floor' }, { type: 'ticker', codes: ['KRW-BTC'] }]));
      } catch (error) { /* 아래 onclose가 다시 시도한다 */ }
    };
    ws.onmessage = onUpbitMessage;
    ws.onerror = () => {};
    ws.onclose = () => {
      clearTimeout(failTimer);
      upbit.ws = null;
      if (upbit.state === 'live' || upbit.state === 'connecting') {
        upbit.state = 'down';
        scheduleUpbitRetry();
      }
    };
  }

  // 업비트 웹소켓이 막힌 환경(회선·일시 차단)을 위한 원화 시세 — 빗썸 공개 시세.
  // 업비트 REST는 사이트 주소(Origin)당 분당 호출 수가 작아 모두가 나눠 쓰면 금방 429가 난다.
  async function loadKrwFallback() {
    const data = await getJSON('https://api.bithumb.com/public/ticker/BTC_KRW', 7000);
    const price = num(data && data.data && data.data.closing_price);
    if (price) krwPrice(price, 'bithumb');
    state.due.krw = Date.now() + DUE.krw;
  }

  async function loadFx() {
    try {
      const data = await getJSON('https://open.er-api.com/v6/latest/USD', 9000);
      const krw = num(data && data.rates && data.rates.KRW);
      if (krw) {
        state.live.fx = krw;
        state.liveAt = Date.now();
        paint();
      }
    } finally {
      state.due.fx = Date.now() + DUE.fx;
    }
  }

  async function loadCoingecko() {
    // CoinGecko가 429(호출 제한)를 내면 Coinlore로 물러난다 — 값은 같고 출처만 달라진다
    if (Date.now() < cgPause.until) return loadCoinlore();
    try {
      const data = await getJSON('https://api.coingecko.com/api/v3/global', 9000);
      const d = data && data.data;
      if (d && d.market_cap_percentage) {
        state.live.cg = {
          btc: num(d.market_cap_percentage.btc),
          eth: num(d.market_cap_percentage.eth),
          mcap: num(d.total_market_cap && d.total_market_cap.usd),
          source: 'coingecko',
        };
        state.liveAt = Date.now();
        cgFails = 0;
        cgPause.clear();
        paint();
        state.due.cg = Date.now() + DUE.cg;
        return;
      }
    } catch (error) {
      cgFails += 1;
      if (cgFails >= 1) cgPause.set(CG_PAUSE_MS);   // 한 번 걸리면 30분 쉰다 — 그동안은 Coinlore
      console.warn('[글로벌 시황] CoinGecko 갱신 실패 — Coinlore로 물러납니다:', error && error.message ? error.message : error);
    }
    return loadCoinlore();
  }

  async function loadCoinlore() {
    const rows = await getJSON('https://api.coinlore.net/api/global/', 9000);
    const row = Array.isArray(rows) && rows[0];
    if (row) {
      state.live.cg = {
        btc: num(row.btc_d),
        eth: num(row.eth_d),
        mcap: num(row.total_mcap),
        source: 'coinlore',
      };
      state.liveAt = Date.now();
      paint();
    }
    state.due.cg = Date.now() + DUE.cg;
  }

  async function loadFng() {
    const data = await getJSON('https://api.alternative.me/fng/?limit=1', 9000);
    const row = data && Array.isArray(data.data) && data.data[0];
    if (row) {
      state.live.fng = num(row.value);
      state.live.fngLabel = row.value_classification || null;
      state.liveAt = Date.now();
      paint();
    }
    state.due.fng = Date.now() + DUE.fng;
  }

  // ---------- 값 계산 ----------
  function rows() {
    const m = state.markets || {};
    const c = state.cryptoJson || {};
    const L = state.live;

    const market = (key) => {
      const entry = m[key] || {};
      const price = num(entry.price);
      const pct = num(entry.changePct);
      return { price, pct, spark: Array.isArray(entry.spark) ? entry.spark : null };
    };

    const fxEntry = market('fx');
    // 표시 환율과 김프 계산 환율을 같게 — 원/달러 카드는 global.json의 KRW=X(실시간에 가까움),
    // 그 값이 없을 때만 er-api 환율을 쓴다
    const fx = fxEntry.price != null ? fxEntry.price : (num(c.usdKrw) != null ? num(c.usdKrw) : num(L.fx));
    const btcUsd = num(L.btcUsd) != null ? num(L.btcUsd) : num(c.btcUsd);
    const ethUsd = num(L.ethUsd) != null ? num(L.ethUsd) : num(c.ethUsd);
    // 김프는 같은 시점의 세 값으로만 계산한다 — 실시간 원화가 없으면 global.json이 함께 받아 둔
    // 김프 값을 그대로 쓴다(신선한 달러에 낡은 원화를 섞으면 값이 튄다)
    const krwLive = num(L.btcKrw);
    const btcKrw = krwLive != null ? krwLive : num(c.btcKrw);
    const kimchi = krwLive != null && fx && btcUsd
      ? (krwLive / (btcUsd * fx) - 1) * 100
      : num(c.kimchiPct);

    const cg = L.cg || {};
    const btcDom = num(cg.btc) != null ? num(cg.btc) : num(c.btcDominance);
    const ethDom = num(cg.eth) != null ? num(cg.eth) : num(c.ethDominance);
    const mcap = num(cg.mcap) != null ? num(cg.mcap) : num(c.totalMcapUsd);
    const fng = L.fng != null ? L.fng : num(c.fng);
    const fngLabel = L.fngLabel || c.fngLabel || '';

    return SPEC.map((spec) => {
      const row = Object.assign({}, spec);
      if (spec.kind === 'market') {
        const entry = market(spec.key);
        row.value = entry.price;
        row.pct = entry.pct;
        row.spark = entry.spark;
      } else if (spec.kind === 'kimchi') {
        row.value = kimchi;
        row.pct = null;
        const krwName = KRW_NAME[state.live.krwSource] || '업비트';
        row.sub = btcKrw ? krwName + ' ' + fmt(btcKrw, 0) : '';
        row.side = fx ? '환율 ' + fmt(fx, 2) : '';
        row.title = [
          btcKrw ? krwName + ' ' + fmt(btcKrw, 0) + '원' : '',
          btcUsd ? '달러 ' + fmt(btcUsd, 2) : '',
          fx ? '환율 ' + fmt(fx, 2) : '',
        ].filter(Boolean).join(' · ');
        row.dir = kimchi == null ? 'flat' : dirOf(kimchi);
      } else if (spec.kind === 'dominance') {
        row.value = btcDom;
        row.pct = null;
        row.eth = ethDom;
        row.tag = ethDom != null ? 'ETH ' + fmt(ethDom, 2) + '%' : 'ETH';
        row.sub = mcap ? '시총 $' + (mcap / 1e12).toFixed(2) + '조' : '';
        row.dir = 'flat';
      } else if (spec.kind === 'fng') {
        row.value = fng;
        row.pct = null;
        row.label2 = FG[fngLabel] || '';
        row.sub = row.label2;
        row.dir = 'flat';
      }
      return row;
    });
  }

  function stripValue(row) {
    if (row.value == null) return '—';
    if (row.kind === 'kimchi') return `${row.value > 0 ? '+' : ''}${fmt(row.value, 2)}%`;
    if (row.kind === 'fng') return String(Math.round(row.value));
    return fmt(row.value, row.digits) + (row.unit === '%' ? '%' : '');
  }

  function updatedMs() {
    return Math.max(state.jsonAt || 0, state.liveAt || 0);
  }

  function syncLabel(withSeconds) {
    const at = updatedMs();
    if (!at) return 'SYNC --:-- KST';
    const kst = new Date(at + 9 * 3600000);
    const hh = String(kst.getUTCHours()).padStart(2, '0');
    const mm = String(kst.getUTCMinutes()).padStart(2, '0');
    const ss = String(kst.getUTCSeconds()).padStart(2, '0');
    return 'SYNC ' + (withSeconds ? `${hh}:${mm}:${ss}` : `${hh}:${mm}`) + ' KST';
  }

  function isStale() {
    const at = updatedMs();
    return !at || Date.now() - at > STALE_MS;
  }

  // ---------- 1안: 상단 전광판 바 ----------
  function buildStrip() {
    if ($('#gbar')) return;
    const bar = document.createElement('div');
    bar.id = 'gbar';
    bar.setAttribute('role', 'region');
    bar.setAttribute('aria-label', '글로벌 시황 전광판');
    bar.innerHTML = '<span class="gb-brand">◆ 글로벌 시황 ◆</span>'
      + '<div class="gb-track">' + SPEC.map((spec) => `<span class="gb-item" data-k="${spec.key}">`
        + `<span class="gb-lbl">${spec.short}</span>`
        + '<span class="gb-pair"><b class="gb-val">—</b><em class="gb-chg"></em><em class="gb-note"></em></span>'
        + '</span>').join('') + '</div>'
      + '<span class="gb-clock"><i></i><span class="gb-time">SYNC --:-- KST</span></span>';
    const topbar = $('#topbar');
    if (topbar && topbar.parentNode) topbar.parentNode.insertBefore(bar, topbar.nextSibling);
    else document.body.insertBefore(bar, document.body.firstChild);
  }

  function paintStrip(list) {
    const bar = $('#gbar');
    if (!bar) return;
    list.forEach((row) => {
      const cell = bar.querySelector(`.gb-item[data-k="${row.key}"]`);
      if (!cell) return;
      const val = cell.querySelector('.gb-val');
      const chg = cell.querySelector('.gb-chg');
      const note = cell.querySelector('.gb-note');
      if (!val || !chg || !note) return;
      const dir = row.pct != null ? dirOf(row.pct) : (row.dir || 'flat');
      val.textContent = stripValue(row);
      val.className = 'gb-val' + (row.kind === 'fng' ? ' is-fear' : ' ' + dir);
      if (row.pct != null) {
        chg.textContent = `${arrow(row.pct)}${fmt(Math.abs(row.pct), 2)}%`;
        chg.className = 'gb-chg ' + dir;
      } else if (row.kind === 'kimchi' && row.value != null) {
        chg.textContent = '';
        chg.className = 'gb-chg ' + dir;
      } else {
        chg.textContent = '';
        chg.className = 'gb-chg';
      }
      note.textContent = row.kind === 'fng' ? (row.label2 ? row.label2.replace('극단적 ', '극단').replace(' ', '') : 'F&G') : '';
      cell.title = row.title || '';
    });
    const clock = bar.querySelector('.gb-clock');
    bar.querySelector('.gb-time').textContent = syncLabel(false);
    clock.classList.toggle('stale', isStale());
  }

  // ---------- 2안: 스크리너 요약 카드 ----------
  function vizFor(spec) {
    if (spec.kind === 'market') return '<canvas class="sg-spark" aria-hidden="true"></canvas>';
    if (spec.kind === 'dominance') return '<span class="sg-dom" aria-hidden="true"><i style="width:0%"></i><i style="width:0%"></i></span>';
    if (spec.kind === 'kimchi') return '<span class="sg-side" aria-hidden="true"></span>';
    return '<span class="sg-spark-gap" aria-hidden="true"></span>';
  }

  function buildCards() {
    const page = $('#screener-page');
    if (!page || $('#scr-global')) return;
    const box = document.createElement('section');
    box.id = 'scr-global';
    box.setAttribute('aria-label', '글로벌 시황 요약');
    box.innerHTML = '<div class="sg-head">'
      + '<span class="sg-title">◆ 글로벌 시황 ◆</span>'
      + '<span class="sg-note">미 선물 · 달러 · 온체인</span>'
      + '<span class="sg-live"><i></i><em class="sg-live-time">LIVE · --:-- KST</em></span>'
      + '<button type="button" class="sg-toggle" aria-expanded="true" aria-controls="scr-global-grid" title="글로벌 시황 카드 접기">'
      + '<span class="sg-toggle-lbl">접기</span><i aria-hidden="true"></i></button>'
      + '</div>'
      + '<div class="sg-mini">' + CARDS_MINI.map((key) => {
        const spec = SPEC.find((item) => item.key === key);
        return `<span class="sg-chip" data-k="${key}" data-dir="flat"><em>${spec ? spec.short : key}</em><b>—</b></span>`;
      }).join('') + '</div><div class="sg-grid" id="scr-global-grid">'
      + SPEC.map((spec) => `<article class="sg-card" data-k="${spec.key}" data-dir="flat">`
        + `<div class="sg-top"><span class="sg-lbl">${spec.label}</span><span class="sg-tag">${spec.tag}</span></div>`
        + '<b class="sg-val">—</b>'
        + (spec.kind === 'fng' ? '<div class="sg-gauge"><i style="left:0%"></i></div>' : '')
        + `<div class="sg-foot"><span class="sg-footval"></span>${vizFor(spec)}</div>`
        + '</article>').join('')
      + '</div>';
    const bar = page.querySelector('.scr-bar');
    page.insertBefore(box, bar ? bar.nextSibling : page.firstChild);

    const toggle = box.querySelector('.sg-toggle');
    if (toggle) {
      toggle.addEventListener('click', () => applyCardsOpen(box.classList.contains('is-collapsed'), true));
    }
    try {
      const mq = window.matchMedia('(max-width: 820px)');
      const onChange = () => applyCardsOpen(storedCardsOpen());
      if (mq.addEventListener) mq.addEventListener('change', onChange);
      else if (mq.addListener) mq.addListener(onChange);
    } catch (e) { /* 미디어쿼리를 못 써도 토글로 연다 */ }
    applyCardsOpen(storedCardsOpen());
  }

  function isNarrow() {
    try { return window.matchMedia('(max-width: 820px)').matches; } catch (e) { return false; }
  }

  function storedCardsOpen() {
    try {
      const raw = localStorage.getItem(CARDS_KEY);
      return raw === null ? null : raw === '1';
    } catch (e) { return null; }
  }

  // 펼침/접힘 — 좁은 화면에서만 접힌다. 넓은 화면은 7칸을 그대로 두고 토글도 숨긴다.
  function applyCardsOpen(open, persist) {
    const box = $('#scr-global');
    if (!box) return;
    const stored = storedCardsOpen();
    const wantOpen = open == null ? (stored == null ? !isNarrow() : stored) : !!open;
    const collapsed = !wantOpen && isNarrow();
    box.classList.toggle('is-collapsed', collapsed);
    const toggle = box.querySelector('.sg-toggle');
    if (toggle) {
      toggle.setAttribute('aria-expanded', String(!collapsed));
      toggle.title = collapsed ? '글로벌 시황 카드 펼치기' : '글로벌 시황 카드 접기';
      const label = toggle.querySelector('.sg-toggle-lbl');
      if (label) label.textContent = collapsed ? '카드 펼치기' : '접기';
    }
    if (persist) {
      try { localStorage.setItem(CARDS_KEY, wantOpen ? '1' : '0'); } catch (e) { /* 저장 못 해도 동작 */ }
    }
    // 접힌 동안(폭 0) 그린 스파크라인은 다시 그려야 보인다
    if (!collapsed) paint();
  }

  function cardValueHtml(row) {
    if (row.value == null) return '—';
    if (row.kind === 'kimchi') {
      return `<span class="sg-sign">${row.value > 0 ? '+' : ''}</span>${fmt(row.value, 2)}<span class="sg-unit">%</span>`;
    }
    if (row.kind === 'fng') return `${Math.round(row.value)}<span class="sg-unit">/100</span>`;
    if (row.unit === '원') return `${fmt(row.value, 2)}<span class="sg-unit">원</span>`;
    if (row.unit === '%') return `${fmt(row.value, 2)}<span class="sg-unit">%</span>`;
    return fmt(row.value, row.digits);
  }

  function paintCards(list) {
    const box = $('#scr-global');
    if (!box) return;
    list.forEach((row) => {
      const card = box.querySelector(`.sg-card[data-k="${row.key}"]`);
      if (!card) return;
      const dir = row.pct != null ? dirOf(row.pct) : (row.dir || 'flat');
      card.dataset.dir = row.kind === 'fng' ? 'flat' : dir;
      const val = card.querySelector('.sg-val');
      if (!val) return;
      val.innerHTML = cardValueHtml(row);
      val.className = 'sg-val' + (row.kind === 'fng' ? ' fear' : '');
      const tag = card.querySelector('.sg-tag');
      if (tag && row.tag) tag.textContent = row.tag;
      const foot = card.querySelector('.sg-footval');
      if (foot) {
        if (row.pct != null) {
          foot.className = 'sg-footval sg-chg ' + dir;
          foot.textContent = `${arrow(row.pct)} ${fmt(Math.abs(row.pct), 2)}%`;
        } else {
          foot.className = 'sg-footval sg-sub';
          foot.textContent = row.sub || '';
        }
      }
      if (row.kind === 'kimchi') {
        const side = card.querySelector('.sg-side');
        if (side) side.textContent = row.side || '';
      }
      if (row.kind === 'dominance') {
        const bar = card.querySelector('.sg-dom');
        if (bar) {
          const btcWidth = row.value == null ? 0 : Math.max(0, Math.min(100, row.value));
          const ethWidth = row.eth == null ? 0 : Math.max(0, Math.min(100, row.eth));
          bar.children[0].style.width = btcWidth + '%';
          bar.children[1].style.width = ethWidth + '%';
          bar.title = `BTC ${row.value == null ? '—' : fmt(row.value, 2)}% · ETH ${row.eth == null ? '—' : fmt(row.eth, 2)}%`;
        }
      }
      if (row.kind === 'fng') {
        const pin = card.querySelector('.sg-gauge i');
        if (pin && row.value != null) pin.style.left = Math.max(0, Math.min(100, row.value)) + '%';
      }
      if (row.kind === 'market') drawSpark(card.querySelector('.sg-spark'), row.spark, dir);
    });
    const live = box.querySelector('.sg-live-time');
    if (live) live.textContent = 'LIVE · ' + syncLabel(true).replace('SYNC ', '');
    const pill = box.querySelector('.sg-live');
    if (pill) pill.classList.toggle('stale', isStale());

    // 접힌 상태의 한 줄 요약 칩 — 같은 값을 카드 대신 칩으로 보여 준다
    const byKey = new Map(list.map((row) => [row.key, row]));
    box.querySelectorAll('.sg-chip').forEach((chip) => {
      const row = byKey.get(chip.dataset.k);
      const value = chip.querySelector('b');
      if (!row || !value) return;
      chip.dataset.dir = row.kind === 'fng' ? 'flat' : (row.pct != null ? dirOf(row.pct) : (row.dir || 'flat'));
      value.textContent = chipValue(row);
      chip.title = row.label + ' ' + chipValue(row);
    });
  }

  function chipValue(row) {
    if (row.value == null) return '—';
    if (row.kind === 'kimchi') return `${row.value > 0 ? '+' : ''}${fmt(row.value, 2)}%`;
    if (row.kind === 'fng') return `${Math.round(row.value)}${row.label2 ? ' ' + row.label2 : ''}`;
    if (row.unit === '%') return `${fmt(row.value, 2)}%`;
    if (row.unit === '원') return `${fmt(row.value, 0)}원`;
    return fmt(row.value, row.digits);
  }

  // 24시간 흐름 미니 스파크 — global.json에 담긴 실제 5분봉 종가를 계단식으로
  function drawSpark(canvas, series, dir) {
    if (!canvas) return;
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    if (!w || !h) return;   // 화면에 없으면(null 레이아웃) 다음 렌더에서 그린다
    const pts = (series || []).map(Number).filter((v) => Number.isFinite(v));
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (pts.length < 2) {
      // 값이 모자라면 기준선만 — 없는 그림을 지어내지 않는다
      ctx.globalAlpha = 0.35;
      ctx.strokeStyle = '#5f6b7d';
      ctx.beginPath();
      ctx.moveTo(0, h / 2);
      ctx.lineTo(w, h / 2);
      ctx.stroke();
      return;
    }
    const min = Math.min(...pts);
    const max = Math.max(...pts);
    const span = max - min || 1;
    const color = dir === 'up' ? '#27c98b' : dir === 'down' ? '#f05b6e' : '#93a0b2';
    const step = w / (pts.length - 1);
    const yAt = (p) => h - 2.5 - ((p - min) / span) * (h - 5);
    ctx.beginPath();
    pts.forEach((p, i) => {
      const x = i * step;
      if (i === 0) ctx.moveTo(x, yAt(p));
      else { ctx.lineTo(x, yAt(pts[i - 1])); ctx.lineTo(x, yAt(p)); }
    });
    ctx.globalAlpha = 0.85;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillStyle = color;
    const lastY = Math.round(yAt(pts[pts.length - 1]));
    ctx.fillRect(Math.max(0, w - 3), lastY - 1, 3, 3);
  }

  // ---------- 그리기 · 주기 ----------
  function paint() {
    try {
      const list = rows();
      paintStrip(list);
      paintCards(list);
    } catch (error) {
      console.warn('[글로벌 시황] 렌더 실패:', error && error.message ? error.message : error);
    }
  }

  function tick(force) {
    if (document.visibilityState === 'hidden') return;
    const now = Date.now();
    if (force || now >= state.due.json) guard('json', loadJson);
    if (force || now >= state.due.prices) guard('prices', loadPrices);
    if (upbit.state !== 'live' && (force || now >= state.due.krw)) guard('krw', loadKrwFallback);
    if (force || now >= state.due.cg) guard('coingecko', loadCoingecko);
    if (force || now >= state.due.fng) guard('fng', loadFng);
    if (now >= state.due.fx) guard('fx', loadFx);
    // 남은 시간이 지나면 '지금 몇 분 값인지'가 바뀐다 — 숫자 판은 다시 그리지 않는다
    paintClock();
  }

  function paintClock() {
    const bar = $('#gbar');
    if (bar) {
      const time = bar.querySelector('.gb-time');
      const clock = bar.querySelector('.gb-clock');
      if (time) time.textContent = syncLabel(false);
      if (clock) clock.classList.toggle('stale', isStale());
    }
    const box = $('#scr-global');
    if (box) {
      const live = box.querySelector('.sg-live-time');
      const pill = box.querySelector('.sg-live');
      if (live) live.textContent = 'LIVE · ' + syncLabel(true).replace('SYNC ', '');
      if (pill) pill.classList.toggle('stale', isStale());
    }
  }

  function init() {
    buildStrip();
    buildCards();
    if (!$('#scr-global')) {
      // 스크리너 화면이 나중에 만들어지면 그때 카드를 붙인다
      const observer = new MutationObserver(() => {
        if ($('#screener-page')) {
          buildCards();
          paint();
          if ($('#scr-global')) observer.disconnect();
        }
      });
      observer.observe(document.body, { childList: true });
    }
    // 탭을 바꾸면 숨어 있던 캔버스가 처음 크기를 얻는다 — 그때 다시 그린다
    new MutationObserver(() => requestAnimationFrame(() => paint()))
      .observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState !== 'visible') return;
      if (!upbit.ws) { upbit.state = 'idle'; openUpbit(); }
      tick(true);
    });
    setInterval(tick, TICK_MS);
    guard('json', loadJson);
    guard('prices', loadPrices);
    guard('coingecko', loadCoingecko);
    state.due.krw = Date.now() + 8000;   // 웹소켓이 8초 안에 못 붙으면 원화 폴백을 부른다
    openUpbit();
    guard('fng', loadFng);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.LiteGlobal = {
    state, rows, paint, refresh: () => tick(true), syncLabel,
    debug: () => ({
      upbit: upbit.state, retry: upbit.retry, krwSource: state.live.krwSource,
      btcKrw: state.live.btcKrw, btcUsd: state.live.btcUsd, ethUsd: state.live.ethUsd, fx: state.live.fx,
      cg: state.live.cg, fng: state.live.fng,
      jsonAt: state.jsonAt, liveAt: state.liveAt, due: state.due,
    }),
  };
})();
