/* PIXEL TRADING FLOOR 체험판 — 규칙 판정 (AI 대신 정해진 규칙, 순수 함수)
   기준: 기존 BTC 신호봇 규칙. 수치 출처 docs/superpowers/specs/2026-09-30-lite-site-design.md */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./indicators.js'));
  else root.LiteBrain = factory(root.LiteInd);
})(typeof self !== 'undefined' ? self : this, function (Ind) {
  'use strict';

  const RULES = Object.freeze({
    trendBandPct: 0.1, boxBars: 72, rsiPeriod: 14,
    rsiLongMax: 75, rsiShortMin: 25, rsiPenalty: 10, // 이 너머로 추격하면 확신도 −10
    pmRsiHot: 70, pmRsiCold: 30,              // PM 과열·과매도 (서버 지표 표기와 같은 기준)
    fngFear: 25, fngGreed: 75,
    fundingCold: -0.03, fundingHot: 0.03,     // %
    lsLow: 0.8, lsHigh: 1.8,
    auxWeight: 0.5, confMin: 50, confMax: 85,
    // 데모 버전은 100배 고정·수수료 없음(대표 결정 2026-10-01: 빨리 체결돼야 재밌다)
    // 손익비(2026-11-20 홍원 님 "3번" 결정): 증거금 기준 익절 +20%·손절 −20% = 손익비 1:1
    //   100배에서 가격 +0.2%·−0.2%, 청산 −0.5%보다 손절이 앞. 손익분기 승률 50%
    //   (이전 +15% / −30%는 손익비 0.5:1·손익분기 67%라 61% 승률이면 손실 구간이었다)
    //   실측(OKX 5분봉 3,785봉·15일): 옛 15/30은 승률 61.4%(손익분기 67% — 마이너스),
    //   20/20은 51.9%(손익분기 50% — 플러스), 25/20은 48.8%. 승률을 지키면서 손익분기를 낮추는 쪽을 고름
    targetMarginPct: 20, stopMarginPct: 20,
    // 진입 조건(2026-11-20 "3번"): 추세 불명확·트리거 없음·RSI 추격은 관망(HOLD)
    //   requireTrigger를 false로 두면 "추세는 있고 추격도 아닌" 자리를 트리거 없이도 진입시킨다
    //   (실측에선 트리거 없는 자리가 오히려 승률이 높았음 — 필요하면 이 한 줄만 바꾸면 된다)
    requireTrigger: true,
    mmPct: 0.5, feePct: 0,                    // 유지증거금률(청산 근사)·편도 수수료
    marginPct: 10, amendMarginPct: 5, pmLowConf: 60,
    defaultLev: 100, maxLev: 125,
  });

  const clean = (n) => (Number.isFinite(n) ? Number(n.toPrecision(10)) : null);
  const round = (n, dp) => (Number.isFinite(n) ? Math.round(n * 10 ** dp) / 10 ** dp : null);

  function clampLev(v) {
    const n = Math.round(Number(v));
    return Number.isFinite(n) && n >= 1 && n <= RULES.maxLev ? n : RULES.defaultLev;
  }

  // 가격 표기 — 1 이상은 자릿수 고정 + 콤마, 1 미만은 유효숫자 4자리(저가 코인이 0.0000으로 뭉개지지 않게)
  // 1~100은 유효숫자 4자리가 되게 — 고배율에서 목표(진입 +0.1~0.2%)가 진입가와 같은 숫자로 찍히지 않게
  function fmtPrice(v) {
    if (!Number.isFinite(v)) return '데이터 없음';
    const abs = Math.abs(v);
    if (abs < 1) return String(Number(v.toPrecision(4)));
    const d = abs >= 1000 ? 0 : abs >= 100 ? 1 : abs >= 10 ? 2 : 3;
    return v.toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  // 시세 스냅숏 → 규칙 재료
  function signals(s) {
    const c5 = s.k5.map((c) => c.c);
    const ema = Ind.emaSeries(s.k1h.map((c) => c.c), 50);
    const ema50h = ema[ema.length - 1];
    const price = s.price;
    const band = (ema50h * RULES.trendBandPct) / 100;
    const trend = price > ema50h + band ? 'UP' : price < ema50h - band ? 'DOWN' : 'FLAT';
    const rsi = Ind.wilderRsi(c5, RULES.rsiPeriod);
    const box = Ind.boxBefore(s.k5, RULES.boxBars);
    const breakout = !box ? null : price > box.high ? 'UP' : price < box.low ? 'DOWN' : null;
    const n = c5.length;
    const bbPrev = Ind.bollingerAt(c5, n - 2);
    const bb = Ind.bollingerAt(c5, n - 1);
    let bbReentry = null;
    if (bbPrev && bb) {
      if (c5[n - 2] < bbPrev.lower && c5[n - 1] > bb.lower) bbReentry = 'LONG';
      else if (c5[n - 2] > bbPrev.upper && c5[n - 1] < bb.upper) bbReentry = 'SHORT';
    }
    let flow = null;
    if (s.oi && Number.isFinite(s.oi.changePct) && Number.isFinite(s.change24hPct)) {
      const oiUp = s.oi.changePct > 0;
      const oiDown = s.oi.changePct < 0;
      const pUp = s.change24hPct > 0;
      const pDown = s.change24hPct < 0;
      flow = oiUp && pUp ? 'NEW_LONG' : oiUp && pDown ? 'NEW_SHORT'
        : oiDown && pUp ? 'SHORT_COVER' : oiDown && pDown ? 'LONG_UNWIND' : 'FLAT';
    }
    // 보조 근거 투표 — 쏠림의 반대편에 표: +1 롱 · −1 숏 · 0 중립 · null 데이터 없음
    const vote = (v, lowLong, highShort) => (v == null ? null : v <= lowLong ? 1 : v >= highShort ? -1 : 0);
    const aux = [
      { key: 'fng', vote: vote(s.fng ? s.fng.value : null, RULES.fngFear, RULES.fngGreed) },
      { key: 'funding', vote: vote(s.fundingPct, RULES.fundingCold, RULES.fundingHot) },
      { key: 'ls', vote: vote(s.topLS ? s.topLS.ratio : null, RULES.lsLow, RULES.lsHigh) },
      { key: 'flow', vote: flow == null ? null : flow === 'NEW_LONG' ? 1 : flow === 'NEW_SHORT' ? -1 : 0 },
    ];
    return {
      display: s.display, kind: s.kind || 'crypto', price, ema50h, trend, rsi, box, breakout, bb, bbReentry, flow, aux,
      change24hPct: s.change24hPct, fundingPct: s.fundingPct, fng: s.fng, topLS: s.topLS, oi: s.oi,
      missing: s.missing || [],
    };
  }

  // 진입가 기준 목표(증거금 +20%)·손절(−20%)·격리 청산가 근사
  function levels(side, entry, lev) {
    const d = side === 'LONG' ? 1 : -1;
    return {
      entry: clean(entry),
      target: clean(entry * (1 + (d * RULES.targetMarginPct) / 100 / lev)),
      stop: clean(entry * (1 - (d * RULES.stopMarginPct) / 100 / lev)),
      liq: clean(entry * (1 - d * (1 / lev - RULES.mmPct / 100))),
    };
  }

  // ACE 판정 — 2026-11-20 홍원 님 결정("3번"): 웹 체험용도 애매한 자리는 관망(HOLD)을 낸다
  //   방향 후보: 1시간 추세(EMA50 ±0.1%), 추세가 없으면 가격이 EMA50 위면 롱·아래면 숏
  //   확신도 = 50 + 10 × (추세 1 + 트리거 1 + 보조 합) − RSI 추격 10, 50~85
  //   추세·트리거가 다 있고 추격이 아닐 때만 진입(ENTRY)
  //   추세 불명확(FLAT)·트리거 없음·RSI 추격은 관망(HOLD) — 어제 73%가 오늘 61%로 떨어진 원인이
  //   "매번 방향을 고르는 규칙"이었으므로, 애매한 자리는 아예 들어가지 않는다(승률이 아니라 기대값을 올린다)
  function decide(sig, { lev }) {
    const candidate = sig.trend === 'UP' ? 'LONG' : sig.trend === 'DOWN' ? 'SHORT'
      : sig.price >= sig.ema50h ? 'LONG' : 'SHORT';
    const dir = candidate === 'LONG' ? 1 : -1;
    const trigger = candidate === 'LONG'
      ? (sig.breakout === 'UP' ? 'BREAKOUT' : sig.bbReentry === 'LONG' ? 'BB_REENTRY' : null)
      : (sig.breakout === 'DOWN' ? 'BREAKOUT' : sig.bbReentry === 'SHORT' ? 'BB_REENTRY' : null);
    const hot = sig.rsi != null && ((candidate === 'LONG' && sig.rsi >= RULES.rsiLongMax)
      || (candidate === 'SHORT' && sig.rsi <= RULES.rsiShortMin));
    const auxScore = sig.aux.reduce((a, x) => a + (x.vote == null ? 0 : x.vote * dir * RULES.auxWeight), 0);
    const points = (sig.trend === 'FLAT' ? 0 : 1) + (trigger ? 1 : 0) + auxScore;
    const confidence = Math.round(Math.min(RULES.confMax, Math.max(RULES.confMin, 50 + 10 * points - (hot ? RULES.rsiPenalty : 0))));
    const reason = sig.trend === 'FLAT' ? 'NO_TREND'
      : !trigger && RULES.requireTrigger ? 'NO_TRIGGER' : hot ? 'RSI_GATE' : 'ENTRY';
    const hold = reason !== 'ENTRY';
    const lvls = levels(candidate, sig.price, lev);
    return {
      ...lvls,
      side: hold ? null : candidate, action: hold ? 'HOLD' : dir === 1 ? 'BUY' : 'SELL',
      bias: hold ? 'PASS' : candidate, candidate, trigger, lev,
      auxScore, confidence, reason, hold, trial: hold, rsiHot: hot,
      // 관망일 때 화면에 남겨 둘 후보 방향과 참고 레벨("돌파하면 이 자리") — 진입 계획은 아니다
      pending: hold ? { side: candidate, ...lvls } : null,
    };
  }

  // 리스크 게이트 — 강등 기준은 "손절이 청산가 안쪽인가" 하나. 손익비·손익분기 승률은 공개만 한다.
  function riskGate(plan) {
    const lev = plan.lev;
    const feeRt = RULES.feePct * 2 * lev; // 왕복 수수료(증거금 대비 %)
    const win = RULES.targetMarginPct - feeRt;
    const loss = RULES.stopMarginPct + feeRt;
    const breakEvenWinPct = win > 0 ? (loss / (win + loss)) * 100 : 100;
    const rr = RULES.targetMarginPct / RULES.stopMarginPct;
    const ruleNote = `승률형 규칙: 증거금 기준 익절 +${RULES.targetMarginPct}% · 손절 −${RULES.stopMarginPct}% (손익비 ${rr.toFixed(2)}) — ${feeRt > 0 ? '수수료 포함 ' : ''}손익분기 승률 약 ${Math.round(breakEvenWinPct)}%`;
    if (!plan.side) {
      // 관망 — 진입 계획이 없으니 게이트는 막을 것이 없다. 규칙 숫자만 참고로 함께 보여 준다
      return {
        rr: round(rr, 2), ok: true, downgrade: false, stopBeyondLiq: false, liq: null,
        breakEvenWinPct: round(breakEvenWinPct, 1),
        reasons: ['관망 판정 — 진입 계획이 없어 리스크 게이트는 통과 처리했습니다.', ruleNote],
        downgradeReasons: [],
      };
    }
    // 손절가가 청산가와 같아도(100배: 둘 다 −0.5%) 손절 전에 청산될 수 있으니 막는다
    const stopBeyondLiq = plan.side === 'LONG' ? plan.stop <= plan.liq : plan.stop >= plan.liq;
    const reasons = [];
    const downgradeReasons = [];
    if (stopBeyondLiq) {
      const r = plan.stop === plan.liq
        ? `${lev}배에서는 손절가와 청산가가 같습니다(${fmtPrice(plan.stop)}) — 손절 전에 청산될 수 있어 진입하지 않습니다`
        : `${lev}배에서는 손절가 ${fmtPrice(plan.stop)}보다 청산가 ${fmtPrice(plan.liq)}가 먼저 옵니다 — 진입하지 않습니다`;
      reasons.push(r);
      downgradeReasons.push(r);
    } else {
      const gap = (Math.abs(plan.stop - plan.liq) / plan.entry) * 100;
      reasons.push(`${lev}배 청산가 ${fmtPrice(plan.liq)} — 손절가가 청산가보다 ${gap.toFixed(2)}%p 앞에 있습니다`);
    }
    reasons.push(ruleNote);
    reasons.push(feeRt > 0 ? `왕복 수수료 = 증거금의 ${feeRt.toFixed(1)}% (편도 ${RULES.feePct}% × ${lev}배)` : '체험판은 수수료 없이 계산합니다 (실제 거래소는 왕복 수수료가 붙습니다)');
    return {
      rr: round(rr, 2), ok: !stopBeyondLiq, downgrade: stopBeyondLiq, stopBeyondLiq, liq: plan.liq,
      breakEvenWinPct: round(breakEvenWinPct, 1), reasons, downgradeReasons,
    };
  }

  // PM 최종 승인(알고리즘 모드) — 청산 위험이면 기각, RSI 과열·확신도 낮음은 비중 축소(데모 버전은 관망 대신 작게)
  function pmReview(plan, sig, gate) {
    if (!plan.side) return { verdict: 'APPROVE', code: 'HOLD_OK', marginPct: 0 };
    if (gate.stopBeyondLiq) return { verdict: 'REJECT', code: 'LIQ', marginPct: 0 };
    const hot = sig.rsi != null && ((plan.side === 'LONG' && sig.rsi >= RULES.pmRsiHot)
      || (plan.side === 'SHORT' && sig.rsi <= RULES.pmRsiCold));
    if (hot) return { verdict: 'AMEND', code: 'RSI', marginPct: RULES.amendMarginPct };
    if (plan.confidence < RULES.pmLowConf) return { verdict: 'AMEND', code: 'LOW_CONF', marginPct: RULES.amendMarginPct };
    return { verdict: 'APPROVE', code: 'OK', marginPct: RULES.marginPct };
  }

  // 최종 — PM은 알고리즘 모드만, 게이트 강등은 모든 모드
  function finalize(plan, gate, pm, mode) {
    let action = plan.action;
    let marginPct = RULES.marginPct;
    const verdict = mode === 'algo' ? pm.verdict : null;
    if (verdict === 'REJECT') action = 'HOLD';
    if (verdict === 'AMEND') marginPct = pm.marginPct;
    if (gate.downgrade) action = 'HOLD';
    const side = action === 'BUY' ? 'LONG' : action === 'SELL' ? 'SHORT' : null;
    return { action, bias: side || 'PASS', side, verdict, marginPct: side ? marginPct : 0, downgraded: !!gate.downgrade };
  }

  function analyze(snap, opts = {}) {
    const lev = clampLev(opts.lev);
    const mode = opts.mode === 'algo' ? 'algo' : 'scalp';
    const sig = signals(snap);
    const plan = decide(sig, { lev });
    const gate = riskGate(plan);
    const pm = pmReview(plan, sig, gate);
    return { lev, mode, sig, plan, gate, pm, final: finalize(plan, gate, pm, mode) };
  }

  return { RULES, clampLev, fmtPrice, signals, levels, decide, riskGate, pmReview, finalize, analyze };
});
