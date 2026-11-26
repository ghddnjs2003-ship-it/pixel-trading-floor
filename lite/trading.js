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
  // 기본 첫 화면 — 트레이딩(시장 대시보드 화면). 다만 소개 페이지 속 임베드(?shell=1·?kiosk=1)는 종전대로 사무실이 먼저다.
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
  // 메인 탭 4개 — 시장 대시보드(기본) · 트레이딩 · 고래·일정 · 픽셀 오피스
  // (2026-11-26 홍원 님 "시장대시보드는 나두고 그옆에 데모트레이딩 버튼을 바꿔야지" — 두 번째 탭 이름만 '트레이딩'으로)
  nav.innerHTML = '<button type="button" data-view="dash">시장 대시보드</button><button type="button" data-view="trading">트레이딩</button><button type="button" data-view="whale" title="실시간 고래 입출금 · 이번 주 경제 일정">고래·일정</button><button type="button" data-view="office">픽셀 오피스</button>';
  topbar.insertBefore(nav, $('#controls'));
  nav.addEventListener('click', (event) => {
    const button = event.target.closest('[data-view]');
    if (!button) return;
    // '트레이딩' 탭을 누르면 그 탭의 첫 화면(홈)부터 — 데모 주문 화면은 홈의 '데모 모드로 체험'으로 들어간다
    if (button.dataset.view === 'trading') setTradeMode('home');
    setView(button.dataset.view);
  });

  const page = document.createElement('section');
  page.id = 'trade-page';
  page.setAttribute('aria-label', '트레이딩');
  page.dataset.mode = 'home';
  // 코인·타임프레임 버튼은 문자열을 미리 만들어 둔다 (템플릿 안에 템플릿을 겹치지 않게)
  const HOME_COINS = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE'];
  const HOME_FRAMES = [['15', '15m'], ['30', '30m'], ['60', '1H'], ['240', '4H']];
  const HOME_COIN_BUTTONS = HOME_COINS.map(function (coin) {
    var on = coin === 'BTC';
    return '<button type="button" data-coin="' + coin + '" class="' + (on ? 'active' : '') + '" aria-pressed="' + on + '">' + coin + '</button>';
  }).join('');
  const HOME_FRAME_BUTTONS = HOME_FRAMES.map(function (frame) {
    var on = frame[0] === '15';
    return '<button type="button" data-frame="' + frame[0] + '" class="' + (on ? 'active' : '') + '" aria-pressed="' + on + '">' + frame[1] + '</button>';
  }).join('');

  page.innerHTML = `
    <section class="trade-home" id="trade-home" aria-label="트레이딩 홈">
      <div class="th-statusbar">
        <span class="th-conn" id="th-conn" title="바이비트 실전 키는 아직 연결되지 않았습니다">미연결</span>
        <button type="button" class="th-small" id="th-guide">가이드</button>
        <span class="th-conn-note dim" id="th-conn-note">데모 기록 확인 중…</span>
        <button type="button" class="th-small th-live-guide" id="th-live-guide">실전 안내</button>
      </div>

      <section class="th-card th-verdict" aria-label="판정">
        <header class="th-head"><span class="th-num">1</span><h2>판정</h2></header>
        <p class="th-verdict-msg" id="th-verdict-msg">바이비트 연결 후 AI 최신 분석과 주문 칸이 열립니다.</p>
        <p class="th-verdict-line" id="th-verdict-line">최근 30분봉 AI 판정 · 기록을 확인하는 중…</p>
      </section>

      <section class="th-card th-auto" aria-label="오토 모드">
        <header class="th-head"><h2><span class="th-bolt">⚡</span> 오토 모드</h2><span class="th-sub" id="th-auto-sub">AI 오토 모드 상태</span></header>
        <div class="th-state" id="th-auto-state">○ 지금 열린 포지션 없음 (연결 대기)</div>
        <p class="th-hint">연결 후 실전·데모 포지션과 가격 정보가 열립니다.</p>
        <div class="th-auto-actions">
          <button type="button" class="th-connect" id="th-connect">바이비트 연결하기</button>
          <button type="button" class="th-demo" id="th-demo">데모 모드로 체험</button>
        </div>
      </section>

      <div class="th-coins-row">
        <span class="th-coin-badge" title="실전 리스크 기준 최대 3배">코인 3×</span>
        <div class="th-coins" id="th-coins" role="group" aria-label="코인 선택">${HOME_COIN_BUTTONS}</div>
      </div>
      <p class="th-chart-meta"><span id="th-tv-symbol">BYBIT:BTCUSDT.P</span> · <span>TradingView 실시간 차트</span></p>
      <div class="th-frames" id="th-frames" role="group" aria-label="타임프레임">${HOME_FRAME_BUTTONS}</div>
      <div class="th-chart">
        <div id="th-tv-chart"></div>
        <div id="th-tv-note" role="status">트레이딩뷰 차트를 불러오는 중…</div>
      </div>

      <section class="th-card th-signals" aria-label="최근 익절 · 청산 신호">
        <header class="th-head"><h2>최근 익절 · 청산 신호</h2></header>
        <p class="th-sig-sub">최근 30분봉 AI · 데모 실제 기록 기준</p>
        <p class="th-sig-note">% = 증거금 기준 · 거래 비용 반영</p>
        <ul class="th-sig-list" id="th-sig-list"><li class="th-sig-empty">기록을 불러오는 중…</li></ul>
        <p class="th-sig-foot" id="th-sig-foot">data/paper_state.json · data/paper_log.jsonl</p>
      </section>

      <div class="th-modal hidden" id="th-modal" role="dialog" aria-modal="true" aria-labelledby="th-modal-title">
        <div class="th-modal-box">
          <header><h2 id="th-modal-title">가이드</h2><button type="button" id="th-modal-close" aria-label="닫기">✕</button></header>
          <div class="th-modal-body" id="th-modal-body"></div>
          <footer>
            <button type="button" class="th-connect" id="th-modal-settings">⚙️ 연결 설정 열기</button>
            <button type="button" class="th-demo" id="th-modal-ok">닫기</button>
          </footer>
        </div>
      </div>
    </section>
    <header class="trade-marketbar">
      <button type="button" id="trade-home-back" class="trade-home-back" title="트레이딩 홈으로 (연결 전 화면)">‹ 홈</button>
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
  wireHome();

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
      // 트레이딩 홈 — 보이는 동안만 트레이딩뷰 위젯과 데모 기록(paper_state·paper_log)을 갱신한다
      if (home.mode === 'home') {
        requestAnimationFrame(mountHomeTv);
        loadHomeData();
      }
      if (home.timer) clearInterval(home.timer);
      home.timer = setInterval(() => {
        if (document.visibilityState !== 'hidden' && state.view === 'trading' && home.mode === 'home') loadHomeData();
      }, 180000);
    } else {
      if (state.loadTimer) clearInterval(state.loadTimer);
      if (state.markTimer) clearInterval(state.markTimer);
      state.loadTimer = 0;
      state.markTimer = 0;
      if (state.feed) state.feed.want([]);
      if (LITE.live && typeof LITE.live.refresh === 'function') LITE.live.refresh();
      if (home.timer) { clearInterval(home.timer); home.timer = 0; }
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

  /* ======================================================================
     트레이딩 홈 — 2026-11-26 시안(downloads/trading_tab_mockup_2026-11-26.png)
     · 상단 상태 바 → 1 판정 → ⚡ 오토 모드 → 코인/타임프레임 + 트레이딩뷰 실시간 차트
       → 최근 익절 · 청산 신호
     · 기록은 실제 파일(data/paper_state.json · data/paper_log.jsonl)만 읽는다.
       청산 건이 0건이면 보유 중인 모의 포지션과 최근 스캔을 그대로 적는다 — 지어낸 수익률은 넣지 않는다.
     · 기존 데모 조종석·수동 주문 화면은 '데모 모드로 체험'에서 같은 탭 안에 연다.
     ====================================================================== */
  const HOME_STATE_URL = 'data/paper_state.json';
  const HOME_LOG_URL = 'data/paper_log.jsonl';
  const HOME_REASON_KO = { TARGET: '익절', STOP: '손절', TIME: '시간 청산' };
  const HOME_ACTION_KO = { HOLD: '관망', OPEN: '진입', CLOSE: '청산', SKIP: '건너뜀', REJECT: '거절' };
  // 검증된 기록(2026-11-19 3.4년 88건 · 아웃샘플 PF 2.02) — 데모 신호가 아닌 '백테스트 참고'로만 표시한다
  const HOME_BACKTEST = '30m M1 아시아+AI · PF 2.02';
  const home = { mode: 'home', coin: 'BTC', frame: '15', widget: null, tvSymbol: '', tvInterval: '', paper: null, log: [], error: '', at: 0, seq: 0, timer: 0, noteTimer: 0 };

  const homeSymbol = (coin) => `BYBIT:${String(coin || 'BTC').toUpperCase()}USDT.P`;

  function homeAgeText(iso) {
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return '';
    const min = Math.max(0, (Date.now() - t) / 60000);
    if (min < 1) return '방금';
    if (min < 60) return `${Math.round(min)}분 전`;
    if (min < 1440) return `${Math.floor(min / 60)}시간 ${Math.round(min % 60)}분 전`;
    return `${Math.floor(min / 1440)}일 전`;
  }
  function homeKst(value) {
    const d = value instanceof Date ? value : new Date(value);
    if (Number.isNaN(d.getTime())) return { day: '—', clock: '—' };
    try {
      const parts = new Intl.DateTimeFormat('ko-KR', {
        timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
      }).formatToParts(d);
      const get = (type) => (parts.find((part) => part.type === type) || {}).value || '';
      return { day: `${get('month')}/${get('day')}`, clock: `${get('hour')}:${get('minute')}` };
    } catch (error) {
      const iso = d.toISOString();
      return { day: `${iso.slice(5, 7)}/${iso.slice(8, 10)}`, clock: iso.slice(11, 16) };
    }
  }
  // '2026-11-26 12:00'(KST 문자열)에서 날짜·시각만
  function homeBarKst(text) {
    if (typeof text !== 'string' || !/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(text)) return null;
    return { day: `${text.slice(5, 7)}/${text.slice(8, 10)}`, clock: text.slice(11, 16) };
  }
  const homeSideKo = (side) => (String(side || '').toUpperCase() === 'SHORT' ? '숏' : '롱');

  /* ---- 화면 모드: 홈(home) ↔ 데모 주문 화면(demo) — 같은 '트레이딩' 탭 안에서 바꾼다 ---- */
  function setTradeMode(mode) {
    const next = mode === 'demo' ? 'demo' : 'home';
    home.mode = next;
    if (page) page.dataset.mode = next;
    if (next === 'demo') {
      // 홈에서 고른 코인을 데모 주문 화면에도 그대로 넘긴다
      const symbol = `${home.coin}USDT`;
      if (state.symbol !== symbol) {
        state.symbol = symbol;
        state.price = null;
        state.change = null;
        state.book = { bids: new Map(), asks: new Map() };
        if ($('#trade-coin')) $('#trade-coin').value = home.coin;
        if ($('#trade-price')) $('#trade-price').textContent = '—';
        if ($('#trade-change')) $('#trade-change').textContent = '—';
        renderOrderBook();
      }
      loadMarket();
      if (state.mobileView === 'chart') requestAnimationFrame(mountTradingView);
    } else {
      requestAnimationFrame(mountHomeTv);
      loadHomeData();
    }
  }

  /* ---- 차트: 트레이딩뷰 공식 위젯 하나 (가짜 차트 금지) ---- */
  function showHomeTvNote(text) {
    const note = $('#th-tv-note');
    if (!note) return;
    note.textContent = text;
    note.classList.remove('hidden');
  }
  function hideHomeTvNote() {
    const note = $('#th-tv-note');
    if (note) note.classList.add('hidden');
  }
  function paintHomeSymbol() {
    const el = $('#th-tv-symbol');
    if (el) el.textContent = homeSymbol(home.coin);
  }
  function mountHomeTv() {
    if (home.mode !== 'home' || state.view !== 'trading') return;
    const host = $('#th-tv-chart');
    if (!host || !host.clientWidth || !host.clientHeight) return;
    const symbol = homeSymbol(home.coin);
    if (home.widget && home.tvSymbol === symbol && home.tvInterval === home.frame) return;
    home.tvSymbol = symbol;
    home.tvInterval = home.frame;
    showHomeTvNote('트레이딩뷰 차트를 불러오는 중…');
    loadTvScript().then(() => {
      if (home.mode !== 'home' || state.view !== 'trading') return;
      if (homeSymbol(home.coin) !== symbol || home.frame !== home.tvInterval) return;
      requestAnimationFrame(() => {
        if (!host.clientWidth || !host.clientHeight) return;
        host.innerHTML = '';
        try {
          home.widget = new window.TradingView.widget({
            container_id: 'th-tv-chart',
            symbol,
            interval: home.frame,
            theme: 'dark',
            style: '1',
            locale: 'kr',
            timezone: 'Asia/Seoul',
            toolbar_bg: '#10151e',
            backgroundColor: '#0b0e13',
            gridColor: 'rgba(38,49,67,.55)',
            autosize: true,
            hide_top_toolbar: false,
            hide_side_toolbar: window.innerWidth <= 768,
            allow_symbol_change: false,
            save_image: false,
            withdateranges: true,
            details: false,
            hotlist: false,
            enable_publishing: false,
            disabled_features: ['header_symbol_search', 'symbol_search_hot_key', 'header_compare', 'header_screenshot', 'header_fullscreen_button', 'header_undo_redo'],
          });
          const iframe = host.querySelector('iframe');
          if (iframe) iframe.addEventListener('load', hideHomeTvNote);
          if (home.noteTimer) clearTimeout(home.noteTimer);
          home.noteTimer = setTimeout(hideHomeTvNote, 8000);
        } catch (error) {
          showHomeTvNote('트레이딩뷰 위젯을 띄우지 못했어요. 새로고침 후 다시 시도해 주세요.');
        }
      });
    }).catch(() => {
      showHomeTvNote('트레이딩뷰 위젯을 불러오지 못했어요. 인터넷 연결이나 광고 차단 설정을 확인해 주세요.');
    });
  }

  /* ---- 기록: 실제 파일만 읽는다 ---- */
  async function loadHomeData() {
    if (home.mode !== 'home' || state.view !== 'trading') return;
    const seq = ++home.seq;
    const bust = `?t=${Date.now()}`;
    let paper = null;
    let logText = '';
    try {
      const res = await fetch(HOME_STATE_URL + bust, { cache: 'no-store' });
      if (res.ok) paper = await res.json();
    } catch (error) { paper = null; }
    try {
      const res = await fetch(HOME_LOG_URL + bust, { cache: 'no-store' });
      if (res.ok) logText = await res.text();
    } catch (error) { logText = ''; }
    if (seq !== home.seq) return;
    home.paper = paper && typeof paper === 'object' ? paper : null;
    home.error = home.paper ? '' : '데모 기록을 불러오지 못했습니다';
    home.log = logText.split('\n').filter((line) => line.trim())
      .map((line) => { try { return JSON.parse(line); } catch (error) { return null; } })
      .filter(Boolean).slice(-40);
    home.at = Date.now();
    paintHome();
  }

  function paintHome() {
    if (!page || home.mode !== 'home') return;
    const paper = home.paper;
    const pos = paper && paper.position ? paper.position : null;
    const closed = paper && Array.isArray(paper.closed) ? paper.closed : [];
    const last = home.log.length ? home.log[home.log.length - 1] : null;

    // 상태 바: 실전 미연결은 사실 그대로, 데모 기록이 있으면 언제 갱신됐는지 함께 적는다
    const note = $('#th-conn-note');
    if (note) {
      if (paper) {
        note.textContent = `데모 기록 ${homeAgeText(paper.updated_at) || '—'} 갱신`;
        note.classList.toggle('dim', !last);
      } else {
        note.textContent = home.error || '데모 기록 없음';
        note.classList.add('dim');
      }
    }

    // 1 판정 — 연결 전 안내 문구는 그대로 두고, 실제 30분봉 AI 판정 요약을 보조로 붙인다
    const line = $('#th-verdict-line');
    if (line) {
      const ai = pos && pos.ai ? pos.ai : null;
      const when = (pos && pos.last_bar_time) ? homeKst(Number(pos.last_bar_time)) : (last && homeBarKst(last.bar_kst)) || null;
      if (ai) {
        const loss = Number.isFinite(Number(ai.loss_prob)) ? ` · 손실확률 ${Number(ai.loss_prob).toFixed(0)}%` : '';
        line.innerHTML = `최근 30분봉 AI 판정 · <b>${safe(ai.decision || '—')}</b>${loss}<em>${when ? `${when.day} ${when.clock} 기준` : ''}</em>`;
      } else if (paper) {
        line.textContent = '최근 30분봉 AI 판정 · 보유 포지션 없음 (다음 스캔 대기)';
      } else {
        line.textContent = '최근 30분봉 AI 판정 · 기록을 불러오지 못했습니다';
      }
    }

    // 오토 모드 상태 상자 — 실제 모의 포지션이 있으면 그대로 적는다
    const box = $('#th-auto-state');
    if (box) {
      const localOpen = LITE.paper && typeof LITE.paper.snapshot === 'function' ? LITE.paper.snapshot().open : [];
      if (pos) {
        const when = homeKst(pos.entry_time);
        box.className = 'th-state on';
        box.textContent = `● 모의 ${homeSideKo(pos.side)} ${priceText(pos.entry)} 보유 · ${Number(pos.bars) || 0}봉 · ${when.day} ${when.clock} 진입`;
      } else if (localOpen && localOpen.length) {
        box.className = 'th-state on';
        box.textContent = `● 로컬 모의 ${localOpen.length}건 보유 (이 브라우저 주문)`;
      } else if (paper && closed.length) {
        const trade = closed[closed.length - 1];
        const r = Number(trade.r);
        box.className = 'th-state';
        box.textContent = `○ 지금 열린 포지션 없음 · 최근 ${HOME_REASON_KO[trade.reason] || '청산'} ${Number.isFinite(r) ? `${r > 0 ? '+' : ''}${r.toFixed(2)}R` : ''}`.trim();
      } else {
        box.className = 'th-state';
        box.textContent = '○ 지금 열린 포지션 없음 (연결 대기)';
      }
    }

    paintHomeSignals(paper, pos, closed, last);
  }

  function paintHomeSignals(paper, pos, closed, last) {
    const list = $('#th-sig-list');
    const foot = $('#th-sig-foot');
    if (!list) return;
    const rows = [];
    // 1) 실제 청산 기록 (최근 3건) — %는 위험금(증거금) 대비, 거래 비용 반영된 pnl 기준
    closed.slice(-3).reverse().forEach((trade) => {
      const when = homeKst(trade.exit_time);
      const r = Number(trade.r);
      const pnl = Number(trade.pnl);
      const pct = Number.isFinite(r) ? `${r > 0 ? '+' : r < 0 ? '−' : ''}${Math.abs(r * 100).toFixed(1)}%` : '—';
      const amount = Number.isFinite(pnl) ? `${pnl >= 0 ? '+' : '−'}${Math.abs(pnl).toFixed(2)} USDT` : '—';
      rows.push(`<li><span class="th-sig-when">${when.day} ${when.clock}</span><span class="th-sig-what"><b>${safe(String(trade.symbol || (paper && paper.symbol) || 'BTC').replace(/USDT$/, ''))} ${homeSideKo(trade.side)}</b> · ${safe(HOME_REASON_KO[trade.reason] || '청산')} <b class="${Number.isFinite(pnl) && pnl < 0 ? 'down' : 'up'}">${pct}</b> <em>(실현 ${amount})</em></span></li>`);
    });
    // 2) 지금 보유 중인 모의 포지션
    if (pos) {
      const when = homeKst(pos.entry_time);
      rows.push(`<li><span class="th-sig-when">${when.day} ${when.clock}</span><span class="th-sig-what"><b>${safe(String((paper && paper.symbol) || 'BTC').replace(/USDT$/, ''))} ${homeSideKo(pos.side)}</b> · 보유 중 <em>(진입 ${priceText(pos.entry)} · ${Number(pos.bars) || 0}봉)</em></span></li>`);
    }
    // 3) 최근 30분봉 스캔 (진입·관망·건너뜀)
    if (last && last.action) {
      const when = homeBarKst(last.bar_kst) || homeKst(last.bar || last.ts);
      const price = Number(last.price);
      rows.push(`<li><span class="th-sig-when">${when.day} ${when.clock}</span><span class="th-sig-what"><b>${safe(String((paper && paper.symbol) || 'BTC').replace(/USDT$/, ''))}</b> 스캔 · ${safe(HOME_ACTION_KO[last.action] || last.action)} <em>(${Number.isFinite(price) ? priceText(price) : '—'})</em></span></li>`);
    }
    // 4) 검증된 백테스트 참고 — 데모/실전 신호가 아니라고 이름에 적는다
    rows.push(`<li class="th-sig-ref"><span class="th-sig-when">백테스트</span><span class="th-sig-what">${safe(HOME_BACKTEST)} <em>(3.4년 88건 · 2026-11-19 검증)</em></span></li>`);
    if (!paper && !rows.length) rows.push('<li class="th-sig-empty">기록 없음 · 30분봉 스캔이 쌓이면 여기에 표시됩니다.</li>');
    list.innerHTML = rows.join('');
    if (foot) {
      const clock = home.at ? homeKst(new Date(home.at)).clock : '';
      foot.textContent = `기준: data/paper_state.json · data/paper_log.jsonl${clock ? ` · ${clock} 확인` : ''}`;
    }
  }

  /* ---- 가이드 · 실전 안내 창 ---- */
  const HOME_MODALS = {
    guide: {
      title: '바이비트 연결 · 이용 가이드',
      body: `
        <p class="th-modal-lead">지금은 <b>실전 키가 연결되지 않은 상태</b>입니다. 주문 버튼은 모두 모의(데모)로만 동작합니다.</p>
        <ol class="th-steps">
          <li><b>바이비트 API 키 만들기</b> — Bybit → API 관리 → 새 키. 권한은 <b>읽기</b>와 <b>선물 주문</b>만 켜고 출금은 켜지 않습니다.</li>
          <li><b>PC에 키 넣기</b> — <code>backend/.env</code>에 <code>BYBIT_API_KEY</code> · <code>BYBIT_API_SECRET</code>, 데모는 <code>BYBIT_DEMO=true</code>.</li>
          <li><b>모의 계좌로 먼저</b> — <code>python -m backend.testnet_auto --loop --demo</code> (Demo Trading 계좌 · 실제 주문 없음).</li>
          <li><b>이 화면에 반영</b> — 30분봉 기록이 <code>data/paper_state.json</code> · <code>data/paper_log.jsonl</code>에 쌓이면 '최근 익절 · 청산 신호'와 오토 모드 상태에 자동으로 나타납니다.</li>
        </ol>
        <p class="th-modal-foot">차트는 트레이딩뷰 실시간 시세(BYBIT 무기한)입니다. 시세·기록이 없으면 값을 지어내지 않고 '기록 없음'으로 적습니다.</p>`,
    },
    live: {
      title: '실전 안내 · 소액 원칙',
      body: `
        <p class="th-modal-lead">실전은 홍원 님이 정한 고정 규칙을 그대로 씁니다. 규칙은 화면에서 바꾸지 않습니다.</p>
        <ul class="th-rules">
          <li>시드 최소 <b>50 USDT</b></li>
          <li>1회 손실 <b>3%</b> (50달러 기준 1.5달러)</li>
          <li>최대 레버리지 <b>3배</b> · 동시 <b>1포지션</b></li>
          <li>30분봉 M1 · <b>아시아 세션(00~08 UTC)</b> · 목표 <b>4R</b></li>
          <li>진입은 Gemini AI 필터가 PASS일 때만</li>
        </ul>
        <p class="th-modal-foot">현재 상태: 실전 키 미연결 — 주문이 거래소로 전송되지 않습니다. 데모 모드로 먼저 확인하세요.</p>`,
    },
  };

  function openHomeModal(key) {
    const data = HOME_MODALS[key];
    if (!data) return;
    const modal = $('#th-modal');
    if (!modal) return;
    $('#th-modal-title').textContent = data.title;
    $('#th-modal-body').innerHTML = data.body;
    modal.dataset.key = key;
    modal.classList.remove('hidden');
    const close = $('#th-modal-close');
    if (close) close.focus();
  }
  function closeHomeModal() {
    const modal = $('#th-modal');
    if (modal) modal.classList.add('hidden');
  }

  // 가이드 · 실전 안내 · 연결 — 바이비트 연결은 ⚙️ 설정 창(연결 상태)을 그대로 연다
  function openConnectSettings() {
    if (window.LiteCockpit && typeof window.LiteCockpit.open === 'function') {
      window.LiteCockpit.open();
      return;
    }
    openHomeModal('guide');
  }

  function wireHome() {
    const coins = $('#th-coins');
    if (coins) {
      coins.addEventListener('click', (event) => {
        const button = event.target.closest('[data-coin]');
        if (!button || button.dataset.coin === home.coin) return;
        home.coin = button.dataset.coin;
        $$('#th-coins [data-coin]').forEach((el) => {
          const on = el.dataset.coin === home.coin;
          el.classList.toggle('active', on);
          el.setAttribute('aria-pressed', String(on));
        });
        paintHomeSymbol();
        mountHomeTv();
      });
    }
    const frames = $('#th-frames');
    if (frames) {
      frames.addEventListener('click', (event) => {
        const button = event.target.closest('[data-frame]');
        if (!button || button.dataset.frame === home.frame) return;
        home.frame = button.dataset.frame;
        $$('#th-frames [data-frame]').forEach((el) => {
          const on = el.dataset.frame === home.frame;
          el.classList.toggle('active', on);
          el.setAttribute('aria-pressed', String(on));
        });
        mountHomeTv();
      });
    }
    if ($('#th-guide')) $('#th-guide').addEventListener('click', () => openHomeModal('guide'));
    if ($('#th-live-guide')) $('#th-live-guide').addEventListener('click', () => openHomeModal('live'));
    if ($('#th-connect')) $('#th-connect').addEventListener('click', openConnectSettings);
    if ($('#th-demo')) $('#th-demo').addEventListener('click', () => setTradeMode('demo'));
    if ($('#trade-home-back')) $('#trade-home-back').addEventListener('click', () => setTradeMode('home'));
    if ($('#th-modal-close')) $('#th-modal-close').addEventListener('click', closeHomeModal);
    if ($('#th-modal-ok')) $('#th-modal-ok').addEventListener('click', closeHomeModal);
    if ($('#th-modal-settings')) $('#th-modal-settings').addEventListener('click', () => { closeHomeModal(); openConnectSettings(); });
    const modal = $('#th-modal');
    if (modal) modal.addEventListener('click', (event) => { if (event.target === modal) closeHomeModal(); });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && modal && !modal.classList.contains('hidden')) closeHomeModal();
    });
    window.addEventListener('resize', () => requestAnimationFrame(mountHomeTv));
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
    setTradeMode('demo');
    setView('trading');
  }

  // '데모 트레이딩 바로가기'·스크리너에서 오면 같은 트레이딩 탭의 데모 주문 화면을 바로 연다
  window.LiteTrading = { show: () => { setTradeMode('demo'); setView('trading'); }, showSymbol, office: () => setView('office'), screener: () => setView('screener'), whale: () => setView('whale'), dash: () => setView('dash'), loadMarket, renderPositions, setDecision, setTradeMode, openConnectSettings };

  // 첫 화면 클래스·탭 상태를 정한다 (대시보드가 기본 — 소개 페이지 임베드는 사무실)
  setView(state.view);
})();
