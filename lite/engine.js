/* PIXEL TRADING FLOOR 체험판 — 엔진
   서버 engine.js와 같은 이벤트를 브라우저 안에서 만든다. 판정은 LiteBrain, 대사는 LiteScript.
   직원별 "생각 시간"은 연출이다(pace 0이면 즉시 — 테스트용). */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./brain.js'), require('./script.js'));
  else root.LiteEngine = factory(root.LiteBrain, root.LiteScript);
})(typeof self !== 'undefined' ? self : this, function (Brain, Script) {
  'use strict';

  const THINK = {
    taro: 1800, vibe: 2200, diana: 2000, nova: 1200, bull: 2000, bear: 2200, blitz: 1800,
    guard: 1600, ace: 2400, risky: 1600, safe: 1800, neutral: 1800, pm: 2000,
  };
  const r2 = (n) => Math.round(n * 100) / 100;
  const CHART_BARS = 144; // 상단 차트 = 최근 12시간 5분봉

  function createBus() {
    const subs = new Set();
    return {
      emit(ev) {
        for (const fn of [...subs]) {
          try { fn(ev); } catch (_) { /* 구독자 오류가 방송을 막지 않게 */ }
        }
      },
      subscribe(fn) {
        subs.add(fn);
        return () => subs.delete(fn);
      },
    };
  }

  function createEngine(deps) {
    const bus = deps.bus;
    const pace = deps.pace == null ? 1 : deps.pace;
    const sleep = deps.sleep || ((ms) => new Promise((ok) => setTimeout(ok, ms)));
    const wait = (ms) => (pace > 0 && ms > 0 ? sleep(ms * pace) : Promise.resolve());
    const now = deps.now || (() => Date.now());
    const emit = (ev) => bus.emit(ev);
    const log = (line, kind = 'sys') => emit({ type: 'log', kind, line: String(line) });
    // 오류는 토스트(run:error)만으로는 몇 초 뒤 사라진다 — 콘솔에도 이유를 남긴다
    const fail = (message) => {
      log(`> ⚠ ${message}`);
      emit({ type: 'run:error', message });
    };
    let running = false;
    // ✂️ 2026-11-20 홍원 님 요청 — 분석과 진입을 나눈다. 정밀 분석이 끝나도 모의 포지션은 열지 않고,
    //    판정 패널의 '🚀 모의 진입' 버튼을 눌렀을 때(pendingEntry)만 연다.
    let pendingEntry = null;
    let pendingSeq = 0;
    // 분석이 도는 동안 배율을 바꾸면, 판정이 나온 뒤 준비된 진입 계획을 이 배율로 다시 계산한다
    let queuedLev = null;

    // 한 라운드 — 전원 동시에 생각 시작, 생각 시간이 짧은 순서로 발언
    async function round(items) {
      for (const it of items) emit({ type: 'agent:start', id: it.id, ...(it.turn ? { turn: it.turn } : {}) });
      const order = items.slice().sort((a, b) => (THINK[a.id] || 1500) - (THINK[b.id] || 1500));
      let elapsed = 0;
      for (const it of order) {
        const t = THINK[it.id] || 1500;
        await wait(t - elapsed);
        elapsed = t;
        emit({ type: 'agent:done', id: it.id, ...(it.turn ? { turn: it.turn } : {}), bubble: it.text.bubble, report: it.text.report });
      }
    }

    async function run(input, opts = {}) {
      if (running) {
        const err = new Error('이미 분석이 진행 중입니다.');
        err.code = 409;
        throw err;
      }
      running = true;
      // 새 분석이 시작되면 지난 진입 계획은 버린다 — 가격이 움직인 뒤 옛 레벨로 들어가지 않게
      pendingEntry = null;
      emit({ type: 'entry:ready', ready: false, reason: 'running' });
      const mode = opts.mode === 'algo' ? 'algo' : 'scalp';
      const lev = Brain.clampLev(opts.lev);
      const kiosk = !!opts.kiosk;
      try {
        let sym;
        try {
          sym = deps.data.resolveSymbol(input);
        } catch (e) {
          fail(e.message);
          return;
        }
        // auto는 늘 false — 화면(hq.js)이 auto면 "자동 분석 개시 · AI <모델>" 배너를 띄워 AI 판정처럼 보인다
        emit({ type: 'run:start', symbol: sym.symbol, display: sym.display, mock: false, mode, auto: false, model: '자체 로직' });
        log(deps.gemini && !kiosk
          ? '> 픽셀 트레이딩 플로어 — Gemini AI가 13역할의 대사를 한 번에 작성합니다 (판정은 리스크 규칙 게이트를 거칩니다)'
          : '> 데모 버전 — AI 엔진 대신 자체 트레이딩 로직으로 판정합니다');
        // 판정 출처 배지 — Gemini를 쓰지 않는 실행(시연·?ai=0)은 처음부터 '체험 규칙'으로 표시
        if (!(deps.gemini && !kiosk && opts.ai !== false)) emit({ type: 'ai', ok: false, code: 'rule' });
        log(`> 바이낸스 ${sym.display} 시세 요청…`);
        const snap = await deps.data.fetchSnapshot(sym);
        const base = Brain.analyze(snap, { lev, mode });
        let an = base;
        let T = null;
        let aiCtx = null;
        // 전광판 배지: 한국 주식은 "KRX 장 시간 · 무기한은 24H", 나머지(코인·미국 주식·금 무기한)는 24시간 거래
        emit({
          type: 'market', priceLine: Script.priceLine(snap), symbol: sym.symbol, display: sym.display, kind: sym.kind === 'krstock' ? 'krstock' : 'crypto',
          axis: snap.axis, tf: '5m',
          candles: snap.k5.slice(-CHART_BARS).map((c) => ({ t: c.t, o: c.o, h: c.h, l: c.l, c: c.c })),
        });
        log('> 실시간 시세 수신 완료');
        log(`> ${Script.priceLine(snap)}`);
        for (const l of Script.marketLines(an, snap)) log(`> ${l}`);
        await wait(600);

        // ✨ Gemini AI 13인 분석 — Worker(GEMINI_API_KEY Secret)가 13인 대사와 판정을 돌려준다.
        //   응답 실패·무료 한도(429)·연타·연결 오류는 여기서 조용히 체험 규칙(B안)으로 넘어간다(화면은 멈추지 않는다).
        if (deps.gemini && !kiosk && opts.ai !== false) {
          log('> ✨ Gemini AI에 13역할 통합 브리핑을 요청합니다…');
          try {
            // 내 계좌·복기·국면·일정·고래·헤드라인을 함께 보낸다(화면에 이미 있는 데이터)
            let ctx = null;
            try { ctx = deps.context ? deps.context({ snap, an: base }) : null; } catch (_) { ctx = null; }
            const ai = await deps.gemini.analyze(base, snap, { lev, mode, ctx });
            if (ai && ai.ok) {
              an = Brain.applyAi(base, ai);
              T = deps.gemini.texts(Script.texts(an, snap), ai, mode);
              emit({ type: 'ai', ok: true, model: ai.model, ms: ai.ms });
              if (an.aiNote) log(`> ⚠ ${an.aiNote}`);
              log(`> ✨ Gemini AI(${ai.model}) 응답 ${(Math.round((ai.ms || 0) / 100) / 10).toFixed(1)}초 — AI 판정 ${an.final.action} ${an.plan.confidence}%`
                + (ctx && ctx.inputs ? ` · 문맥 ${Object.keys(ctx.inputs).filter((k) => ctx.inputs[k] !== '없음').length}/6` : ''));
              aiCtx = ctx;
            } else {
              emit({ type: 'ai', ok: false, code: ai && ai.code });
              const why = ai && ai.code && ai.code !== 'cooldown' ? ` (${ai.code})` : '';
              log(`> ⚠ Gemini AI 응답 없음${why} — 체험 규칙(B안)으로 판정합니다`);
            }
          } catch (e) {
            emit({ type: 'ai', ok: false, code: 'client_error' });
            log('> ⚠ Gemini AI 연결 오류 — 체험 규칙(B안)으로 판정합니다');
          }
        }
        if (!T) T = Script.texts(an, snap);

        log('── 1/5 애널리스트 브리핑 · 기술 / 펀더멘털 / 뉴스 / 소셜 ──', 'stage');
        if (mode === 'algo') {
          await round([{ id: 'taro', text: T.taro }, { id: 'diana', text: T.diana }, { id: 'nova', text: T.nova }, { id: 'vibe', text: T.vibe }]);
          log('── 2/5 리서치룸 BULL vs BEAR · 1라운드 ──', 'stage');
          await round([{ id: 'bull', turn: 1, text: T.bull[0] }, { id: 'bear', turn: 2, text: T.bear[0] }]);
          log('── BULL vs BEAR · 2라운드 반박 ──', 'stage');
          await round([{ id: 'bull', turn: 3, text: T.bull[1] }, { id: 'bear', turn: 4, text: T.bear[1] }]);
          log('── 3/5 ACE 결재 · 토론 서류 전달 ──', 'stage');
          await round([{ id: 'ace', text: T.ace }]);
          log('── 4/5 리스크 심사 · 보수 ──', 'stage');
          await round([{ id: 'safe', text: T.safe }]);
          await round([{ id: 'neutral', text: T.neutral }]);
          await round([{ id: 'risky', text: T.risky }]);
          log('── 5/5 PM 최종 승인 ──', 'stage');
          await round([{ id: 'pm', text: T.pm }]);
        } else {
          await round([{ id: 'taro', text: T.taro }, { id: 'vibe', text: T.vibe }]);
          log(`── 스캘핑 데스크 (${lev}x) ──`, 'stage');
          await round([{ id: 'blitz', text: T.blitz }]);
          await round([{ id: 'guard', text: T.guard }]);
          log('── 최종 판정 ──', 'stage');
          await round([{ id: 'ace', text: T.ace }]);
        }

        const { plan, gate, final } = an;
        const bal = deps.paper ? deps.paper.balance() : null;
        const sizing = final.side && bal != null
          ? { marginRequired: r2((bal * final.marginPct) / 100), notional: r2((bal * final.marginPct * lev) / 100), notionalPctOfAccount: final.marginPct * lev }
          : null;
        emit({
          type: 'risk', rr: gate.rr, ok: gate.ok, reasons: gate.reasons.slice(), sizing,
          downgradeReasons: gate.downgradeReasons.slice(), scope: 'scalp', side: plan.side, liq: gate.liq,
          stopBeyondLiq: gate.stopBeyondLiq, downgrade: gate.downgrade, minRR: null, mode,
        });
        log('── 리스크 게이트 ──', 'stage');
        for (const r of gate.reasons) log(`> ${r}`);

        const lv = (v) => (plan.side ? Script.fmtPrice(v) : '-');
        const dec = {
          type: 'decision', action: final.action, confidence: plan.confidence,
          entry: lv(plan.entry), stop: lv(plan.stop), target: lv(plan.target), rationale: T.rationale, report: T.ace.report,
        };
        if (mode === 'scalp') dec.scalp = { bias: final.bias, entry: lv(plan.entry), stop: lv(plan.stop), target: lv(plan.target), note: T.scalpNote };
        if (mode === 'algo') {
          dec.verdict = final.verdict;
          dec.sizing = T.pmSizing;
        }
        if (an.aiNote) dec.aiNote = an.aiNote;
        if (aiCtx) {
          dec.context = { inputs: aiCtx.inputs, regime: aiCtx.regime ? aiCtx.regime.label : null,
            account: aiCtx.account ? { balance: aiCtx.account.balance, openCount: aiCtx.account.open.length } : null,
            lessons: aiCtx.lessons ? aiCtx.lessons.slice(0, 2) : null };
        }
        dec.rr = gate.rr;
        dec.riskOk = gate.ok;
        dec.riskReasons = gate.reasons.slice();
        dec.liq = gate.liq;
        if (sizing) dec.riskSizing = sizing;
        // 상단 차트용 — 실제로 들어가는 판정의 숫자 레벨, 그리고 규칙이 본 6시간 박스(관망일 때 "돌파 대기"선)
        dec.plan = final.side
          ? { side: final.side, entry: plan.entry, target: plan.target, stop: plan.stop, liq: plan.liq, lev, at: now() }
          : null;
        // 분석이 끝난 뒤에도 사무실이 이어서 이야기할 재료(VIBE 등)
        dec.mood = { rsi: an.sig.rsi, fng: snap.fng || null, fundingPct: Number.isFinite(snap.fundingPct) ? snap.fundingPct : null };
        const k5 = snap.k5;
        const box = an.sig.box;
        const n = k5.length;
        dec.rule = {
          trend: an.sig.trend, candidate: plan.candidate, trigger: plan.trigger, reason: plan.reason, trial: !!plan.trial,
          blocked: gate.downgrade ? 'LIQ' : final.verdict === 'REJECT' ? 'PM' : null,
          // 박스 = 마지막 봉 직전 72개 — 끝 시각은 그 마지막 봉이 닫히는 때
          box: box && n > box.bars + 2
            ? { high: box.high, low: box.low, from: k5[n - 1 - box.bars].t, to: k5[n - 2].t + (k5[n - 2].t - k5[n - 3].t) }
            : null,
        };
        emit(dec);
        log(`>>> 최종 판정: ${final.action} (${plan.confidence}%)${final.verdict ? ` · PM ${final.verdict}` : ''}`, 'stage');

        // ✂️ 분석과 진입 분리 — 여기서는 '진입 준비'만 하고 포지션은 열지 않는다.
        if (final.side && kiosk) {
          pendingEntry = null;
          log('> 소개 화면 자동 시연 — 모의 포지션은 열지 않습니다');
          emit({ type: 'entry:ready', ready: false, reason: 'DEMO' });
        } else if (final.side && gate.ok && deps.paper) {
          pendingEntry = {
            token: ++pendingSeq,
            symbol: sym.symbol, display: sym.display, side: final.side, mode,
            entry: plan.entry, stop: plan.stop, target: plan.target, liq: plan.liq,
            leverage: lev, confidence: plan.confidence, rr: gate.rr, axis: snap.axis, marginPct: final.marginPct,
            at: now(),
          };
          log('> 진입 준비 완료 — 자동으로 들어가지 않습니다. 원하시면 판정 패널의 🚀 모의 진입을 누르세요');
          emit({
            type: 'entry:ready', ready: true, token: pendingEntry.token,
            symbol: sym.symbol, display: sym.display, side: final.side, mode,
            entry: plan.entry, stop: plan.stop, target: plan.target, liq: plan.liq,
            leverage: lev, confidence: plan.confidence, rr: gate.rr, marginPct: final.marginPct,
          });
        } else if (final.side) {
          pendingEntry = null;
          log(`> 진입하지 않습니다 — ${!deps.paper ? '모의 계좌를 쓸 수 없습니다' : '리스크 게이트를 통과하지 못했습니다'}`);
          emit({ type: 'entry:ready', ready: false, reason: !deps.paper ? 'NOPAPER' : 'GATE' });
        } else {
          pendingEntry = null;
          log('> 관망 — 이번 판정으로는 포지션을 열지 않습니다');
          emit({ type: 'entry:ready', ready: false, reason: 'HOLD' });
        }
      } catch (e) {
        fail(e && e.message ? e.message : String(e));
      } finally {
        emit({ type: 'run:end' });
        running = false;
        // 분석 중에 배율을 바꿨다면 방금 준비한 진입 계획을 새 배율로 다시 계산한다
        const q = queuedLev;
        queuedLev = null;
        if (q && q !== lev) retuneLev(q);
      }
    }

    // 판정 패널의 '🚀 모의 진입' — 사용자가 눌렀을 때만 지금 준비된 계획으로 모의 포지션을 연다.
    function enterPending(opts = {}) {
      if (!pendingEntry) return { ok: false, code: 'NONE', error: '진입할 판정이 없습니다 — ◆ 정밀 분석을 먼저 눌러 주세요' };
      if (!deps.paper) return { ok: false, code: 'NOPAPER', error: '모의 계좌를 쓸 수 없습니다' };
      if (opts.token != null && String(opts.token) !== String(pendingEntry.token)) {
        return { ok: false, code: 'STALE', error: '판정이 바뀌었습니다 — 다시 분석한 뒤 눌러 주세요' };
      }
      const p = pendingEntry;
      const res = deps.paper.open({
        symbol: p.symbol, display: p.display, side: p.side, mode: p.mode, entry: p.entry, stop: p.stop,
        target: p.target, liq: p.liq, leverage: p.leverage, confidence: p.confidence, rr: p.rr,
        axis: p.axis, marginPct: p.marginPct,
      });
      if (!res.position) {
        // 이미 보유·동시 보유 한도·잔고 부족 — 한 번 쓴 계획은 버리고 화면 버튼도 내린다
        pendingEntry = null;
        log(`> 모의 진입 보류 — ${res.skipped}`);
        emit({ type: 'entry:ready', ready: false, reason: 'SKIPPED', message: res.skipped });
        return { ok: false, code: 'SKIPPED', error: res.skipped };
      }
      pendingEntry = null;
      log(`> 🚀 모의 진입(직접 누름): ${p.display} ${p.side} ${p.leverage}x @ ${Script.fmtPrice(p.entry)} · 증거금 ${res.position.margin.toFixed(2)} USDT · 청산가 ${Script.fmtPrice(p.liq)}`);
      emit({ type: 'position', action: 'open', position: res.position });
      emit({ type: 'entry:ready', ready: false, reason: 'ENTERED' });
      return { ok: true, position: res.position };
    }

    // 🎚️ 배율 변경 — 판정은 그대로 두고, 대기 중인 진입 계획의 진입·손절·목표·청산가와 리스크 게이트를
    //   새 배율로 다시 계산한다. 2026-11-20 홍원 님 요청(화면 숫자와 실제 진입이 어긋나지 않게).
    function retuneLev(v) {
      const L = Brain.clampLev(v);
      if (running) { queuedLev = L; return { ok: false, code: 'RUNNING', lev: L }; }
      if (!pendingEntry) return { ok: false, code: 'NONE', lev: L };
      const p = pendingEntry;
      if (p.leverage === L) return { ok: false, code: 'SAME', lev: L };
      const lv = Brain.levels(p.side, p.entry, L);
      const gate = Brain.riskGate({ side: p.side, lev: L, entry: lv.entry, stop: lv.stop, target: lv.target, liq: lv.liq });
      const bal = deps.paper ? deps.paper.balance() : null;
      const sizing = bal != null
        ? { marginRequired: r2((bal * p.marginPct) / 100), notional: r2((bal * p.marginPct * L) / 100), notionalPctOfAccount: p.marginPct * L }
        : null;
      const out = {
        type: 'levels', side: p.side, leverage: L, symbol: p.symbol, display: p.display,
        entry: Script.fmtPrice(lv.entry), stop: Script.fmtPrice(lv.stop), target: Script.fmtPrice(lv.target),
        liq: lv.liq, rr: gate.rr, ok: gate.ok, reasons: gate.reasons.slice(), stopBeyondLiq: gate.stopBeyondLiq, sizing,
      };
      if (!gate.ok) {
        // 새 배율에서는 손절이 청산가 밖 — 계획을 접고 버튼도 내린다(다시 분석해 새로 받아야 한다)
        pendingEntry = null;
        log(`> 🔁 배율 ${L}배 — ${gate.reasons[0]}`);
        emit(out);
        emit({ type: 'entry:ready', ready: false, reason: 'LEV_GATE', message: gate.reasons[0] });
        return { ok: false, code: 'GATE', lev: L, error: gate.reasons[0] };
      }
      pendingEntry = { ...p, ...lv, leverage: L, rr: gate.rr };
      log(`> 🔁 배율 ${L}배로 다시 계산 — 진입 ${Script.fmtPrice(lv.entry)} · 손절 ${Script.fmtPrice(lv.stop)} · 청산가 ${Script.fmtPrice(lv.liq)}`);
      emit(out);
      emit({
        type: 'entry:ready', ready: true, token: pendingEntry.token,
        symbol: p.symbol, display: p.display, side: p.side, mode: p.mode,
        entry: lv.entry, stop: lv.stop, target: lv.target, liq: lv.liq,
        leverage: L, confidence: p.confidence, rr: gate.rr, marginPct: p.marginPct,
      });
      return { ok: true, code: 'RETUNED', lev: L, liq: lv.liq, rr: gate.rr };
    }

    return { run, enterPending, retuneLev, getPending: () => (pendingEntry ? { ...pendingEntry } : null), isRunning: () => running };
  }

  return { THINK, createBus, createEngine };
});
