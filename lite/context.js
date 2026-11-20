/* PIXEL TRADING FLOOR — AI 프롬프트 문맥 조립 (P0 · 2026-11-20)
   화면에 이미 있는 데이터를 '◆ 정밀 분석'의 AI 요청에 함께 실어 보낸다.
   1) 내 책: 모의 계좌 잔고·보유 포지션·오늘 손익 (lite/paper.js, 이 브라우저)
   2) 복기 교훈: 최근 청산 3건에서 만든 3줄(같은 localStorage 계좌 기록)
   3) 시장 국면: 5분봉 ATR 비율·백분위·추세강도·1H EMA50 위치 + 공포탐욕지수
   4) 이미 화면에 있는 것: 경제 일정(data/calendar.json ← lite/whale.js), 고래 입출금, 헤드라인
   없는 값은 지어내지 않는다 — inputs에 '없음'으로 남겨 Gemini가 '데이터 없음'이라고 말하게 한다. */

(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LiteContext = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const r = (v, nd = 2) => (Number.isFinite(+v) ? Math.round(+v * Math.pow(10, nd)) / Math.pow(10, nd) : null);
  const txt = (v, n = 90) => (typeof v === 'string' ? v.replace(/\s+/g, ' ').trim().slice(0, n) : '');

  function atrSeries(candles, period = 14) {
    const out = new Array(candles.length).fill(null);
    if (candles.length <= period) return out;
    const trs = [];
    let prev = candles[0].c;
    for (const c of candles) {
      trs.push(Math.max(c.h - c.l, Math.abs(c.h - prev), Math.abs(c.l - prev)));
      prev = c.c;
    }
    let acc = 0;
    for (let i = 1; i <= period; i++) acc += trs[i];
    acc /= period;
    out[period] = acc;
    for (let i = period + 1; i < candles.length; i++) {
      acc = (acc * (period - 1) + trs[i]) / period;
      out[i] = acc;
    }
    return out;
  }

  function ema(values, period) {
    const k = 2 / (period + 1);
    const out = new Array(values.length);
    let prev = values[0];
    for (let i = 0; i < values.length; i++) {
      prev = i === 0 ? values[0] : values[i] * k + prev * (1 - k);
      out[i] = prev;
    }
    return out;
  }

  function avg(list) {
    const v = list.filter((x) => Number.isFinite(x));
    return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null;
  }

  // 시장 국면 — 5분봉만으로 계산(미래 봉 없음). 값이 모자라면 null.
  function regime(k5, fng) {
    if (!Array.isArray(k5) || k5.length < 30) return null;
    const i = k5.length - 1;
    const closes = k5.map((c) => c.c);
    const atr = atrSeries(k5, 14);
    const a = atr[i];
    if (!a) return null;
    const price = closes[i];
    const atrPct = (a / price) * 100;
    const short = avg(atr.slice(-14));
    const mid = avg(atr.slice(-100));
    const pcts = [];
    for (let k = Math.max(0, i - 99); k <= i; k++) if (atr[k]) pcts.push((atr[k] / closes[k]) * 100);
    let pctile = null;
    if (pcts.length >= 20) pctile = Math.round((pcts.filter((v) => v <= atrPct).length / pcts.length) * 100);
    const e20 = ema(closes, 20);
    const e50 = ema(closes, Math.min(50, closes.length));
    const trend = a ? Math.abs(e20[i] - e50[i]) / a : null;
    const win = k5.slice(-48);
    const hi = Math.max(...win.map((c) => c.h));
    const lo = Math.min(...win.map((c) => c.l));
    const pos = hi > lo ? ((price - lo) / (hi - lo)) * 100 : null;
    const label = (atrPct >= 1.2 || (short && mid && short / mid >= 1.8)) ? '급변(변동성 폭발)'
      : (pctile != null && pctile <= 15) || (short && mid && short / mid <= 0.62) ? '죽은 장(변동성 위축)'
        : trend != null && trend >= 1.0 ? '추세장'
          : trend != null && trend < 0.35 ? '횡보(방향 없음)' : '정상 범위';
    return {
      label,
      atrPct: r(atrPct, 3),
      atrRatio: short && mid ? r(short / mid, 1) : null,
      atrPctile: pctile,
      trendStrength: r(trend, 1),
      pos48: r(pos, 0),
      // 공포탐욕은 화면 칩과 같은 값(있을 때만)
      fng: fng && Number.isFinite(+fng.value) ? { value: +fng.value, label: txt(fng.label, 24) } : null,
    };
  }

  // 내 책 — lite/paper.js 스냅숏(이 브라우저 localStorage). 값이 없으면 null.
  function account(snap) {
    if (!snap || !snap.account) return null;
    const acc = snap.account;
    const open = (snap.open || []).map((p) => ({
      symbol: p.display || p.symbol,
      side: p.side,
      entry: p.entry,
      lev: p.leverage,
      roePct: p.roePct,
    }));
    return {
      balance: r(acc.balance, 2),
      equity: r(acc.equity, 2),
      todayPnl: r(acc.todayPnl, 2),
      todayTrades: acc.todayTrades,
      winRate: acc.winRate,
      trades: acc.closedCount,
      open,
      note: open.length ? '이미 보유한 종목·방향은 중복 진입하지 마세요' : '보유 포지션 없음',
    };
  }

  // 복기 교훈 — 최근 청산 기록에서 3줄(요약 1줄 + 사례 2줄)
  function lessonsFrom(snap) {
    const closed = (snap && (snap.closed || (snap.trades || []))) || [];
    if (!closed.length) return ['아직 웹 모의계좌에 청산 기록이 없습니다 — 성적을 지어내지 마세요.'];
    const rows = closed.slice(0, 5);
    const wins = rows.filter((t) => (t.realizedAmt != null ? t.realizedAmt > 0 : t.pnl > 0)).length;
    const sumPct = rows.reduce((a, t) => a + (Number(t.realizedRoePct != null ? t.realizedRoePct : t.movePct) || 0), 0);
    const out = [`최근 ${rows.length}건 ${wins}승 ${rows.length - wins}패 · 합 ${r(sumPct, 1)}%`];
    for (const t of rows.slice(0, 2)) {
      const pct = Number(t.realizedRoePct != null ? t.realizedRoePct : t.movePct);
      out.push(`· ${txt(t.display || t.symbol, 10)} ${t.side === 'LONG' ? '롱' : t.side === 'SHORT' ? '숏' : t.side} `
        + `${txt(t.closeReason || t.reason, 12)} ${Number.isFinite(pct) ? r(pct, 1) + '%' : ''}`);
    }
    const losses = rows.filter((t) => (t.realizedAmt != null ? t.realizedAmt <= 0 : t.pnl <= 0));
    const longLoss = losses.filter((t) => t.side === 'LONG').length;
    const shortLoss = losses.filter((t) => t.side === 'SHORT').length;
    if (longLoss >= 2 && longLoss >= shortLoss) out.push('교훈: 롱 손실이 몰림 — 하락 추세 역추세 롱을 피하세요');
    else if (shortLoss >= 2 && shortLoss >= longLoss) out.push('교훈: 숏 손실이 몰림 — 상승 추세 역추세 숏을 피하세요');
    else if (losses.length >= 3 && losses.length >= rows.length - 1) out.push('교훈: 손실 비중이 높음 — 진입 자리를 더 엄격히');
    return out;
  }

  // 경제 일정(이미 data/calendar.json을 읽는 lite/whale.js 스냅숏) 중 24시간 내
  function calendarFrom(whaleSnap, nowMs) {
    const events = whaleSnap && Array.isArray(whaleSnap.calendar) ? whaleSnap.calendar : [];
    if (!events.length) return [];
    const now = nowMs || Date.now();
    const order = { high: 0, medium: 1, low: 2 };
    return events
      .filter((e) => Number.isFinite(e.atMs) && e.atMs >= now - 15 * 60000 && e.atMs <= now + 24 * 3600000)
      .sort((a, b) => (order[a.impact] == null ? 3 : order[a.impact]) - (order[b.impact] == null ? 3 : order[b.impact]))
      .slice(0, 3)
      .map((e) => ({ time: txt(e.time || e.date, 12), title: txt(e.title || e.titleEn, 60), impact: txt(e.impact, 8) }));
  }

  function whalesFrom(whaleSnap) {
    const rows = whaleSnap && Array.isArray(whaleSnap.rows) ? whaleSnap.rows : [];
    return rows.slice(0, 3).map((w) => ({
      usd: Number.isFinite(+w.usd) ? Math.round(+w.usd / 1000) * 1000 : null,
      from: txt(w.from, 16), to: txt(w.to, 16),
    }));
  }

  function headlinesFrom(list) {
    return (Array.isArray(list) ? list : []).slice(0, 3)
      .map((n) => txt(typeof n === 'string' ? n : n && (n.title || n.text), 90)).filter(Boolean);
  }

  function build(opts) {
    const o = opts || {};
    const snap = o.paperSnap || null;
    const whaleSnap = o.whaleSnap || null;
    const acc = account(snap);
    const lessons = lessonsFrom(snap);
    const reg = regime(o.k5, o.fng);
    const cal = calendarFrom(whaleSnap, o.nowMs);
    const whales = whalesFrom(whaleSnap);
    const heads = headlinesFrom(o.news);
    return {
      account: acc,
      lessons,
      regime: reg,
      calendar: cal,
      whales,
      headlines: heads,
      inputs: {
        내계좌: acc ? '읽음' : '없음',
        복기: lessons.length && lessons[0].indexOf('아직') === 0 ? '없음' : '읽음',
        시장국면: reg ? '읽음' : '없음',
        경제일정: cal.length ? '읽음(' + cal.length + '건)' : '없음',
        고래입출금: whales.length ? '읽음(' + whales.length + '건)' : '없음',
        헤드라인: heads.length ? '읽음(' + heads.length + '건)' : '없음',
      },
    };
  }

  return { build, regime, lessonsFrom, account, calendarFrom, whalesFrom, headlinesFrom, atrSeries, ema };
});
