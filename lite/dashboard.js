/* PIXEL TRADING FLOOR — 시장 대시보드 탭 (기본 첫 화면)
   · 좌측: 종합 요약(현재가 · 24H 변화 · 지지/저항 · 수급 3대장) + 실시간 캔들 차트 + 데모 트레이딩 CTA
   · 우측: 실시간 수급 스캐너(바이비트 선물 USDT 전체) + 오늘의 일정 & 고래 동향 요약
   · 글로벌 시황 칩은 LiteGlobal이 이미 받아 둔 값읽어 쓴다(값을 새로 지어내지 않는다)
   · 지지/저항은 실제 캔들 데이터로 계산한다 — 최근 스윙(2봉 프랙털) 고점·저점 중 지금 가격과 가장 가까운 값,
     스윙이 없으면 최근 50봉 최고가·최저가로 물러난다. 절대 고정값을 넣지 않는다.
   · 이 탭이 보이는 동안만 REST 폴링을 돌리고, 감춰지면 타이머를 모두 정리한다 */
(function () {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const num = (v) => (v === null || v === undefined || v === '' || !Number.isFinite(Number(v)) ? null : Number(v));

  const API = 'https://api.bybit.com/v5/market';
  const FRAMES = [
    { key: '1m', api: '1', label: '1m' },
    { key: '5m', api: '5', label: '5m' },
    { key: '15m', api: '15', label: '15m' },
    { key: '1H', api: '60', label: '1H' },
    { key: '4H', api: '240', label: '4H' },
    { key: '1D', api: 'D', label: '1D' },
  ];
  const MAIN_COINS = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE'];
  const GLYPHS = { BTC: '₿', ETH: 'Ξ', SOL: '◎', XRP: '✕', DOGE: 'Ð', BNB: 'B', LTC: 'Ł', ADA: '₳', DOT: '●', TRX: 'T', LINK: 'L' };
  const GLYPH_BG = { BTC: '#f7931a', ETH: '#627eea', SOL: '#14f195', XRP: '#23292f', DOGE: '#c2a633', BNB: '#f0b90b' };
  const CHIP_KEYS = ['nq', 'dxy', 'fx', 'kimchi', 'fng'];
  const CHIP_LABEL = { nq: 'NQ', dxy: 'DXY', fx: '원/달러', kimchi: '김프', fng: '공포지수', es: 'ES', btcd: 'BTC.D' };

  const CHART_MS = 5000;      // 캔들 보충 (현재가도 이 값으로 따라 움직인다)
  const FLOW_MS = 30000;      // 수급 3대장
  const SCAN_MS = 15000;      // 스캐너 전체 목록
  const WHALE_MS = 10000;     // 고래·일정 스냅숏 읽기
  const CHIP_MS = 5000;       // 시황 칩

  const state = {
    active: false, built: false,
    symbol: 'BTCUSDT', frame: FRAMES[3],
    candles: [], loading: false, chartError: '',
    summary: null, summaryLoading: false, summaryError: '',
    levels: { support: null, resistance: null, basis: '' },
    scan: { rows: [], bySymbol: new Map(), at: 0, loading: false, error: '', expanded: false, query: '', sort: 'turnover', dir: -1, lastPaint: '' },
    whaleAt: 0,
    timers: { chart: 0, flow: 0, scan: 0, whale: 0, chips: 0 },
  };

  /* ---------------------------------------------------------------- 표기 */

  function priceText(n) {
    const v = Number(n);
    if (!Number.isFinite(v)) return '—';
    return v.toLocaleString('en-US', { minimumFractionDigits: v < 0.01 ? 8 : v < 1 ? 5 : v < 100 ? 4 : 2, maximumFractionDigits: v < 0.01 ? 8 : v < 1 ? 5 : v < 100 ? 4 : 2 });
  }
  function compactUsd(v) {
    if (!Number.isFinite(v)) return '—';
    if (Math.abs(v) >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
    if (Math.abs(v) >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
    if (Math.abs(v) >= 1e3) return `$${(v / 1e3).toFixed(1)}K`;
    return `$${v.toFixed(2)}`;
  }
  function pctText(v, digits = 2) {
    if (!Number.isFinite(Number(v))) return '—';
    const n = Number(v);
    return `${n > 0 ? '+' : n < 0 ? '−' : ''}${Math.abs(n).toFixed(digits)}%`;
  }
  function clockText(d = new Date()) {
    return d.toLocaleTimeString('ko-KR', { hour12: false });
  }
  function baseOf(symbol) {
    return String(symbol || '').toUpperCase().replace(/USDT$/, '');
  }
  function glyphOf(base) {
    return GLYPHS[base] || base.slice(0, 1);
  }
  function glyphBg(base) {
    return GLYPH_BG[base] || '#3d4c66';
  }
  function ageText(ts) {
    const s = Math.max(0, Math.floor((Date.now() - ts) / 1000));
    if (s < 60) return `${s}초 전`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}분 전`;
    const h = Math.floor(m / 60);
    return h < 24 ? `${h}시간 ${m % 60}분 전` : `${Math.floor(h / 24)}일 전`;
  }
  function kstTodayKey(d = new Date()) {
    const k = new Date(d.getTime() + 9 * 3600000);
    return k.toISOString().slice(0, 10);
  }
  async function getJSON(url, timeout = 9000) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    try {
      const res = await fetch(url, { signal: ctrl.signal, cache: 'no-store', credentials: 'omit' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } finally {
      clearTimeout(timer);
    }
  }

  /* ---------------------------------------------------------------- 화면 뼈대 */

  function build() {
    if (state.built) return;
    state.built = true;

    // 상단바 오른쪽 시황 칩 (PC) — 모바일에선 숨기고 #dash-mstrip 이 대신한다
    const topbar = $('#topbar');
    if (topbar) {
      const chips = document.createElement('div');
      chips.id = 'dash-chips';
      chips.setAttribute('role', 'region');
      chips.setAttribute('aria-label', '글로벌 시황 요약');
      chips.innerHTML = CHIP_KEYS.map((k) => `<span class="dash-chip" data-k="${k}"><span class="dc-lbl">${CHIP_LABEL[k] || k}</span><b class="dc-val">—</b><em class="dc-chg flat"></em></span>`).join('');
      topbar.appendChild(chips);
    }

    const page = document.createElement('section');
    page.id = 'dash-page';
    page.setAttribute('aria-label', '시장 대시보드');
    page.innerHTML = `
      <div id="dash-mstrip" role="region" aria-label="글로벌 시황 한 줄 요약">
        ${CHIP_KEYS.map((k) => `<span class="dash-chip" data-k="${k}"><span class="dc-lbl">${CHIP_LABEL[k] || k}</span><b class="dc-val">—</b><em class="dc-chg flat"></em></span>`).join('')}
        <button type="button" class="dash-chip-more" aria-expanded="false">전체 ▾</button>
      </div>
      <div class="dash-wrap">
        <div class="dash-main">
          <section class="dash-card dash-sum-card" aria-label="종합 요약">
            <div class="dash-sum-head">
              <span class="dash-coin-icon" id="dash-coin-icon">₿</span>
              <select id="dash-symbol" aria-label="코인 선택">${MAIN_COINS.map((c) => `<option value="${c}USDT">${c}/USDT</option>`).join('')}</select>
              <span class="dash-sum-title">종합 요약</span>
              <span class="dash-sum-live" id="dash-sum-live"><i></i><em id="dash-sum-time">실시간</em></span>
            </div>
            <div class="dash-sum-grid">
              <div class="dash-cell">
                <small>현재가</small>
                <span class="dash-price" id="dash-price">—</span>
                <span class="dash-chg-badge flat" id="dash-chg">24H —</span>
              </div>
              <div class="dash-cell">
                <small>주요 지지선 (지지)</small>
                <span class="dash-level sup" id="dash-sup">—</span>
                <span class="lv-note" id="dash-sup-note">캔들을 불러오는 중…</span>
              </div>
              <div class="dash-cell">
                <small>주요 저항선 (저항)</small>
                <span class="dash-level res" id="dash-res">—</span>
                <span class="lv-note" id="dash-res-note">캔들을 불러오는 중…</span>
              </div>
            </div>
            <div class="dash-flow" id="dash-flow" aria-label="선물 수급 3대장 요약">
              <span class="dash-flow-cell"><small>펀딩비</small><b id="dash-funding">—</b></span>
              <span class="dash-flow-cell"><small>미결제약정 OI</small><b id="dash-oi">—</b></span>
              <span class="dash-lsbar">
                <span class="ls-txt"><b class="long" id="dash-long">롱 —</b> vs <b class="short" id="dash-short">숏 —</b></span>
                <span class="ls-bar"><i class="long" id="dash-ls-long" style="width:0%"></i><i class="short"></i></span>
              </span>
              <span class="dash-flow-note" id="dash-flow-note">Bybit 선형 무기한 · 30초 갱신</span>
            </div>
          </section>

          <section class="dash-card dash-chart-card" aria-label="실시간 캔들 차트">
            <div class="dash-chart-toolbar">
              <div class="dash-frames" id="dash-frames" role="group" aria-label="타임프레임">${FRAMES.map((f) => `<button type="button" data-frame="${f.key}" class="${f.key === state.frame.key ? 'active' : ''}">${f.label}</button>`).join('')}</div>
              <span class="dash-ohlc" id="dash-ohlc"></span>
            </div>
            <div class="dash-chart-wrap">
              <canvas id="dash-chart" aria-label="캔들 차트"></canvas>
              <div id="dash-chart-msg">Bybit 캔들을 불러오는 중…</div>
            </div>
          </section>

          <button type="button" id="dash-cta" class="dash-cta">데모 트레이딩 바로가기 →</button>
        </div>

        <aside class="dash-side">
          <section class="dash-card dash-scan-card" aria-label="실시간 수급 스캐너">
            <div class="dash-scan-head">
              <h2>실시간 수급 스캐너</h2>
              <span class="dash-scan-live" id="dash-scan-live"><i></i><em>실시간</em></span>
              <button type="button" class="dash-scan-toggle" id="dash-scan-toggle" aria-expanded="false">전체 펼치기</button>
            </div>
            <input id="dash-scan-search" type="search" placeholder="종목 검색 (예: BTC, SOL)" autocomplete="off" spellcheck="false" aria-label="종목 검색">
            <div class="dash-scan-cols"><span>자산</span><span>24H 변화</span><span>거래대금 (24H)</span><span>수급 강도</span></div>
            <div id="dash-scan-rows"><div class="dash-scan-msg">바이비트 시세를 불러오는 중…</div></div>
            <div class="dash-scan-foot"><span>기준: 바이비트 선물 USDT 마켓</span><span id="dash-scan-updated"></span></div>
          </section>

          <section class="dash-card dash-ev-card" aria-label="오늘의 일정과 고래 동향">
            <h2>오늘의 일정 &amp; 고래 동향</h2>
            <div class="dash-ev-sec">
              <small>주요 경제 일정 (KST)</small>
              <div id="dash-ev-events"><div class="dash-ev-empty">일정을 불러오는 중…</div></div>
            </div>
            <div class="dash-ev-sec">
              <small>최근 고래 이동 (TRC20 USDT · 50만 달러 이상)</small>
              <div id="dash-ev-whales"><div class="dash-ev-empty">고래 이체를 찾는 중…</div></div>
            </div>
            <button type="button" class="dash-ev-more" id="dash-ev-more">고래·일정 탭에서 자세히 보기 →</button>
            <div class="dash-ev-foot">ForexFactory 주간 일정 · TronScan 공개 API</div>
          </section>
        </aside>
      </div>`;
    document.body.appendChild(page);

    $('#dash-cta').addEventListener('click', () => {
      if (window.LiteTrading && typeof window.LiteTrading.show === 'function') window.LiteTrading.show();
    });
    $('#dash-ev-more').addEventListener('click', () => {
      if (window.LiteTrading && typeof window.LiteTrading.whale === 'function') window.LiteTrading.whale();
    });
    $('#dash-symbol').addEventListener('change', (event) => setSymbol(event.target.value));
    $('#dash-frames').addEventListener('click', (event) => {
      const button = event.target.closest('[data-frame]');
      if (!button) return;
      const frame = FRAMES.find((f) => f.key === button.dataset.frame);
      if (!frame || frame.key === state.frame.key) return;
      state.frame = frame;
      $$('#dash-frames [data-frame]').forEach((b) => b.classList.toggle('active', b.dataset.frame === state.frame.key));
      state.candles = [];
      paintChartMessage('캔들을 불러오는 중…');
      loadCandles(true);
    });
    $('#dash-scan-toggle').addEventListener('click', () => {
      state.scan.expanded = !state.scan.expanded;
      const button = $('#dash-scan-toggle');
      button.textContent = state.scan.expanded ? '접기' : '전체 펼치기';
      button.setAttribute('aria-expanded', String(state.scan.expanded));
      paintScanner(true);
    });
    $('#dash-scan-search').addEventListener('input', (event) => {
      state.scan.query = (event.target.value || '').trim().toUpperCase();
      paintScanner(true);
    });
    $('#dash-scan-rows').addEventListener('click', (event) => {
      const row = event.target.closest('[data-symbol]');
      if (row) setSymbol(row.dataset.symbol);
    });
    $('#dash-mstrip').addEventListener('click', (event) => {
      const more = event.target.closest('.dash-chip-more');
      if (!more) return;
      const strip = $('#dash-mstrip');
      const open = strip.classList.toggle('open');
      more.textContent = open ? '접기 ▴' : '전체 ▾';
      more.setAttribute('aria-expanded', String(open));
    });

    window.addEventListener('resize', () => requestAnimationFrame(() => drawChart()));
  }

  /* ---------------------------------------------------------------- 종합 요약 · 수급 3대장 */

  async function loadSummary() {
    if (state.summaryLoading) return;
    state.summaryLoading = true;
    const symbol = state.symbol;
    const seq = ++loadSummary.seq;
    const [tick, oi] = await Promise.allSettled([
      getJSON(`${API}/tickers?category=linear&symbol=${symbol}`),
      getJSON(`${API}/open-interest?category=linear&symbol=${symbol}&intervalTime=1h&limit=24`),
    ]);
    if (seq !== loadSummary.seq) { state.summaryLoading = false; return; }

    const data = { symbol, at: Date.now() };
    let problems = [];
    if (tick.status === 'fulfilled') {
      const row = (tick.value.list || [])[0];
      if (row) {
        data.price = num(row.lastPrice);
        data.changePct = num(row.price24hPcnt) == null ? null : num(row.price24hPcnt) * 100;
        data.turnover = num(row.turnover24h);
        data.funding = num(row.fundingRate) == null ? null : num(row.fundingRate) * 100;
        data.intervalHours = num(row.fundingIntervalHour) || 8;
        data.oiUsd = num(row.openInterestValue);
        if (data.oiUsd == null && num(row.openInterest) != null && data.price) data.oiUsd = num(row.openInterest) * data.price;
      } else problems.push('tickers');
    } else problems.push('tickers');

    if (oi.status === 'fulfilled') {
      const rows = (oi.value.list || []).map((r) => ({ v: num(r.openInterest), t: num(r.timestamp) }))
        .filter((r) => r.v != null && r.v > 0 && r.t != null).sort((a, b) => a.t - b.t);
      if (rows.length >= 2) data.oiChange = ((rows[rows.length - 1].v - rows[0].v) / rows[0].v) * 100;
      if (data.oiUsd == null && rows.length && data.price) data.oiUsd = rows[rows.length - 1].v * data.price;
    }

    const ratio = await loadRatio(symbol);
    if (ratio) { data.longShare = ratio; } else problems.push('account-ratio');

    if (data.symbol === state.symbol) {
      state.summary = data;
      state.summaryError = problems.length >= 3 ? `수급 데이터를 불러오지 못했습니다 (${problems.join(' · ')})` : '';
      state.summaryLoading = false;
      paintSummary();
    } else {
      state.summaryLoading = false;
    }
  }

  // 롱/숏 계정 비율 — 실패하면 화면은 '—'를 유지한다
  async function loadRatio(symbol) {
    try {
      const body = await getJSON(`${API}/account-ratio?category=linear&symbol=${symbol}&period=1h&limit=1`);
      const row = (body.list || [])[0];
      const buy = num(row && row.buyRatio);
      const sell = num(row && row.sellRatio);
      if (buy != null && sell != null && buy + sell > 0) return buy / (buy + sell);
    } catch (e) { /* 값이 없을 뿐이다 */ }
    return null;
  }

  function paintSummary() {
    const d = state.summary;
    if (!d || d.symbol !== state.symbol) return;

    $('#dash-coin-icon').textContent = glyphOf(baseOf(d.symbol));
    $('#dash-coin-icon').style.background = glyphBg(baseOf(d.symbol));

    const price = $('#dash-price');
    price.textContent = d.price != null ? `$${priceText(d.price)}` : '—';
    const chg = $('#dash-chg');
    chg.textContent = `24H ${pctText(d.changePct)}`;
    chg.className = `dash-chg-badge ${d.changePct == null ? 'flat' : d.changePct > 0 ? 'up' : d.changePct < 0 ? 'down' : 'flat'}`;

    const sup = $('#dash-sup');
    const res = $('#dash-res');
    sup.textContent = state.levels.support != null ? `$${priceText(state.levels.support)}` : '—';
    res.textContent = state.levels.resistance != null ? `$${priceText(state.levels.resistance)}` : '—';
    $('#dash-sup-note').textContent = state.levels.basis ? `실시간 계산 · ${state.levels.basis}` : '실시간 계산 대기 중';
    $('#dash-res-note').textContent = state.levels.basis ? `실시간 계산 · ${state.levels.basis}` : '실시간 계산 대기 중';

    const funding = $('#dash-funding');
    if (d.funding == null) { funding.textContent = '—'; funding.className = ''; }
    else {
      funding.textContent = pctText(d.funding, 4);
      funding.className = d.funding > 0 ? 'up' : d.funding < 0 ? 'down' : '';
    }

    const oiEl = $('#dash-oi');
    if (d.oiUsd == null) oiEl.textContent = '—';
    else oiEl.textContent = `${compactUsd(d.oiUsd)}${d.oiChange != null ? ` · 24H ${pctText(d.oiChange, 1)}` : ''}`;

    const longEl = $('#dash-long');
    const shortEl = $('#dash-short');
    const barLong = $('#dash-ls-long');
    if (d.longShare == null) {
      longEl.textContent = '롱 —';
      shortEl.textContent = '숏 —';
      barLong.style.width = '0%';
    } else {
      const share = Math.max(0, Math.min(1, d.longShare));
      longEl.textContent = `롱 ${(share * 100).toFixed(0)}%`;
      shortEl.textContent = `숏 ${((1 - share) * 100).toFixed(0)}%`;
      barLong.style.width = `${share * 100}%`;
    }

    const note = $('#dash-flow-note');
    if (state.summaryError) { note.textContent = `${state.summaryError} · 30초 뒤 다시`; note.className = 'dash-flow-note is-error'; }
    else {
      note.textContent = `Bybit 선형 무기한 · ${d.intervalHours || 8}시간 펀딩 · 30초 갱신 · 롱/숏 계정 기준`;
      note.className = 'dash-flow-note';
    }

    const live = $('#dash-sum-live');
    live.classList.toggle('off', !!state.summaryError);
    $('#dash-sum-time').textContent = state.summaryError ? '지연' : `${clockText()} 실시간`;
  }

  /* ---------------------------------------------------------------- 지지/저항 계산 (실제 캔들 기반) */

  function calcLevels(candles) {
    const n = candles.length;
    if (n < 10) return null;
    const last = candles[n - 1].c;
    const win = candles.slice(Math.max(0, n - 120));
    const swingHighs = [];
    const swingLows = [];
    for (let i = 2; i < win.length - 2; i += 1) {
      const c = win[i];
      if (c.h >= win[i - 1].h && c.h >= win[i - 2].h && c.h >= win[i + 1].h && c.h >= win[i + 2].h) swingHighs.push(c.h);
      if (c.l <= win[i - 1].l && c.l <= win[i - 2].l && c.l <= win[i + 1].l && c.l <= win[i + 2].l) swingLows.push(c.l);
    }
    const above = swingHighs.filter((h) => h > last);
    const below = swingLows.filter((l) => l < last);
    const resistance = above.length ? Math.min(...above) : Math.max(...win.map((c) => c.h));
    const support = below.length ? Math.max(...below) : Math.min(...win.map((c) => c.l));
    const basis = above.length && below.length ? `최근 ${win.length}봉 스윙 기준` : `최근 ${win.length}봉 고저 기준`;
    return { support, resistance, basis };
  }

  /* ---------------------------------------------------------------- 캔들 차트 */

  async function loadCandles(force) {
    state.loading = true;
    const symbol = state.symbol;
    const interval = state.frame.api;
    const seq = ++loadCandles.seq;
    try {
      const body = await getJSON(`${API}/kline?category=linear&symbol=${symbol}&interval=${interval}&limit=200`);
      if (seq !== loadCandles.seq) return;
      const rows = (body && body.result && body.result.list) || [];
      const candles = rows.map((k) => ({ t: Number(k[0]), o: Number(k[1]), h: Number(k[2]), l: Number(k[3]), c: Number(k[4]), v: Number(k[5]) }))
        .filter((c) => [c.t, c.o, c.h, c.l, c.c].every(Number.isFinite))
        .sort((a, b) => a.t - b.t);
      if (symbol !== state.symbol) return;
      state.candles = candles;
      state.chartError = candles.length ? '' : '캔들이 비어 있습니다';
    } catch (error) {
      if (seq !== loadCandles.seq) return;
      state.chartError = `캔들을 받지 못했습니다 (${error && error.message ? error.message : error})`;
    } finally {
      state.loading = false;
    }
    // 요약 카드의 현재가가 캔들 종가를 따라 실시간으로 움직이게 한다 (24H 변화는 티커 주기를 따른다)
    if (state.summary && state.candles.length) state.summary.price = state.candles[state.candles.length - 1].c;
    if (!force && state.summary && state.summary.price != null && state.candles.length) {
      state.candles[state.candles.length - 1].c = state.summary.price;
    }
    paintFromCandles();
  }

  function paintFromCandles() {
    const levels = calcLevels(state.candles);
    if (levels) state.levels = levels;
    paintSummary();
    drawChart();
    paintOhlc();
    paintChartMessage(state.candles.length ? '' : (state.chartError || '캔들을 불러오는 중…'));
  }

  function paintChartMessage(message) {
    const el = $('#dash-chart-msg');
    if (!el) return;
    el.textContent = message;
    el.style.display = message ? 'grid' : 'none';
  }

  function paintOhlc() {
    const el = $('#dash-ohlc');
    const n = state.candles.length;
    if (!n) { el.textContent = ''; return; }
    const c = state.candles[n - 1];
    const diff = c.c - c.o;
    const cls = diff >= 0 ? 'up' : 'down';
    el.innerHTML = `${esc(baseOf(state.symbol))}/USDT · ${esc(state.frame.key)} · BYBIT 시 <b>${priceText(c.o)}</b> 고 <b>${priceText(c.h)}</b> 저 <b>${priceText(c.l)}</b> 종 <b>${priceText(c.c)}</b> <span class="${cls}">${diff >= 0 ? '+' : '−'}${priceText(Math.abs(diff))} (${pctText((diff / c.o) * 100)})</span>`;
  }

  function drawChart() {
    const canvas = $('#dash-chart');
    if (!canvas) return;
    const wrap = canvas.parentNode;
    const box = wrap.getBoundingClientRect();
    if (!box.width || !box.height) return;
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    if (canvas.width !== Math.round(box.width * ratio) || canvas.height !== Math.round(box.height * ratio)) {
      canvas.width = Math.round(box.width * ratio);
      canvas.height = Math.round(box.height * ratio);
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, box.width, box.height);

    const candles = state.candles;
    if (candles.length < 2) return;

    const padTop = 10;
    const padRight = 66;
    const volumeH = 44;
    const padBottom = 22;
    const plotH = box.height - padTop - padBottom - volumeH - 6;
    const plotW = box.width - padRight - 6;

    let min = Infinity;
    let max = -Infinity;
    for (const c of candles) { if (c.l < min) min = c.l; if (c.h > max) max = c.h; }
    const levels = state.levels;
    const extra = [];
    if (levels.support != null) extra.push(levels.support);
    if (levels.resistance != null) extra.push(levels.resistance);
    for (const v of extra) { if (v < min) min = v; if (v > max) max = v; }
    const span = (max - min) || max * 0.001 || 1;
    min -= span * 0.04;
    max += span * 0.04;
    const y = (v) => padTop + (1 - (v - min) / (max - min)) * plotH;
    const xStep = plotW / candles.length;
    const x = (i) => i * xStep + xStep / 2;

    // 눈금
    ctx.font = '10px Arial, sans-serif';
    ctx.fillStyle = '#66748a';
    ctx.strokeStyle = '#161e2c';
    ctx.lineWidth = 1;
    const gridLines = 5;
    ctx.textAlign = 'left';
    for (let i = 0; i <= gridLines; i += 1) {
      const v = min + ((max - min) * i) / gridLines;
      const py = Math.round(y(v)) + 0.5;
      ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(plotW, py); ctx.stroke();
      ctx.fillText(priceText(v), plotW + 8, py + 3);
    }

    // 지지/저항선
    const drawLevel = (v, color, label) => {
      if (!Number.isFinite(v)) return;
      const py = Math.round(y(v)) + 0.5;
      ctx.save();
      ctx.setLineDash([6, 4]);
      ctx.strokeStyle = color;
      ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(plotW, py); ctx.stroke();
      ctx.restore();
      ctx.fillStyle = color;
      ctx.fillRect(plotW - 74, py - 9, 72, 17);
      ctx.fillStyle = '#0b0e13';
      ctx.textAlign = 'center';
      ctx.fillText(`${label} ${priceText(v)}`, plotW - 38, py + 3);
      ctx.textAlign = 'left';
    };
    drawLevel(levels.support, '#22c58b', '지지');
    drawLevel(levels.resistance, '#f05268', '저항');

    // 거래량
    let vMax = 0;
    for (const c of candles) if (c.v > vMax) vMax = c.v;
    const vTop = padTop + plotH + 6;
    for (let i = 0; i < candles.length; i += 1) {
      const c = candles[i];
      const rise = c.c >= c.o;
      const h = vMax ? (c.v / vMax) * volumeH : 0;
      ctx.fillStyle = rise ? 'rgba(34,197,139,.25)' : 'rgba(240,82,104,.25)';
      ctx.fillRect(x(i) - Math.max(1, xStep * 0.3), vTop + volumeH - h, Math.max(1.5, xStep * 0.6), h);
    }

    // 캔들
    for (let i = 0; i < candles.length; i += 1) {
      const c = candles[i];
      const rise = c.c >= c.o;
      const color = rise ? '#22c58b' : '#f05268';
      ctx.strokeStyle = color;
      ctx.fillStyle = rise ? '#0b0e13' : color;
      const cx = Math.round(x(i)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(cx, y(c.h));
      ctx.lineTo(cx, y(c.l));
      ctx.stroke();
      const bw = Math.max(1, xStep * 0.62);
      const top = Math.min(y(c.o), y(c.c));
      const bh = Math.max(1, Math.abs(y(c.o) - y(c.c)));
      ctx.fillRect(cx - bw / 2, top, bw, bh);
      if (rise) { ctx.strokeRect(cx - bw / 2, top, bw, bh); }
    }

    // 현재가 점선 + 오른쪽 태그
    const price = state.summary && state.summary.price != null ? state.summary.price : candles[candles.length - 1].c;
    if (Number.isFinite(price) && price > min && price < max) {
      const py = Math.round(y(price)) + 0.5;
      ctx.save();
      ctx.setLineDash([4, 4]);
      ctx.strokeStyle = '#e2e8f0';
      ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(plotW, py); ctx.stroke();
      ctx.restore();
      ctx.fillStyle = '#26324a';
      ctx.fillRect(plotW + 2, py - 9, padRight - 6, 17);
      ctx.fillStyle = '#eef2f8';
      ctx.textAlign = 'center';
      ctx.fillText(priceText(price), plotW + 2 + (padRight - 6) / 2, py + 3);
      ctx.textAlign = 'left';
    }
  }

  /* ---------------------------------------------------------------- 실시간 수급 스캐너 */

  async function loadScanner() {
    if (state.scan.loading) return;
    state.scan.loading = true;
    const seq = ++loadScanner.seq;
    try {
      const body = await getJSON(`${API}/tickers?category=linear`, 12000);
      if (seq !== loadScanner.seq) return;
      const list = (body && body.result && body.result.list) || [];
      const rows = [];
      const bySymbol = new Map();
      for (const raw of list) {
        const symbol = String(raw.symbol || '').toUpperCase();
        if (!symbol.endsWith('USDT')) continue;
        const price = num(raw.lastPrice);
        if (price == null || price <= 0) continue;
        const row = {
          symbol,
          base: baseOf(symbol),
          price,
          pct: num(raw.price24hPcnt) == null ? null : num(raw.price24hPcnt) * 100,
          turnover: num(raw.turnover24h) || 0,
          prevPrice: price,
        };
        rows.push(row);
        bySymbol.set(symbol, row);
      }
      if (seq !== loadScanner.seq) return;
      state.scan.rows = rows;
      state.scan.bySymbol = bySymbol;
      state.scan.at = Date.now();
      state.scan.error = '';
    } catch (error) {
      if (seq !== loadScanner.seq) return;
      state.scan.error = `바이비트 시세를 받지 못했습니다 (${error && error.message ? error.message : error}) · 잠시 뒤 다시`;
    } finally {
      state.scan.loading = false;
    }
    paintScanner(true);
  }

  function strengthBadge(row, top) {
    if (!Number.isFinite(row.turnover) || row.turnover <= 0 || !top) return '<span class="sc-badge weak">—</span>';
    const share = row.turnover / top;
    const kind = share >= 0.25 ? '강함' : share >= 0.06 ? '보통' : '약함';
    return `<span class="sc-badge ${share >= 0.25 ? 'strong' : share >= 0.06 ? 'mid' : 'weak'}" title="거래대금 기준 (${(share * 100).toFixed(1)}% of 상위)">${kind}</span>`;
  }

  function scannerRows() {
    let rows = state.scan.rows.slice();
    if (state.scan.query) {
      rows = rows.filter((r) => r.symbol.includes(state.scan.query) || r.base.includes(state.scan.query));
    }
    const dir = state.scan.dir;
    const sort = state.scan.expanded ? state.scan.sort : 'turnover';
    rows.sort((a, b) => {
      const va = sort === 'pct' ? (a.pct == null ? -Infinity : a.pct) : a.turnover;
      const vb = sort === 'pct' ? (b.pct == null ? -Infinity : b.pct) : b.turnover;
      return (va - vb) * dir;
    });
    return rows;
  }

  function paintScanner(force) {
    const box = $('#dash-scan-rows');
    if (!box) return;
    const rows = scannerRows();
    const top = state.scan.rows.length ? state.scan.rows.reduce((m, r) => Math.max(m, r.turnover), 0) : 0;
    const key = `${state.scan.expanded}|${state.scan.query}|${state.scan.sort}|${state.scan.dir}|${state.scan.at}`;
    const changed = key !== state.scan.lastPaint || force;

    if (!rows.length) {
      box.innerHTML = `<div class="dash-scan-msg${state.scan.error ? ' err' : ''}">${esc(state.scan.error || (state.scan.query ? '일치하는 종목이 없습니다' : '바이비트 시세를 불러오는 중…'))}</div>`;
      state.scan.lastPaint = key;
      $('#dash-scan-updated').textContent = '';
      return;
    }

    // 접힌 기본 화면은 거래대금 상위 8개만 — 펼치면 검색·정렬한 전체 목록
    const shown = state.scan.expanded || state.scan.query ? rows : rows.slice(0, 8);
    if (changed || !state.scan.expanded) {
      box.className = state.scan.expanded ? 'expanded' : '';
      box.innerHTML = shown.map((r) => {
        const prev = state.scan.prevPainted && state.scan.prevPainted.get(r.symbol);
        const flash = prev != null && r.price !== prev ? (r.price > prev ? ' flash-up' : ' flash-down') : '';
        return `<button type="button" class="dash-scan-row" data-symbol="${esc(r.symbol)}" title="${esc(r.symbol)} 차트로 전환">
          <span class="sc-asset"><span class="sc-glyph" style="background:${glyphBg(r.base)}">${esc(glyphOf(r.base))}</span><span class="sc-name">${esc(r.base)} <small>/USDT</small></span></span>
          <span class="sc-pct${r.pct == null ? '' : r.pct >= 0 ? ' up' : ' down'}">${pctText(r.pct)}</span>
          <span class="sc-price${flash}">${priceText(r.price)}</span>
          ${strengthBadge(r, top)}
        </button>`;
      }).join('');
      const painted = new Map();
      shown.forEach((r) => painted.set(r.symbol, r.price));
      state.scan.prevPainted = painted;
      state.scan.lastPaint = key;
    } else {
      // 펼친 목록에서는 값이 바뀐 줄만 고친다 — 스크롤 위치가 흔들리지 않게
      const prev = state.scan.prevPainted || new Map();
      shown.forEach((r) => {
        const el = box.querySelector(`[data-symbol="${r.symbol}"]`);
        if (!el) return;
        const priceEl = $('.sc-price', el);
        if (!priceEl) return;
        const was = prev.get(r.symbol);
        priceEl.textContent = priceText(r.price);
        priceEl.className = `sc-price${was != null && r.price !== was ? (r.price > was ? ' flash-up' : ' flash-down') : ''}`;
        const pctEl = $('.sc-pct', el);
        if (pctEl && r.pct != null) { pctEl.textContent = pctText(r.pct); pctEl.className = `sc-pct${r.pct >= 0 ? ' up' : ' down'}`; }
      });
      const painted = new Map();
      shown.forEach((r) => painted.set(r.symbol, r.price));
      state.scan.prevPainted = painted;
    }

    const updated = $('#dash-scan-updated');
    if (updated) updated.textContent = state.scan.at ? `${clockText(new Date(state.scan.at))} 갱신` : '';
    const live = $('#dash-scan-live');
    if (live) {
      live.classList.toggle('off', !!state.scan.error);
      const em = live.querySelector('em');
      if (em) em.textContent = state.scan.error ? '지연' : '실시간';
    }
  }

  // 스캐너 정렬 토글 — 펼친 목록의 열 머리글을 눌러 정렬한다
  function bindScannerSort() {
    const cols = $('.dash-scan-cols');
    if (!cols || cols.dataset.sortBound) return;
    cols.dataset.sortBound = '1';
    cols.addEventListener('click', (event) => {
      const span = event.target.closest('span');
      if (!span || span === span.parentNode.firstChild) return;
      const key = span.textContent.includes('24H 변화') ? 'pct' : span.textContent.includes('거래대금') ? 'turnover' : null;
      if (!key) return;
      if (state.scan.sort === key) state.scan.dir = -state.scan.dir;
      else { state.scan.sort = key; state.scan.dir = -1; }
      paintScanner(true);
    });
  }

  function setSymbol(symbol) {
    const code = String(symbol || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (!code) return;
    if (code === state.symbol) return;
    state.symbol = code;
    state.summary = null;
    state.levels = { support: null, resistance: null, basis: '' };
    state.candles = [];
    const select = $('#dash-symbol');
    const base = baseOf(code);
    if (select && ![...select.options].some((o) => o.value === code)) {
      const option = document.createElement('option');
      option.value = code;
      option.textContent = `${base}/USDT`;
      select.appendChild(option);
    }
    if (select) select.value = code;
    paintSummary();
    paintChartMessage('캔들을 불러오는 중…');
    loadCandles(true);
    loadSummary();
  }

  /* ---------------------------------------------------------------- 오늘의 일정 & 고래 동향 */

  function paintWhaleCard() {
    const whaleBox = $('#dash-ev-whales');
    const evBox = $('#dash-ev-events');
    if (!whaleBox || !evBox) return;
    let snap = null;
    try { snap = window.LiteWhale && typeof window.LiteWhale.snapshot === 'function' ? window.LiteWhale.snapshot() : null; } catch (e) { snap = null; }

    // 고래 — 실제 스냅숏의 최근 이체 2건. 아직 받은 게 없으면 그 사실을 그대로 보여 준다
    const rows = snap && Array.isArray(snap.rows) ? snap.rows.slice(0, 2) : [];
    if (rows.length) {
      whaleBox.innerHTML = rows.map((r) => `<div class="dash-wh-row">
          <span class="dash-wh-ico">🐋</span>
          <span class="dash-wh-main"><span class="dash-wh-usd">${compactUsd(r.usd)}</span>
          <span class="dash-wh-route">${esc(r.from)} → ${esc(r.to)}</span></span>
          <span class="dash-wh-age">${esc(ageText(r.ts))}</span>
        </div>`).join('');
    } else if (snap && snap.error) {
      whaleBox.innerHTML = `<div class="dash-ev-empty">고래 이체를 받지 못했습니다 — 잠시 뒤 다시 시도합니다</div>`;
    } else if (snap && snap.fetching) {
      whaleBox.innerHTML = `<div class="dash-ev-empty">온체인을 훑는 중…</div>`;
    } else {
      whaleBox.innerHTML = `<div class="dash-ev-empty">최근 24시간 안의 50만 달러 이상 이체가 아직 없습니다</div>`;
    }

    // 일정 — 오늘(KST) 남은/지난 1건 + 다가오는 1건. 주말 등으로 없으면 다음 일정을 보여 준다
    const events = snap && snap.calendar ? snap.calendar.filter((e) => Number.isFinite(e.atMs)) : null;
    if (events && events.length) {
      const today = kstTodayKey();
      const now = Date.now();
      const sorted = events.slice().sort((a, b) => a.atMs - b.atMs);
      const upcoming = sorted.filter((e) => e.atMs > now);
      const todayNext = sorted.find((e) => e.date === today && e.atMs > now);
      const picks = [];
      if (todayNext) picks.push(todayNext);
      for (const e of upcoming) {
        if (picks.length >= 2) break;
        if (!picks.includes(e)) picks.push(e);
      }
      if (picks.length) {
        evBox.innerHTML = picks.map((e) => `<button type="button" class="dash-ev-row" title="${esc(e.titleEn || e.title)}">
            <span class="dash-ev-time">${esc(e.time || '--:--')}</span>
            <span class="dash-ev-stars ${e.impact === 'high' ? 'high' : ''}">${e.impact === 'high' ? '★★★' : '★★'}</span>
            <span class="dash-ev-title">${esc(e.title || e.titleEn || '')}</span>
            <span class="dash-ev-fc">${e.forecast ? `예상 ${esc(e.forecast)}` : e.previous ? `전월 ${esc(e.previous)}` : ''}</span>
          </button>`).join('');
        $$('.dash-ev-row', evBox).forEach((el) => el.addEventListener('click', () => {
          if (window.LiteTrading && typeof window.LiteTrading.whale === 'function') window.LiteTrading.whale();
        }));
      } else {
        evBox.innerHTML = `<div class="dash-ev-empty">이번 주 남은 일정이 없습니다</div>`;
      }
    } else if (snap && snap.calError) {
      evBox.innerHTML = `<div class="dash-ev-empty">일정표를 받지 못했습니다 — 고래·일정 탭에서 다시 시도합니다</div>`;
    } else {
      evBox.innerHTML = `<div class="dash-ev-empty">일정을 불러오는 중…</div>`;
    }
  }

  /* ---------------------------------------------------------------- 시황 칩 */

  function paintChips() {
    let rows = [];
    try { rows = window.LiteGlobal && typeof window.LiteGlobal.rows === 'function' ? window.LiteGlobal.rows() : []; } catch (e) { rows = []; }
    const map = {};
    rows.forEach((r) => { map[r.key] = r; });
    $$('.dash-chip[data-k]').forEach((chip) => {
      const key = chip.dataset.k;
      const row = map[key];
      if (!chip || !row) return;
      const val = $('.dc-val', chip);
      const chg = $('.dc-chg', chip);
      if (!val) return;
      let text = '—';
      if (row.value != null) {
        if (key === 'kimchi') text = pctText(row.value);
        else if (key === 'fng') text = String(Math.round(row.value));
        else text = Number(row.value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      }
      val.textContent = text;
      const dir = row.pct != null ? (row.pct > 0 ? 'up' : row.pct < 0 ? 'down' : 'flat') : (row.dir || 'flat');
      if (chg) {
        if (row.pct != null) chg.textContent = pctText(row.pct);
        else chg.textContent = row.kind === 'fng' && row.label2 ? row.label2 : '';
        chg.className = `dc-chg ${dir}`;
      }
      chip.title = row.title || (CHIP_LABEL[key] || key);
    });
  }

  /* ---------------------------------------------------------------- 활성화 · 주기 */

  function startTimers() {
    stopTimers();
    state.timers.chart = setInterval(() => { if (canRun()) loadCandles(); }, CHART_MS);
    state.timers.flow = setInterval(() => { if (canRun()) loadSummary(); }, FLOW_MS);
    state.timers.scan = setInterval(() => { if (canRun()) loadScanner(); }, SCAN_MS);
    state.timers.whale = setInterval(() => { if (canRun()) paintWhaleCard(); }, WHALE_MS);
    state.timers.chips = setInterval(() => { if (canRun()) paintChips(); }, CHIP_MS);
  }

  function stopTimers() {
    for (const key of Object.keys(state.timers)) {
      if (state.timers[key]) clearInterval(state.timers[key]);
      state.timers[key] = 0;
    }
  }

  function canRun() {
    return state.active && document.visibilityState === 'visible';
  }

  function setActive(active) {
    build();
    const next = !!active;
    if (state.active === next) return;
    state.active = next;
    if (!next) { stopTimers(); return; }
    // 첫 진입 — 데이터를 받아 온다. 값이 오기 전까지 화면은 '—'를 유지한다
    if (!state.candles.length) paintChartMessage('캔들을 불러오는 중…');
    loadCandles(true);
    loadSummary();
    loadScanner();
    paintWhaleCard();
    paintChips();
    bindScannerSort();
    startTimers();
  }

  document.addEventListener('visibilitychange', () => {
    if (!state.active) return;
    if (document.visibilityState === 'visible') {
      loadCandles();
      loadSummary();
      loadScanner();
      paintWhaleCard();
      paintChips();
    }
  });

  // 기본 첫 화면으로 대시보드가 이미 열려 있으면(trading.js가 로드된 뒤라 지금이 첫 진입) 바로 켠다
  if (document.documentElement.classList.contains('dash-view')) {
    setActive(true);
    // 고래·일정도 대시보드 요약 카드 때문에 같이 받아 둔다 — trading.js 로드 시점엔 아직 모듈이 없었다
    if (window.LiteWhale && typeof window.LiteWhale.setActive === 'function') window.LiteWhale.setActive(true);
  }

  window.LiteDash = { setActive, setSymbol, debug: () => ({ ...state, timers: undefined }) };
})();
