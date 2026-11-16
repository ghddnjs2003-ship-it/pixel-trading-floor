/* Demo derivatives terminal: Bybit public market data and the local paper ledger only. */
(function () {
  'use strict';

  const LITE = window.LITE;
  if (!LITE || !window.LiteLive || !LITE.paper) return;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const COINS = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE'];
  const DECISION_KEY = 'pixel-lite-latest-decision-v1';
  const FRAMES = [
    { key: '1m', api: '1', ms: 60000, label: '1분' },
    { key: '3m', api: '3', ms: 180000, label: '3분' },
    { key: '5m', api: '5', ms: 300000, label: '5분' },
    { key: '15m', api: '15', ms: 900000, label: '15분' },
    { key: '30m', api: '30', ms: 1800000, label: '30분' },
    { key: '1H', api: '60', ms: 3600000, label: '1시간' },
    { key: '4H', api: '240', ms: 14400000, label: '4시간' },
    { key: '1D', api: 'D', ms: 86400000, label: '1일' },
    { key: '1W', api: 'W', ms: 604800000, label: '1주' },
  ];
  const OPTIONS = [
    ['ma20', 'MA20'], ['ma50', 'MA50'], ['ema20', 'EMA20'],
    ['bb', '볼린저'], ['volume', '거래량'], ['rsi', 'RSI'],
  ];
  const fmt = (n, digits = 2) => Number.isFinite(Number(n))
    ? Number(n).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
    : '—';
  const priceText = (n) => {
    const value = Number(n);
    return fmt(value, value < 0.01 ? 8 : value < 1 ? 5 : value < 100 ? 4 : 2);
  };
  const signed = (n, digits = 2) => `${n > 0 ? '+' : n < 0 ? '−' : ''}${fmt(Math.abs(n), digits)}`;
  const safe = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const tickerUrl = window.LiteLive.URL_TICKER;
  const candleUrl = window.LiteLive.URL_KLINE;
  const state = {
    view: 'office', symbol: 'BTCUSDT', frame: FRAMES[3], candles: [], price: null, change: null,
    feed: null, requestSeq: 0, loadTimer: 0, markTimer: 0, accountTimer: 0, pending: [], tab: 'positions',
    marginMode: 'ISOLATED', indicators: new Set(['ma20', 'ma50', 'volume']),
    chart: { visibleCount: 100, rightTime: null, drag: null, loadingOlder: false, exhausted: false },
    book: { bids: new Map(), asks: new Map() }, orderSide: 'LONG', latestDecision: null,
  };

  const topbar = $('#topbar');
  if (!topbar) return;
  const nav = document.createElement('nav');
  nav.id = 'view-switch';
  nav.setAttribute('aria-label', '화면 전환');
  nav.innerHTML = '<button type="button" data-view="office" class="active">사무실</button><button type="button" data-view="screener">스크리너</button><button type="button" data-view="whale" title="실시간 고래 입출금 · 이번 주 경제 일정">고래·일정</button><button type="button" data-view="trading" disabled aria-disabled="true" title="실전 연동 예정">트레이딩</button>';
  topbar.insertBefore(nav, $('#controls'));
  nav.addEventListener('click', (event) => {
    const button = event.target.closest('[data-view]');
    if (button) setView(button.dataset.view);
  });

  const page = document.createElement('section');
  page.id = 'trade-page';
  page.setAttribute('aria-label', '데모 트레이딩');
  page.innerHTML = `
    <header class="trade-marketbar">
      <label class="trade-coin-select"><span>종목</span><select id="trade-coin" aria-label="코인 선택">${COINS.map((coin) => `<option value="${coin}">${coin}/USDT</option>`).join('')}</select></label>
      <div class="trade-quote"><small>LAST PRICE</small><b id="trade-price">—</b><span id="trade-change">—</span></div>
      <div class="trade-account"><span>모의 잔고 <b id="trade-balance">—</b> USDT</span><span>평가 자산 <b id="trade-equity">—</b> USDT</span><span>주문 가능 <b id="trade-available">—</b> USDT</span></div>
      <span class="trade-live"><i></i> BYBIT · DEMO</span>
    </header>
    <section id="trade-ai-banner" class="trade-ai-banner hidden" aria-label="AI 최신 판정">
      <div class="trade-ai-main"><b id="trade-ai-action">AI 판정 대기</b><span id="trade-ai-symbol"></span><span id="trade-ai-summary"></span></div>
      <div class="trade-ai-levels"><span>진입 <b id="trade-ai-entry">—</b></span><span>TP <b id="trade-ai-target">—</b></span><span>SL <b id="trade-ai-stop">—</b></span></div>
      <button id="trade-ai-apply" type="button" disabled>주문창에 적용</button>
    </section>
    <div class="trade-layout">
      <main class="trade-main">
        <section class="trade-chart-panel">
          <div class="trade-chart-toolbar">
            <div class="trade-frame-tabs" id="trade-frames">${FRAMES.map((f) => `<button type="button" data-frame="${f.key}" class="${f.key === state.frame.key ? 'active' : ''}">${f.label}</button>`).join('')}</div>
            <div class="trade-indicators" id="trade-indicators">${OPTIONS.map(([key, label]) => `<label><input type="checkbox" data-indicator="${key}"${state.indicators.has(key) ? ' checked' : ''}>${label}</label>`).join('')}</div>
          </div>
          <div class="trade-chart-wrap"><canvas id="trade-chart" aria-label="실시간 캔들 차트"></canvas><div id="trade-chart-message" role="status">Bybit 캔들을 불러오는 중…</div><span id="trade-history-status" role="status" aria-live="polite"></span><button id="trade-latest" type="button" class="hidden">최신으로</button></div>
          <div id="trade-rsi-wrap" class="trade-rsi-wrap hidden"><span>RSI(14)</span><canvas id="trade-rsi" aria-label="RSI 14 보조지표"></canvas></div>
        </section>
        <section class="trade-orders-panel">
          <div class="trade-subtabs" id="trade-subtabs"><button type="button" data-tab="positions" class="active">열린 포지션 <span id="trade-open-count">0</span></button><button type="button" data-tab="fills">체결 내역</button></div>
          <div class="trade-table-wrap"><table class="trade-table"><thead id="trade-table-head"></thead><tbody id="trade-table-body"></tbody></table></div>
        </section>
      </main>
      <aside class="trade-orderbook" aria-label="실시간 호가창">
        <div class="trade-book-head"><b>호가창</b><span id="trade-book-state">연결 중</span></div>
        <div class="trade-book-columns"><span>가격 (USDT)</span><span>수량</span><span>누적</span></div>
        <div id="trade-asks" class="trade-book-side asks"></div>
        <div class="trade-book-last"><span>현재가 <b id="trade-book-price">—</b></span><span>스프레드 <b id="trade-book-spread">—</b></span></div>
        <div id="trade-bids" class="trade-book-side bids"></div>
        <div class="trade-book-hint">호가를 누르면 지정가에 입력</div>
      </aside>
      <aside class="trade-ticket">
        <div class="trade-ticket-head"><span>주문</span><span class="trade-paper-chip">모의 거래</span></div>
        <div class="trade-margin-modes"><button type="button" data-margin="ISOLATED" class="active">격리</button><button type="button" data-margin="CROSS">교차</button><span>마진 모드</span></div>
        <label class="trade-field"><span>레버리지</span><select id="trade-leverage">${Array.from({ length: 50 }, (_, i) => `<option value="${i + 1}"${i === 4 ? ' selected' : ''}>${i + 1}x</option>`).join('')}</select></label>
        <div class="trade-side-select" id="trade-side-select"><span>방향</span><button type="button" data-select-side="LONG" class="selected long">LONG</button><button type="button" data-select-side="SHORT" class="short">SHORT</button></div>
        <div class="trade-order-types"><button type="button" data-order-type="market" class="active">시장가</button><button type="button" data-order-type="limit">지정가</button></div>
        <label class="trade-field"><span id="trade-price-label">시장가 예상 가격</span><input id="trade-limit-price" type="number" min="0" step="any" inputmode="decimal" placeholder="Bybit 현재가" disabled></label>
        <div class="trade-risk-fields">
          <label class="trade-field"><span>익절가 TP (선택)</span><input id="trade-tp" type="number" min="0" step="any" inputmode="decimal" placeholder="미설정"></label>
          <label class="trade-field"><span>손절가 SL (선택)</span><input id="trade-sl" type="number" min="0" step="any" inputmode="decimal" placeholder="미설정"></label>
        </div>
        <label class="trade-field"><span>주문 증거금</span><div class="trade-input-suffix"><input id="trade-amount" type="number" min="0.01" step="0.01" value="100" inputmode="decimal"><b>USDT</b></div></label>
        <div class="trade-percent-buttons" id="trade-percent">${[25, 50, 75, 100].map((n) => `<button type="button" data-percent="${n}">${n}%</button>`).join('')}</div>
        <div class="trade-order-summary"><span>포지션 규모</span><b id="trade-notional">— USDT</b><span>예상 수량</span><b id="trade-qty">—</b><span>사용 가능 잔고</span><b id="trade-free">— USDT</b></div>
        <div id="trade-order-error" role="status" aria-live="polite"></div>
        <div class="trade-actions"><button type="button" data-open-side="LONG" class="trade-long">LONG · 매수</button><button type="button" data-open-side="SHORT" class="trade-short">SHORT · 매도</button></div>
        <p class="trade-disclaimer">실제 주문이 전송되지 않는 데모입니다. 시세는 Bybit 선형 무기한 공개 데이터를 사용합니다.</p>
      </aside>
    </div>
    <footer class="trade-account-footer"><span>모의 계좌</span><span>잔고 <b id="trade-footer-balance">—</b> USDT</span><span>평가 자산 <b id="trade-footer-equity">—</b> USDT</span><span>미실현 손익 <b id="trade-footer-pnl">—</b> USDT</span><span>사용 가능 <b id="trade-footer-available">—</b> USDT</span></footer>`;
  document.body.appendChild(page);
  state.latestDecision = readStoredDecision();
  window.LITE.latestDecision = state.latestDecision;
  renderDecisionBanner();

  $('#trade-coin').addEventListener('change', (event) => {
    state.symbol = `${event.target.value}USDT`;
    state.price = null;
    state.change = null;
    state.book = { bids: new Map(), asks: new Map() };
    renderOrderBook();
    resetChartHistory();
    $('#trade-price').textContent = '—';
    $('#trade-change').textContent = '—';
    loadMarket();
  });
  $('#trade-frames').addEventListener('click', (event) => {
    const button = event.target.closest('[data-frame]');
    if (!button) return;
    state.frame = FRAMES.find((frame) => frame.key === button.dataset.frame) || FRAMES[3];
    $$('#trade-frames button').forEach((el) => el.classList.toggle('active', el === button));
    resetChartHistory();
    loadMarket();
  });
  $('#trade-latest').addEventListener('click', () => {
    state.chart.rightTime = null;
    $('#trade-history-status').textContent = '';
    drawChart();
  });
  $('#trade-ai-apply').addEventListener('click', applyLatestDecision);
  $('#trade-indicators').addEventListener('change', (event) => {
    const input = event.target.closest('[data-indicator]');
    if (!input) return;
    if (input.checked) state.indicators.add(input.dataset.indicator);
    else state.indicators.delete(input.dataset.indicator);
    $('#trade-rsi-wrap').classList.toggle('hidden', !state.indicators.has('rsi'));
    drawChart();
  });
  $('#trade-subtabs').addEventListener('click', (event) => {
    const button = event.target.closest('[data-tab]');
    if (!button) return;
    state.tab = button.dataset.tab;
    $$('#trade-subtabs button').forEach((el) => el.classList.toggle('active', el === button));
    renderPositions();
  });
  $('.trade-margin-modes').addEventListener('click', (event) => {
    const button = event.target.closest('[data-margin]');
    if (!button) return;
    state.marginMode = button.dataset.margin;
    $$('.trade-margin-modes [data-margin]').forEach((el) => el.classList.toggle('active', el === button));
  });
  $$('.trade-order-types button').forEach((button) => button.addEventListener('click', () => {
    selectOrderType(button.dataset.orderType);
  }));
  $('#trade-percent').addEventListener('click', (event) => {
    const button = event.target.closest('[data-percent]');
    if (!button) return;
    const account = LITE.paper.snapshot().account;
    $('#trade-amount').value = (account.availableBalance * Number(button.dataset.percent) / 100).toFixed(2);
    updateTicket();
  });
  $('#trade-amount').addEventListener('input', updateTicket);
  $('#trade-tp').addEventListener('input', updateTicket);
  $('#trade-sl').addEventListener('input', updateTicket);
  $('#trade-leverage').addEventListener('change', updateTicket);
  $('#trade-side-select').addEventListener('click', (event) => {
    const button = event.target.closest('[data-select-side]');
    if (button) selectOrderSide(button.dataset.selectSide);
  });
  $('.trade-actions').addEventListener('click', (event) => {
    const button = event.target.closest('[data-open-side]');
    if (button) {
      selectOrderSide(button.dataset.openSide);
      placeOrder(button.dataset.openSide);
    }
  });
  $('#trade-table-body').addEventListener('click', (event) => {
    const close = event.target.closest('[data-close-position]');
    if (close) closePositionById(close.dataset.closePosition);
    const cancel = event.target.closest('[data-cancel-order]');
    if (cancel) {
      state.pending = state.pending.filter((order) => order.id !== cancel.dataset.cancelOrder);
      renderPositions();
    }
  });
  $('.trade-orderbook').addEventListener('click', (event) => {
    const row = event.target.closest('[data-book-price]');
    if (!row) return;
    selectOrderType('limit');
    $('#trade-limit-price').value = row.dataset.bookPrice;
    updateTicket();
  });
  LITE.bus.subscribe((event) => {
    if (event && event.type === 'position') renderPositions();
  });
  window.addEventListener('resize', drawChart);
  const chartCanvas = $('#trade-chart');
  chartCanvas.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 || state.candles.length < 2) return;
    chartCanvas.setPointerCapture(event.pointerId);
    state.chart.drag = {
      pointerId: event.pointerId,
      x: event.clientX,
      rightTime: state.chart.rightTime == null ? state.candles[state.candles.length - 1].t : state.chart.rightTime,
      width: chartCanvas.getBoundingClientRect().width,
    };
    chartCanvas.classList.add('dragging');
  });
  chartCanvas.addEventListener('pointermove', (event) => {
    const drag = state.chart.drag;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const step = Math.max(1, drag.width / state.chart.visibleCount);
    const latest = state.candles[state.candles.length - 1].t;
    state.chart.rightTime = Math.min(latest, drag.rightTime - ((event.clientX - drag.x) / step) * state.frame.ms);
    drawChart();
    maybeLoadOlder();
  });
  const endChartDrag = (event) => {
    if (!state.chart.drag || state.chart.drag.pointerId !== event.pointerId) return;
    state.chart.drag = null;
    chartCanvas.classList.remove('dragging');
  };
  chartCanvas.addEventListener('pointerup', endChartDrag);
  chartCanvas.addEventListener('pointercancel', endChartDrag);
  chartCanvas.addEventListener('wheel', (event) => {
    if (state.view !== 'trading' || state.candles.length < 2) return;
    event.preventDefault();
    const oldCount = state.chart.visibleCount;
    const nextCount = Math.max(20, Math.min(500, Math.round(oldCount * Math.exp(event.deltaY * 0.001))));
    if (nextCount === oldCount) return;
    const range = visibleRange();
    const bounds = chartCanvas.getBoundingClientRect();
    const xRatio = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width));
    const anchorIndex = range.start + xRatio * (range.end - range.start);
    const nextEnd = Math.round(anchorIndex + (1 - xRatio) * (nextCount - 1));
    state.chart.visibleCount = nextCount;
    state.chart.rightTime = nextCount === oldCount ? state.chart.rightTime : (state.chart.rightTime == null && nextEnd >= state.candles.length - 1
      ? null
      : state.candles[Math.max(0, Math.min(state.candles.length - 1, nextEnd))].t);
    drawChart();
    maybeLoadOlder();
  }, { passive: false });

  function setView(view) {
    state.view = view === 'trading' ? 'trading' : view === 'screener' ? 'screener' : view === 'whale' ? 'whale' : 'office';
    document.documentElement.classList.toggle('trading-view', state.view === 'trading');
    document.documentElement.classList.toggle('screener-view', state.view === 'screener');
    document.documentElement.classList.toggle('whale-view', state.view === 'whale');
    // 스크리너는 자기 탭이 보일 때만 Bybit 시세를 갱신한다 (사무실·트레이딩에서는 요청하지 않음)
    if (window.LiteScreener && typeof window.LiteScreener.setActive === 'function') window.LiteScreener.setActive(state.view === 'screener');
    // 고래·일정도 그 화면이 보이는 동안에만 TronScan을 부른다
    if (window.LiteWhale && typeof window.LiteWhale.setActive === 'function') window.LiteWhale.setActive(state.view === 'whale');
    $$('#view-switch [data-view]').forEach((button) => button.classList.toggle('active', button.dataset.view === state.view));
    if (state.view === 'trading') {
      if (LITE.live && LITE.live.feed) LITE.live.feed.want([]);
      state.feed = state.feed || window.LiteLive.createFeed({ WebSocketImpl: window.WebSocket, onTick: onMarketTick });
      loadMarket();
      refreshSubscriptions();
      refreshTimer();
      renderPositions();
    } else {
      if (state.loadTimer) clearInterval(state.loadTimer);
      if (state.markTimer) clearInterval(state.markTimer);
      state.loadTimer = 0;
      state.markTimer = 0;
      if (state.feed) state.feed.want([]);
      if (LITE.live && typeof LITE.live.refresh === 'function') LITE.live.refresh();
    }
  }

  function readStoredDecision() {
    try {
      const raw = localStorage.getItem(DECISION_KEY);
      if (!raw) return null;
      const decision = JSON.parse(raw);
      return decision && ['LONG', 'SHORT', 'HOLD'].includes(decision.action) ? decision : null;
    } catch (error) {
      console.warn('저장된 AI 판정을 불러오지 못했습니다.', error);
      return null;
    }
  }

  function setDecision(event) {
    const side = event.plan && (event.plan.side === 'LONG' || event.plan.side === 'SHORT') ? event.plan.side : null;
    const actionText = String(event.action || '').toUpperCase();
    const action = side || (actionText === 'BUY' ? 'LONG' : actionText === 'SELL' ? 'SHORT' : 'HOLD');
    const sourcePlan = event.plan || {};
    state.latestDecision = {
      action,
      side,
      symbol: String(event.symbol || ''),
      entry: Number.isFinite(Number(sourcePlan.entry)) ? Number(sourcePlan.entry) : null,
      target: Number.isFinite(Number(sourcePlan.target)) ? Number(sourcePlan.target) : null,
      stop: Number.isFinite(Number(sourcePlan.stop)) ? Number(sourcePlan.stop) : null,
      summary: String(event.rationale || event.report || '').trim(),
      timestamp: Number(event.timestamp) || Date.now(),
    };
    window.LITE.latestDecision = state.latestDecision;
    try {
      localStorage.setItem(DECISION_KEY, JSON.stringify(state.latestDecision));
    } catch (error) {
      console.warn('최신 AI 판정을 브라우저에 저장하지 못했습니다.', error);
      $('#trade-ai-summary').textContent = '최신 판정은 표시되지만 브라우저 저장에 실패했습니다.';
    }
    renderDecisionBanner();
  }

  function renderDecisionBanner() {
    const decision = state.latestDecision;
    const banner = $('#trade-ai-banner');
    if (!decision) {
      banner.classList.add('hidden');
      return;
    }
    banner.classList.remove('hidden');
    $('#trade-ai-action').textContent = `AI ${decision.action}`;
    $('#trade-ai-action').className = `trade-ai-${decision.action.toLowerCase()}`;
    $('#trade-ai-symbol').textContent = decision.symbol ? decision.symbol.toUpperCase() : '';
    $('#trade-ai-summary').textContent = decision.summary || '요약이 제공되지 않았습니다.';
    $('#trade-ai-entry').textContent = decision.entry ? priceText(decision.entry) : '—';
    $('#trade-ai-target').textContent = decision.target ? priceText(decision.target) : '—';
    $('#trade-ai-stop').textContent = decision.stop ? priceText(decision.stop) : '—';
    $('#trade-ai-apply').disabled = !decision.side;
    $('#trade-ai-apply').title = decision.side ? '추천 방향·진입·TP·SL을 주문창에 채웁니다.' : '관망 판정은 주문에 적용할 방향이 없습니다.';
  }

  function selectOrderType(type) {
    const limit = type === 'limit';
    $$('.trade-order-types button').forEach((button) => button.classList.toggle('active', button.dataset.orderType === (limit ? 'limit' : 'market')));
    $('#trade-limit-price').disabled = !limit;
    if (!limit) $('#trade-limit-price').value = '';
    else if (!$('#trade-limit-price').value && state.price) $('#trade-limit-price').value = state.price;
    $('#trade-price-label').textContent = limit ? '지정 가격' : '시장가 예상 가격';
    updateTicket();
  }

  function selectOrderSide(side) {
    if (side !== 'LONG' && side !== 'SHORT') return;
    state.orderSide = side;
    $$('#trade-side-select [data-select-side]').forEach((button) => button.classList.toggle('selected', button.dataset.selectSide === side));
    $$('.trade-actions [data-open-side]').forEach((button) => button.classList.toggle('recommended', button.dataset.openSide === side));
  }

  function applyLatestDecision() {
    const decision = state.latestDecision;
    if (!decision || !decision.side) return;
    selectOrderSide(decision.side);
    if (Number.isFinite(decision.entry) && decision.entry > 0) {
      selectOrderType('limit');
      $('#trade-limit-price').value = decision.entry;
    }
    $('#trade-tp').value = Number.isFinite(decision.target) && decision.target > 0 ? decision.target : '';
    $('#trade-sl').value = Number.isFinite(decision.stop) && decision.stop > 0 ? decision.stop : '';
    const symbol = decision.symbol.toUpperCase().replace(/[^A-Z0-9]/g, '');
    const coin = COINS.find((name) => symbol === name || symbol.startsWith(`${name}USDT`));
    if (coin && state.symbol !== `${coin}USDT`) {
      state.symbol = `${coin}USDT`;
      $('#trade-coin').value = coin;
      state.price = null;
      state.change = null;
      state.book = { bids: new Map(), asks: new Map() };
      renderOrderBook();
      resetChartHistory();
      loadMarket();
    }
    updateTicket();
    $('#trade-order-error').textContent = 'AI 추천 방향과 가격을 주문창에 적용했습니다. 주문을 확인한 뒤 실행하세요.';
  }

  async function loadMarket() {
    if (state.view !== 'trading') return;
    const seq = ++state.requestSeq;
    state.chart.loadingOlder = false;
    state.chart.exhausted = false;
    $('#trade-chart-message').textContent = 'Bybit 캔들을 불러오는 중…';
    $('#trade-chart-message').classList.remove('hidden');
    $('#trade-coin').value = state.symbol.replace(/USDT$/, '');
    try {
      const url = `${candleUrl}?category=linear&symbol=${encodeURIComponent(state.symbol)}&interval=${state.frame.api}&limit=500`;
      const response = await fetch(url);
      if (!response.ok) throw new Error(`Bybit 캔들 HTTP ${response.status}`);
      const body = await response.json();
      if (body.retCode !== 0 || !Array.isArray(body.result && body.result.list)) {
        throw new Error(`Bybit 캔들 오류: ${body.retMsg || '응답을 읽을 수 없습니다'}`);
      }
      if (seq !== state.requestSeq) return;
      state.candles = parseCandles(body.result.list);
      if (state.candles.length < 2) throw new Error('Bybit 캔들 데이터가 부족합니다.');
      $('#trade-chart-message').classList.add('hidden');
      $('#trade-history-status').textContent = '';
      drawChart();
      refreshSubscriptions();
    } catch (error) {
      if (seq !== state.requestSeq) return;
      $('#trade-chart-message').textContent = error.message || '시세를 불러오지 못했습니다.';
      $('#trade-chart-message').classList.remove('hidden');
    }
    refreshTickers();
  }

  function parseCandles(rows) {
    return rows.map((row) => ({
      t: Number(row[0]), o: Number(row[1]), h: Number(row[2]), l: Number(row[3]), c: Number(row[4]), v: Number(row[5]),
    })).filter((candle) => [candle.t, candle.o, candle.h, candle.l, candle.c, candle.v].every(Number.isFinite))
      .sort((a, b) => a.t - b.t);
  }

  function resetChartHistory() {
    state.chart.rightTime = null;
    state.chart.loadingOlder = false;
    state.chart.exhausted = false;
    $('#trade-history-status').textContent = '';
    $('#trade-latest').classList.add('hidden');
  }

  function visibleRange() {
    const candles = state.candles;
    if (!candles.length) return { start: 0, end: -1 };
    const latestIndex = candles.length - 1;
    let end = latestIndex;
    if (state.chart.rightTime != null) {
      let low = 0, high = candles.length;
      while (low < high) {
        const mid = (low + high) >> 1;
        if (candles[mid].t <= state.chart.rightTime) low = mid + 1;
        else high = mid;
      }
      end = low - 1;
    }
    return { start: Math.max(0, end - state.chart.visibleCount + 1), end };
  }

  function maybeLoadOlder() {
    if (state.view !== 'trading' || state.chart.loadingOlder || state.chart.exhausted || state.candles.length < 2) return;
    const range = visibleRange();
    if (range.start <= 35) loadOlderCandles();
  }

  async function loadOlderCandles() {
    if (state.chart.loadingOlder || state.chart.exhausted || state.candles.length < 2) return;
    const seq = state.requestSeq;
    const symbol = state.symbol;
    const interval = state.frame.api;
    const oldest = state.candles[0].t;
    state.chart.loadingOlder = true;
    let loaded = false;
    $('#trade-history-status').textContent = '과거 캔들을 불러오는 중…';
    try {
      const url = `${candleUrl}?category=linear&symbol=${encodeURIComponent(symbol)}&interval=${interval}&end=${oldest - 1}&limit=500`;
      const response = await fetch(url);
      if (!response.ok) throw new Error(`Bybit 과거 캔들 HTTP ${response.status}`);
      const body = await response.json();
      if (body.retCode !== 0 || !Array.isArray(body.result && body.result.list)) {
        throw new Error(`Bybit 과거 캔들 오류: ${body.retMsg || '응답을 읽을 수 없습니다'}`);
      }
      if (seq !== state.requestSeq || symbol !== state.symbol || interval !== state.frame.api) return;
      const older = parseCandles(body.result.list).filter((candle) => candle.t < oldest);
      if (!older.length) {
        state.chart.exhausted = true;
        $('#trade-history-status').textContent = '더 불러올 과거 캔들이 없습니다.';
        return;
      }
      const byTime = new Map(older.concat(state.candles).map((candle) => [candle.t, candle]));
      state.candles = [...byTime.values()].sort((a, b) => a.t - b.t);
      loaded = true;
      $('#trade-history-status').textContent = '';
      drawChart();
    } catch (error) {
      if (seq === state.requestSeq) $('#trade-history-status').textContent = error.message || '과거 캔들을 불러오지 못했습니다.';
    } finally {
      if (seq === state.requestSeq) {
        state.chart.loadingOlder = false;
        if (loaded) maybeLoadOlder();
      }
    }
  }

  function refreshSubscriptions() {
    if (!state.feed || state.view !== 'trading') return;
    const topics = new Set([`kline.${state.frame.api}.${state.symbol}`, `tickers.${state.symbol}`, `orderbook.50.${state.symbol}`]);
    for (const pos of LITE.paper.snapshot().open) {
      topics.add(`kline.1.${pos.symbol}`);
      topics.add(`tickers.${pos.symbol}`);
    }
    for (const order of state.pending) topics.add(`tickers.${order.symbol}`);
    state.feed.want([...topics]);
  }

  function refreshTimer() {
    if (state.loadTimer) clearInterval(state.loadTimer);
    if (state.markTimer) clearInterval(state.markTimer);
    state.loadTimer = setInterval(() => {
      if (state.view === 'trading') refreshTickers();
    }, 2000);
    state.markTimer = setInterval(() => {
      if (state.view === 'trading') {
        refreshSubscriptions();
        renderPositions();
      }
    }, 1000);
  }

  async function refreshTickers() {
    const symbols = new Set([state.symbol, ...LITE.paper.snapshot().open.map((position) => position.symbol), ...state.pending.map((order) => order.symbol)]);
    await Promise.all([...symbols].map(async (symbol) => {
      try {
        const response = await fetch(`${tickerUrl}?category=linear&symbol=${encodeURIComponent(symbol)}`);
        if (!response.ok) throw new Error(`Bybit ticker HTTP ${response.status}`);
        const body = await response.json();
        const row = body.retCode === 0 && body.result && body.result.list && body.result.list[0];
        if (!row) throw new Error(`Bybit ticker 오류: ${body.retMsg || '시세를 찾을 수 없습니다'}`);
        processPrice(symbol, Number(row.lastPrice), Number(row.price24hPcnt) * 100);
      } catch (error) {
        if (state.view === 'trading' && symbol === state.symbol) $('#trade-order-error').textContent = error.message;
      }
    }));
  }

  function updateQuote(price, change) {
    if (!Number.isFinite(price) || price <= 0) return;
    state.price = price;
    if (Number.isFinite(change)) state.change = change;
    $('#trade-price').textContent = `$${priceText(price)}`;
    const changeNode = $('#trade-change');
    changeNode.textContent = `${state.change >= 0 ? '+' : ''}${fmt(state.change, 2)}%`;
    changeNode.className = state.change >= 0 ? 'up' : 'down';
    $('#trade-book-price').textContent = priceText(price);
    const last = state.candles[state.candles.length - 1];
    if (last) {
      const bucket = candleBucket(Date.now());
      if (bucket === last.t) {
        last.c = price;
        last.h = Math.max(last.h, price);
        last.l = Math.min(last.l, price);
      } else if (bucket > last.t) {
        state.candles.push({ t: bucket, o: last.c, h: Math.max(last.c, price), l: Math.min(last.c, price), c: price, v: 0 });
      }
    }
    updateTicket();
    drawChart();
  }

  function candleBucket(timestamp) {
    if (state.frame.api === 'W') return Math.floor((timestamp - 3 * 86400000) / state.frame.ms) * state.frame.ms + 3 * 86400000;
    return Math.floor(timestamp / state.frame.ms) * state.frame.ms;
  }

  function updateBookSide(side, levels) {
    for (const [price, quantity] of levels) {
      if (quantity === 0) side.delete(price);
      else side.set(price, quantity);
    }
  }

  function renderOrderBook() {
    const asks = [...state.book.asks.entries()].filter(([, quantity]) => quantity > 0)
      .sort((a, b) => b[0] - a[0]).slice(0, 14);
    const bids = [...state.book.bids.entries()].filter(([, quantity]) => quantity > 0)
      .sort((a, b) => b[0] - a[0]).slice(0, 14);
    const rows = (items, side) => {
      let cumulative = 0;
      const totals = new Array(items.length);
      const order = side === 'ask' ? [...items.keys()].reverse() : [...items.keys()];
      order.forEach((index) => {
        cumulative += items[index][1];
        totals[index] = cumulative;
      });
      return items.map(([price, quantity], index) =>
        `<button type="button" class="trade-book-row ${side}" data-book-price="${price}" title="지정가 ${priceText(price)} · 누적 ${totals[index].toFixed(4)}"><i style="width:${Math.min(100, totals[index] / cumulative * 100)}%"></i><b>${priceText(price)}</b><span>${quantity.toFixed(4)}</span><span>${totals[index].toFixed(4)}</span></button>`
      ).join('') || '<div class="trade-book-empty">호가 대기 중</div>';
    };
    $('#trade-asks').innerHTML = rows(asks, 'ask');
    $('#trade-bids').innerHTML = rows(bids, 'bid');
    if (state.price) $('#trade-book-price').textContent = priceText(state.price);
    const bestAsk = asks.length ? Math.min(...asks.map(([price]) => price)) : null;
    const bestBid = bids.length ? Math.max(...bids.map(([price]) => price)) : null;
    const spread = bestAsk != null && bestBid != null ? bestAsk - bestBid : null;
    $('#trade-book-spread').textContent = spread == null ? '—' : `${priceText(spread)} (${(spread / ((bestAsk + bestBid) / 2) * 100).toFixed(3)}%)`;
    if (!asks.length && !bids.length) $('#trade-book-state').textContent = '연결 중';
  }

  function onMarketTick(tick) {
    if (tick.type === 'orderbook') {
      if (tick.symbol === state.symbol) {
        if (tick.action === 'snapshot') {
          state.book.bids.clear();
          state.book.asks.clear();
        }
        updateBookSide(state.book.bids, tick.bids);
        updateBookSide(state.book.asks, tick.asks);
        $('#trade-book-state').textContent = 'LIVE';
        renderOrderBook();
      }
      return;
    }
    if (tick.type === 'ticker') {
      processPrice(tick.symbol, tick.price, tick.changePct);
      return;
    }
    if (tick.symbol === state.symbol && tick.interval === state.frame.api) {
      const candle = { t: tick.t, o: tick.o, h: tick.h, l: tick.l, c: tick.c, v: tick.v || 0 };
      const index = state.candles.findIndex((item) => item.t === candle.t);
      if (index >= 0) state.candles[index] = candle;
      else if (!state.candles.length || candle.t > state.candles[state.candles.length - 1].t) state.candles.push(candle);
      state.price = tick.c;
      drawChart();
      updateTicket();
    }
    for (const pos of LITE.paper.snapshot().open) {
      if (pos.symbol !== tick.symbol || tick.symbol === state.symbol) continue;
      const result = LITE.paper.tick(pos.symbol, tick.c);
      result.done.forEach((position) => LITE.bus.emit({ type: 'position', action: 'close', position }));
      if (result.marked) scheduleAccountRefresh();
    }
    renderPositions();
  }

  function processPrice(symbol, price, change) {
    if (!Number.isFinite(price) || price <= 0) return;
    if (symbol === state.symbol) updateQuote(price, change);
    let marked = false;
    for (const position of LITE.paper.snapshot().open) {
      if (position.symbol !== symbol) continue;
      const result = LITE.paper.tick(symbol, price);
      result.done.forEach((closed) => LITE.bus.emit({ type: 'position', action: 'close', position: closed }));
      marked = marked || result.marked > 0;
    }
    if (marked) scheduleAccountRefresh();
    matchPending(symbol, price);
    renderPositions();
  }

  function scheduleAccountRefresh() {
    if (state.accountTimer || typeof window.loadPositions !== 'function') return;
    state.accountTimer = setTimeout(() => {
      state.accountTimer = 0;
      window.loadPositions();
    }, 500);
  }

  function matchPending(symbol, price) {
    const filled = [];
    state.pending = state.pending.filter((order) => {
      if (order.symbol !== symbol) return true;
      const crosses = order.side === 'LONG' ? price <= order.limitPrice : price >= order.limitPrice;
      if (crosses) filled.push(order);
      return !crosses;
    });
    filled.forEach((order) => openPosition(order, order.limitPrice));
    if (filled.length) renderPositions();
  }

  function updateTicket() {
    const snapshot = LITE.paper.snapshot();
    const account = snapshot.account;
    $('#trade-balance').textContent = fmt(account.balance);
    $('#trade-equity').textContent = fmt(account.equity);
    $('#trade-available').textContent = fmt(account.availableBalance);
    $('#trade-footer-balance').textContent = fmt(account.balance);
    $('#trade-footer-equity').textContent = fmt(account.equity);
    $('#trade-footer-pnl').textContent = signed(account.unrealized);
    $('#trade-footer-pnl').className = account.unrealized >= 0 ? 'up' : 'down';
    $('#trade-footer-available').textContent = fmt(account.availableBalance);
    $('#trade-free').textContent = `${fmt(account.availableBalance)} USDT`;
    const amount = Number($('#trade-amount').value);
    const leverage = Number($('#trade-leverage').value);
    const price = $('.trade-order-types .active').dataset.orderType === 'limit' ? Number($('#trade-limit-price').value) : state.price;
    const notional = amount * leverage;
    $('#trade-notional').textContent = `${fmt(notional)} USDT`;
    $('#trade-qty').textContent = price > 0 && Number.isFinite(notional) ? `${(notional / price).toFixed(6)} ${state.symbol.replace(/USDT$/, '')}` : '—';
    if (amount > account.availableBalance) $('#trade-order-error').textContent = '주문 증거금이 사용 가능 잔고보다 큽니다.';
    else if (!$('#trade-order-error').textContent.startsWith('Bybit')) $('#trade-order-error').textContent = '';
  }

  function placeOrder(side) {
    const amount = Number($('#trade-amount').value);
    const leverage = Number($('#trade-leverage').value);
    const snapshot = LITE.paper.snapshot();
    if (!Number.isFinite(amount) || amount < 0.01 || amount > snapshot.account.availableBalance) {
      $('#trade-order-error').textContent = `주문 증거금을 확인하세요. 사용 가능 잔고 ${fmt(snapshot.account.availableBalance)} USDT`;
      return;
    }
    if (!Number.isInteger(leverage) || leverage < 1 || leverage > 50) {
      $('#trade-order-error').textContent = '레버리지는 1~50x 범위여야 합니다.';
      return;
    }
    const isLimit = $('.trade-order-types .active').dataset.orderType === 'limit';
    const price = isLimit ? Number($('#trade-limit-price').value) : state.price;
    if (!Number.isFinite(price) || price <= 0) {
      $('#trade-order-error').textContent = isLimit ? '지정 가격을 입력하세요.' : 'Bybit 현재가를 아직 받지 못했습니다.';
      return;
    }
    const target = $('#trade-tp').value === '' ? null : Number($('#trade-tp').value);
    const stop = $('#trade-sl').value === '' ? null : Number($('#trade-sl').value);
    const direction = side === 'LONG' ? 1 : -1;
    if (target != null && (!Number.isFinite(target) || target <= 0 || (target - price) * direction <= 0)) {
      $('#trade-order-error').textContent = '익절가는 진입가 기준 수익 방향으로 입력하세요.';
      return;
    }
    if (stop != null && (!Number.isFinite(stop) || stop <= 0 || (stop - price) * direction >= 0)) {
      $('#trade-order-error').textContent = '손절가는 진입가 기준 손실 방향으로 입력하세요.';
      return;
    }
    $('#trade-order-error').textContent = '';
    const order = { id: `ord-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, symbol: state.symbol, side, amount, leverage, limitPrice: price, marginMode: state.marginMode, target, stop };
    if (isLimit) {
      const crosses = side === 'LONG' ? state.price <= price : state.price >= price;
      if (!crosses) {
        state.pending.unshift(order);
        $('#trade-order-error').textContent = '지정 주문을 대기 목록에 등록했습니다.';
        renderPositions();
        return;
      }
    }
    openPosition(order, isLimit ? price : state.price);
  }

  function openPosition(order, price) {
    const liquidationMove = Math.max(0.005, 1 / order.leverage - 0.005);
    const liq = order.side === 'LONG' ? price * (1 - liquidationMove) : price * (1 + liquidationMove);
    const stop = Number.isFinite(order.stop) ? order.stop : liq;
    const result = LITE.paper.open({
      symbol: order.symbol, display: order.symbol.replace(/USDT$/, ''), side: order.side, mode: 'manual', axis: 'perp',
      entry: price, stop, target: Number.isFinite(order.target) ? order.target : null, liq, leverage: order.leverage, marginAmount: order.amount,
      source: 'manual', marginMode: order.marginMode,
    });
    if (!result.position) {
      $('#trade-order-error').textContent = result.skipped || '모의 주문을 실행하지 못했습니다.';
      renderPositions();
      return;
    }
    LITE.bus.emit({ type: 'position', action: 'open', position: result.position });
    $('#trade-order-error').textContent = `${order.symbol.replace(/USDT$/, '')} ${order.side} 모의 주문 체결`;
    if (typeof window.loadPositions === 'function') window.loadPositions();
    refreshSubscriptions();
    renderPositions();
  }

  async function closePositionById(id) {
    const result = await fetch('/api/positions/close', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id }),
    });
    const body = await result.json().catch(() => null);
    if (!result.ok || !body || !body.ok) {
      $('#trade-order-error').textContent = body && body.error ? body.error : `청산 실패 (${result.status})`;
      return;
    }
    if (typeof window.loadPositions === 'function') await window.loadPositions();
    renderPositions();
    refreshSubscriptions();
  }

  function renderPositions() {
    if (!$('#trade-table-body')) return;
    const snapshot = LITE.paper.snapshot();
    $('#trade-open-count').textContent = snapshot.open.length;
    updateTicket();
    const head = $('#trade-table-head');
    const body = $('#trade-table-body');
    if (state.tab === 'positions') {
      head.innerHTML = '<tr><th>계약</th><th>방향</th><th>진입가</th><th>현재가</th><th>TP</th><th>SL</th><th>수량</th><th>레버리지</th><th>미실현 PnL</th><th>수익률</th><th></th></tr>';
      const positions = snapshot.open;
      const pending = state.pending.map((order) => `<tr class="pending"><td>${safe(order.symbol.replace(/USDT$/, ''))}/USDT</td><td>${order.side}</td><td>${priceText(order.limitPrice)}</td><td colspan="7">지정 주문 대기 · ${fmt(order.amount)} USDT · TP ${order.target ? priceText(order.target) : '—'} / SL ${order.stop ? priceText(order.stop) : '—'}</td><td><button type="button" data-cancel-order="${safe(order.id)}">취소</button></td></tr>`).join('');
      body.innerHTML = positions.map((position) => {
        const pnl = Number(position.unrealizedAmt) || 0;
        const roe = Number(position.roePct) || 0;
        return `<tr><td>${safe(position.symbol.replace(/USDT$/, ''))}/USDT</td><td class="${position.side === 'LONG' ? 'up' : 'down'}">${position.side}</td><td>${priceText(position.entry)}</td><td>${priceText(position.lastPrice)}</td><td class="up">${position.target ? priceText(position.target) : '—'}</td><td class="down">${position.stop && position.stop !== position.liq ? priceText(position.stop) : '—'}</td><td>${Number(position.qty).toFixed(6)}</td><td>${position.leverage}x · ${safe(position.marginMode || 'ISOLATED')}</td><td class="${pnl >= 0 ? 'up' : 'down'}">${signed(pnl)} USDT</td><td class="${roe >= 0 ? 'up' : 'down'}">${signed(roe)}%</td><td><button class="trade-close" type="button" data-close-position="${safe(position.id)}">시장가 청산</button></td></tr>`;
      }).join('') + pending;
      if (!positions.length && !pending) body.innerHTML = '<tr><td colspan="11" class="trade-empty">열린 포지션이 없습니다. 주문 패널에서 바로 모의 주문을 시작하세요.</td></tr>';
    } else {
      head.innerHTML = '<tr><th>체결 시각</th><th>계약</th><th>방향</th><th>진입가</th><th>청산가</th><th>레버리지</th><th>실현 PnL</th><th>수익률</th><th>결과</th></tr>';
      const trades = snapshot.trades;
      body.innerHTML = trades.map((trade) => {
        const at = new Date(trade.closedAt);
        return `<tr><td>${safe(Number.isNaN(at.getTime()) ? '—' : at.toLocaleString())}</td><td>${safe(trade.symbol.replace(/USDT$/, ''))}/USDT</td><td>${trade.side}</td><td>${priceText(trade.entry)}</td><td>${priceText(trade.exit)}</td><td>${trade.leverage}x</td><td class="${trade.pnl >= 0 ? 'up' : 'down'}">${signed(trade.pnl)} USDT</td><td class="${trade.roePct >= 0 ? 'up' : 'down'}">${signed(trade.roePct)}%</td><td>${safe(trade.reason)}</td></tr>`;
      }).join('') || '<tr><td colspan="9" class="trade-empty">아직 체결 내역이 없습니다.</td></tr>';
    }
  }

  function average(index, period, exponential) {
    const values = state.candles.slice(0, index + 1).map((candle) => candle.c);
    if (values.length < period) return null;
    if (!exponential) return values.slice(-period).reduce((sum, value) => sum + value, 0) / period;
    const alpha = 2 / (period + 1);
    let value = values.slice(0, period).reduce((sum, price) => sum + price, 0) / period;
    for (let i = period; i < values.length; i++) value += alpha * (values[i] - value);
    return value;
  }

  function drawChart() {
    const canvas = $('#trade-chart');
    if (!canvas || document.documentElement.classList.contains('hidden')) return;
    const box = canvas.getBoundingClientRect();
    if (!box.width || !box.height) return;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(box.width * ratio);
    canvas.height = Math.round(box.height * ratio);
    const ctx = canvas.getContext('2d');
    ctx.scale(ratio, ratio);
    ctx.clearRect(0, 0, box.width, box.height);
    const allData = state.candles;
    const range = visibleRange();
    const data = allData.slice(range.start, range.end + 1);
    updateLatestButton();
    if (data.length < 2) return;
    const positionLevels = LITE.paper.snapshot().open
      .filter((position) => position.symbol === state.symbol)
      .flatMap((position) => [
        { label: '진입', price: position.entry, color: '#cbd5e1' },
        ...(position.target ? [{ label: 'TP', price: position.target, color: '#22c58b' }] : []),
        ...(position.stop && position.stop !== position.liq ? [{ label: 'SL', price: position.stop, color: '#f05268' }] : []),
      ]);
    const hasVolume = state.indicators.has('volume');
    const hasRsi = state.indicators.has('rsi');
    const chartBottom = box.height - (hasVolume ? 52 : 18) - (hasRsi ? 8 : 0);
    const max = Math.max(...data.map((candle) => candle.h), ...positionLevels.map((level) => level.price));
    const min = Math.min(...data.map((candle) => candle.l), ...positionLevels.map((level) => level.price));
    const pad = (max - min || max * 0.001) * 0.08;
    const top = min - pad, high = max + pad;
    const y = (price) => chartBottom - ((price - top) / (high - top)) * (chartBottom - 22);
    const xStep = box.width / data.length;
    ctx.font = '10px monospace';
    ctx.textBaseline = 'middle';
    ctx.strokeStyle = '#263247';
    ctx.fillStyle = '#77849a';
    for (let i = 0; i < 5; i++) {
      const py = 16 + (chartBottom - 16) * i / 4;
      ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(box.width, py); ctx.stroke();
      ctx.fillText(fmt(high - (high - top) * i / 4, state.price < 1 ? 5 : 2), 5, py + 1);
    }
    const volumeMax = Math.max(...data.map((candle) => candle.v || 0)) || 1;
    data.forEach((candle, index) => {
      const cx = index * xStep + xStep / 2;
      const rise = candle.c >= candle.o;
      const color = rise ? '#22c58b' : '#f05268';
      ctx.strokeStyle = color;
      ctx.fillStyle = rise ? 'rgba(34,197,139,.8)' : 'rgba(240,82,104,.82)';
      ctx.beginPath(); ctx.moveTo(cx, y(candle.h)); ctx.lineTo(cx, y(candle.l)); ctx.stroke();
      const barWidth = Math.max(1, xStep * 0.62);
      const bodyTop = Math.min(y(candle.o), y(candle.c));
      const bodyHeight = Math.max(1, Math.abs(y(candle.o) - y(candle.c)));
      ctx.fillRect(cx - barWidth / 2, bodyTop, barWidth, bodyHeight);
      if (hasVolume) {
        const volumeHeight = (candle.v || 0) / volumeMax * 34;
        ctx.fillStyle = rise ? 'rgba(34,197,139,.22)' : 'rgba(240,82,104,.22)';
        ctx.fillRect(cx - barWidth / 2, box.height - 38 - volumeHeight, barWidth, volumeHeight);
      }
    });
    if (state.indicators.has('ma20')) drawLine(ctx, data, range.start, (i) => average(i, 20, false), xStep, y, '#eabf54');
    if (state.indicators.has('ma50')) drawLine(ctx, data, range.start, (i) => average(i, 50, false), xStep, y, '#69a9ff');
    if (state.indicators.has('ema20')) drawLine(ctx, data, range.start, (i) => average(i, 20, true), xStep, y, '#bc86ff');
    if (state.indicators.has('bb')) {
      drawLine(ctx, data, range.start, (i) => bollinger(i).upper, xStep, y, 'rgba(148,163,184,.8)');
      drawLine(ctx, data, range.start, (i) => bollinger(i).lower, xStep, y, 'rgba(148,163,184,.8)');
    }
    if (state.price) {
      ctx.setLineDash([4, 4]); ctx.strokeStyle = '#e2e8f0'; ctx.beginPath(); ctx.moveTo(0, y(state.price)); ctx.lineTo(box.width, y(state.price)); ctx.stroke(); ctx.setLineDash([]);
    }
    positionLevels.forEach((level) => {
      const py = y(level.price);
      ctx.setLineDash([5, 4]);
      ctx.strokeStyle = level.color;
      ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(box.width - 76, py); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = level.color;
      ctx.fillRect(box.width - 74, py - 9, 72, 18);
      ctx.fillStyle = '#0b0e13';
      ctx.font = '9px monospace';
      ctx.fillText(`${level.label} ${priceText(level.price)}`, box.width - 70, py);
    });
    if (hasRsi) drawRsi(range.start, range.end);
  }

  function drawLine(ctx, data, offset, calc, xStep, y, color) {
    ctx.beginPath();
    ctx.strokeStyle = color;
    let started = false;
    data.forEach((_, index) => {
      const value = calc(offset + index);
      if (!Number.isFinite(value)) return;
      const px = index * xStep + xStep / 2;
      if (!started) { ctx.moveTo(px, y(value)); started = true; }
      else ctx.lineTo(px, y(value));
    });
    if (started) ctx.stroke();
  }

  function bollinger(index) {
    if (index < 19) return { upper: null, lower: null };
    const values = state.candles.slice(index - 19, index + 1).map((candle) => candle.c);
    const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
    const deviation = Math.sqrt(values.reduce((sum, value) => sum + (value - mean) ** 2, 0) / values.length);
    return { upper: mean + 2 * deviation, lower: mean - 2 * deviation };
  }

  function drawRsi(start, end) {
    const canvas = $('#trade-rsi');
    const box = canvas.getBoundingClientRect();
    if (!box.width || !box.height) return;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(box.width * ratio); canvas.height = Math.round(box.height * ratio);
    const ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio);
    ctx.clearRect(0, 0, box.width, box.height);
    const values = [];
    for (let i = Math.max(14, start); i <= end; i++) {
      let gain = 0, loss = 0;
      for (let j = i - 13; j <= i; j++) {
        const change = state.candles[j].c - state.candles[j - 1].c;
        if (change > 0) gain += change; else loss -= change;
      }
      values.push({ index: i, value: loss === 0 ? 100 : 100 - 100 / (1 + gain / loss) });
    }
    ctx.strokeStyle = '#273247'; ctx.setLineDash([3, 3]);
    [30, 50, 70].forEach((rsi) => { const py = box.height * (1 - rsi / 100); ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(box.width, py); ctx.stroke(); });
    ctx.setLineDash([]); ctx.strokeStyle = '#bf8fff'; ctx.beginPath();
    let started = false;
    values.forEach(({ index, value }) => {
      const px = (index - start) * box.width / Math.max(1, end - start + 1);
      const py = box.height * (1 - value / 100);
      if (!started) { ctx.moveTo(px, py); started = true; }
      else ctx.lineTo(px, py);
    });
    if (started) ctx.stroke();
  }

  function updateLatestButton() {
    const range = visibleRange();
    $('#trade-latest').classList.toggle('hidden', state.chart.rightTime == null || range.end >= state.candles.length - 1);
  }

  function refreshPositions() {
    renderPositions();
    drawChart();
    refreshSubscriptions();
  }
  LITE.bus.subscribe((event) => { if (event && event.type === 'position') refreshPositions(); });
  // 스크리너에서 고른 종목으로 데모 트레이딩을 연다 — 목록에 없는 종목도 그때그때 붙인다
  function showSymbol(symbol) {
    const code = String(symbol || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (!code) return;
    const base = code.replace(/USDT$/, '');
    const select = $('#trade-coin');
    if (select && base && ![...select.options].some((option) => option.value === base)) {
      const option = document.createElement('option');
      option.value = base;
      option.textContent = `${base}/USDT`;
      select.appendChild(option);
    }
    state.symbol = code;
    state.price = null;
    state.change = null;
    state.book = { bids: new Map(), asks: new Map() };
    renderOrderBook();
    resetChartHistory();
    $('#trade-price').textContent = '—';
    $('#trade-change').textContent = '—';
    if (select && base) select.value = base;
    setView('trading');
  }

  window.LiteTrading = { show: () => setView('trading'), showSymbol, office: () => setView('office'), screener: () => setView('screener'), whale: () => setView('whale'), loadMarket, renderPositions, setDecision };
})();
