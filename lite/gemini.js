/* PIXEL TRADING FLOOR — ✨ Gemini AI 13인 분석 클라이언트
   Cloudflare Worker(POST /ai-analyze)가 Gemini 키를 숨긴 채 13인 대사·판정을 돌려준다.
   브라우저에는 키가 없다. 응답이 없거나 무료 한도(429)면 null을 돌려주고
   엔진은 체험 규칙(LiteBrain B안)으로 그대로 판정한다 — 화면이 멈추지 않는다.
   숫자(진입·손절·목표·청산)는 보내기 전 규칙(brain.js)이 계산해 함께 넘긴다. 2026-11-20 */

(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./brain.js'));
  else root.LiteGemini = factory(root.LiteBrain);
})(typeof self !== 'undefined' ? self : this, function (Brain) {
  'use strict';

  const ENDPOINT = 'https://pixel-global.jooyon124.workers.dev/ai-analyze';
  const TIMEOUT_MS = 20000;   // Worker가 Gemini 두 모델을 순서대로 시도한다
  const MIN_GAP_MS = 15000;   // 연타로 무료 한도를 태우지 않게
  const AGENT_KEYS = ['taro', 'diana', 'nova', 'vibe', 'bull1', 'bull2', 'bear1', 'bear2', 'ace', 'risky', 'safe', 'neutral', 'pm'];
  const SCALP_KEYS = ['taro', 'vibe', 'blitz', 'guard', 'ace'];

  const num = (v) => (Number.isFinite(+v) ? +v : null);

  function payload(an, snap) {
    const s = an.sig;
    return {
      market: {
        symbol: snap.symbol, display: snap.display, price: snap.price,
        change24hPct: num(snap.change24hPct), trend: s.trend, ema50h: s.ema50h,
        rsi: s.rsi, box: s.box ? { high: s.box.high, low: s.box.low } : null,
        breakout: s.breakout, bbReentry: s.bbReentry, flow: s.flow,
        fundingPct: num(snap.fundingPct),
        fng: snap.fng ? { value: snap.fng.value, label: snap.fng.label } : null,
        topLS: snap.topLS ? num(snap.topLS.ratio) : null,
        oiChangePct: s.oi ? num(s.oi.changePct) : null,
      },
      rule: {
        lev: an.lev, mode: an.mode, breakEvenWinPct: an.gate.breakEvenWinPct,
        candidate: an.plan.candidate, hold: !!an.plan.hold, reason: an.plan.reason,
        confidence: an.plan.confidence,
        levels: {
          long: Brain.levels('LONG', snap.price, an.lev),
          short: Brain.levels('SHORT', snap.price, an.lev),
        },
      },
    };
  }

  function create(opts = {}) {
    const endpoint = opts.endpoint || ENDPOINT;
    const fetchImpl = opts.fetch || (typeof fetch === 'function' ? fetch : null);
    let lastAt = 0;
    let inflight = false;

    async function analyze(an, snap, o = {}) {
      if (!fetchImpl) return { ok: false, code: 'no_fetch' };
      if (inflight) return { ok: false, code: 'busy' };
      const now = Date.now();
      if (now - lastAt < MIN_GAP_MS) return { ok: false, code: 'cooldown' };
      lastAt = now;
      inflight = true;
      const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
      const timer = ctrl ? setTimeout(() => { try { ctrl.abort(); } catch (_) { /* 무시 */ } }, TIMEOUT_MS) : null;
      try {
        const res = await fetchImpl(endpoint, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload(an, snap)),
          signal: ctrl ? ctrl.signal : undefined,
        });
        const body = await res.json().catch(() => null);
        if (!res.ok || !body || body.ok !== true) {
          return { ok: false, code: body && body.error ? String(body.error) : `http_${res.status}`, status: res.status };
        }
        const keys = (o.mode || an.mode) === 'algo' ? AGENT_KEYS : SCALP_KEYS;
        if (!body.agents || !body.action) return { ok: false, code: 'bad_shape' };
        return {
          ok: true, source: 'gemini', model: String(body.model || 'gemini'),
          ms: Number(body.ms) || 0, action: body.action,
          confidence: Math.max(0, Math.min(100, Math.round(Number(body.confidence) || 0))),
          rationale: String(body.rationale || ''), nextCondition: String(body.nextCondition || ''),
          agents: body.agents, keys,
        };
      } catch (e) {
        return { ok: false, code: e && e.name === 'AbortError' ? 'timeout' : 'network' };
      } finally {
        if (timer) clearTimeout(timer);
        inflight = false;
      }
    }

    return { analyze };
  }

  // 규칙이 만든 대사(base)에 Gemini 발언을 덮어쓴다 — 빠진 사람은 규칙 대사가 남는다
  function texts(base, ai, mode) {
    const A = (ai && ai.agents) || {};
    const out = Object.assign({}, base);
    const pick = (a, fallback) => (a && a.bubble ? { bubble: a.bubble, report: a.report || a.bubble } : fallback);
    const set = (key, id) => { if (A[id] && A[id].bubble) out[key] = { bubble: A[id].bubble, report: A[id].report || A[id].bubble }; };
    if (mode === 'algo') {
      set('taro', 'taro'); set('diana', 'diana'); set('nova', 'nova'); set('vibe', 'vibe');
      set('ace', 'ace'); set('risky', 'risky'); set('safe', 'safe'); set('neutral', 'neutral'); set('pm', 'pm');
      out.bull = [pick(A.bull1, base.bull[0]), pick(A.bull2, base.bull[1])];
      out.bear = [pick(A.bear1, base.bear[0]), pick(A.bear2, base.bear[1])];
    } else {
      set('taro', 'taro'); set('vibe', 'vibe'); set('ace', 'ace');
      set('blitz', 'blitz'); set('guard', 'guard');
    }
    const head = ai && ai.rationale ? `✨ Gemini AI · ${ai.rationale}` : '✨ Gemini AI 판정';
    const next = ai && ai.nextCondition ? ` · 다음 조건: ${ai.nextCondition}` : '';
    out.rationale = head + next;
    return out;
  }

  return { ENDPOINT, AGENT_KEYS, SCALP_KEYS, create, payload, texts };
});
