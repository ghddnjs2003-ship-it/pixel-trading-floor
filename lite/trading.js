/* Demo derivatives terminal: Bybit public market data and the local paper ledger only. */
(function () {
  'use strict';

  const LITE = window.LITE;
  if (!LITE || !window.LiteLive || !LITE.paper) return;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const COINS = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE'];
  const DECISION_KEY = 'pixel-lite-latest-decision-v1';
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
  // 기본 첫 화면 — 시장 대시보드. 다만 소개 페이지 속 임베드(?shell=1·?kiosk=1)는 종전대로 사무실이 먼저다.
  const DEFAULT_VIEW = (new URLSearchParams(location.search).get('shell') === '1' || LITE.kiosk) ? 'office' : 'dash';

  const state = {
    view: DEFAULT_VIEW, symbol: 'BTCUSDT', price: null, change: null,
    feed: null, requestSeq: 0, loadTimer: 0, markTimer: 0, accountTimer: 0, pending: [], tab: 'positions',
    marginMode: 'ISOLATED', book: { bids: new Map(), asks: new Map() }, orderSide: 'LONG', latestDecision: null, mobileView: 'chart',
  };

  const topbar = $('#topbar');
  if (!topbar) return;
  const nav = document.createElement('nav');
  nav.id = 'view-switch';
  nav.setAttribute('aria-label', '화면 전환');
  // 메인 탭 4개 — 시장 대시보드(기본) · 데모 트레이딩 · 고래·일정 · 픽셀 오피스
  nav.innerHTML = '<button type="button" data-view="dash">시장 대시보드</button><button type="button" data-view="trading">데모 트레이딩</button><button type="button" data-view="whale" title="실시간 고래 입출금 · 이번 주 경제 일정">고래·일정</button><button type="button" data-view="office">픽셀 오피스</button>';
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
    <nav class="trade-msv" id="trade-msv" role="tablist" aria-label="트레이딩 화면 전환">
      <button type="button" role="tab" data-tview="chart" class="active" aria-selected="true">차트</button>
      <button type="button" role="tab" data-tview="order" aria-selected="false">주문</button>
      <button type="button" role="tab" data-tview="book" aria-selected="false">호가</button>
      <button type="button" role="tab" data-tview="positions" aria-selected="false">포지션 <b id="trade-msv-count">0</b></button>
    </nav>
    <div class="trade-layout">
      <main class="trade-main">
        <section class="trade-chart-panel">
          <div class="trade-chart-wrap">
            <div class="trade-tv">
              <div id="trade-tv-chart"></div>
              <div id="trade-tv-note" role="status">트레이딩뷰 차트를 불러오는 중…</div>
            </div>
          </div>
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
        <label class="trade-field trade-field-wide"><span id="trade-price-label">시장가 예상 가격</span><input id="trade-limit-price" type="number" min="0" step="any" inputmode="decimal" placeholder="Bybit 현재가" disabled></label>
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

  // 휴대폰: 한 번에 한 화면 — 차트 · 주문 · 호가 · 포지션 (넓은 화면은 탭을 숨기고 세 칸을 함께 보여 준다)
  const PANE_SELECTORS = { chart: '.trade-chart-panel', order: '.trade-ticket', book: '.trade-orderbook', positions: '.trade-orders-panel' };
  Object.keys(PANE_SELECTORS).forEach((key) => {
    const pane = $(PANE_SELECTORS[key]);
    if (pane) pane.dataset.pane = key;
  });
  page.dataset.tview = state.mobileView;
  function setMobileView(view) {
    if (!PANE_SELECTORS[view]) return;
    state.mobileView = view;
    page.dataset.tview = view;
    $$('#trade-msv [data-tview]').forEach((button) => {
      const on = button.dataset.tview === view;
      button.classList.toggle('active', on);
      button.setAttribute('aria-selected', String(on));
    });
    const pane = $(PANE_SELECTORS[view]);
    if (pane) pane.scrollTop = 0;
    if (view === 'chart') requestAnimationFrame(mountTradingView);
    if (view === 'book') requestAnimationFrame(renderOrderBook);
  }
  $('#trade-msv').addEventListener('click', (event) => {
    const button = event.target.closest('[data-tview]');
    if (button) setMobileView(button.dataset.tview);
  });

  state.latestDecision = readStoredDecision();
  window.LITE.latestDecision = state.latestDecision;
  renderDecisionBanner();

  $('#trade-coin').addEventListener('change', (event) => {
    state.symbol = `${event.target.value}USDT`;
    state.price = null;
    state.change = null;
    state.book = { bids: new Map(), asks: new Map() };
    renderOrderBook();
    $('#trade-price').textContent = '—';
    $('#trade-change').textContent = '—';
    loadMarket();
  });
  $('#trade-ai-apply').addEventListener('click', applyLatestDecision);
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

  function setView(view) {
    state.view = view === 'trading' ? 'trading' : view === 'screener' ? 'screener' : view === 'whale' ? 'whale' : view === 'dash' ? 'dash' : 'office';
    document.documentElement.classList.toggle('trading-view', state.view === 'trading');
    document.documentElement.classList.toggle('screener-view', state.view === 'screener');
    document.documentElement.classList.toggle('whale-view', state.view === 'whale');
    document.documentElement.classList.toggle('dash-view', state.view === 'dash');
    // 스크리너는 자기 탭이 보일 때만 Bybit 시세를 갱신한다 (사무실·트레이딩에서는 요청하지 않음)
    if (window.LiteScreener && typeof window.LiteScreener.setActive === 'function') window.LiteScreener.setActive(state.view === 'screener');
    // 고래·일정은 그 화면이 보이는 동안 + 대시보드(요약 카드)가 보이는 동안에도 TronScan을 부른다
    if (window.LiteWhale && typeof window.LiteWhale.setActive === 'function') window.LiteWhale.setActive(state.view === 'whale' || state.view === 'dash');
    // 시장 대시보드 — 보일 때 실시간 갱신을 켜고, 감춰질 때는 끈다
    if (window.LiteDash && typeof window.LiteDash.setActive === 'function') window.LiteDash.setActive(state.view === 'dash');
    $$('#view-switch [data-view]').forEach((button) => button.classList.toggle('active', button.dataset.view === state.view));
    if (state.view === 'trading') {
      if (LITE.live && LITE.live.feed) LITE.live.feed.want([]);
      state.feed = state.feed || window.LiteLive.createFeed({ WebSocketImpl: window.WebSocket, onTick: onMarketTick });
      loadMarket();
      refreshSubscriptions();
      refreshTimer();
      renderPositions();
      if (state.mobileView === 'chart') requestAnimationFrame(mountTradingView);
    } else {
      if (state.loadTimer) clearInterval(state.loadTimer);
      if (state.markTimer) clearInterval(state.markTimer);
      state.loadTimer = 0;
      state.markTimer = 0;
      if (state.feed) state.feed.want([]);
      if (LITE.live && typeof LITE.live.refresh === 'function') LITE.live.refresh();
    }
  }

  // ===== 차트는 트레이딩뷰 공식 위젯 하나만 — 우리 캔버스 차트·지표 체크박스·시간봉 버튼은 없앴다 =====
  // (2026-11-20 홍원 님 "아니 저렇게 말고 우리차트는 지우고 트레이딩뷰만 사용할거야 전부다")
  // 시간봉·지표는 위젯 자체 툴바에서 고르고, 우리 화면은 종목만 위젯과 맞춘다.
  const tv = { widget: null, script: null, symbol: '', width: 0, noteTimer: 0 };
  // 위젯은 거래소 심볼을 따로 쓴다 — 바이비트 무기한(BYBIT:BTCUSDT.P), 미국 주식은 NASDAQ
  const TV_STOCKS = {
    NVDA: 'NASDAQ:NVDA', AAPL: 'NASDAQ:AAPL', MSFT: 'NASDAQ:MSFT', TSLA: 'NASDAQ:TSLA', AMZN: 'NASDAQ:AMZN',
    GOOGL: 'NASDAQ:GOOGL', META: 'NASDAQ:META', AMD: 'NASDAQ:AMD', COIN: 'NASDAQ:COIN', MSTR: 'NASDAQ:MSTR',
    SPY: 'AMEX:SPY', QQQ: 'NASDAQ:QQQ', TQQQ: 'NASDAQ:TQQQ',
  };
  const TV_INTERVAL = '15'; // 첫 화면 시간봉 — 이후에는 위젯 툴바에서 고른다

  function tvSymbol(symbol) {
    const code = String(symbol || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (!code) return 'BYBIT:BTCUSDT.P';
    if (TV_STOCKS[code]) return TV_STOCKS[code];
    return `BYBIT:${code.replace(/USDT$/, '')}USDT.P`;
  }

  function loadTvScript() {
    if (window.TradingView && typeof window.TradingView.widget === 'function') return Promise.resolve();
    if (tv.script) return tv.script;
    tv.script = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = 'https://s3.tradingview.com/tv.js';
      script.async = true;
      script.onload = () => (window.TradingView && typeof window.TradingView.widget === 'function'
        ? resolve()
        : reject(new Error('TradingView 위젯을 읽지 못했습니다.')));
      script.onerror = () => { tv.script = null; reject(new Error('TradingView 스크립트를 불러오지 못했습니다.')); };
      document.head.appendChild(script);
    });
    return tv.script;
  }

  function showTvNote(text) {
    const note = $('#trade-tv-note');
    if (!note) return;
    note.textContent = text;
    note.classList.remove('hidden');
  }

  function hideTvNote() {
    const note = $('#trade-tv-note');
    if (note) note.classList.add('hidden');
  }

  function mountTradingView() {
    if (state.view !== 'trading') return;
    const host = $('#trade-tv-chart');
    if (!host) return;
    // 숨은 칸(휴대폰의 다른 탭)에서는 폭이 0이라 위젯이 찌그러진다 — 보이는 칸이 생기면 그때 만든다
    if (!host.clientWidth || !host.clientHeight) return;
    const symbol = tvSymbol(state.symbol);
    if (tv.widget && tv.symbol === symbol && tv.width) return;
    tv.symbol = symbol;
    tv.width = host.clientWidth;
    showTvNote('트레이딩뷰 차트를 불러오는 중…');
    loadTvScript().then(() => {
      if (state.view !== 'trading' || tvSymbol(state.symbol) !== symbol) return;
      // 위젯은 칸의 실제 크기를 재고 그리므로, 화면에 올린 다음 프레임에 만든다
      requestAnimationFrame(() => {
        if (!host.clientWidth || !host.clientHeight) return;
        host.innerHTML = '';
        try {
          tv.widget = new window.TradingView.widget({
            container_id: 'trade-tv-chart',
            symbol,
            interval: TV_INTERVAL,
            theme: 'dark',
            style: '1',
            locale: 'kr',
            timezone: 'Asia/Seoul',
            toolbar_bg: '#10151e',
            backgroundColor: '#0b0e13',
            gridColor: 'rgba(38,49,67,.55)',
            autosize: true,
            hide_top_toolbar: false,
            // 좁은 화면은 왼쪽 그리기 툴바·범례를 접어 캔들 폭과 높이를 살린다
            hide_side_toolbar: window.innerWidth <= 768,
            hide_legend: window.innerWidth <= 768,
            allow_symbol_change: false,
            save_image: false,
            withdateranges: true,
            details: false,
            hotlist: false,
            enable_publishing: false,
            // 위젯 툴바에서 종목 검색·비교·스크린숏·전체화면은 쓰지 않는다 — 좁은 폭에서 버튼이 넘치지 않게
            disabled_features: ['header_symbol_search', 'symbol_search_hot_key', 'header_compare', 'header_screenshot', 'header_fullscreen_button', 'header_undo_redo'],
          });
          const iframe = host.querySelector('iframe');
          if (iframe) iframe.addEventListener('load', hideTvNote);
          if (tv.noteTimer) clearTimeout(tv.noteTimer);
          tv.noteTimer = setTimeout(hideTvNote, 8000);
        } catch (error) {
          showTvNote('트레이딩뷰 위젯을 띄우지 못했어요. 새로고침 후 다시 시도해 주세요.');
        }
      });
    }).catch(() => {
      showTvNote('트레이딩뷰 위젯을 불러오지 못했어요. 인터넷 연결이나 광고 차단 설정을 확인해 주세요.');
    });
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
      loadMarket();
    }
    updateTicket();
    $('#trade-order-error').textContent = 'AI 추천 방향과 가격을 주문창에 적용했습니다. 주문을 확인한 뒤 실행하세요.';
    // 휴대폰은 주문 화면으로 넘겨 채워진 값을 바로 보게 한다
    if (window.matchMedia('(max-width: 820px)').matches) setMobileView('order');
  }

  async function loadMarket() {
    if (state.view !== 'trading') return;
    // 차트는 트레이딩뷰 위젯 — 지금 종목으로 맞춘다 (이미 같은 종목이면 그대로 둔다)
    mountTradingView();
    $('#trade-coin').value = state.symbol.replace(/USDT$/, '');
    refreshTickers();
    refreshSubscriptions();
  }

  function refreshSubscriptions() {
    if (!state.feed || state.view !== 'trading') return;
    // 차트는 트레이딩뷰가 스스로 그리므로 캔들(kline)은 받지 않는다 — 호가·현재가만 구독한다
    const topics = new Set([`tickers.${state.symbol}`, `orderbook.50.${state.symbol}`]);
    for (const pos of LITE.paper.snapshot().open) {
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
    updateTicket();
  }

  function updateBookSide(side, levels) {
    for (const [price, quantity] of levels) {
      if (quantity === 0) side.delete(price);
      else side.set(price, quantity);
    }
  }

  // 호가 단 수는 호가창 높이에 맞춘다 — 휴대폰에서 28줄이 눌려 글자가 겹치지 않게
  function orderBookLevels() {
    const side = $('#trade-asks');
    const height = side ? side.clientHeight : 0;
    if (height < 40) return 14;
    return Math.max(4, Math.min(14, Math.floor(height / 18)));
  }

  function renderOrderBook() {
    const levels = orderBookLevels();
    // 매도 호가는 현재가에 가까운(낮은) 단부터 남기고 다시 높은 순으로 그린다
    const asks = [...state.book.asks.entries()].filter(([, quantity]) => quantity > 0)
      .sort((a, b) => a[0] - b[0]).slice(0, levels).reverse();
    const bids = [...state.book.bids.entries()].filter(([, quantity]) => quantity > 0)
      .sort((a, b) => b[0] - a[0]).slice(0, levels);
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
    }
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
    if ($('#trade-msv-count')) $('#trade-msv-count').textContent = snapshot.open.length;
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
      if (!positions.length && !pending) body.innerHTML = '<tr><td colspan="11" class="trade-empty">열린 포지션이 없습니다. 주문 화면에서 바로 모의 주문을 시작하세요.</td></tr>';
    } else {
      head.innerHTML = '<tr><th>체결 시각</th><th>계약</th><th>방향</th><th>진입가</th><th>청산가</th><th>레버리지</th><th>실현 PnL</th><th>수익률</th><th>결과</th></tr>';
      const trades = snapshot.trades;
      body.innerHTML = trades.map((trade) => {
        const at = new Date(trade.closedAt);
        return `<tr><td>${safe(Number.isNaN(at.getTime()) ? '—' : at.toLocaleString())}</td><td>${safe(trade.symbol.replace(/USDT$/, ''))}/USDT</td><td>${trade.side}</td><td>${priceText(trade.entry)}</td><td>${priceText(trade.exit)}</td><td>${trade.leverage}x</td><td class="${trade.pnl >= 0 ? 'up' : 'down'}">${signed(trade.pnl)} USDT</td><td class="${trade.roePct >= 0 ? 'up' : 'down'}">${signed(trade.roePct)}%</td><td>${safe(trade.reason)}</td></tr>`;
      }).join('') || '<tr><td colspan="9" class="trade-empty">아직 체결 내역이 없습니다.</td></tr>';
    }
    const table = $('.trade-table');
    if (table) table.classList.toggle('is-empty', !!body.querySelector('.trade-empty'));
  }

  function refreshPositions() {
    renderPositions();
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
    $('#trade-price').textContent = '—';
    $('#trade-change').textContent = '—';
    if (select && base) select.value = base;
    setMobileView('chart');
    setView('trading');
  }

  window.LiteTrading = { show: () => setView('trading'), showSymbol, office: () => setView('office'), screener: () => setView('screener'), whale: () => setView('whale'), dash: () => setView('dash'), loadMarket, renderPositions, setDecision };

  // 첫 화면 클래스·탭 상태를 정한다 (대시보드가 기본 — 소개 페이지 임베드는 사무실)
  setView(state.view);
})();
