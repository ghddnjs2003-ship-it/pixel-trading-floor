/* ── ✨ Gemini AI 13인 분석 (POST /ai-analyze) ───────────────────────────────
   이 파일은 pixel-global.current.js(기존 /global.json·/calendar.json 중계) 뒤에
   붙여 worker.js를 만든다(build.py). Gemini 키는 Worker Secret GEMINI_API_KEY에만
   있고 이 코드에는 없다.
   프론트엔드가 시세·지표·규칙 레벨을 보내면 Gemini가 13인 대사와 최종 판정을
   JSON으로 돌려준다. 진입·손절·목표·청산 숫자는 프론트의 규칙(brain.js)이 다시
   계산해 리스크 게이트와 화면 스키마가 깨지지 않게 한다.
   2026-11-20 홍원 님 요청: 픽셀 오피스 ◆ 정밀 분석을 진짜 Gemini로. */

const AI_MODELS = ['gemini-3.5-flash-lite', 'gemini-3.1-flash-lite'];
const AI_KEYS_ALGO = ['taro', 'diana', 'nova', 'vibe', 'bull1', 'bull2', 'bear1', 'bear2', 'ace', 'risky', 'safe', 'neutral', 'pm'];
const AI_KEYS_SCALP = ['taro', 'vibe', 'blitz', 'guard', 'ace'];
const AI_ORIGINS = new Set(['https://ghddnjs2003-ship-it.github.io']);
const AI_MAX_BODY = 12000;
const AI_RATE = { windowMs: 10 * 60 * 1000, max: 15, gapMs: 2500 };
const AI_HITS = new Map();

function aiCors(req) {
  const o = req.headers.get('Origin');
  if (!o) return { 'Access-Control-Allow-Origin': '*' };           // 브라우저 밖 호출
  if (AI_ORIGINS.has(o) || /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(o)) {
    return { 'Access-Control-Allow-Origin': o, Vary: 'Origin' };
  }
  return null;
}

function aiHeaders(req) {
  const c = aiCors(req) || {};
  return {
    ...c,
    'Access-Control-Allow-Methods': 'POST,GET,OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
  };
}

const aiJ = (req, status, obj) => new Response(JSON.stringify(obj), { status, headers: aiHeaders(req) });

// 남용 방지 — 한 사람(IP)이 10분에 15회, 최소 2.5초 간격. isolate 메모리 기준이라 완벽하진 않다.
function aiAllow(req) {
  const ip = req.headers.get('CF-Connecting-IP') || 'unknown';
  const now = Date.now();
  const hits = (AI_HITS.get(ip) || []).filter((t) => now - t < AI_RATE.windowMs);
  if (hits.length >= AI_RATE.max) return { ok: false, reason: 'per_ip', retryAfterSec: 60 };
  if (hits.length && now - hits[hits.length - 1] < AI_RATE.gapMs) return { ok: false, reason: 'too_fast', retryAfterSec: 3 };
  hits.push(now);
  AI_HITS.set(ip, hits);
  if (AI_HITS.size > 500) {
    for (const [k, v] of AI_HITS) if (!v.some((t) => now - t < AI_RATE.windowMs)) AI_HITS.delete(k);
  }
  return { ok: true };
}

const aiNum = (v, lo, hi) => (Number.isFinite(+v) && +v >= lo && +v <= hi ? Math.round(+v * 1e6) / 1e6 : null);
const aiStr = (v, max = 32) => (typeof v === 'string' ? v.slice(0, max) : '');

function aiLevels(x) {
  if (!x || typeof x !== 'object') return null;
  const out = { entry: aiNum(x.entry, 0, 1e9), stop: aiNum(x.stop, 0, 1e9), target: aiNum(x.target, 0, 1e9), liq: aiNum(x.liq, 0, 1e9) };
  return out.entry && out.stop ? out : null;
}

// 프론트가 보낸 스냅숏을 필요한 값만 골라 정리한다(문자열 길이·숫자 범위 제한)
function aiCtx(body) {
  const b = body && typeof body === 'object' ? body : {};
  const m = b.market && typeof b.market === 'object' ? b.market : {};
  const rr = b.rule && typeof b.rule === 'object' ? b.rule : {};
  const lv = rr.levels && typeof rr.levels === 'object' ? rr.levels : {};
  return {
    symbol: aiStr(m.symbol, 20),
    display: aiStr(m.display, 20),
    price: aiNum(m.price, 0, 1e12),
    change24hPct: aiNum(m.change24hPct, -100, 1000),
    trend: aiStr(m.trend, 8),
    ema50h: aiNum(m.ema50h, 0, 1e12),
    rsi14_5m: aiNum(m.rsi, 0, 100),
    box6h: m.box ? { high: aiNum(m.box.high, 0, 1e12), low: aiNum(m.box.low, 0, 1e12) } : null,
    breakout: aiStr(m.breakout, 8) || null,
    bbReentry: aiStr(m.bbReentry, 8) || null,
    flow: aiStr(m.flow, 16) || null,
    fundingPct: aiNum(m.fundingPct, -10, 10),
    fearGreed: m.fng ? { value: aiNum(m.fng.value, 0, 100), label: aiStr(m.fng.label, 24) } : null,
    topTraderLongShort: aiNum(m.topLS, 0, 100),
    oiChangePct: aiNum(m.oiChangePct, -100, 100),
    mode: rr.mode === 'algo' ? 'algo' : 'scalp',
    lev: aiNum(rr.lev, 1, 125),
    riskRule: {
      note: '증거금 기준 익절 +20% · 손절 −30%(손익분기 승률 60%). 100배에서 손절 −0.3%, 청산 −0.5%',
      breakEvenWinPct: aiNum(rr.breakEvenWinPct, 0, 100),
      candidate: aiStr(rr.candidate, 6) || null,
      ruleSaidHold: !!rr.hold,
      ruleReason: aiStr(rr.reason, 16),
      ruleConfidence: aiNum(rr.confidence, 0, 100),
      levels: { long: aiLevels(lv.long), short: aiLevels(lv.short) },
    },
  };
}

function aiSystem(c) {
  const keys = c.mode === 'algo' ? AI_KEYS_ALGO : AI_KEYS_SCALP;
  const schema = keys.map((k) => `"${k}":{"bubble":"…","report":"[ROLE] …"}`).join(',');
  const roster = c.mode === 'algo'
    ? 'TARO=기술적 분석, DIANA=펀더멘털·수급, NOVA=뉴스, VIBE=소셜·센티먼트, BULL=매수 논거(1·2라운드), BEAR=매도 논거(1·2라운드), ACE=수석 트레이더 결재, RISKY=공격적 리스크, NEUTRAL=중립 리스크, SAFE=보수적 리스크, PM=최종 승인'
    : 'TARO=기술적 분석, VIBE=소셜·센티먼트, BLITZ=스캘퍼, GUARD=리스크 관리, ACE=최종 판정';
  const keysKo = c.mode === 'algo'
    ? 'taro, diana, nova, vibe, bull1, bull2, bear1, bear2, ace, risky, safe, neutral, pm (13명 전원)'
    : 'taro, vibe, blitz, guard, ace (5명)';
  return [
    "너는 '픽셀 트레이딩 플로어'의 암호화폐 선물 트레이딩 팀이다. 아래 사용자 메시지의 JSON이 지금 이 순간의 시세·지표·규칙 판정 전부다.",
    '이 데이터에 있는 숫자만 근거로 회의하고 판정하라. 없는 시세·뉴스·수치를 지어내지 말고, 없으면 "데이터 없음"이라고 말한다.',
    `[팀] ${roster}`,
    `[발언 키] ${keysKo}`,
    '[리스크 규칙]',
    `- 이 데모는 레버리지 ${c.lev || 100}배 고정, 증거금 기준 익절 +20% · 손절 −30%(손익분기 승률 60%)다.`,
    '- 규칙이 계산한 levels(long/short)의 진입·손절·목표·청산 숫자는 실제 화면에 쓰이니 그대로 인용해도 된다.',
    '- 손절가가 청산가보다 바깥이면 절대 진입하지 말고 HOLD. 추세가 불명확(FLAT)하거나 RSI 추격 자리면 HOLD 쪽으로 기울여라.',
    '- 확신이 없으면 HOLD가 정답이다. 억지로 BUY/SELL을 만들지 마라.',
    '[말투]',
    '- bubble은 40자 이내 한 문장(사무실 말풍선), report는 2줄 이내·각 90자 이내로 "[ROLE] " 접두어로 시작.',
    '- 한국어 반말체로 짧고 단호하게. 서로의 의견을 실제로 주고받듯 써라(BULL과 BEAR는 2라운드에서 서로 반박).',
    '- 각자 자기 말만 한다. bubble·report 앞에 다른 직원 이름표(예: "BEAR1 반박:")를 붙이지 마라 — 화면이 그 사람 말풍선에 그대로 띄운다.',
    '[출력] JSON 하나만 출력한다. 설명·코드블록·머리말 금지.',
    `{"agents":{${schema}},"verdict":{"action":"BUY|SELL|HOLD","confidence":0-100,"rationale":"핵심 근거 한 문장","nextCondition":"관망일 때 다시 볼 조건 한 문장"}}`,
  ].join('\n');
}

function aiParse(txt) {
  if (typeof txt !== 'string') return null;
  let s = txt.trim();
  const fence = s.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fence) s = fence[1].trim();
  const a = s.indexOf('{'), b = s.lastIndexOf('}');
  if (a < 0 || b <= a) return null;
  try { return JSON.parse(s.slice(a, b + 1)); } catch (_) { return null; }
}

// Gemini 응답을 화면이 쓸 수 있는 모양으로 다듬는다. 형식이 어긋나면 null → 폴백
function aiClean(data, keys) {
  if (!data || typeof data !== 'object') return null;
  const v = data.verdict && typeof data.verdict === 'object' ? data.verdict : {};
  const act = String(v.action || '').toUpperCase();
  const action = act === 'BUY' || act === 'SELL' ? act : 'HOLD';
  const confidence = Math.max(0, Math.min(100, Math.round(Number(v.confidence) || 0)));
  const src = data.agents && typeof data.agents === 'object' ? data.agents : {};
  const agents = {};
  for (const k of keys) {
    const a = src[k];
    if (!a || typeof a !== 'object') continue;
    const bubble = typeof a.bubble === 'string' ? a.bubble.trim().slice(0, 120) : '';
    const report = typeof a.report === 'string' ? a.report.trim().slice(0, 400) : '';
    if (bubble || report) agents[k] = { bubble: bubble || report.slice(0, 60), report: report || bubble };
  }
  if (Object.keys(agents).length < Math.ceil(keys.length * 0.6)) return null;
  return {
    action,
    confidence,
    rationale: (typeof v.rationale === 'string' ? v.rationale : '').trim().slice(0, 300),
    nextCondition: (typeof v.nextCondition === 'string' ? v.nextCondition : '').trim().slice(0, 300),
    agents,
  };
}

async function aiRoute(req, env) {
  if (req.method === 'OPTIONS') return new Response(null, { status: 204, headers: aiHeaders(req) });
  if (!aiCors(req)) return aiJ(req, 403, { ok: false, error: 'origin_not_allowed' });
  if (req.method === 'GET') return aiJ(req, 200, { ok: true, service: 'ai-analyze', hasKey: !!(env && env.GEMINI_API_KEY), models: AI_MODELS });
  if (req.method !== 'POST') return aiJ(req, 405, { ok: false, error: 'method_not_allowed' });
  if (Number(req.headers.get('content-length') || 0) > AI_MAX_BODY) return aiJ(req, 413, { ok: false, error: 'body_too_large' });
  const allow = aiAllow(req);
  if (!allow.ok) return aiJ(req, 429, { ok: false, error: 'rate_limited', reason: allow.reason, retryAfterSec: allow.retryAfterSec });
  if (!env || !env.GEMINI_API_KEY) return aiJ(req, 503, { ok: false, error: 'no_key' });

  let body;
  try { body = await req.json(); } catch (_) { return aiJ(req, 400, { ok: false, error: 'bad_json' }); }
  const c = aiCtx(body);
  if (!c.price || !c.symbol) return aiJ(req, 400, { ok: false, error: 'bad_market' });

  const keys = c.mode === 'algo' ? AI_KEYS_ALGO : AI_KEYS_SCALP;
  const prompt = aiSystem(c);
  const t0 = Date.now();
  let lastErr = 'failed';
  for (const model of AI_MODELS) {
    try {
      const res = await fetch('https://generativelanguage.googleapis.com/v1beta/openai/chat/completions', {
        method: 'POST',
        headers: { Authorization: `Bearer ${env.GEMINI_API_KEY}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model,
          messages: [{ role: 'system', content: prompt }, { role: 'user', content: JSON.stringify(c) }],
          response_format: { type: 'json_object' },
          temperature: 0.7,
          max_tokens: 2048,
        }),
        signal: AbortSignal.timeout(22000),
      });
      if (!res.ok) {
        lastErr = `http_${res.status}`;
        if (res.status === 429) return aiJ(req, 429, { ok: false, error: 'quota', model });
        continue;
      }
      const j = await res.json();
      const out = aiClean(aiParse(j && j.choices && j.choices[0] && j.choices[0].message && j.choices[0].message.content), keys);
      if (out) return aiJ(req, 200, { ok: true, source: 'gemini', model, ms: Date.now() - t0, ...out });
      lastErr = 'bad_shape';
    } catch (e) {
      lastErr = String((e && e.name) || e).slice(0, 60);
    }
  }
  return aiJ(req, 502, { ok: false, error: lastErr });
}

export default {
  async fetch(req, env, ctx) {
    const u = new URL(req.url);
    if (u.pathname === '/ai-analyze' || u.pathname === '/ai-analyze/') return aiRoute(req, env);
    if (req.method === 'OPTIONS') return new Response(null, { status: 204, headers: H });
    const c = u.pathname.includes('calendar'), ttl = c ? 600 : 45, k = new Request(u.origin + (c ? '/calendar.json' : '/global.json'));
    const h = await caches.default.match(k);
    if (h) return h;
    try {
      const d = c ? await cal() : await gl(), rp = new Response(JSON.stringify(d), { headers: { ...H, 'Cache-Control': `public,max-age=${ttl}` } });
      ctx.waitUntil(caches.default.put(k, rp.clone()));
      return rp;
    } catch (e) {
      return new Response(JSON.stringify({ error: String(e) }), { status: 502, headers: H });
    }
  },
};
