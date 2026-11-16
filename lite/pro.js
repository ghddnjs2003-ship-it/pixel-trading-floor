/* PIXEL TRADING FLOOR — 실전 기능 3종 (트레이딩 탭)
   1) 선물 수급 3대장 : 펀딩비 · 미결제약정(OI) · 롱/숏 비율 — Bybit V5 공개 API(키 불필요), 30초 갱신, 정산 카운트다운 1초 틱
   2) 손절 기준 수량 계산기 : 최대 손실액 + 손절가로 진입 수량·필요 증거금·손익비를 계산해 주문 증량칸(#trade-amount)에 넣는다
   3) 모의 매매 성적표 : LITE.paper 모의 계좌의 실제 체결·청산 내역으로 누적 수익 · 승률 · 평균 손익비 · MDD · 수익 곡선 · 복기 표를 집계
   실주문은 보내지 않는다(모의 계좌만). 값이 없으면 화면은 '—'로 두고 지어내지 않는다. */
(function () {
  'use strict';
  const LITE = window.LITE;
  if (!LITE || !LITE.paper) return;
  const $ = (sel, root = document) => (root || document).querySelector(sel);
  const $$ = (sel, root = document) => [...(root || document).querySelectorAll(sel)];
  const fmt = (n, digits = 2) => (Number.isFinite(Number(n))
    ? Number(n).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits })
    : '—');
  const num = (v) => (v === null || v === undefined || v === '' || !Number.isFinite(Number(v)) ? null : Number(v));
  const round2 = (n) => Math.round(Number(n) * 100) / 100;
  const sum = (list) => list.reduce((a, b) => a + b, 0);
  const DAY = 86400000;

  /* API 주소는 시세 모듈과 같은 곳을 쓴다 — 거래소를 바꿀 때 한 곳만 고치면 된다 */
  const BASE = (() => {
    const ticker = window.LiteLive && window.LiteLive.URL_TICKER;
    if (typeof ticker === 'string') return ticker.replace(/\/v5\/market\/tickers\/?$/, '');
    return 'https://api.bybit.com';
  })();

  async function getJSON(url) {
    const res = await fetch(url, { cache: 'no-store', credentials: 'omit' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    if (!body || body.retCode !== 0) throw new Error((body && body.retMsg) || '응답을 읽을 수 없습니다');
    return body.result || {};
  }

  /* ============================================================ 1. 선물 수급 3대장 */

  const flow = { seq: 0, symbol: '', at: 0, loading: false, error: '', data: null, nextReloadAt: 0 };

  function symbolNow() {
    const sel = $('#trade-coin');
    const base = sel && sel.value ? String(sel.value).toUpperCase() : 'BTC';
    return /USDT$/.test(base) ? base : `${base}USDT`;
  }

  function priceText(n) {
    const v = Number(n);
    if (!Number.isFinite(v)) return '—';
    return fmt(v, v < 0.01 ? 8 : v < 1 ? 5 : v < 100 ? 4 : 2);
  }

  function compactUsd(v) {
    if (!Number.isFinite(v)) return '—';
    if (Math.abs(v) >= 1e9) return `$${(v / 1e9).toFixed(2)}B`;
    if (Math.abs(v) >= 1e6) return `$${(v / 1e6).toFixed(2)}M`;
    if (Math.abs(v) >= 1e3) return `$${(v / 1e3).toFixed(1)}K`;
    return `$${fmt(v, 2)}`;
  }

  function sparkline(values, w, h) {
    const pts = (values || []).map(Number).filter(Number.isFinite);
    if (pts.length < 2) return '';
    const min = Math.min(...pts);
    const max = Math.max(...pts);
    const span = (max - min) || Math.abs(max) * 0.001 || 1;
    const points = pts.map((v, i) => {
      const x = (i / (pts.length - 1)) * (w - 2) + 1;
      const y = h - 2 - ((v - min) / span) * (h - 4);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    });
    const last = points[points.length - 1].split(',');
    const rise = pts[pts.length - 1] >= pts[0];
    const color = rise ? '#3fb98a' : '#d9687a';
    return `<svg class="pf-spark" viewBox="0 0 ${w} ${h}" aria-hidden="true">`
      + `<polyline points="${points.join(' ')}" fill="none" stroke="${color}" stroke-width="1.4" stroke-linejoin="round" />`
      + `<circle cx="${last[0]}" cy="${last[1]}" r="1.8" fill="${color}" /></svg>`;
  }

  function hms(ms) {
    const s = Math.max(0, Math.floor(ms / 1000));
    return `${String(Math.floor(s / 3600)).padStart(2, '0')}:${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  }

  function buildFlow() {
    if ($('#pro-flow')) return;
    const marketbar = $('.trade-marketbar');
    if (!marketbar || !marketbar.parentNode) return;
    const bar = document.createElement('section');
    bar.id = 'pro-flow';
    bar.setAttribute('aria-label', '선물 수급 — 펀딩비 · 미결제약정 · 롱숏 비율');
    bar.innerHTML =
      '<span class="pf-tag"><i></i><b>선물 수급</b>3대장</span>'
      + '<div class="pf-cell">'
        + '<span class="pf-lbl">펀딩비 <em id="pf-fund-int">8시간</em> <em id="pf-sym">BTCUSDT</em></span>'
        + '<span class="pf-val" id="pf-fund-val">—</span>'
        + '<span class="pf-sub pf-clock">다음 정산 <b class="pf-count" id="pf-count">--:--:--</b>'
        + '<span class="pf-cycle"><i id="pf-cycle-fill"></i></span></span>'
      + '</div>'
      + '<div class="pf-cell">'
        + '<span class="pf-lbl">미결제약정 OI <em>선물 전체</em></span>'
        + '<span class="pf-val" id="pf-oi-val">—</span>'
        + '<span class="pf-sub"><span id="pf-spark"></span><b id="pf-oi-chg">—</b> 24시간</span>'
      + '</div>'
      + '<div class="pf-cell">'
        + '<span class="pf-lbl">롱/숏 비율 <em>계정 기준</em></span>'
        + '<span class="pf-gauge"><b class="pf-long" id="pf-long">롱 —</b>'
        + '<span class="pf-bar"><i class="long" id="pf-bar-long" style="width:0%"></i><i class="short" id="pf-bar-short" style="width:0%"></i></span>'
        + '<b class="pf-short" id="pf-short">숏 —</b></span>'
        + '<span class="pf-sub"><span class="pf-ratio" id="pf-ratio">비율 —</span><span class="pf-hint" id="pf-hint"></span></span>'
      + '</div>'
      + '<span class="pf-note" id="pf-note">Bybit 선형 무기한 · 30초 갱신<b>모의 전용</b></span>';
    marketbar.parentNode.insertBefore(bar, marketbar.nextSibling);
    setInterval(tickFlow, 1000);
    loadFlow();
  }

  async function loadFlow() {
    if (flow.loading) return;
    const symbol = symbolNow();
    const seq = ++flow.seq;
    flow.loading = true;
    flow.symbol = symbol;
    const [tick, oi, ratio] = await Promise.allSettled([
      getJSON(`${BASE}/v5/market/tickers?category=linear&symbol=${symbol}`),
      getJSON(`${BASE}/v5/market/open-interest?category=linear&symbol=${symbol}&intervalTime=1h&limit=24`),
      getJSON(`${BASE}/v5/market/account-ratio?category=linear&symbol=${symbol}&period=1h&limit=1`),
    ]);
    if (seq !== flow.seq) { flow.loading = false; return; }
    const problems = [];
    const data = { symbol, at: Date.now() };

    /* 펀딩비 · 다음 정산 시각 · OI 규모 — tickers 한 번에 온다 */
    if (tick.status === 'fulfilled') {
      const row = (tick.value.list || [])[0];
      if (row) {
        const rate = num(row.fundingRate);
        data.funding = rate == null ? null : rate * 100;      // % 단위
        data.intervalHours = num(row.fundingIntervalHour) || 8;
        const next = num(row.nextFundingTime);
        data.nextFundingAt = next && next > 0 ? next : null;
        const last = num(row.lastPrice);
        data.price = last;
        const oiCoin = num(row.openInterest);
        const oiValue = num(row.openInterestValue);
        data.oiCoin = oiCoin;
        data.oiUsd = oiValue != null && oiValue > 0
          ? oiValue
          : (oiCoin != null && last ? oiCoin * last : null);
      }
    } else problems.push('tickers');

    /* OI 24시간 흐름 — 1시간 간격 24점(오래된 것부터) */
    if (oi.status === 'fulfilled') {
      const rows = (oi.value.list || []).map((row) => ({ value: num(row.openInterest), t: num(row.timestamp) }))
        .filter((row) => row.value != null && row.value > 0 && row.t != null)
        .sort((a, b) => a.t - b.t);
      if (rows.length >= 2) {
        data.oiSeries = rows.map((row) => row.value);
        data.oiChange24h = ((rows[rows.length - 1].value - rows[0].value) / rows[0].value) * 100;
        data.oiSeriesFrom = rows[0].t;
        data.oiSeriesTo = rows[rows.length - 1].t;
      } else if (rows.length === 1) {
        data.oiSeries = [rows[0].value];
      }
      if (data.oiUsd == null && rows.length && data.price) data.oiUsd = rows[rows.length - 1].value * data.price;
    } else problems.push('open-interest');

    /* 롱/숏 계정 비율 */
    if (ratio.status === 'fulfilled') {
      const row = (ratio.value.list || [])[0];
      if (row) {
        const buy = num(row.buyRatio);
        const sell = num(row.sellRatio);
        if (buy != null && sell != null) {
          data.longShare = buy;
          data.shortShare = sell;
          data.ratioAt = num(row.timestamp);
        }
      }
    } else problems.push('account-ratio');

    flow.data = data;
    flow.at = data.at;
    flow.loading = false;
    flow.error = problems.length === 3 ? `수급 데이터를 불러오지 못했습니다 (${problems.join(' · ')})` : '';
    const parts = [];
    if (data.funding == null) parts.push('펀딩');
    if (data.oiUsd == null || data.oiChange24h == null) parts.push('OI');
    if (data.longShare == null) parts.push('롱숏');
    flow.partial = parts;
    renderFlow();
  }

  function renderFlow() {
    const bar = $('#pro-flow');
    const data = flow.data;
    if (!bar || !data) return;

    $('#pf-sym').textContent = data.symbol;
    $('#pf-fund-int').textContent = `${data.intervalHours || 8}시간`;

    /* 펀딩비 */
    const fund = $('#pf-fund-val');
    if (data.funding == null) {
      fund.textContent = '—';
      fund.className = 'pf-val';
    } else {
      fund.textContent = `${data.funding > 0 ? '+' : data.funding < 0 ? '−' : ''}${fmt(Math.abs(data.funding), 4)}%`;
      fund.className = `pf-val ${data.funding > 0 ? 'up' : data.funding < 0 ? 'down' : ''}`;
    }
    tickFlow(true);

    /* 미결제약정 */
    const oiVal = $('#pf-oi-val');
    if (data.oiUsd == null) {
      oiVal.textContent = '—';
    } else {
      const coin = data.symbol.replace(/USDT$/, '');
      oiVal.innerHTML = `${compactUsd(data.oiUsd)}${data.oiCoin ? ` <small>약 ${fmt(data.oiCoin, 0)} ${coin}</small>` : ''}`;
    }
    $('#pf-spark').innerHTML = data.oiSeries && data.oiSeries.length > 1 ? sparkline(data.oiSeries, 62, 16) : '';
    const chg = $('#pf-oi-chg');
    if (data.oiChange24h == null) {
      chg.textContent = '—';
      chg.className = '';
    } else {
      chg.textContent = `${data.oiChange24h > 0 ? '+' : data.oiChange24h < 0 ? '−' : ''}${fmt(Math.abs(data.oiChange24h), 1)}%`;
      chg.className = data.oiChange24h > 0 ? 'up' : data.oiChange24h < 0 ? 'down' : '';
    }

    /* 롱/숏 비율 */
    const longShare = data.longShare;
    if (longShare == null) {
      $('#pf-long').textContent = '롱 —';
      $('#pf-short').textContent = '숏 —';
      $('#pf-bar-long').style.width = '0%';
      $('#pf-bar-short').style.width = '0%';
      $('#pf-ratio').textContent = '비율 —';
      $('#pf-hint').textContent = '';
    } else {
      const share = Math.max(0, Math.min(1, longShare));
      $('#pf-long').textContent = `롱 ${(share * 100).toFixed(1)}%`;
      $('#pf-short').textContent = `숏 ${((1 - share) * 100).toFixed(1)}%`;
      $('#pf-bar-long').style.width = `${share * 100}%`;
      $('#pf-bar-short').style.width = `${(1 - share) * 100}%`;
      $('#pf-ratio').textContent = `비율 ${share > 0 ? (share / (1 - share)).toFixed(2) : '—'}`;
      $('#pf-hint').textContent = share >= 0.55 ? '롱 우위 — 되돌림 주의'
        : share <= 0.45 ? '숏 우위 — 숏스퀴즈 주의'
          : '균형 구간';
    }

    /* 상태 줄 */
    const note = $('#pf-note');
    if (flow.error) {
      note.className = 'pf-note is-error';
      note.innerHTML = `${flow.error}<b>30초 후 재시도</b>`;
    } else {
      note.className = 'pf-note';
      const left = flow.partial && flow.partial.length ? ` · ${flow.partial.join('/')} 지연` : '';
      note.innerHTML = `Bybit 선형 무기한 · 30초 갱신${left}<b>모의 전용</b>`;
    }
  }

  let lastCount = 0;
  function tickFlow(forceRender) {
    if (!forceRender && !document.documentElement.classList.contains('trading-view')) return;
    const now = Date.now();
    const data = flow.data;

    /* 정산 카운트다운 — 매초 */
    if (data && data.nextFundingAt) {
      const left = data.nextFundingAt - now;
      const count = $('#pf-count');
      if (count) count.textContent = hms(left);
      const fill = $('#pf-cycle-fill');
      const cycle = (data.intervalHours || 8) * 3600000;
      if (fill) {
        const pct = Math.min(100, Math.max(0, ((cycle - left) / cycle) * 100));
        fill.style.width = `${pct.toFixed(2)}%`;
      }
      /* 정산 시각이 지나면 한 번 새로 받아 다음 주기로 넘긴다 */
      if (!forceRender && left <= -2000 && now > flow.nextReloadAt) {
        flow.nextReloadAt = now + 60000;
        loadFlow();
      }
    }

    if (forceRender) return;
    if (document.visibilityState === 'hidden') return;
    /* 종목이 바뀌었거나(스크리너에서 넘어온 경우 포함) 30초가 지났으면 새로 받는다 */
    if (flow.symbol !== symbolNow() || now - flow.at >= 30000) {
      if (now - lastCount > 3000) {
        lastCount = now;
        loadFlow();
      }
    }
  }

  /* ============================================================ 2. 손절 기준 수량 계산기 */

  const risk = { mode: 'risk' };

  function buildRisk() {
    const ticket = $('.trade-ticket');
    const fields = $('.trade-risk-fields');
    if (!ticket || !fields || $('.pro-risk')) return;
    const sl = $('#trade-sl');
    const tp = $('#trade-tp');
    const lev = $('#trade-leverage');
    const amount = $('#trade-amount');
    const percent = $('#trade-percent');
    if (!sl || !tp || !lev || !amount) return;

    const mark = document.createElement('div');
    mark.className = 'pro-risk';
    mark.innerHTML =
      '<div class="pr-head"><b>수량 계산</b>'
        + '<div class="pr-modes"><button type="button" data-pr-mode="risk" class="active">손실 한도</button>'
        + '<button type="button" data-pr-mode="manual">증거금 직접</button></div></div>'
      + '<div class="pr-body">'
        + '<div class="pr-loss"><span>최대 손실</span>'
          + '<div class="pr-input"><input id="pro-loss" type="number" min="1" step="1" value="100" inputmode="decimal" aria-label="최대 손실 금액(USDT)"><em>USDT</em></div></div>'
        + '<div class="pr-chips" id="pro-chips">'
          + '<button type="button" data-pct="0.5">0.5%</button>'
          + '<button type="button" data-pct="1" class="active">1%</button>'
          + '<button type="button" data-pct="2">2%</button>'
          + '<span id="pro-seed">잔고 —</span></div>'
        + '<div class="pr-result">'
          + '<div class="pr-big"><span>진입 수량</span><b id="pr-qty">—<small>BTC</small></b></div>'
          + '<div class="pr-line"><span>필요 증거금</span><b id="pr-margin">—</b></div>'
          + '<div class="pr-line pr-rr"><span>손익비</span><b id="pr-rr">—</b></div>'
          + '<div class="pr-mini"><i id="pr-loss-amt">—</i><i id="pr-profit-amt">—</i></div>'
          + '<div class="pr-hint" id="pr-hint"></div>'
        + '</div>'
      + '</div>';
    fields.parentNode.insertBefore(mark, fields.nextSibling);

    const amountLabel = amount.closest('label');
    const summary = [...($('.trade-order-summary') || { children: [] }).children];
    const tpLabel = tp.closest('label') && tp.closest('label').querySelector('span');
    const slLabel = sl.closest('label') && sl.closest('label').querySelector('span');
    const originTp = tpLabel ? tpLabel.textContent : '';
    const originSl = slLabel ? slLabel.textContent : '';
    const coin = () => symbolNow().replace(/USDT$/, '');

    function entryPrice() {
      const active = $('.trade-order-types .active');
      if (active && active.dataset.orderType === 'limit') {
        const v = Number($('#trade-limit-price').value);
        if (Number.isFinite(v) && v > 0) return v;
      }
      const text = ($('#trade-price') || {}).textContent || '';
      const v = Number(String(text).replace(/[^0-9.]/g, ''));
      return Number.isFinite(v) && v > 0 ? v : 0;
    }

    function side() {
      const selected = $('#trade-side-select .selected');
      return selected && selected.dataset.selectSide === 'SHORT' ? 'SHORT' : 'LONG';
    }

    function setAmount(margin) {
      const next = margin > 0 ? margin.toFixed(2) : '';
      if (amount.value === next) return;
      amount.value = next;
      amount.dispatchEvent(new Event('input', { bubbles: true }));
    }

    function calc() {
      const snap = LITE.paper.snapshot();
      const account = snap.account || {};
      const balance = Number(account.balance) || 0;
      const available = Number(account.availableBalance) || 0;

      /* 시드 비율 칩 — 지금 모의 계좌 잔고 기준 금액 */
      $$('#pro-chips [data-pct]').forEach((button) => {
        const pct = Number(button.dataset.pct);
        button.textContent = `${pct}% · $${fmt(round2(balance * pct / 100), 0)}`;
        button.title = `모의 계좌 잔고 $${fmt(balance, 2)}의 ${pct}%`;
      });
      const seed = $('#pro-seed');
      if (seed) seed.textContent = `잔고 $${fmt(balance, 0)}`;

      const entry = entryPrice();
      const slValue = Number(sl.value);
      const tpValue = Number(tp.value);
      const leverage = Number(lev.value) || 1;
      const maxLoss = Number($('#pro-loss').value) || 0;
      const dir = side();
      const dist = dir === 'LONG' ? entry - slValue : slValue - entry;
      const slSet = sl.value !== '' && Number.isFinite(slValue) && slValue > 0;
      const tpSet = tp.value !== '' && Number.isFinite(tpValue) && tpValue > 0;
      const ok = entry > 0 && slSet && dist > 0 && leverage >= 1 && maxLoss > 0;
      const qty = ok ? maxLoss / dist : 0;
      const margin = ok ? (qty * entry) / leverage : 0;
      const reward = tpSet ? (dir === 'LONG' ? tpValue - entry : entry - tpValue) : 0;
      const rr = ok && reward > 0 ? reward / dist : 0;
      const profit = ok && reward > 0 ? qty * reward : 0;
      const digits = qty >= 1000 ? 2 : qty >= 1 ? 4 : qty >= 0.001 ? 6 : 8;

      $('#pr-qty').innerHTML = ok && qty > 0 ? `${fmt(qty, digits)}<small>${coin()}</small>` : `—<small>${coin()}</small>`;
      $('#pr-margin').textContent = ok && margin > 0 ? `$${fmt(margin, 0)} (${leverage}x)` : '—';
      const rrEl = $('#pr-rr');
      rrEl.textContent = rr > 0 ? `1 : ${rr.toFixed(2)}` : '—';
      rrEl.className = rr >= 2 ? 'good' : '';
      $('#pr-loss-amt').innerHTML = ok ? `손절 시 <b style="color:#f2687b">−$${fmt(maxLoss, 0)}</b>` : '';
      $('#pr-profit-amt').innerHTML = profit > 0 ? `목표 시 <b style="color:#2fd39a">+$${fmt(profit, 0)}</b>` : '';

      const hint = $('#pr-hint');
      hint.className = 'pr-hint';
      if (!entry) {
        hint.textContent = 'Bybit 현재가를 기다리는 중입니다';
      } else if (!slSet) {
        hint.textContent = '손절가 SL을 넣으면 진입 수량을 계산합니다';
      } else if (dist <= 0) {
        hint.className = 'pr-hint is-warn';
        hint.textContent = `${dir === 'LONG' ? '롱' : '숏'} 손절가는 진입가 ${dir === 'LONG' ? '아래' : '위'}로 넣으세요`;
      } else if (maxLoss <= 0) {
        hint.className = 'pr-hint is-warn';
        hint.textContent = '최대 손실 금액을 입력하세요';
      } else if (margin > 0 && margin > available) {
        hint.className = 'pr-hint is-warn';
        hint.innerHTML = `필요 증거금 <b>$${fmt(margin, 0)}</b> > 주문 가능 <b>$${fmt(available, 0)}</b> — 손실 한도를 줄이세요`;
      } else if (!tpSet) {
        hint.innerHTML = `목표가를 넣으면 손익비를 계산합니다 · 수량 <b>${fmt(qty, digits)} ${coin()}</b>`;
      } else if (reward <= 0) {
        hint.className = 'pr-hint is-warn';
        hint.textContent = `${dir === 'LONG' ? '롱' : '숏'} 목표가는 진입가 ${dir === 'LONG' ? '위' : '아래'}로 넣으세요`;
      } else if (rr >= 2) {
        hint.innerHTML = `계획대로면 1:${rr.toFixed(1)} — 그대로 두세요`;
      } else {
        const need = entry + (dir === 'LONG' ? 1 : -1) * dist * 2;
        hint.innerHTML = `1:2 맞추려면 목표가 <b>${priceText(need)}</b> ${dir === 'LONG' ? '이상' : '이하'}`;
      }

      if (risk.mode === 'risk' && ok && margin >= 0.01) setAmount(margin);
    }

    function setMode(mode) {
      risk.mode = mode === 'manual' ? 'manual' : 'risk';
      const manual = risk.mode === 'manual';
      mark.classList.toggle('pr-manual', manual);
      ticket.classList.toggle('pro-mode', !manual);
      $$('[data-pr-mode]', mark).forEach((button) => button.classList.toggle('active', button.dataset.prMode === risk.mode));
      [amountLabel, percent].forEach((el) => { if (el) el.classList.toggle('pro-hide', !manual); });
      summary.slice(0, 4).forEach((el) => el.classList.toggle('pro-hide', !manual));
      if (tpLabel) tpLabel.textContent = manual ? originTp : '목표가 TP';
      if (slLabel) slLabel.textContent = manual ? originSl : '손절가 SL';
      if (!manual) calc();
    }

    $('#pro-loss').addEventListener('input', () => {
      $$('#pro-chips [data-pct]').forEach((button) => button.classList.remove('active'));
      calc();
    });
    $('#pro-chips').addEventListener('click', (event) => {
      const button = event.target.closest('[data-pct]');
      if (!button) return;
      const snap = LITE.paper.snapshot();
      const balance = Number(snap.account && snap.account.balance) || 0;
      $$('#pro-chips [data-pct]').forEach((x) => x.classList.toggle('active', x === button));
      $('#pro-loss').value = String(Math.round(round2(balance * Number(button.dataset.pct) / 100)));
      calc();
    });
    $$('[data-pr-mode]', mark).forEach((button) => button.addEventListener('click', () => setMode(button.dataset.prMode)));
    [sl, tp, lev, $('#trade-limit-price')].forEach((el) => {
      if (el) ['input', 'change'].forEach((evt) => el.addEventListener(evt, calc));
    });
    $$('.trade-order-types button').forEach((button) => button.addEventListener('click', () => setTimeout(calc, 30)));
    $$('[data-select-side]').forEach((button) => button.addEventListener('click', () => setTimeout(calc, 30)));
    $('#trade-coin').addEventListener('change', () => setTimeout(calc, 60));
    setInterval(() => {
      if (!document.documentElement.classList.contains('trading-view')) return;
      if (document.visibilityState === 'hidden') return;
      calc();
    }, 1200);

    /* 처음 최대 손실 = 잔고 1% */
    const initial = LITE.paper.snapshot();
    const startBalance = Number(initial.account && initial.account.balance) || 0;
    if (startBalance > 0) $('#pro-loss').value = String(Math.max(1, Math.round(round2(startBalance / 100))));
    setMode('risk');
  }

  /* ============================================================ 3. 모의 매매 성적표 */

  const stats = { period: '30', timer: 0, built: false };

  function periodSince(period) {
    if (period === 'today') {
      const d = new Date();
      d.setHours(0, 0, 0, 0);
      return d.getTime();
    }
    if (period === '7') return Date.now() - 7 * DAY;
    if (period === '30') return Date.now() - 30 * DAY;
    return 0;
  }

  const PERIOD_LABEL = { today: '오늘', 7: '7일', 30: '30일', all: '전체' };

  function aggregate(period) {
    const snap = LITE.paper.snapshot();
    const account = snap.account || {};
    const all = (snap.trades || [])
      .filter((trade) => Number.isFinite(Date.parse(trade.closedAt)))
      .sort((a, b) => Date.parse(a.closedAt) - Date.parse(b.closedAt));
    const since = periodSince(period);
    const list = since ? all.filter((trade) => Date.parse(trade.closedAt) >= since) : all;
    const totalClosed = Number(account.closedCount) || all.length;

    const wins = list.filter((trade) => trade.pnl > 0);
    const losses = list.filter((trade) => !(trade.pnl > 0));
    const pnl = round2(sum(list.map((trade) => Number(trade.pnl) || 0)));
    const startCap = list.length
      ? round2(Number(list[0].balanceAfter) - Number(list[0].pnl))
      : round2(Number(account.balance) - (Number(account.pnl) || 0));
    const endCap = list.length ? round2(Number(list[list.length - 1].balanceAfter)) : round2(account.balance);
    const returnPct = startCap > 0 ? (pnl / startCap) * 100 : 0;
    const winSum = sum(wins.map((trade) => Number(trade.pnl) || 0));
    const lossSum = Math.abs(sum(losses.map((trade) => Number(trade.pnl) || 0)));
    const avgWin = wins.length ? winSum / wins.length : 0;
    const avgLoss = losses.length ? lossSum / losses.length : 0;
    const rr = avgWin > 0 && avgLoss > 0 ? avgWin / avgLoss : null;
    const winRate = list.length ? (wins.length / list.length) * 100 : null;

    /* 수익 곡선 — '전체'는 모의 계좌가 저장한 실제 곡선, 기간은 체결 잔고로 잇는다 */
    let curve = [];
    if (period === 'all' && Array.isArray(account.curve) && account.curve.length > 1) {
      curve = account.curve
        .map((point) => ({ t: Number(point.t), b: Number(point.b) }))
        .filter((point) => Number.isFinite(point.t) && Number.isFinite(point.b));
    } else if (list.length) {
      curve = [{ t: Date.parse(list[0].openedAt) || Date.parse(list[0].closedAt), b: startCap }]
        .concat(list.map((trade) => ({ t: Date.parse(trade.closedAt), b: Number(trade.balanceAfter) })))
        .filter((point) => Number.isFinite(point.t) && Number.isFinite(point.b));
    }

    let peak = -Infinity;
    let mdd = 0;
    let mddPeak = null;
    let mddTrough = null;
    curve.forEach((point) => {
      if (point.b > peak) peak = point.b;
      if (peak > 0) {
        const dd = ((point.b - peak) / peak) * 100;
        if (dd < mdd) {
          mdd = dd;
          mddPeak = peak;
          mddTrough = point.b;
        }
      }
    });

    let streak = 0;
    let maxStreak = 0;
    list.forEach((trade) => {
      if (trade.pnl > 0) streak = 0;
      else {
        streak += 1;
        if (streak > maxStreak) maxStreak = streak;
      }
    });

    const holdAvg = list.length ? sum(list.map((trade) => Number(trade.holdMin) || 0)) / list.length : 0;
    const base = period === 'all' && curve.length ? curve[0].b : startCap;

    return {
      period, list, all, totalClosed, wins: wins.length, losses: losses.length, pnl, startCap, endCap,
      returnPct, avgWin, avgLoss, rr, winRate, curve, mdd, mddPeak, mddTrough, maxStreak, holdAvg, base,
    };
  }

  function openStats() {
    const modal = $('#pro-modal');
    if (!modal) return;
    modal.classList.remove('hidden');
    renderStats();
    const close = modal.querySelector('.pm-close');
    if (close) close.focus();
    if (stats.timer) clearInterval(stats.timer);
    stats.timer = setInterval(() => {
      if (modal.classList.contains('hidden')) return;
      if (document.visibilityState === 'hidden') return;
      renderStats();
    }, 3000);
  }

  function closeStats() {
    const modal = $('#pro-modal');
    if (!modal) return;
    modal.classList.add('hidden');
    if (stats.timer) { clearInterval(stats.timer); stats.timer = 0; }
    const button = $('.pro-stats-btn');
    if (button) button.focus();
  }

  function statsIsOpen() {
    const modal = $('#pro-modal');
    return !!modal && !modal.classList.contains('hidden');
  }

  function buildStats() {
    const live = $('.trade-live');
    if (!live || !live.parentNode || $('.pro-stats-btn')) return;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'pro-stats-btn';
    button.textContent = '📊 매매 성적';
    button.setAttribute('aria-haspopup', 'dialog');
    live.parentNode.insertBefore(button, live);

    const modal = document.createElement('div');
    modal.id = 'pro-modal';
    modal.className = 'hidden';
    modal.innerHTML =
      '<div class="pm-backdrop" data-pm-close></div>'
      + '<div class="pm-panel" role="dialog" aria-modal="true" aria-label="모의 매매 성적표">'
        + '<header class="pm-head">'
          + '<div class="pm-title">◆ 모의 매매 성적표 <span class="pm-acc" id="pm-acc">모의 계좌</span></div>'
          + '<div class="pm-period" id="pm-period">'
            + Object.keys(PERIOD_LABEL).map((key) => `<button type="button" data-period="${key}"${key === stats.period ? ' class="active"' : ''}>${PERIOD_LABEL[key]}</button>`).join('')
          + '</div>'
          + '<button type="button" class="pm-close" data-pm-close aria-label="닫기">✕</button>'
        + '</header>'
        + '<div class="pm-cards" id="pm-cards"></div>'
        + '<section class="pm-curve">'
          + '<div class="pm-curve-head"><b>계좌 수익 곡선</b><span id="pm-curve-sub">—</span>'
            + '<span class="pm-legend"><span class="pm-lg-eq"><i></i>평가 자산</span><span class="pm-lg-base"><i></i>시작 자본</span></span></div>'
          + '<div class="pm-canvas-wrap"><canvas id="pm-canvas" aria-label="계좌 수익 곡선"></canvas>'
            + '<div class="pm-empty-curve hidden" id="pm-empty-curve"></div></div>'
        + '</section>'
        + '<section class="pm-trades">'
          + '<div class="pm-trades-head">최근 거래 복기 <span id="pm-trades-sub"></span><em id="pm-trades-note"></em></div>'
          + '<table class="pm-table"><thead><tr>'
            + '<th>시간</th><th>종목</th><th>방향</th><th>진입 → 청산</th><th>레버리지</th><th>손익(USDT)</th><th>ROE</th><th>청산 사유</th>'
          + '</tr></thead><tbody id="pm-rows"></tbody></table>'
        + '</section>'
        + '<div class="pm-foot" id="pm-foot"></div>'
      + '</div>';
    document.body.appendChild(modal);
    stats.built = true;

    button.addEventListener('click', () => {
      if (statsIsOpen()) closeStats();
      else openStats();
    });
    $$('[data-pm-close]', modal).forEach((el) => el.addEventListener('click', closeStats));
    $('#pm-period', modal).addEventListener('click', (event) => {
      const target = event.target.closest('[data-period]');
      if (!target) return;
      stats.period = target.dataset.period;
      $$('#pm-period button', modal).forEach((el) => el.classList.toggle('active', el === target));
      renderStats();
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && statsIsOpen()) closeStats();
    });
    window.addEventListener('resize', () => { if (statsIsOpen()) drawCurve(); });
    LITE.bus.subscribe((event) => {
      if (event && event.type === 'position' && statsIsOpen()) renderStats();
    });
  }

  function reasonChip(reason) {
    const text = String(reason || '청산');
    if (text === '익절') return `<span class="pm-res tp">익절</span>`;
    if (text === '손절' || text === '강제 청산') return `<span class="pm-res sl">${text}</span>`;
    return `<span class="pm-res mid">${text}</span>`;
  }

  function renderStats() {
    if (!stats.built) return;
    const data = aggregate(stats.period);
    const account = LITE.paper.snapshot().account || {};

    /* 기간 요약 */
    const acc = $('#pm-acc');
    if (acc) acc.textContent = `${symbolNow()} 선물 · 모의`;

    const cards = $('#pm-cards');
    const hasTrades = data.list.length > 0;
    const profitDir = data.pnl > 0 ? 'up' : data.pnl < 0 ? 'down' : '';
    cards.innerHTML =
      '<div class="pm-card"><span class="pm-card-lbl">누적 수익<i>' + PERIOD_LABEL[stats.period] + ' · ' + data.list.length + '건</i></span>'
        + `<b class="pm-card-val ${profitDir}">${hasTrades ? `${data.pnl > 0 ? '+' : data.pnl < 0 ? '−' : ''}$${fmt(Math.abs(data.pnl), 0)}<small>(${data.returnPct > 0 ? '+' : data.returnPct < 0 ? '−' : ''}${fmt(Math.abs(data.returnPct), 1)}%)</small>` : '—'}</b>`
        + `<span class="pm-card-sub">시작 <b>$${fmt(data.startCap, 0)}</b> → 현재 <b>$${fmt(data.endCap, 0)}</b></span></div>`
      + '<div class="pm-card"><span class="pm-card-lbl">승률<i>' + (hasTrades ? `${data.wins}승 ${data.losses}패` : '기록 없음') + '</i></span>'
        + `<b class="pm-card-val">${data.winRate == null ? '—' : `${fmt(data.winRate, 1)}%`}<small>${hasTrades ? `${data.list.length}건` : ''}</small></b>`
        + (hasTrades
          ? `<span class="pm-mini-bar"><i class="pm-bar-w" style="width:${(data.wins / data.list.length) * 100}%"></i><i class="pm-bar-l" style="width:${(data.losses / data.list.length) * 100}%"></i></span>`
          : '<span class="pm-card-sub">청산된 거래가 쌓이면 표시됩니다</span>') + '</div>'
      + '<div class="pm-card"><span class="pm-card-lbl">평균 손익비<i>이익÷손실</i></span>'
        + `<b class="pm-card-val">${data.rr == null ? '—' : `1 : ${data.rr.toFixed(2)}`}</b>`
        + `<span class="pm-card-sub">${data.rr == null && hasTrades && !data.losses ? '손실 0건 — 손익비 계산 불가' : `평균 이익 <b>$${fmt(data.avgWin, 0)}</b> · 평균 손실 <b>$${fmt(data.avgLoss, 0)}</b>`}</span></div>`
      + '<div class="pm-card"><span class="pm-card-lbl">최대 낙폭 MDD<i>고점 대비</i></span>'
        + `<b class="pm-card-val ${data.mdd < 0 ? 'down' : ''}">${data.mdd < 0 ? `${fmt(data.mdd, 1)}%` : (data.curve.length > 1 ? '0.0%' : '—')}</b>`
        + `<span class="pm-card-sub">${data.mddPeak == null ? '낙폭 구간 없음' : `고점 <b>$${fmt(data.mddPeak, 0)}</b> → 저점 <b>$${fmt(data.mddTrough, 0)}</b>`}</span></div>`;

    /* 수익 곡선 */
    const sub = $('#pm-curve-sub');
    if (sub) sub.textContent = `${fmt(data.startCap, 0)} → ${fmt(data.endCap, 0)} USDT · ${PERIOD_LABEL[stats.period]}`;
    const empty = $('#pm-empty-curve');
    if (empty) {
      const none = data.curve.length < 2;
      empty.classList.toggle('hidden', !none);
      empty.innerHTML = none
        ? (data.list.length ? '곡선을 그릴 만큼의 청산 기록이 아직 없습니다' : '아직 청산된 모의 거래가 없습니다<br>트레이딩 탭에서 주문해 보고, TP·SL이나 시장가 청산으로 거래가 끝나면 여기에 실제 곡선이 그려집니다')
        : '';
    }
    drawCurve(data);

    /* 복기 표 */
    const rows = data.list.slice(-8).reverse();
    const tbody = $('#pm-rows');
    const tradesSub = $('#pm-trades-sub');
    if (tradesSub) tradesSub.textContent = rows.length ? `최근 ${rows.length}건 / ${PERIOD_LABEL[stats.period]} ${data.list.length}건` : `${PERIOD_LABEL[stats.period]} 기록 없음`;
    const note = $('#pm-trades-note');
    if (note) {
      const hidden = data.totalClosed > data.all.length;
      const hold = data.holdAvg
        ? `${Math.floor(data.holdAvg / 60)}시간 ${Math.round(data.holdAvg % 60)}분`
        : (data.list.length ? '1분 미만' : '');
      note.textContent = hidden
        ? `최근 ${data.all.length}건만 표시 (전체 ${data.totalClosed}건)`
        : (data.list.length ? `평균 보유 ${hold}` : '');
    }
    const table = $('.pm-table');
    if (table) table.classList.toggle('is-empty', !rows.length);
    tbody.innerHTML = rows.length
      ? rows.map((trade) => {
        const at = new Date(trade.closedAt);
        const when = Number.isNaN(at.getTime()) ? '—'
          : `${String(at.getMonth() + 1).padStart(2, '0')}-${String(at.getDate()).padStart(2, '0')} ${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`;
        const up = trade.pnl > 0;
        const cls = up ? 'up' : 'down';
        const roe = Number(trade.roePct);
        return `<tr>
          <td>${when}</td>
          <td>${trade.symbol ? String(trade.symbol).toUpperCase() : '—'}</td>
          <td><span class="pm-side ${trade.side === 'SHORT' ? 'short' : 'long'}">${trade.side || '—'}</span></td>
          <td>${priceText(trade.entry)} → ${priceText(trade.exit)}</td>
          <td>${Number(trade.leverage) || '—'}x</td>
          <td class="${cls}">${up ? '+' : '−'}$${fmt(Math.abs(Number(trade.pnl) || 0), 2)}</td>
          <td class="${Number.isFinite(roe) ? (roe >= 0 ? 'up' : 'down') : ''}">${Number.isFinite(roe) ? `${roe >= 0 ? '+' : '−'}${fmt(Math.abs(roe), 2)}%` : '—'}</td>
          <td class="reason">${reasonChip(trade.reason)}${trade.note ? ` ${trade.note}` : ''}</td>
        </tr>`;
      }).join('')
      : '<tr class="pm-empty-row"><td colspan="8">아직 청산된 모의 거래가 없습니다.<br>트레이딩 탭에서 LONG · 매수 또는 SHORT · 매도를 눌러 주문하고, TP·SL 도달이나 시장가 청산으로 거래가 끝나면 이 표에 실제 기록이 쌓입니다.</td></tr>';

    /* 하단 요약 */
    const foot = $('#pm-foot');
    const holdText = data.holdAvg ? `${Math.floor(data.holdAvg / 60)}시간 ${Math.round(data.holdAvg % 60)}분`
      : (data.list.length ? '1분 미만' : '—');
    foot.innerHTML = `<span>모의 계좌 · 실제 주문 없음</span>`
      + `<span>이익 실현 <b>${data.wins}건</b></span>`
      + `<span>손실 <b>${data.losses}건</b></span>`
      + `<span>최대 연속 손실 <b>${data.maxStreak}회</b></span>`
      + `<span>평균 보유 <b>${holdText}</b></span>`
      + `<span>평가 자산 <b>$${fmt(account.equity, 0)}</b></span>`
      + `<span class="pm-warn">투자 조언 아님</span>`;
  }

  function drawCurve(data) {
    const canvas = $('#pm-canvas');
    if (!canvas) return;
    const curve = data ? data.curve : aggregate(stats.period).curve;
    const base = data ? data.base : 0;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const box = canvas.getBoundingClientRect();
    const w = Math.round(box.width);
    const h = Math.round(box.height) || 210;
    if (!w) return;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (curve.length < 2) return;

    const padL = 52;
    const padR = 46;
    const padT = 14;
    const padB = 20;
    const values = curve.map((point) => point.b).concat(Number.isFinite(base) ? [base] : []);
    const lo0 = Math.min(...values);
    const hi0 = Math.max(...values);
    const span0 = (hi0 - lo0) || Math.abs(hi0) * 0.01 || 100;
    const lo = lo0 - span0 * 0.12;
    const hi = hi0 + span0 * 0.12;
    const x = (i) => padL + (i / (curve.length - 1)) * (w - padL - padR);
    const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (h - padT - padB);
    const digits = hi0 >= 1000 ? 0 : 2;

    ctx.font = '9px Arial, sans-serif';
    ctx.textBaseline = 'middle';
    for (let i = 0; i <= 4; i++) {
      const value = hi - ((hi - lo) * i) / 4;
      const gy = y(value);
      ctx.strokeStyle = '#1d2734';
      ctx.beginPath();
      ctx.moveTo(padL, gy);
      ctx.lineTo(w - padR, gy);
      ctx.stroke();
      ctx.fillStyle = '#6f7d90';
      ctx.textAlign = 'right';
      ctx.fillText(fmt(value, digits), padL - 8, gy);
    }

    /* 시작 자본 기준선 — 마지막 값 라벨과 겹치면 글자는 생략한다 */
    const lastPointY = y(curve[curve.length - 1].b);
    if (Number.isFinite(base)) {
      const by = y(base);
      ctx.strokeStyle = '#46566a';
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(padL, by);
      ctx.lineTo(w - padR, by);
      ctx.stroke();
      ctx.setLineDash([]);
      if (w >= 620 && Math.abs(by - lastPointY) > 18) {
        ctx.textAlign = 'right';
        ctx.fillStyle = '#5d6b7d';
        ctx.fillText(`시작 $${fmt(base, digits)}`, w - padR - 4, by - 9);
      }
    }

    /* 면적 */
    const grad = ctx.createLinearGradient(0, padT, 0, h - padB);
    grad.addColorStop(0, 'rgba(47, 211, 154, .26)');
    grad.addColorStop(1, 'rgba(47, 211, 154, 0)');
    ctx.beginPath();
    ctx.moveTo(x(0), y(curve[0].b));
    for (let i = 1; i < curve.length; i++) ctx.lineTo(x(i), y(curve[i].b));
    ctx.lineTo(x(curve.length - 1), h - padB);
    ctx.lineTo(x(0), h - padB);
    ctx.closePath();
    ctx.fillStyle = grad;
    ctx.fill();

    /* 선 */
    ctx.beginPath();
    ctx.moveTo(x(0), y(curve[0].b));
    for (let i = 1; i < curve.length; i++) ctx.lineTo(x(i), y(curve[i].b));
    ctx.strokeStyle = '#35d29c';
    ctx.lineWidth = 1.9;
    ctx.lineJoin = 'round';
    ctx.stroke();

    /* 체결 지점 */
    const step = curve.length > 90 ? 2 : 1;
    for (let i = 1; i < curve.length; i += step) {
      const up = curve[i].b >= curve[i - 1].b;
      ctx.beginPath();
      ctx.arc(x(i), y(curve[i].b), 2.1, 0, Math.PI * 2);
      ctx.fillStyle = up ? 'rgba(53, 210, 156, .9)' : 'rgba(242, 104, 123, .95)';
      ctx.fill();
    }

    /* 고점 · 저점 · 현재 */
    const highIdx = curve.reduce((best, point, i) => (point.b > curve[best].b ? i : best), 0);
    const lowIdx = curve.reduce((best, point, i) => (point.b < curve[best].b ? i : best), 0);
    const marker = (idx, color, label, lift, below) => {
      const px = x(idx);
      const py = y(curve[idx].b);
      ctx.beginPath();
      ctx.arc(px, py, 3.4, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.strokeStyle = '#0e141c';
      ctx.lineWidth = 1.5;
      ctx.stroke();
      if (w < 620) return;
      const right = idx > curve.length - 4;
      ctx.font = '9px Arial, sans-serif';
      ctx.textAlign = right ? 'right' : 'left';
      const tx = px + (right ? -7 : 8);
      const ty = below ? py + 14 : py - lift;
      const tw = ctx.measureText(label).width;
      ctx.fillStyle = 'rgba(12, 18, 25, .9)';
      ctx.fillRect(right ? tx - tw - 4 : tx - 4, ty - 7, tw + 8, 14);
      ctx.fillStyle = color;
      ctx.fillText(label, tx, ty);
    };
    if (highIdx !== lowIdx) {
      marker(highIdx, '#8fa3b6', `고점 ${fmt(curve[highIdx].b, digits)}`, 12, false);
      const lowY = y(curve[lowIdx].b);
      marker(lowIdx, '#f2687b', `저점 ${fmt(curve[lowIdx].b, digits)}`, 26, lowY > h - padB - 46);
    }
    const lastValue = curve[curve.length - 1].b;
    ctx.beginPath();
    ctx.arc(x(curve.length - 1), y(lastValue), 4, 0, Math.PI * 2);
    ctx.fillStyle = '#35d29c';
    ctx.fill();
    ctx.fillStyle = '#eafff6';
    ctx.font = 'bold 11px Arial, sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    ctx.fillText(`$${fmt(lastValue, digits)}`, x(curve.length - 1) - 9, y(lastValue) - 10);

    /* x축 — 하루 안의 곡선이면 시각, 며칠에 걸치면 날짜. 점이 적으면 같은 라벨을 겁치지 않는다 */
    const timeMode = curve[curve.length - 1].t - curve[0].t < 36 * 3600000;
    const tickCount = Math.min(5, curve.length);
    const usedIdx = new Set();
    const usedLabel = new Set();
    ctx.fillStyle = '#6a7889';
    ctx.font = '9px Arial, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    for (let i = 0; i < tickCount; i++) {
      const idx = tickCount === 1 ? 0 : Math.round((i * (curve.length - 1)) / (tickCount - 1));
      if ([...usedIdx].some((used) => Math.abs(used - idx) < 2)) continue;
      const at = new Date(curve[idx].t);
      const label = timeMode
        ? `${String(at.getHours()).padStart(2, '0')}:${String(at.getMinutes()).padStart(2, '0')}`
        : `${String(at.getMonth() + 1).padStart(2, '0')}/${String(at.getDate()).padStart(2, '0')}`;
      if (usedLabel.has(label)) continue;
      usedIdx.add(idx);
      usedLabel.add(label);
      ctx.fillText(label, x(idx), h - padB + 6);
    }
  }

  /* ============================================================ 시작 */

  function start() {
    buildFlow();
    buildRisk();
    buildStats();
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => setTimeout(start, 0));
  else setTimeout(start, 0);
})();
