/* PIXEL TRADING FLOOR — 롱/숏 계정 비율 공용 로더 (LiteRatio)
   Bybit `/v5/market/account-ratio`가 HTTP 200에 빈 목록을 주는 일이 잦다(불규칙, 2026-11 확인).
   그래서 값이 비면 다른 공개 API로 자동 폴백하고, 어느 거래소 값인지 함께 돌려준다.

     1순위  Bybit  account-ratio            (period 1h → 5min, 계정 기준 · 앱 기본 거래소)
     2순위  Bybit  long-short-ratio         (종목 기준 — 계정 기준이 계속 비면)
     3순위  Binance globalLongShortAccountRatio (period 15m, 지역 차단 시 조용히 건너뛴다)
     4순위  Bitget account-long-short       (period 1h, CORS 허용 확인된 최후 폴백)

   값이 하나도 없으면 null을 돌려준다 — 지어내지 않는다. 호출부는 실패 사유를 LiteRatio.lastError로 읽는다.
   응답 하나가 늦어도 전체가 멈추지 않도록 각 요청에 시간 제한을 둔다. */
(function () {
  'use strict';
  if (window.LiteRatio) return;

  const BYBIT = 'https://api.bybit.com/v5/market';
  const BITGET = 'https://api.bitget.com/api/v2/mix/market/account-long-short';
  const BINANCE = 'https://fapi.binance.com/futures/data/globalLongShortAccountRatio';

  const num = (v) => (v === null || v === undefined || v === '' || !Number.isFinite(Number(v)) ? null : Number(v));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function getJSON(url, ms = 7000) {
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = ctrl ? setTimeout(() => ctrl.abort(), ms) : 0;
    try {
      const res = await fetch(url, { cache: 'no-store', credentials: 'omit', signal: ctrl ? ctrl.signal : undefined });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.json();
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  /* buy/sell(또는 long/short) 두 값을 받아 0~1 롱 비중으로 정규화 — 합이 0이면 없는 값으로 본다 */
  function share(buy, sell) {
    const b = num(buy);
    const s = num(sell);
    if (b == null) return null;
    if (s == null) return null;
    const total = b + s;
    if (!(total > 0)) return null;
    return b / total;
  }

  function pack(longShare, source, at, extra) {
    const shareValue = Math.max(0, Math.min(1, longShare));
    return Object.assign({
      longShare: shareValue,
      shortShare: 1 - shareValue,
      ratio: shareValue >= 1 ? null : shareValue / (1 - shareValue),
      source,
      at: at || Date.now(),
    }, extra || {});
  }

  async function fromBybitAccount(symbol, period) {
    const body = await getJSON(`${BYBIT}/account-ratio?category=linear&symbol=${symbol}&period=${period}&limit=1`);
    const row = (body && body.list ? body.list : [])[0];
    const v = share(row && row.buyRatio, row && row.sellRatio);
    if (v == null) throw new Error(`빈 목록(list=${body && body.list ? body.list.length : 0})`);
    return pack(v, 'Bybit', num(row.timestamp), { basis: '계정 기준', period });
  }

  async function fromBybitPosition(symbol) {
    const body = await getJSON(`${BYBIT}/long-short-ratio?category=linear&symbol=${symbol}&period=1h&limit=1`);
    const row = (body && body.list ? body.list : [])[0];
    const v = share(row && row.buyRatio, row && row.sellRatio);
    if (v == null) throw new Error(`빈 목록(list=${body && body.list ? body.list.length : 0})`);
    return pack(v, 'Bybit', num(row.timestamp), { basis: '종목 기준', period: '1h' });
  }

  async function fromBitget(symbol) {
    const body = await getJSON(`${BITGET}?symbol=${encodeURIComponent(symbol)}&productType=usdt-futures&period=1h`);
    if (!body || String(body.code) !== '00000' || !Array.isArray(body.data) || !body.data.length) {
      throw new Error(`code=${body && body.code} ${body && body.msg ? body.msg : ''}`.trim());
    }
    /* 응답은 오래된 것부터 — 마지막(가장 최근) 봉을 쓴다 */
    const row = body.data[body.data.length - 1];
    const v = share(row.longAccountRatio, row.shortAccountRatio);
    if (v == null) throw new Error('비율 비어 있음');
    return pack(v, 'Bitget', num(row.ts), { basis: '계정 기준', period: '1h' });
  }

  async function fromBinance(symbol) {
    const body = await getJSON(`${BINANCE}?symbol=${encodeURIComponent(symbol)}&period=15m&limit=1`);
    const row = Array.isArray(body) ? body[0] : null;
    const v = share(row && row.longAccount, row && row.shortAccount);
    if (v == null) throw new Error(body && body.msg ? String(body.msg).slice(0, 60) : '응답 비어 있음');
    return pack(v, 'Binance', num(row.timestamp), { basis: '계정 기준', period: '15m' });
  }

  /* 캐시 — 같은 종목을 15초 안에 다시 부르면 이전 결과를 쓴다(대시보드·트레이딩 탭이 동시에 부른다) */
  const cache = new Map();
  const CACHE_MS = 15000;

  async function load(symbol, opts) {
    load.lastError = '';
    const force = !!(opts && opts.force);
    const hit = cache.get(symbol);
    if (!force && hit && Date.now() - hit.storedAt < CACHE_MS) {
      if (hit.value) return hit.value;
      if (hit.error) load.lastError = hit.error;
    }
    const errors = [];
    const steps = [
      () => fromBybitAccount(symbol, '1h'),
      () => fromBybitAccount(symbol, '5min'),
      () => fromBybitPosition(symbol),
      () => fromBinance(symbol),
      () => fromBitget(symbol),
    ];
    const labels = ['Bybit 계정', 'Bybit 계정(5m)', 'Bybit 종목', 'Binance', 'Bitget'];
    for (let i = 0; i < steps.length; i += 1) {
      try {
        const value = await steps[i]();
        cache.set(symbol, { storedAt: Date.now(), value, error: '' });
        load.lastError = '';
        load.lastSource = value.source;
        return value;
      } catch (e) {
        const label = labels[i];
        errors.push(`${label} ${e && e.message ? e.message : '실패'}`);
        if (i === 0) await sleep(600);   // Bybit는 곧바로 다시 부르면 또 빈 목록을 주기도 한다
      }
    }
    load.lastError = errors.join(' · ');
    load.lastSource = '';
    cache.set(symbol, { storedAt: Date.now(), value: null, error: load.lastError });
    return null;
  }

  window.LiteRatio = {
    load,
    lastError: '',
    lastSource: '',
    /* 화면 표기용 짧은 출처 이름 */
    label: (source) => (source === 'Bybit' ? 'Bybit' : source === 'Bitget' ? 'Bitget' : source === 'Binance' ? 'Binance' : ''),
  };
})();
