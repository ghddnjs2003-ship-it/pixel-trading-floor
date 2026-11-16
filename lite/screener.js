/* PIXEL TRADING FLOOR — 스크리너 탭 (바이비트 V5 공개 시세 · 실시간 WebSocket)
   · 무기한(linear)·현물(spot)의 USDT 종목을 24시간 거래대금 순으로 보여 준다
   · 가격은 바이비트 공개 웹소켓(tickers.{종목})에서 받아 들어오는 즉시 바뀐다
       - 무기한: wss://stream.bybit.com/v5/public/linear — ticker는 delta(바뀐 값만)라 종목별로 합쳐 쓴다
       - 현물  : wss://stream.bybit.com/v5/public/spot   — ticker는 snapshot(매번 전체)이다
   · 웹소켓은 화면에 보이는 구간 + 즐겨찾기 + 정렬 상위 종목만 구독한다(연결당 구독 수를 지키고 데이터를 아낀다)
   · 전체 목록·신규 상장·거래대금 순위는 REST(/v5/market/tickers)로 45초마다 가볍게 보충한다
   · 가격이 움직이면 그 줄만 값이 바뀌고, 상승은 은은한 초록·하락은 은은한 빨강으로 짧게 번쩍인다
   · 가격 소수 자릿수는 instruments-info의 tickSize를 따라 거래소 화면과 같게 맞춘다
   · 종목을 누르면 데모 트레이딩 화면으로 넘어가 그 종목의 실시간 차트·호가창·주문창이 열린다
   · 스크리너 탭을 벗어나거나 브라우저 탭이 가려지면 소켓·타이머를 모두 정리한다 */
(function () {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  const API = 'https://api.bybit.com/v5/market';
  const WS_URL = {
    linear: 'wss://stream.bybit.com/v5/public/linear',
    spot: 'wss://stream.bybit.com/v5/public/spot',
  };

  const REST_MS = 45000;      // 전체 목록 보충 주기 (실시간 가격은 웹소켓이 맡는다)
  const SORT_MS = 2500;       // 실시간 값으로 정렬 순서를 다시 맞추는 주기 (값이 올 때마다 줄이 튀지 않게)
  const LIVE_MS = 1000;       // 상태 배지·시각 표시 갱신
  const SCROLL_MS = 220;      // 스크롤이 멈춘 뒤 구독 갱신
  const PING_MS = 20000;      // 바이비트 권장 하트비트
  const DEAD_MS = 45000;      // 이 시간 동안 아무 메시지도 없으면 다시 연결
  const BATCH = 10;           // 한 subscribe 요청의 args 상한(현물 10개) — 무기한도 같게 맞춘다
  const BATCH_MS = 150;       // 구독 요청 간격(초당 메시지 수 제한 보호)
  const MAX_SUBS = { linear: 80, spot: 48 };
  const TOP_SUBS = { linear: 56, spot: 24 };

  const FAV_KEY = 'pixel-screener-favs-v1';
  const GLYPHS = {
    BTCUSDT: '₿', ETHUSDT: 'Ξ', SOLUSDT: '◎', XRPUSDT: '✕', DOGEUSDT: 'Ð', BNBUSDT: 'B',
    LTCUSDT: 'Ł', ADAUSDT: '₳', DOTUSDT: '●', TRXUSDT: 'T', LINKUSDT: 'L', BCHUSDT: 'B',
  };
  const BADGE_COLORS = ['#f7931a', '#627eea', '#14f195', '#f0b90b', '#e84142', '#26a17b', '#8247e5', '#ff007a', '#2a5ada', '#00a3ff', '#e6007a', '#c2a633'];

  const state = {
    market: 'linear', rows: [], index: new Map(), els: new Map(), ticks: new Map(), order: [],
    total: 0, query: '', favOnly: false, sort: 'turnover', dir: -1,
    favs: new Set(), linearSymbols: null, linearLoading: false, loading: false, pending: false, error: '',
    active: false, updatedAt: 0, lastTickAt: 0, ticksIn: 0, seq: 0, rowHeight: 44,
    restTimer: 0, sortTimer: 0, liveTimer: 0, scrollTimer: 0, frame: 0, dirty: new Set(),
  };
  const feeds = new Map();

  const reduceMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function say(message, kind) {
    if (typeof toast === 'function') toast(message, kind || '');
    else console.info('[screener]', message);
  }

  // ---- 숫자 표기 (거래소 화면과 같은 축약) ----
  function fmtPrice(value, digits) {
    if (!Number.isFinite(value)) return '—';
    return value.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  // Bybit이 보내 준 lastPrice 문자열은 종목별 호가 단위를 이미 담고 있다("71.480", "0.9585").
  // 그 소수 자릿수를 쓰고 끝의 0만 덜어 내면 거래소 화면과 같은 표기가 된다.
  function priceText(row) {
    let digits = row.decimals;
    if (!Number.isFinite(digits)) digits = row.price >= 1 ? 4 : 8;
    let text = fmtPrice(row.price, Math.min(8, Math.max(0, digits)));
    if (text.indexOf('.') >= 0) {
      text = text.replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '.0');
    } else {
      text += '.0';
    }
    return text;
  }

  function fmtTurn(value) {
    if (!Number.isFinite(value) || value <= 0) return '—';
    const short = (n, unit) => `${Number(n < 10 ? n.toFixed(2) : n.toFixed(1))}${unit}`;
    if (value >= 1e9) return short(value / 1e9, 'B');
    if (value >= 1e6) return `${Math.round(value / 1e6)}M`;
    if (value >= 1e3) return `${Math.round(value / 1e3)}K`;
    return String(Math.round(value));
  }

  function pctText(pct) {
    if (!Number.isFinite(pct)) return '—';
    const sign = pct > 0 ? '+' : pct < 0 ? '−' : '';
    return `${sign}${Math.abs(pct).toFixed(2)}%`;
  }

  function timeText(ms) {
    if (!ms) return '—';
    const at = new Date(ms);
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(at.getHours())}:${pad(at.getMinutes())}:${pad(at.getSeconds())}`;
  }

  function glyph(symbol) {
    return GLYPHS[symbol] || symbol.replace(/^\d+/, '').slice(0, 1) || '•';
  }

  function badgeColor(symbol) {
    let hash = 0;
    for (let i = 0; i < symbol.length; i++) hash = (hash * 31 + symbol.charCodeAt(i)) >>> 0;
    return BADGE_COLORS[hash % BADGE_COLORS.length];
  }

  // ---- 즐겨찾기 (이 브라우저에 저장) ----
  function loadFavs() {
    try {
      const raw = JSON.parse(window.localStorage.getItem(FAV_KEY) || '[]');
      if (Array.isArray(raw)) raw.forEach((symbol) => state.favs.add(String(symbol).toUpperCase()));
    } catch (error) {
      /* 저장이 막힌 브라우저 — 이번 방문에서만 동작한다 */
    }
  }

  function saveFavs() {
    try {
      window.localStorage.setItem(FAV_KEY, JSON.stringify([...state.favs]));
    } catch (error) {
      /* 저장 실패는 무시한다 */
    }
  }

  // ---- Bybit 공개 REST (전체 목록·순위 보충) ----
  async function fetchTickers(market) {
    const response = await fetch(`${API}/tickers?category=${market}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`Bybit 시세 HTTP ${response.status}`);
    const body = await response.json();
    if (!body || body.retCode !== 0 || !body.result || !Array.isArray(body.result.list)) {
      throw new Error((body && body.retMsg) || 'Bybit 응답을 읽을 수 없습니다');
    }
    return body.result.list;
  }

  function normalize(list, market) {
    const rows = [];
    for (const row of list) {
      const symbol = String(row.symbol || '').toUpperCase();
      if (!symbol.endsWith('USDT')) continue; // USDT 페어만 — USDC·역방향 계약은 뺀다
      const price = Number(row.lastPrice);
      const turnover = Number(row.turnover24h);
      const pct = Number(row.price24hPcnt) * 100;
      if (!Number.isFinite(price) || price <= 0) continue;
      const raw = String(row.lastPrice || '');
      const decimals = raw.indexOf('.') >= 0 ? raw.split('.')[1].length : NaN;
      rows.push({
        symbol, market, price, decimals,
        turnover: Number.isFinite(turnover) ? turnover : 0,
        pct: Number.isFinite(pct) ? pct : 0,
        prevPrice: price, liveAt: 0, move: 0,
      });
    }
    return rows;
  }

  // 현물에서 고른 종목이 무기한에도 있는지 확인할 목록 (한 번만 받아 둔다)
  function ensureLinearSymbols() {
    if (state.linearSymbols || state.linearLoading) return;
    state.linearLoading = true;
    fetchTickers('linear')
      .then((list) => {
        const set = new Set();
        list.forEach((row) => {
          const symbol = String(row.symbol || '').toUpperCase();
          if (symbol.endsWith('USDT')) set.add(symbol);
        });
        state.linearSymbols = set;
      })
      .catch(() => { /* 확인하지 못하면 그대로 넘긴다 — 트레이딩 화면이 오류를 알린다 */ })
      .finally(() => { state.linearLoading = false; });
  }

  // ---- 웹소켓 피드 (시장별 연결 하나) ----
  // Bybit 공개 채널 규칙: 한 요청에 args 10개(현물), 연결 전체 args 길이 21,000자 이하,
  // 20초마다 ping, 연결은 IP당 5분에 500개까지 — 화면당 하나만 열고 재접속은 천천히(최대 15초) 한다.
  function feedFor(market) {
    let feed = feeds.get(market);
    if (!feed) {
      feed = {
        market, ws: null, subs: new Set(), want: new Set(), queue: [], pump: 0,
        retry: 0, retryTimer: 0, pingTimer: 0, lastRecv: 0, stopped: true, state: 'idle',
      };
      feeds.set(market, feed);
    }
    return feed;
  }

  function setFeedState(feed, next) {
    if (feed.state === next) return;
    feed.state = next;
    if (feed.market === state.market) paintLive();
  }

  function feedConnect(feed) {
    if (feed.stopped || feed.ws) return;
    if (!state.active || state.market !== feed.market || document.visibilityState === 'hidden') return;
    if (!feed.want.size) return;
    let ws;
    try {
      ws = new WebSocket(WS_URL[feed.market]);
    } catch (error) {
      feedRetry(feed);
      return;
    }
    feed.ws = ws;
    feed.lastRecv = Date.now();
    setFeedState(feed, 'connecting');
    ws.onopen = () => {
      if (feed.ws !== ws) return;
      feed.retry = 0;
      feed.subs.clear();
      feed.queue = [];
      feed.lastRecv = Date.now();
      setFeedState(feed, 'live');
      feedSync(feed, feed.want);
      startPing(feed, ws);
    };
    ws.onmessage = (event) => {
      if (feed.ws !== ws) return;
      feed.lastRecv = Date.now();
      let msg = null;
      try { msg = JSON.parse(event.data); } catch (error) { return; }
      if (!msg || typeof msg !== 'object') return;
      // 우리가 보낸 ping의 응답은 {op:'ping', ret_msg:'pong'}로 온다 — 데이터가 아니다
      if (msg.ret_msg === 'pong' || msg.op === 'pong') return;
      if (msg.op === 'ping') { // 서버가 보낸 ping이면 같은 args로 pong을 돌려준다
        try { ws.send(JSON.stringify({ op: 'pong', args: Array.isArray(msg.args) ? msg.args : [] })); } catch (error) { /* close가 뒤따른다 */ }
        return;
      }
      if (msg.op === 'subscribe' || msg.op === 'unsubscribe') {
        if (msg.success === false) console.warn('[screener] Bybit 구독이 거부되었습니다.', msg.ret_msg);
        return;
      }
      if (typeof msg.topic === 'string' && msg.data) onTicker(feed.market, msg);
    };
    ws.onclose = () => {
      if (feed.ws !== ws) return;
      feed.ws = null;
      feed.subs.clear();
      stopPing(feed);
      setFeedState(feed, 'down');
      feedRetry(feed);
    };
    ws.onerror = () => { /* 곧 close가 온다 */ };
  }

  function feedRetry(feed) {
    if (feed.stopped || feed.retryTimer) return;
    if (!state.active || state.market !== feed.market || document.visibilityState === 'hidden') return;
    if (!feed.want.size) return;
    const wait = Math.min(15000, 800 * Math.pow(2, feed.retry));
    feed.retry = Math.min(feed.retry + 1, 5);
    feed.retryTimer = setTimeout(() => {
      feed.retryTimer = 0;
      feedConnect(feed);
    }, wait);
  }

  function startPing(feed, ws) {
    stopPing(feed);
    feed.pingTimer = setInterval(() => {
      if (feed.ws !== ws || ws.readyState !== 1) return;
      if (Date.now() - feed.lastRecv > DEAD_MS) { // 응답이 끊겼다 — 새로 연결한다
        try { ws.close(); } catch (error) { /* 이미 닫힘 */ }
        return;
      }
      try { ws.send('{"op":"ping"}'); } catch (error) { /* close가 뒤따른다 */ }
    }, PING_MS);
  }

  function stopPing(feed) {
    if (feed.pingTimer) clearInterval(feed.pingTimer);
    feed.pingTimer = 0;
  }

  function feedStop(feed) {
    feed.stopped = true;
    feed.want = new Set();
    feed.subs.clear();
    feed.queue = [];
    feed.retry = 0;
    if (feed.pump) clearInterval(feed.pump);
    if (feed.retryTimer) clearTimeout(feed.retryTimer);
    feed.pump = 0;
    feed.retryTimer = 0;
    stopPing(feed);
    const ws = feed.ws;
    feed.ws = null;
    if (ws) { try { ws.close(); } catch (error) { /* 이미 닫힘 */ } }
    setFeedState(feed, 'idle');
  }

  // 원하는 구독 목록으로 맞춘다 — 빠진 것은 풀고 새 것은 10개씩 끊어 보낸다
  function feedSync(feed, wanted) {
    feed.want = new Set(wanted || []);
    if (feed.stopped) return;
    if (!feed.ws) { feedConnect(feed); return; }
    if (feed.ws.readyState !== 1) return; // onopen에서 다시 맞춘다
    const drop = [...feed.subs].filter((topic) => !feed.want.has(topic));
    const add = [...feed.want].filter((topic) => !feed.subs.has(topic));
    drop.forEach((topic) => feed.subs.delete(topic));
    add.forEach((topic) => feed.subs.add(topic));
    if (drop.length) feed.queue.push({ op: 'unsubscribe', args: drop });
    if (add.length) feed.queue.push({ op: 'subscribe', args: add });
    pumpQueue(feed);
  }

  function pumpQueue(feed) {
    if (feed.pump) return;
    const step = () => {
      const ws = feed.ws;
      if (!feed.queue.length || !ws || ws.readyState !== 1) {
        clearInterval(feed.pump);
        feed.pump = 0;
        feed.queue = [];
        return;
      }
      const item = feed.queue[0];
      const args = item.args.splice(0, BATCH);
      if (args.length) {
        try { ws.send(JSON.stringify({ op: item.op, args })); } catch (error) { /* close가 뒤따른다 */ }
      }
      if (!item.args.length) feed.queue.shift();
    };
    step();
    feed.pump = setInterval(step, BATCH_MS);
  }

  // 지금 받아야 할 종목: 즐겨찾기 → 화면에 보이는 구간(위아래 여유) → 정렬 상위
  function wantedTopics() {
    const rows = viewRows();
    if (!rows.length) return [];
    const picked = [];
    const seen = new Set();
    const push = (symbol) => {
      if (!symbol || seen.has(symbol)) return;
      seen.add(symbol);
      picked.push(`tickers.${symbol}`);
    };
    for (const row of rows) if (state.favs.has(row.symbol)) push(row.symbol);
    const win = viewportWindow(rows.length);
    for (let i = win.start; i < win.end && i < rows.length; i++) push(rows[i].symbol);
    const top = Math.min(TOP_SUBS[state.market] || 40, rows.length);
    for (let i = 0; i < top; i++) push(rows[i].symbol);
    return picked.slice(0, MAX_SUBS[state.market] || 60);
  }

  function viewportWindow(count) {
    const holder = $('#scr-rows');
    const height = state.rowHeight || 44;
    if (!holder || !count) return { start: 0, end: Math.min(count, TOP_SUBS[state.market] || 40) };
    const first = Math.max(0, Math.floor(holder.scrollTop / height));
    const visible = Math.max(1, Math.ceil(holder.clientHeight / height));
    return { start: Math.max(0, first - 6), end: Math.min(count, first + visible + 18) };
  }

  function refreshSubs() {
    if (!state.active || document.visibilityState === 'hidden') return;
    const feed = feedFor(state.market);
    feed.stopped = false;
    feedSync(feed, wantedTopics());
  }

  // ---- 실시간 값 반영 ----
  function onTicker(market, msg) {
    if (!state.active || market !== state.market) return;
    const topic = String(msg.topic || '');
    if (topic.indexOf('tickers.') !== 0) return;
    const symbol = topic.slice(8).toUpperCase();
    const data = Array.isArray(msg.data) ? msg.data[0] : msg.data;
    if (!symbol || !data) return;

    let tick = data;
    if (market === 'linear') {
      // 무기한 ticker는 delta — 바뀐 필드만 오므로 종목별로 합쳐 둬야 한다
      const cache = state.ticks.get(symbol) || {};
      for (const key in data) cache[key] = data[key];
      state.ticks.set(symbol, cache);
      tick = cache;
    }

    const row = state.index.get(symbol);
    if (row) {
      const price = Number(tick.lastPrice);
      const pct = Number(tick.price24hPcnt) * 100;
      const turnover = Number(tick.turnover24h);
      if (Number.isFinite(price) && price > 0) {
        const raw = String(tick.lastPrice || '');
        if (raw.indexOf('.') >= 0) row.decimals = raw.split('.')[1].length;
        if (price !== row.price) {
          row.prevPrice = row.price;
          row.move = price > row.price ? 1 : -1;
          row.price = price;
        }
      }
      if (Number.isFinite(pct)) row.pct = pct;
      if (Number.isFinite(turnover) && turnover > 0) row.turnover = turnover;
      row.liveAt = Date.now();
      markDirty(symbol);
    }

    state.ticksIn += 1;
    state.lastTickAt = Date.now();
    scheduleLive();
  }

  function markDirty(symbol) {
    state.dirty.add(symbol);
    if (!state.frame) state.frame = requestAnimationFrame(flushDirty);
  }

  function flushDirty() {
    state.frame = 0;
    const symbols = [...state.dirty];
    state.dirty.clear();
    for (const symbol of symbols) {
      const row = state.index.get(symbol);
      const el = state.els.get(symbol);
      if (!row || !el) continue;
      paint(el, row);
      if (row.move) {
        flash(el, row.move > 0);
        row.move = 0;
      }
    }
  }

  function flash(el, up) {
    if (reduceMotion || !el || typeof el.animate !== 'function') return;
    const at = performance.now();
    if (el.flashAt && at - el.flashAt < 900) return; // 초당 수십 번 오는 값을 다 번쩍이지 않게
    el.flashAt = at;
    const tone = up ? '39, 201, 139' : '240, 91, 110';
    try {
      el.animate([{ backgroundColor: `rgba(${tone}, .1)` }, { backgroundColor: `rgba(${tone}, 0)` }], { duration: 480, easing: 'ease-out' });
      el.priceEl.animate([{ color: up ? '#63e6b6' : '#ff9aa5' }, { color: '#e8eef7' }], { duration: 480, easing: 'ease-out' });
    } catch (error) {
      /* Web Animations를 지원하지 않는 브라우저 — 숫자만 갱신한다 */
    }
  }

  // ---- 그리기 ----
  function compare(a, b) {
    if (state.sort === 'symbol') return a.symbol.localeCompare(b.symbol) * state.dir;
    if (state.sort === 'price') return (a.price - b.price) * state.dir;
    if (state.sort === 'change') return (a.pct - b.pct) * state.dir;
    return (a.turnover - b.turnover) * state.dir;
  }

  function viewRows() {
    const query = state.query.trim().toUpperCase();
    let rows = state.rows;
    if (query) rows = rows.filter((row) => row.symbol.includes(query));
    if (state.favOnly) rows = rows.filter((row) => state.favs.has(row.symbol));
    return [...rows].sort(compare);
  }

  function createRow(row) {
    const el = document.createElement('div');
    el.className = 'scr-row';
    el.setAttribute('role', 'button');
    el.setAttribute('tabindex', '0');
    el.dataset.symbol = row.symbol;
    el.setAttribute('aria-label', row.symbol);
    el.innerHTML = `<span class="scr-name">`
      + `<button class="scr-star" type="button" data-fav="${esc(row.symbol)}" aria-pressed="false" aria-label="즐겨찾기" title="즐겨찾기">★</button>`
      + `<i class="scr-badge" style="--badge:${badgeColor(row.symbol)}" aria-hidden="true">${esc(glyph(row.symbol))}</i>`
      + `<span class="scr-sym"><b><span class="scr-base">${esc(row.symbol.replace(/USDT$/, ''))}</span><span class="scr-quote">USDT</span></b><small></small></span></span>`
      + `<span class="scr-price"></span><span class="scr-chg scr-flat"></span><span class="scr-turn"></span>`;
    el.priceEl = $('.scr-price', el);
    el.chgEl = $('.scr-chg', el);
    el.turnEl = $('.scr-turn', el);
    el.turnSmall = $('.scr-sym small', el);
    el.starEl = $('.scr-star', el);
    return el;
  }

  function paint(el, row) {
    const shown = priceText(row);
    if (el.priceEl.textContent !== shown) el.priceEl.textContent = shown;
    const chg = pctText(row.pct);
    if (el.chgEl.textContent !== chg) el.chgEl.textContent = chg;
    const tone = row.pct > 0 ? 'scr-chg scr-up' : row.pct < 0 ? 'scr-chg scr-down' : 'scr-chg scr-flat';
    if (el.chgEl.className !== tone) el.chgEl.className = tone;
    const turn = fmtTurn(row.turnover);
    if (el.turnEl.textContent !== turn) el.turnEl.textContent = turn;
    if (el.turnSmall.textContent !== turn) el.turnSmall.textContent = turn;
    const fav = state.favs.has(row.symbol);
    if (el.starEl.getAttribute('aria-pressed') !== String(fav)) {
      el.starEl.setAttribute('aria-pressed', String(fav));
      el.starEl.title = fav ? '즐겨찾기 해제' : '즐겨찾기';
    }
    const label = `${row.symbol} ${shown}`;
    if (el.getAttribute('aria-label') !== label) el.setAttribute('aria-label', label);
  }

  // 목록은 줄 DOM을 종목별로 재사용한다 — 실시간 값이 와도 스크롤·포커스가 튀지 않는다
  function syncList(rows) {
    const holder = $('#scr-rows');
    if (!holder) return;
    state.order = rows.map((row) => row.symbol);

    if (!rows.length) {
      state.els.forEach((el) => el.remove());
      state.els.clear();
      const message = state.error
        ? `Bybit 시세를 불러오지 못했습니다 — ${state.error}`
        : state.rows.length ? '조건에 맞는 종목이 없습니다' : '종목이 없습니다';
      holder.innerHTML = `<div class="scr-msg${state.error ? ' scr-msg-err' : ''}">${esc(message)}</div>`;
      return;
    }

    const note = holder.firstElementChild;
    if (note && note.classList.contains('scr-msg')) note.remove();

    const live = new Set(state.order);
    for (const [symbol, el] of [...state.els]) {
      if (!live.has(symbol)) { el.remove(); state.els.delete(symbol); }
    }

    let cursor = holder.firstElementChild;
    for (const row of rows) {
      let el = state.els.get(row.symbol);
      if (!el) { el = createRow(row); state.els.set(row.symbol, el); }
      if (cursor === el) cursor = cursor.nextElementSibling;
      else holder.insertBefore(el, cursor);
      paint(el, row);
    }
    const first = holder.firstElementChild;
    if (first) {
      const height = first.offsetHeight;
      if (height > 20) state.rowHeight = height;
    }
  }

  function render() {
    const rows = viewRows();
    $('#scr-count').innerHTML = `<b>${rows.length}</b>종목 / 전체 ${state.total}`;
    syncList(rows);
  }

  function sortHead() {
    $$('.scr-head [data-sort]').forEach((button) => {
      const on = button.dataset.sort === state.sort;
      button.classList.toggle('on', on);
      const arrow = button.querySelector('i');
      if (arrow) arrow.textContent = on ? (state.dir > 0 ? '▴' : '▾') : '';
    });
  }

  // 값이 움직여도 줄이 계속 튀지 않게, 정렬 순서는 2.5초마다 한 번만 다시 맞춘다
  // (순서가 바뀌면 그때 상위에 새로 들어온 종목도 바로 구독해 실시간 값을 받는다)
  function refreshOrder() {
    if (!state.active || state.sort === 'symbol') return;
    const rows = viewRows();
    if (rows.length !== state.order.length) { render(); refreshSubs(); return; }
    for (let i = 0; i < rows.length; i++) {
      if (rows[i].symbol !== state.order[i]) { syncList(rows); refreshSubs(); return; }
    }
  }

  function paintLive() {
    const chip = $('#scr-live');
    const feed = feeds.get(state.market);
    const status = feed ? feed.state : 'idle';
    const labels = { live: ['live', '실시간'], connecting: ['connecting', '연결 중'], down: ['down', '재연결 중'], idle: ['idle', '대기'] };
    const entry = labels[status] || labels.idle;
    if (chip) {
      chip.dataset.state = entry[0];
      const text = $('em', chip);
      if (text && text.textContent !== entry[1]) text.textContent = entry[1];
    }
    const foot = $('#scr-foot-live');
    if (foot) foot.textContent = status === 'live' ? '실시간 WebSocket · 45초 전체 갱신' : '실시간 연결 중 · 45초 전체 갱신';
    const updated = $('#scr-updated');
    if (updated) {
      if (status === 'live' && state.lastTickAt) updated.textContent = `실시간 ${timeText(state.lastTickAt)}`;
      else updated.textContent = state.updatedAt ? `갱신 ${timeText(state.updatedAt)}` : '—';
    }
  }

  function scheduleLive() {
    if (state.liveTimer) return;
    state.liveTimer = setTimeout(() => { state.liveTimer = 0; paintLive(); }, LIVE_MS);
  }

  // ---- 불러오기 ----
  function mergeRows(list, market) {
    const rows = normalize(list, market);
    const now = Date.now();
    for (const row of rows) {
      const old = state.index.get(row.symbol);
      if (!old) continue;
      if (now - (old.liveAt || 0) < 8000) {
        // 웹소켓이 방금 넣은 값이 REST보다 최신이다 — 그 값만 살리고 나머지는 REST를 쓴다
        row.price = old.price;
        row.pct = old.pct;
        row.turnover = old.turnover;
        row.liveAt = old.liveAt;
      }
    }
    return rows;
  }

  async function load(force) {
    if (!state.active && !force) return;
    if (document.visibilityState === 'hidden' && !force) return;
    if (state.loading) { state.pending = true; return; }
    state.loading = true;
    const seq = ++state.seq;
    const market = state.market;
    $('#scr-refresh').classList.add('spinning');
    try {
      const list = await fetchTickers(market);
      if (seq !== state.seq || market !== state.market) return;
      state.rows = mergeRows(list, market);
      state.index = new Map(state.rows.map((row) => [row.symbol, row]));
      state.total = state.rows.length;
      state.error = '';
      state.updatedAt = Date.now();
      ensureLinearSymbols();
      render();
      paintLive();
      refreshSubs();
    } catch (error) {
      if (seq !== state.seq) return;
      state.error = (error && error.message) || '시세를 불러오지 못했습니다';
      if (state.rows.length) {
        paintLive();
        say(`${state.error} — 잠시 뒤 다시 시도합니다`, 'err');
      } else {
        render();
      }
      console.warn('[screener] Bybit 시세를 불러오지 못했습니다.', error);
    } finally {
      state.loading = false;
      $('#scr-refresh').classList.remove('spinning');
      if (state.pending) { state.pending = false; load(); }
    }
  }

  function setMarket(market) {
    if (market !== 'linear' && market !== 'spot') return;
    const previous = feeds.get(state.market);
    if (previous) feedStop(previous);
    state.market = market;
    state.rows = [];
    state.index = new Map();
    state.ticks.clear();
    state.order = [];
    state.total = 0;
    state.error = '';
    state.updatedAt = 0;
    state.lastTickAt = 0;
    state.ticksIn = 0;
    const els = $('#scr-rows');
    if (els) {
      state.els.forEach((el) => el.remove());
      state.els.clear();
      els.innerHTML = '<div class="scr-msg">Bybit 시세를 불러오는 중…</div>';
    }
    $$('#scr-market button').forEach((button) => {
      const on = button.dataset.market === market;
      button.classList.toggle('active', on);
      button.setAttribute('aria-pressed', String(on));
    });
    $('#scr-foot-market').textContent = market === 'spot' ? 'Bybit 현물 · USDT 페어' : 'Bybit 무기한 선물 · USDT 페어';
    $('#scr-count').textContent = '—';
    paintLive();
    load();
  }

  function openSymbol(symbol, market) {
    const code = String(symbol || '').toUpperCase();
    if (!code) return;
    if (market === 'spot' && state.linearSymbols && !state.linearSymbols.has(code)) {
      say(`${code} — 무기한 선물에 상장되지 않은 현물 전용 종목입니다`, 'err');
      return;
    }
    if (window.LiteTrading && typeof window.LiteTrading.showSymbol === 'function') {
      window.LiteTrading.showSymbol(code);
    }
  }

  // ---- 화면 만들기 ----
  let page = null;

  function build() {
    if ($('#screener-page')) { page = $('#screener-page'); return; }
    page = document.createElement('section');
    page.id = 'screener-page';
    page.setAttribute('aria-label', '스크리너');
    page.innerHTML = `
      <header class="scr-bar">
        <div class="scr-market" id="scr-market" role="group" aria-label="시장 구분">
          <button type="button" data-market="linear" class="active" aria-pressed="true">무기한</button>
          <button type="button" data-market="spot" aria-pressed="false">현물</button>
        </div>
        <label class="scr-search"><svg viewBox="0 0 16 16" width="13" height="13" aria-hidden="true"><circle cx="7" cy="7" r="4.6" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M10.4 10.4 14 14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg><input id="scr-search" type="search" placeholder="종목 검색 (예: BTC)" autocomplete="off" spellcheck="false" aria-label="종목 검색"></label>
        <button id="scr-fav-toggle" type="button" aria-pressed="false" title="즐겨찾기만 보기"><span aria-hidden="true">★</span><em>즐겨찾기</em></button>
        <div class="scr-meta">
          <span id="scr-live" class="scr-live" data-state="idle" title="바이비트 공개 웹소켓"><i></i><em>대기</em></span>
          <span id="scr-count">—</span>
          <span class="scr-venue"><i></i>Bybit</span>
          <button id="scr-refresh" type="button" title="지금 새로고침" aria-label="새로고침"><svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true"><path d="M13.2 8a5.2 5.2 0 1 1-1.6-3.8" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><path d="M13.6 1.6v3.6H10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
          <span id="scr-updated">—</span>
        </div>
      </header>
      <div class="scr-head" role="row">
        <button type="button" data-sort="symbol">종목<i></i></button>
        <button type="button" data-sort="price">가격<i></i></button>
        <button type="button" data-sort="change">24h<i></i></button>
        <button type="button" data-sort="turnover" class="on">거래대금<i>▾</i></button>
      </div>
      <div id="scr-rows" role="rowgroup"><div class="scr-msg">Bybit 시세를 불러오는 중…</div></div>
      <footer class="scr-foot"><span id="scr-foot-market">Bybit 무기한 선물 · USDT 페어</span><span id="scr-foot-live">실시간 연결 중 · 45초 전체 갱신</span><span>모의 거래 전용 · 투자 조언 아님</span></footer>`;
    document.body.appendChild(page);

    $('#scr-market').addEventListener('click', (event) => {
      const button = event.target.closest('[data-market]');
      if (button) setMarket(button.dataset.market);
    });
    $('#scr-search').addEventListener('input', (event) => {
      state.query = event.target.value || '';
      render();
      refreshSubs();
    });
    $('#scr-search').addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { event.target.value = ''; state.query = ''; render(); refreshSubs(); }
    });
    $('#scr-fav-toggle').addEventListener('click', () => {
      state.favOnly = !state.favOnly;
      const button = $('#scr-fav-toggle');
      button.setAttribute('aria-pressed', String(state.favOnly));
      button.classList.toggle('on', state.favOnly);
      if (state.favOnly && !state.favs.size) say('즐겨찾기가 비어 있습니다 — 종목 왼쪽의 ★를 눌러 담아 두세요');
      render();
      refreshSubs();
    });
    $('#scr-refresh').addEventListener('click', () => load(true));
    $('.scr-head').addEventListener('click', (event) => {
      const button = event.target.closest('[data-sort]');
      if (!button) return;
      const key = button.dataset.sort;
      if (state.sort === key) state.dir = -state.dir;
      else { state.sort = key; state.dir = key === 'symbol' ? 1 : -1; }
      sortHead();
      render();
      refreshSubs();
    });
    $('#scr-rows').addEventListener('scroll', () => {
      if (state.scrollTimer) clearTimeout(state.scrollTimer);
      state.scrollTimer = setTimeout(() => {
        state.scrollTimer = 0;
        refreshSubs();
      }, SCROLL_MS);
    }, { passive: true });
    $('#scr-rows').addEventListener('click', (event) => {
      const star = event.target.closest('[data-fav]');
      if (star) {
        const symbol = star.dataset.fav;
        if (state.favs.has(symbol)) state.favs.delete(symbol);
        else state.favs.add(symbol);
        saveFavs();
        render();
        refreshSubs();
        return;
      }
      const row = event.target.closest('.scr-row');
      if (row) openSymbol(row.dataset.symbol, state.market);
    });
    $('#scr-rows').addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      const row = event.target.closest('.scr-row');
      if (!row || event.target.closest('[data-fav]')) return;
      event.preventDefault();
      openSymbol(row.dataset.symbol, state.market);
    });
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') {
        if (state.active) resume();
      } else if (state.active) {
        pause();
      }
    });
    window.addEventListener('online', () => { if (state.active) refreshSubs(); });
  }

  function startTimers() {
    stopTimers();
    state.restTimer = setInterval(() => load(), REST_MS);
    state.sortTimer = setInterval(refreshOrder, SORT_MS);
  }

  function stopTimers() {
    if (state.restTimer) clearInterval(state.restTimer);
    if (state.sortTimer) clearInterval(state.sortTimer);
    state.restTimer = 0;
    state.sortTimer = 0;
    if (state.liveTimer) clearTimeout(state.liveTimer);
    state.liveTimer = 0;
  }

  function pause() {
    stopTimers();
    for (const feed of feeds.values()) feedStop(feed);
    paintLive();
  }

  function resume() {
    if (!state.active || document.visibilityState === 'hidden') return;
    if (!state.rows.length || Date.now() - state.updatedAt > REST_MS) load();
    else { render(); paintLive(); }
    startTimers();
    refreshSubs();
  }

  // 탭이 보일 때만 돈다 — 사무실·트레이딩 화면에서는 소켓도 타이머도 남기지 않는다
  function setActive(active) {
    build();
    const next = !!active;
    if (state.active === next) {
      if (next) refreshSubs();
      return;
    }
    state.active = next;
    if (!state.active) {
      pause();
      return;
    }
    resume();
  }

  loadFavs();
  build();
  window.LiteScreener = {
    setActive,
    refresh: () => load(true),
    setMarket,
    debug: () => {
      const feed = feeds.get(state.market);
      return {
        market: state.market,
        active: state.active,
        socket: feed ? feed.state : 'idle',
        subscribed: feed && feed.ws ? feed.subs.size : 0,
        rows: state.rows.length,
        listed: state.total,
        ticks: state.ticksIn,
        lastTickAt: state.lastTickAt,
        firstSymbol: state.rows[0] && state.rows[0].symbol,
      };
    },
  };
})();
