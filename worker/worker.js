const H={'Access-Control-Allow-Origin':'*','Access-Control-Allow-Methods':'GET,OPTIONS','Content-Type':'application/json;charset=utf-8'},UA='Mozilla/5.0 Chrome/126.0.0.0',iso=(d=new Date())=>new Date(d.getTime()+324e5).toISOString().slice(0,19)+'+09:00',r=(v,n=4)=>Number.isFinite(v)?Math.round(v*10**n)/10**n:null;const ds=(a,n=36)=>{const c=a.filter(Number.isFinite);if(c.length<=n)return c.map(v=>r(v));const s=(c.length-1)/(n-1),o=Array.from({length:n},(_,i)=>c[Math.round(i*s)]);o[n-1]=c.at(-1);return o.map(v=>r(v))};async function yh(s){const q=await fetch(`https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(s)}?range=1d&interval=5m&includePrePost=true`,{headers:{'User-Agent':UA}});if(!q.ok)throw 0;const j=await q.json(),m=j?.chart?.result?.[0]?.meta||{},c=j?.chart?.result?.[0]?.indicators?.quote?.[0]?.close||[],p=Number.isFinite(+m.regularMarketPrice)?+m.regularMarketPrice:+c.filter(Number.isFinite).at(-1),pv=+(m.chartPreviousClose??m.previousClose);if(!Number.isFinite(p))throw 0;return{price:r(p),prevClose:r(pv),changePct:pv?r((p/pv-1)*100,3):null,spark:ds(c),at:iso(),source:'yahoo'}}async function tv(s,k){const q=await fetch(`https://scanner.tradingview.com/${k}/scan`,{method:'POST',headers:{'User-Agent':UA,'Content-Type':'application/json'},body:JSON.stringify({symbols:{tickers:[s],query:{types:[]}},columns:['close','change','prev_close_price']})});if(!q.ok)return null;const d=(await q.json())?.data?.[0]?.d;if(!d)return null;const[p,c,pv]=d.map(Number);return{price:r(p),prevClose:r(pv),changePct:r(c,3),spark:[],at:iso(),source:'tradingview'}}const gm=(y,t,k)=>yh(y).catch(()=>tv(t,k));const KM={'FOMC Meeting Minutes':'FOMC 의사록','FOMC Statement':'FOMC 성명서','Federal Funds Rate':'미 기준금리','Unemployment Claims':'신규 실업수당 청구건수','Philly Fed Manufacturing Index':'필라델피아 연은 제조업지수','ISM Manufacturing PMI':'ISM 제조업 PMI','ISM Services PMI':'ISM 서비스업 PMI','Non-Farm Employment Change':'비농업 고용자수','ADP Non-Farm Employment Change':'ADP 비농업 고용자수','Unemployment Rate':'실업률'};const ko=s=>KM[s]||s.replace(/^FOMC Member (\w+) Speaks$/,'FOMC 위원 $1 연설').replace(/^Fed Chair(?:man)? (\w+) Speaks$/,'연준 의장 $1 연설').replace(/m\/m/g,'(전월대비)').replace(/y\/y/g,'(전년대비)');async function gl(){const[nq,es,dxy,fx]=await Promise.all([gm('NQ=F','CME_MINI:NQ1!','america'),gm('ES=F','CME_MINI:ES1!','america'),gm('DX-Y.NYB','TVC:DXY','cfd'),gm('KRW=X','FX_IDC:USDKRW','forex')]);return{updatedAt:iso(),source:'cloudflare-worker',markets:{...(nq&&{nq}),...(es&&{es}),...(dxy&&{dxy}),...(fx&&{fx})}}}async function cal(){const q=await fetch('https://nfs.faireconomy.media/ff_calendar_thisweek.json',{headers:{'User-Agent':UA}});if(!q.ok)throw 0;const rows=await q.json(),ev=[];for(const x of rows){if(String(x.country).toUpperCase()!=='USD')continue;const im=String(x.impact).toLowerCase();if(im!=='high'&&im!=='medium')continue;const ms=Date.parse(x.date||'');if(!Number.isFinite(ms))continue;const s=new Date(ms+324e5).toISOString();ev.push({at:s.slice(0,19)+'+09:00',atMs:ms,date:s.slice(0,10),time:s.slice(11,16),impact:im,impactStars:im==='high'?3:2,title:ko((x.title||'').trim()),titleEn:x.title||'',forecast:String(x.forecast||''),previous:String(x.previous||'')})}ev.sort((a,b)=>a.atMs-b.atMs);return{updatedAt:iso(),source:'ForexFactory',timezone:'Asia/Seoul',events:ev}}

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
const AI_MAX_BODY = 24000;
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
const aiList = (v, n, max) => (Array.isArray(v) ? v.filter((x) => typeof x === 'string' && x.trim()).slice(0, n).map((x) => x.slice(0, max)) : []);

// 내 책·복기·국면·일정·고래·헤드라인 — 프론트가 보낸 것 중 필요한 값만 남긴다(문자열·숫자 제한).
function aiContext(cx) {
  if (!cx || typeof cx !== 'object') return null;
  const acc = cx.account && typeof cx.account === 'object' ? cx.account : null;
  const reg = cx.regime && typeof cx.regime === 'object' ? cx.regime : null;
  const inputs = cx.inputs && typeof cx.inputs === 'object' ? cx.inputs : null;
  const out = {
    account: acc ? {
      balance: aiNum(acc.balance, 0, 1e9), equity: aiNum(acc.equity, 0, 1e9),
      todayPnl: aiNum(acc.todayPnl, -1e9, 1e9), winRate: aiNum(acc.winRate, 0, 100),
      openCount: Array.isArray(acc.open) ? acc.open.length : 0,
      open: (Array.isArray(acc.open) ? acc.open : []).slice(0, 3).map((p) => ({
        symbol: aiStr(p && p.symbol, 16), side: aiStr(p && p.side, 6),
        entry: aiNum(p && p.entry, 0, 1e12), roePct: aiNum(p && p.roePct, -100000, 100000),
      })),
    } : null,
    lessons: aiList(cx.lessons, 4, 140),
    regime: reg ? {
      label: aiStr(reg.label, 20), atrPct: aiNum(reg.atrPct, 0, 100), atrRatio: aiNum(reg.atrRatio, 0, 10),
      atrPctile: aiNum(reg.atrPctile, 0, 100), trendStrength: aiNum(reg.trendStrength, 0, 100),
      pos48: aiNum(reg.pos48, 0, 100),
      fng: reg.fng && Number.isFinite(+reg.fng.value) ? { value: aiNum(reg.fng.value, 0, 100), label: aiStr(reg.fng.label, 24) } : null,
    } : null,
    calendar: (Array.isArray(cx.calendar) ? cx.calendar : []).slice(0, 3).map((e) => ({
      time: aiStr(e && e.time, 12), title: aiStr(e && e.title, 60), impact: aiStr(e && e.impact, 8),
    })),
    whales: (Array.isArray(cx.whales) ? cx.whales : []).slice(0, 3).map((w) => ({
      usd: aiNum(w && w.usd, 0, 1e12), from: aiStr(w && w.from, 16), to: aiStr(w && w.to, 16),
    })),
    headlines: aiList(cx.headlines, 3, 90),
    inputs: inputs ? Object.fromEntries(Object.entries(inputs).slice(0, 8).map(([k, v]) => [aiStr(k, 12), aiStr(String(v), 24)])) : null,
  };
  const empty = !out.account && !out.lessons.length && !out.regime && !out.calendar.length
    && !out.whales.length && !out.headlines.length;
  return empty ? null : out;
}

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
    // 2026-11-20 P0 — 화면에 이미 있는 데이터(내 계좌·복기·국면·일정·고래·헤드라인)
    context: aiContext(b.context),
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
    '[문맥 사용 — 중요]',
    '- context.account: 내 모의계좌 잔고·보유다. 이미 보유한 종목과 같은 방향의 신규 진입은 HOLD 쪽으로 기울이고, "이미 X 롱 보유 중"이라고 말해라.',
    '- context.lessons: 최근 청산 복기다. 같은 실수(추세 반대 진입·손절 반복)를 반복하지 말고 근거로 인용해라.',
    '- context.regime: 변동성·추세·공포탐욕 국면이다. 숫자를 인용해 판단 근거로 써라(예: "ATR%가 0.3, 백분위 12라 4R까지 갈 힘이 약하다").',
    '- context.calendar·headlines·whales: 이미 수집된 경제일정·헤드라인·고래 입출금이다. 있으면 인용하고, 없으면 없다고 말해라.',
    '- context.inputs는 어떤 자료를 실제로 읽었는지(읽음/없음)다. "없음"인 자료를 지어내지 마라.',
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
