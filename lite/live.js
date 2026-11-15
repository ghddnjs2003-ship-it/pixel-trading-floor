/* PIXEL TRADING FLOOR 데모 버전 — 실시간 시세 (바이비트 선물 웹소켓, chart.js 다음에 로드)
   5초·10초·60초 폴링 대신 거래소 화면처럼 1초 안에 움직인다.
   · kline.1.<종목> — 진행 중인 1분봉(현재가·고저). 차트 마지막 봉·큰 가격·보유 포지션 평가와 체결
   · tickers.<종목> — 24시간 등락률. 큰 가격 옆 등락률과 하단 시세 띠
   웹소켓이 막히거나 끊기면 기존 폴링이 그대로 이어받는다(chart.js의 poll·shim.js의 settleTick).
   createFeed·parse·wanted까지는 순수 함수(Node 테스트), install()이 브라우저에 붙인다. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LiteLive = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const URL_WS = 'wss://stream.bybit.com/v5/public/linear';
  const URL_KLINE = 'https://api.bybit.com/v5/market/kline';
  const URL_TICKER = 'https://api.bybit.com/v5/market/tickers';
  const TAPE_SYMBOLS = new Set(['BTC', 'ETH', 'SOL', 'XRP']);
  const num = (v) => (v == null || v === '' ? NaN : +v);

  const streamName = (symbol, kind) => `${kind === 'kline' ? 'kline.1' : 'tickers'}.${String(symbol).toUpperCase()}`;

  async function fetchPrice(symbol, fetchFn) {
    const request = fetchFn || fetch;
    const url = `${URL_TICKER}?category=linear&symbol=${encodeURIComponent(String(symbol).toUpperCase())}`;
    const response = await request(url);
    if (!response.ok) throw new Error(`Bybit ticker HTTP ${response.status}`);
    const body = await response.json();
    const price = num(body && body.retCode === 0 && body.result && body.result.list && body.result.list[0] && body.result.list[0].lastPrice);
    if (!Number.isFinite(price) || !(price > 0)) throw new Error(`Bybit ticker response error: ${body && body.retMsg || 'invalid response'}`);
    return price;
  }

  // 거래소 메시지 → 우리 모양. 구독 응답·알 수 없는 메시지·숫자가 깨진 건 null
  function parse(m) {
    if (!m || typeof m !== 'object') return null;
    if (typeof m.topic !== 'string' || !m.data) return null;
    const symbol = String(m.data.symbol || m.topic.split('.').pop()).toUpperCase();
    if (m.topic.startsWith('orderbook.') && !Array.isArray(m.data)) {
      const levels = (rows) => (Array.isArray(rows) ? rows : []).map((row) => [num(row[0]), num(row[1])])
        .filter((row) => row.every(Number.isFinite));
      return { type: 'orderbook', symbol, action: m.type, bids: levels(m.data.b), asks: levels(m.data.a) };
    }
    if (m.topic.startsWith('kline.') && Array.isArray(m.data) && m.data.length) {
      const k = m.data[m.data.length - 1];
      const t = num(k.start); const o = num(k.open); const h = num(k.high); const l = num(k.low); const c = num(k.close);
      if (![t, o, h, l, c].every(Number.isFinite)) return null;
      return { type: 'kline', symbol, topic: m.topic, interval: m.topic.split('.')[1], t, o, h, l, c, v: num(k.volume), closed: !!k.confirm };
    }
    if (m.topic.startsWith('tickers.') && !Array.isArray(m.data)) {
      const price = num(m.data.lastPrice); const open = num(m.data.prevPrice24h);
      if (!Number.isFinite(price) || !Number.isFinite(open) || !(open > 0)) return null;
      return { type: 'ticker', symbol, price, changePct: ((price - open) / open) * 100 };
    }
    return null;
  }

  // 지금 받아야 할 스트림 — 차트 종목(시세+등락률), 보유 종목(시세), 시세 띠(등락률).
  function wanted({ chart, open, tape }) {
    const out = new Set();
    const perp = (x) => x && x.symbol && x.axis !== 'spot';
    if (perp(chart)) { out.add(streamName(chart.symbol, 'kline')); out.add(streamName(chart.symbol, 'ticker')); }
    for (const p of open || []) if (perp(p)) out.add(streamName(p.symbol, 'kline'));
    for (const s of tape || []) if (TAPE_SYMBOLS.has(s)) out.add(streamName(`${s}USDT`, 'ticker'));
    return [...out];
  }

  // 웹소켓 한 줄 — 필요한 스트림 목록(want)만 알려 주면 접속·구독·재접속(1→2→4…최대 15초)을 맡는다
  function createFeed(opts = {}) {
    const WS = opts.WebSocketImpl === undefined ? (typeof WebSocket === 'function' ? WebSocket : null) : opts.WebSocketImpl;
    const setTimer = opts.setTimer || ((fn, ms) => setTimeout(fn, ms));
    const clearTimer = opts.clearTimer || ((id) => clearTimeout(id));
    const now = opts.now || (() => Date.now());
    let desired = new Set();
    let subscribed = new Set();
    let ws = null;
    let state = 'idle';
    let attempt = 0;
    let timer = null;
    let pingTimer = null;
    let lastAt = 0;
    let msgId = 0;

    const setState = (s) => {
      if (s === state) return;
      state = s;
      if (opts.onState) opts.onState(s);
    };
    const send = (op, args) => {
      if (!ws || ws.readyState !== 1 || !args.length) return;
      try { ws.send(JSON.stringify({ op, args, req_id: ++msgId })); } catch (_) { /* 곧 close가 온다 */ }
    };
    const stopPing = () => {
      if (pingTimer) { clearTimer(pingTimer); pingTimer = null; }
    };
    function keepAlive(sock) {
      stopPing();
      pingTimer = setTimer(() => {
        pingTimer = null;
        if (ws !== sock || sock.readyState !== 1) return;
        try { sock.send(JSON.stringify({ op: 'ping' })); } catch (_) { return; }
        keepAlive(sock);
      }, 20000);
    };
    function sync() {
      const off = [...subscribed].filter((x) => !desired.has(x));
      const on = [...desired].filter((x) => !subscribed.has(x));
      if (off.length) { send('unsubscribe', off); off.forEach((x) => subscribed.delete(x)); }
      if (on.length) { send('subscribe', on); on.forEach((x) => subscribed.add(x)); }
    }
    function connect() {
      timer = null;
      if (!WS || !desired.size || ws) return;
      let sock;
      try { sock = new WS(URL_WS); } catch (_) { sock = null; }
      if (!sock) { schedule(); return; }
      ws = sock;
      sock.onopen = () => {
        if (ws !== sock) return;
        attempt = 0;
        subscribed = new Set();
        setState('live');
        sync();
        keepAlive(sock);
      };
      sock.onmessage = (ev) => {
        let m = null;
        try { m = JSON.parse(ev.data); } catch (_) { m = null; }
        const tk = parse(m);
        if (!tk) return;
        lastAt = now();
        if (opts.onTick) opts.onTick(tk);
      };
      sock.onclose = () => {
        if (ws !== sock) return;
        stopPing();
        ws = null;
        subscribed = new Set();
        if (desired.size) { setState('down'); schedule(); }
      };
      sock.onerror = () => { /* 이어서 close가 온다 */ };
    }
    function schedule() {
      if (timer || !desired.size) return;
      const ms = Math.min(15000, 1000 * Math.pow(2, attempt));
      attempt += 1;
      timer = setTimer(connect, ms);
    }
    function closeSocket() {
      const s = ws;
      stopPing();
      ws = null;
      subscribed = new Set();
      if (s) { try { s.close(); } catch (_) { /* 이미 닫힘 */ } }
    }

    return {
      want(list) {
        desired = new Set(list || []);
        if (!WS) { if (desired.size) setState('unsupported'); return; }
        if (!desired.size) { this.stop(); return; }
        if (ws) sync();
        else if (!timer) connect();
      },
      stop() {
        desired = new Set();
        if (timer) { clearTimer(timer); timer = null; }
        attempt = 0;
        closeSocket();
        if (state !== 'unsupported') setState('idle');
      },
      state: () => state,
      lastAt: () => lastAt,
      streams: () => [...desired],
    };
  }

  /* ----------------------------------------------------------------- 브라우저 */
  function install(win) {
    const doc = win.document;
    const LITE = win.LITE;
    if (!LITE || !LITE.chart || !LITE.paper) return null;
    const TAPE = (LITE.data && LITE.data.TAPE) || [];
    const tapeNow = new Map();
    let openSet = new Set();
    let uiTimer = 0;
    let tapeAt = 0;

    function refreshOpen() { openSet = new Set(LITE.paper.openSymbols().map((s) => s.symbol)); }

    // 보유 종목이 있으면 화면 밖이어도 받는다(체결이 늦지 않게), 없으면 차트가 보일 때만
    function refresh() {
      refreshOpen();
      const st = LITE.chart.state();
      const open = LITE.paper.openSymbols();
      if (doc.hidden || (!LITE.chart.isSeen() && !open.length)) { feed.want([]); return; }
      feed.want(wanted({ chart: st.symbol ? { symbol: st.symbol, axis: st.axis } : null, open, tape: TAPE }));
    }

    // 오른쪽 가상 포지션 칸·모의 계좌 잔고는 서버 응답 모양을 읽는 app.js·hq.js가 그리므로 같은 길로 새로 그린다
    function scheduleUI() {
      if (uiTimer) return;
      uiTimer = win.setTimeout(() => {
        uiTimer = 0;
        if (typeof win.loadPositions === 'function') win.loadPositions();
      }, 500);
    }

    function onTick(tk) {
      if (tk.type === 'kline') {
        if (openSet.has(tk.symbol)) {
          const r = LITE.paper.tick(tk.symbol, tk.c);
          for (const p of r.done) LITE.bus.emit({ type: 'position', action: 'close', position: p });
          if (r.done.length) refreshOpen();
          if (r.marked) scheduleUI();
        }
        LITE.chart.applyTick(tk);
        return;
      }
      const sym = tk.symbol.replace(/USDT$/, '');
      tapeNow.set(sym, { sym, price: tk.price, changePct: tk.changePct });
      const cs = LITE.chart.state();
      if (cs.symbol === tk.symbol) {
        const el = doc.querySelector('#board-change');
        if (el) {
          el.textContent = `${tk.changePct >= 0 ? '+' : ''}${tk.changePct.toFixed(2)}%`;
          el.className = tk.changePct >= 0 ? 'up' : 'down';
        }
      }
      const t = Date.now();
      if (t - tapeAt > 2000 && typeof win.renderTape === 'function' && TAPE.every((s) => tapeNow.has(s))) {
        tapeAt = t;
        win.renderTape(TAPE.map((s) => tapeNow.get(s)));
      }
    }

    const feed = createFeed({ WebSocketImpl: win.WebSocket, onTick });
    LITE.bus.subscribe((ev) => { if (ev && (ev.type === 'market' || ev.type === 'position')) refresh(); });
    doc.addEventListener('visibilitychange', refresh);
    win.setInterval(refresh, 2000);
    win.setTimeout(refresh, 600);
    LITE.live = { feed, refresh };
    loadDefaultMarket();
    return LITE.live;

    async function loadDefaultMarket() {
      if (LITE.chart.state().symbol) return;
      try {
        const url = `${URL_KLINE}?category=linear&symbol=BTCUSDT&interval=5&limit=144`;
        const response = await win.fetch(url);
        if (!response.ok) throw new Error(`Bybit kline HTTP ${response.status}`);
        const body = await response.json();
        const rows = body && body.retCode === 0 && body.result && body.result.list;
        if (!Array.isArray(rows)) throw new Error(`Bybit kline response error: ${body && body.retMsg || 'invalid response'}`);
        const candles = rows.map((row) => ({
          t: num(row[0]), o: num(row[1]), h: num(row[2]), l: num(row[3]), c: num(row[4]), v: num(row[5]),
        })).filter((c) => [c.t, c.o, c.h, c.l, c.c].every(Number.isFinite)).sort((a, b) => a.t - b.t);
        if (candles.length < 2) throw new Error(`Bybit returned only ${candles.length} valid candles`);
        if (LITE.chart.state().symbol) return;
        LITE.bus.emit({ type: 'market', symbol: 'BTCUSDT', display: 'BTCUSDT', axis: 'perp', candles });
        const symbolEl = doc.querySelector('#board-symbol');
        const priceEl = doc.querySelector('#board-price');
        if (symbolEl && symbolEl.textContent.trim() === '—') symbolEl.textContent = 'BTCUSDT';
        if (priceEl && priceEl.textContent.trim() === '—') priceEl.textContent = `$${candles[candles.length - 1].c.toLocaleString('en-US')}`;
      } catch (error) {
        if (win.console && typeof win.console.warn === 'function') win.console.warn('[LiteLive] Bybit BTCUSDT chart initialization failed:', error);
        if (LITE.chart.state().symbol || !LITE.data || typeof LITE.data.fetchRecentCandles !== 'function') return;
        try {
          const candles = await LITE.data.fetchRecentCandles('BTCUSDT', { axis: 'perp', limit: 144 });
          if (!candles || candles.length < 2 || LITE.chart.state().symbol) return;
          LITE.bus.emit({ type: 'market', symbol: 'BTCUSDT', display: 'BTCUSDT', axis: 'perp', candles });
          const symbolEl = doc.querySelector('#board-symbol');
          const priceEl = doc.querySelector('#board-price');
          if (symbolEl && symbolEl.textContent.trim() === '—') symbolEl.textContent = 'BTCUSDT';
          if (priceEl && priceEl.textContent.trim() === '—') priceEl.textContent = `$${candles[candles.length - 1].c.toLocaleString('en-US')}`;
        } catch (fallbackError) {
          if (win.console && typeof win.console.warn === 'function') win.console.warn('[LiteLive] BTCUSDT fallback candle load failed:', fallbackError);
        }
      }
    }
  }

  if (typeof window !== 'undefined' && window.document && window.LITE && !window.LITE_NO_BOOT) install(window);

  return { URL_WS, URL_KLINE, URL_TICKER, streamName, fetchPrice, parse, wanted, createFeed, install };
});
