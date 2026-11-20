/* PIXEL TRADING FLOOR 데모 버전 — 픽셀 오피스 상단 차트 (hq.js 다음에 로드, app.js의 drawChart를 대신한다)
   자체 캔버스 차트는 없앴다 — 트레이딩뷰 공식 위젯(Advanced Real-Time Chart) 하나만 띄운다.
   2026-11-20 홍원 님 "아니 저렇게 말고 우리차트는 지우고 트레이딩뷰만 사용할거야 전부다"
   · 종목은 분석·워치리스트 클릭·보유 포지션을 따라 바뀐다 (코인은 BYBIT 무기한, 미국 주식은 NASDAQ)
   · 직원 잡담·주문창 현재가가 쓰는 candles()/state()는 최근 5분봉을 20초에 한 번 조용히 받아 채운다
   model()은 남겨 둔 순수 함수(Node 테스트), install()이 브라우저에서 위젯을 띄운다. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./brain.js'));
  else root.LiteChart = factory(root.LiteBrain);
})(typeof self !== 'undefined' ? self : this, function (Brain) {
  'use strict';

  const BARS = 144;
  const BAR_MS = 300000;
  const fmt = Brain.fmtPrice;
  const signed = (v, d) => `${v < 0 ? '−' : '+'}${Math.abs(v).toFixed(d)}`;
  const movePct = (entry, x) => ((x - entry) / entry) * 100;
  const EXIT = { 익절: ['win', '익절'], 손절: ['loss', '손절'], '강제 청산': ['liq', '청산'], '보유 시간 만료': ['exit', '만료'], '수동 청산': ['exit', '정리'] };

  // 진행 중인 봉을 이어 붙인다 — 같은 시각은 새 값으로, 새 시각은 뒤에, 최대 max개
  function mergeCandles(base, recent, max = BARS) {
    const by = new Map();
    for (const c of base || []) by.set(c.t, c);
    for (const c of recent || []) by.set(c.t, c);
    return [...by.values()].sort((a, b) => a.t - b.t).slice(-max);
  }

  // 실시간 틱(1분봉 현재 상태)을 진행 중인 5분봉에 반영 — 종가·고저만 바꾸고, 봉이 바뀌었으면 새 봉을 연다. 원본은 그대로.
  function applyTick(candles, tick, max = BARS) {
    if (!candles || !candles.length || !tick || !Number.isFinite(tick.price)) return candles;
    const step = barMs(candles);
    const bucket = Math.floor(tick.t / step) * step;
    const last = candles[candles.length - 1];
    if (bucket < last.t) return candles;
    const hi = Number.isFinite(tick.high) ? tick.high : tick.price;
    const lo = Number.isFinite(tick.low) ? tick.low : tick.price;
    if (bucket === last.t) {
      const next = { ...last, c: tick.price, h: Math.max(last.h, hi, tick.price), l: Math.min(last.l, lo, tick.price) };
      return [...candles.slice(0, -1), next];
    }
    const o = Number.isFinite(tick.open) ? tick.open : tick.price;
    return [...candles, { t: bucket, o, h: Math.max(hi, o, tick.price), l: Math.min(lo, o, tick.price), c: tick.price }].slice(-max);
  }

  function barMs(candles) {
    return candles && candles.length > 1 ? candles[candles.length - 1].t - candles[candles.length - 2].t : BAR_MS;
  }

  // 이번에 받아 올 봉 개수 — 마지막 봉(진행 중이었을 수 있음)부터 지금까지 + 여유, 오래 비었으면 전부 새로
  function barsToFetch(candles, now) {
    if (!candles || !candles.length) return BARS;
    const gap = now - candles[candles.length - 1].t;
    return Math.max(3, Math.min(BARS, Math.ceil(gap / barMs(candles)) + 2));
  }

  // 세로 범위 — 캔들 고저 + 보여 줄 레벨, 위아래 8% 여유
  function priceRange(candles, levels = []) {
    let lo = Infinity;
    let hi = -Infinity;
    for (const c of candles || []) {
      if (c.l < lo) lo = c.l;
      if (c.h > hi) hi = c.h;
    }
    for (const v of levels) {
      if (!Number.isFinite(v)) continue;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    }
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null;
    const pad = (hi - lo || Math.abs(hi) * 0.002 || 1) * 0.08;
    return { min: lo - pad, max: hi + pad };
  }

  // 캔들이 차트 높이의 minShare 이상을 쓰게 — 가까운 레벨부터 범위에 넣고, 더 넣으면 캔들이 납작해지는 먼 레벨은 off
  // (off 레벨은 가장자리에 화살표 가격표로 보여 준다. 20배 손절 −2.5%·청산 −4.5%가 12시간 캔들보다 먼 경우가 많다)
  function fitLevels(candles, levels, minShare = 0.45) {
    let lo = Infinity;
    let hi = -Infinity;
    for (const c of candles || []) {
      if (c.l < lo) lo = c.l;
      if (c.h > hi) hi = c.h;
    }
    if (!Number.isFinite(lo)) return { range: priceRange([], levels), off: [] };
    const allowed = (hi - lo || Math.abs(hi) * 0.002 || 1) / minShare;
    const dist = (v) => (v > hi ? v - hi : v < lo ? lo - v : 0);
    const inside = [];
    const off = [];
    for (const v of levels.filter(Number.isFinite).sort((a, b) => dist(a) - dist(b))) {
      const nlo = Math.min(lo, v);
      const nhi = Math.max(hi, v);
      if (nhi - nlo <= allowed) {
        lo = nlo;
        hi = nhi;
        inside.push(v);
      } else off.push(v);
    }
    return { range: priceRange(candles, inside), off };
  }

  // 오른쪽 가격표가 겹치면 위아래로 밀어낸다 (y = 가운데)
  function layoutTags(tags, top, bottom) {
    const out = tags.map((t) => ({ ...t })).sort((a, b) => a.y - b.y);
    let cur = top;
    for (const t of out) {
      t.y = Math.max(t.y, cur + t.h / 2);
      cur = t.y + t.h / 2 + 1;
    }
    cur = bottom;
    for (let i = out.length - 1; i >= 0; i--) {
      const t = out[i];
      t.y = Math.min(t.y, cur - t.h / 2);
      cur = t.y - t.h / 2 - 1;
    }
    return out;
  }

  function held(ms) {
    const m = Math.max(0, Math.floor(ms / 60000));
    return m >= 60 ? `${Math.floor(m / 60)}시간 ${m % 60}분` : `${m}분`;
  }

  // 데모 버전은 관망 없이 매번 방향을 고른다 — 관망은 리스크 게이트·PM이 막을 때만
  function holdWhy(r) {
    if (r.blocked === 'LIQ') return '이 배율은 청산가가 손절보다 가까워 진입하지 않습니다';
    if (r.blocked === 'PM') return 'PM이 기각했습니다';
    return '조건을 기다립니다';
  }

  // 그릴 것 목록(가격·시각 공간). s = { candles, price, open, closed, plan, rule, conf, kiosk, now }
  function model(s) {
    const out = { levels: [], zones: [], lines: [], links: [], markers: [], tags: [], liq: null, hud: null };
    const candles = s.candles || [];
    if (!candles.length) return out;
    const t0 = candles[0].t;
    const price = Number.isFinite(s.price) ? s.price : candles[candles.length - 1].c;
    const now = s.now || Date.now();

    // 끝난 거래 — 차트 구간 안에서 정리된 것만 (스냅숏 순서 = 최근 것부터)
    for (const p of s.closed || []) {
      const exitT = Date.parse(p.closedAt);
      if (!(exitT >= t0)) continue;
      const long = p.side === 'LONG';
      if (p.openedMs >= t0) out.markers.push({ kind: long ? 'buy' : 'sell', t: p.openedMs, price: p.entry, label: '' });
      const [kind, word] = EXIT[p.closeReason] || ['exit', '정리'];
      out.markers.push({ kind, t: exitT, price: p.exitPrice, label: `${word} ${signed(p.realizedAmt, 2)}` });
      out.links.push({ from: { t: Math.max(t0, p.openedMs), price: p.entry }, to: { t: exitT, price: p.exitPrice }, tone: p.realizedAmt > 0 ? 'up' : 'down' });
    }

    const pos = (s.open || [])[0] || null;
    const plan = pos ? null : s.plan || null;
    const live = pos || plan;
    let tone = null;
    if (live) {
      const long = live.side === 'LONG';
      const dir = long ? 1 : -1;
      const lev = live.leverage || live.lev;
      const from = live.openedMs || live.at || candles[candles.length - 1].t;
      out.zones.push(
        { kind: 'profit', from, to: null, lo: Math.min(live.entry, live.target), hi: Math.max(live.entry, live.target) },
        { kind: 'loss', from, to: null, lo: Math.min(live.entry, live.stop), hi: Math.max(live.entry, live.stop) },
      );
      out.lines.push({ kind: 'target', price: live.target, from }, { kind: 'entry', price: live.entry, from }, { kind: 'stop', price: live.stop, from });
      out.markers.push({ kind: long ? 'buy' : 'sell', t: from, price: live.entry, label: long ? '매수' : '매도' });
      out.tags.push(
        { kind: 'target', price: live.target, text: `목표 ${fmt(live.target)}`, sub: `${signed(movePct(live.entry, live.target), 2)}%` },
        { kind: 'entry', price: live.entry, text: `진입 ${fmt(live.entry)}`, sub: `${long ? '롱' : '숏'} ${lev}x` },
        { kind: 'stop', price: live.stop, text: `손절 ${fmt(live.stop)}`, sub: `${signed(movePct(live.entry, live.stop), 2)}%` },
      );
      if (Number.isFinite(live.liq)) out.liq = { price: live.liq, side: live.side };
      out.levels.push(live.entry, live.target, live.stop);
      const roe = movePct(live.entry, price) * dir * lev;
      tone = roe >= 0 ? 'up' : 'down';
      if (pos) {
        const amt = (pos.qty || 0) * (price - pos.entry) * dir;
        out.hud = { text: `${long ? 'LONG' : 'SHORT'} ${lev}x 보유 중 · ${held(now - from)}`, pnl: { text: `${signed(amt, 2)} USDT · ROE ${signed(roe, 1)}%`, tone } };
      } else {
        const act = `${long ? '매수' : '매도'} 판정${Number.isFinite(s.conf) ? ` ${s.conf}%` : ''}`;
        out.hud = { text: `${act} · ${s.kiosk ? '시연이라 진입하지 않음' : '모의 포지션 보류'}`, pnl: { text: `가상 ROE ${signed(roe, 1)}%`, tone } };
      }
    } else if (s.rule && s.action === 'HOLD') {
      out.hud = { text: `관망 · ${holdWhy(s.rule)}`, pnl: null };
    } else {
      // 보유도 관망 판정도 없으면(진입했던 포지션이 끝났거나 다시 들어온 화면) 방금 거래 결과를
      const done = (s.closed || []).find((p) => Date.parse(p.closedAt) >= t0);
      if (done) {
        const [, word] = EXIT[done.closeReason] || ['exit', '정리'];
        out.hud = { text: `방금 거래 ${word} ${signed(done.realizedAmt, 2)} USDT · 다음 판정은 ▶ ANALYZE`, pnl: null };
      }
    }

    out.lines.push({ kind: 'current', price, from: null });
    const posRoe = pos ? movePct(pos.entry, price) * (pos.side === 'LONG' ? 1 : -1) * pos.leverage : null;
    out.tags.push({ kind: 'current', price, text: `현재 ${fmt(price)}`, sub: pos ? `ROE ${signed(posRoe, 1)}%` : '', tone: pos ? tone : null });
    out.levels.push(price);
    return out;
  }

  /* ----------------------------------------------------------------- 브라우저
     상단 차트는 트레이딩뷰 공식 위젯 하나로 그린다 (2026-11-20 홍원 님 "우리 차트는 지우고 트레이딩뷰만").
     우리 캔버스 대신 #office-tv-chart 안에 위젯을 띄우고, 종목이 바뀌면 위젯 심볼만 따라 바꾼다.
     직원 잡담(lite/life.js)·주문창 현재가(lite/ui.js)가 쓰는 candles()/state()는 그대로 살려 두려고
     최근 5분봉을 20초에 한 번만 조용히 받아 둔다(그리는 데는 쓰지 않는다). */
  const TV_STOCKS = {
    NVDA: 'NASDAQ:NVDA', AAPL: 'NASDAQ:AAPL', MSFT: 'NASDAQ:MSFT', TSLA: 'NASDAQ:TSLA', AMZN: 'NASDAQ:AMZN',
    GOOGL: 'NASDAQ:GOOGL', META: 'NASDAQ:META', AMD: 'NASDAQ:AMD', COIN: 'NASDAQ:COIN', MSTR: 'NASDAQ:MSTR',
    SPY: 'AMEX:SPY', QQQ: 'NASDAQ:QQQ', TQQQ: 'NASDAQ:TQQQ',
    SAMSUNG: 'KRX:005930', SKHYNIX: 'KRX:000660',
  };
  const TV_INTERVAL = '5';
  const TV_POLL_MS = 20000;

  // 우리 종목(BTCUSDT·삼성전자…)을 트레이딩뷰 심볼로 — 코인은 바이비트 무기한, 미국 주식은 나스닥
  function tvSymbol(symbol) {
    const code = String(symbol || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (!code) return 'BYBIT:BTCUSDT.P';
    if (TV_STOCKS[code]) return TV_STOCKS[code];
    if (/^\d{6}$/.test(code)) return `KRX:${code}`;
    return `BYBIT:${code.replace(/USDT$/, '')}USDT.P`;
  }

  function tvNarrow(win) {
    return (win.innerWidth || 0) <= 768;
  }

  function install(win) {
    const doc = win.document;
    const LITE = win.LITE;
    const host = doc.querySelector('#office-tv-chart');
    const note = doc.querySelector('#office-tv-note');
    if (!LITE || !host) return null;

    const st = { symbol: null, display: null, axis: 'perp', candles: [], price: null, liveAt: 0 };
    const tv = { widget: null, script: null, symbol: '', width: 0, timer: 0, noteTimer: 0 };
    let busy = false;

    function showNote(text) {
      if (!note) return;
      note.textContent = text;
      note.classList.remove('hidden');
    }

    function hideNote() {
      if (note) note.classList.add('hidden');
    }

    function loadScript() {
      if (win.TradingView && typeof win.TradingView.widget === 'function') return Promise.resolve();
      if (tv.script) return tv.script;
      tv.script = new Promise((resolve, reject) => {
        const script = doc.createElement('script');
        script.src = 'https://s3.tradingview.com/tv.js';
        script.async = true;
        script.onload = () => (win.TradingView && typeof win.TradingView.widget === 'function'
          ? resolve()
          : reject(new Error('TradingView 위젯을 읽지 못했습니다.')));
        script.onerror = () => { tv.script = null; reject(new Error('TradingView 스크립트를 불러오지 못했습니다.')); };
        doc.head.appendChild(script);
      });
      return tv.script;
    }

    function tvOptions(symbol) {
      return {
        container_id: 'office-tv-chart',
        symbol,
        interval: TV_INTERVAL,
        theme: 'dark',
        style: '1',
        locale: 'kr',
        timezone: 'Asia/Seoul',
        toolbar_bg: '#0b0b13',
        backgroundColor: '#08080e',
        gridColor: 'rgba(38,42,58,.5)',
        autosize: true,
        hide_top_toolbar: false,
        hide_side_toolbar: tvNarrow(win),
        hide_legend: true,
        allow_symbol_change: false,
        save_image: false,
        withdateranges: true,
        details: false,
        hotlist: false,
        enable_publishing: false,
      };
    }

    // 칸이 화면에 보일 때만 만든다 — 숨은 칸에서 만들면 폭이 0이라 찌그러진다
    function mount() {
      if (!host.clientWidth) return;
      const symbol = tvSymbol(st.symbol);
      if (tv.widget && tv.symbol === symbol && tv.width) return;
      tv.symbol = symbol;
      tv.width = host.clientWidth;
      showNote('트레이딩뷰 차트를 불러오는 중…');
      loadScript().then(() => {
        if (tvSymbol(st.symbol) !== symbol) return;
        win.requestAnimationFrame(() => {
          if (!host.clientWidth) return;
          host.innerHTML = '';
          try {
            tv.widget = new win.TradingView.widget(tvOptions(symbol));
            const iframe = host.querySelector('iframe');
            if (iframe) iframe.addEventListener('load', hideNote);
            if (tv.noteTimer) win.clearTimeout(tv.noteTimer);
            tv.noteTimer = win.setTimeout(hideNote, 8000);
          } catch (error) {
            showNote('트레이딩뷰 위젯을 띄우지 못했어요. 새로고침 후 다시 시도해 주세요.');
          }
        });
      }).catch(() => {
        showNote('트레이딩뷰 위젯을 불러오지 못했어요. 인터넷 연결이나 광고 차단 설정을 확인해 주세요.');
      });
    }

    function render() {
      mount();
    }

    // 화면에 보이는 동안에만 최근 5분봉을 받아 둔다 (잡담·주문창 현재가용, 20초 간격)
    async function poll() {
      if (!st.symbol || busy || doc.hidden || !isSeen()) return;
      if (Date.now() - st.liveAt < TV_POLL_MS - 2000) return;
      busy = true;
      const symbol = st.symbol;
      try {
        const rows = await LITE.data.fetchRecentCandles(symbol, { axis: st.axis, limit: 144 });
        if (symbol === st.symbol && rows && rows.length) {
          st.candles = rows.map(normRow).slice(-144);
          st.price = rows[rows.length - 1].c;
          st.liveAt = Date.now();
        }
      } catch (_) {
        // 잠깐 끊기면 다음 차례에
      } finally {
        busy = false;
      }
    }

    function normRow(c) {
      return { t: c.t, o: c.o, h: c.h, l: c.l, c: c.c };
    }

    // 정밀 분석·워치리스트 클릭이 보내는 시장 이벤트 — 종목이 바뀌면 위젯도 그 종목으로
    LITE.bus.subscribe((ev) => {
      if (!ev || ev.type !== 'market') return;
      st.symbol = ev.symbol || null;
      st.display = ev.display || ev.symbol || null;
      st.axis = ev.axis || 'perp';
      const last = Array.isArray(ev.candles) ? ev.candles[ev.candles.length - 1] : null;
      if (last && Number.isFinite(last.c)) {
        st.price = last.c;
        st.candles = ev.candles.slice(-144).map(normRow);
        st.liveAt = Date.now();
      }
      render();
    });

    // 웹소켓 틱(lite/live.js) — 현재가만 받아 전광판에 바로 반영한다
    function applyTick(tk) {
      if (!tk || tk.symbol !== st.symbol || !Number.isFinite(tk.c)) return false;
      st.price = tk.c;
      st.liveAt = Date.now();
      const priceEl = doc.querySelector('#board-price');
      if (priceEl) priceEl.textContent = `$${fmt(st.price)}`;
      return true;
    }

    // 사무실 탭이 보이고 있나 — 다른 탭(대시보드·트레이딩·고래·스크리너)에서는 시세를 받지 않는다
    function isSeen() {
      const cls = doc.documentElement.classList;
      return !cls.contains('dash-view') && !cls.contains('trading-view')
        && !cls.contains('screener-view') && !cls.contains('whale-view');
    }

    // 열자마자 보유 종목이 있으면 그 종목 차트부터 (없으면 트레이딩뷰 기본 BTC)
    function restore() {
      if (LITE.kiosk || st.symbol || !LITE.paper) return;
      const open = LITE.paper.snapshot().open;
      if (!open.length) return;
      const p = open[0];
      if (st.symbol) return;
      st.symbol = p.symbol;
      st.display = p.display || p.symbol;
      st.axis = p.axis || 'perp';
      render();
    }

    if (typeof win.ResizeObserver === 'function') {
      new win.ResizeObserver(() => {
        // 다른 탭에 가려져 있다가 다시 보이면 그때 위젯을 만든다
        if (host.clientWidth && (!tv.widget || !tv.width)) mount();
      }).observe(host);
    }
    doc.addEventListener('visibilitychange', () => { if (!doc.hidden) poll(); });
    win.setInterval(poll, 5000);
    win.setTimeout(restore, 900);
    win.setTimeout(() => { render(); poll(); }, 1200);

    const api = {
      render,
      poll,
      model: () => ({ levels: [], zones: [], lines: [], links: [], markers: [], tags: [], liq: null, hud: null }),
      applyTick,
      isSeen,
      state: () => ({ symbol: st.symbol, display: st.display, axis: st.axis, bars: st.candles.length, price: st.price, liveAt: st.liveAt, lastT: st.candles.length ? st.candles[st.candles.length - 1].t : null }),
      candles: () => st.candles.slice(),
      tags: () => [],
    };
    win.drawChart = render; // app.js가 시세 수신·창 크기 변경 때 부르는 함수를 이쪽으로
    LITE.chart = api;
    render();
    return api;
  }

  if (typeof window !== 'undefined' && window.document && window.LITE && !window.LITE_NO_BOOT) install(window);

  return { BARS, mergeCandles, applyTick, barsToFetch, priceRange, fitLevels, layoutTags, model, tvSymbol, install };
});
