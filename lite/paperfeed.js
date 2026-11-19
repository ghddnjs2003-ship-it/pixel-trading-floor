/* PIXEL TRADING FLOOR — 🤖 30분봉 Gemini AI 모의매매 현황 (백엔드 paper_loop 기록 읽기)
   backend/paper_loop.py가 30분마다 남기는 data/paper_state.json · data/paper_log.jsonl을 읽어
   성적표 모달(#pro-modal) 안의 #pm-ai 자리에 그린다.
   · 서버 없는 정적 사이트라 저장소에 커밋된 최신 스냅숏을 읽는다(모의매매 루프가 30분마다 갱신·푸시)
   · Gemini API 키는 backend/.env에만 있다 — 이 파일도, 저장소도, 화면도 키를 모른다
   · 상태 파일에는 모의 계좌 숫자·판단 문장만 있다(주문·키·개인정보 없음) */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else (root.LITE = root.LITE || {}).paperWeb = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const STATE_URL = 'data/paper_state.json';
  const LOG_URL = 'data/paper_log.jsonl';
  const TTL = 60000;   // 새로고침 간격(모달을 열어 둔 동안)
  const ROWS = 8;      // 판단 로그 표시 줄 수
  const ACTION_KO = { HOLD: '관망', OPEN: '진입', CLOSE: '청산', SKIP: '건너뜀', REJECT: '거절' };
  const ACTION_CLS = { HOLD: 'hold', OPEN: 'open', CLOSE: 'close', SKIP: 'skip', REJECT: 'reject' };

  const S = { data: null, log: [], at: 0, busy: false, error: null };

  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const isNum = (v) => v != null && Number.isFinite(Number(v));
  const num = (v, dp = 0) => (isNum(v)
    ? Number(v).toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp }) : '—');
  const pct = (v, dp = 2) => (isNum(v) ? `${Number(v) > 0 ? '+' : ''}${Number(v).toFixed(dp)}%` : '—');

  // ISO(UTC) → KST 'MM-DD HH:mm' (브라우저 시간대와 무관하게 한국 시간으로)
  function kst(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    try {
      const parts = new Intl.DateTimeFormat('ko-KR', {
        timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit',
        hour: '2-digit', minute: '2-digit', hour12: false,
      }).formatToParts(d);
      const get = (type) => (parts.find((p) => p.type === type) || {}).value || '';
      return `${get('month')}-${get('day')} ${get('hour')}:${get('minute')}`;
    } catch (_) {
      return d.toISOString().slice(5, 16).replace('T', ' ');
    }
  }

  // '2026-11-20 07:00' → '11-20 07:00' (연도는 표에서 뺀다)
  function shortTime(text) {
    const s = String(text == null ? '' : text);
    return s.replace(/^\d{4}-(\d{2}-\d{2}\s+\d{2}:\d{2}).*$/, '$1');
  }

  function actionChip(action) {
    const key = String(action || '').toUpperCase();
    return `<span class="pm-ai-chip ${ACTION_CLS[key] || 'hold'}">${esc(ACTION_KO[key] || key || '—')}</span>`;
  }

  async function load() {
    if (S.busy) return;
    S.busy = true;
    try {
      const bust = `?t=${Date.now()}`;
      const [res, logRes] = await Promise.all([
        fetch(STATE_URL + bust, { cache: 'no-store' }),
        fetch(LOG_URL + bust, { cache: 'no-store' }),
      ]);
      if (!res.ok) throw new Error(`상태 파일을 못 받았습니다 (HTTP ${res.status})`);
      const data = await res.json();
      let log = [];
      if (logRes.ok) {
        const text = await logRes.text();
        log = text.split('\n').filter((line) => line.trim())
          .slice(-60)
          .map((line) => { try { return JSON.parse(line); } catch (_) { return null; } })
          .filter(Boolean)
          .reverse(); // 최신 스캔이 위로
      }
      S.data = data;
      S.log = log;
      S.at = Date.now();
      S.error = null;
    } catch (e) {
      S.error = e && e.message ? e.message : String(e);
    } finally {
      S.busy = false;
      render();
    }
  }

  function emptyBox(text, sub) {
    return `<div class="pm-ai-empty"><b>${esc(text)}</b>${sub ? `<span>${esc(sub)}</span>` : ''}</div>`;
  }

  function card(label, note, value, sub, cls) {
    return '<div class="pm-card pm-ai-card">'
      + `<span class="pm-card-lbl">${esc(label)}<i>${esc(note || '')}</i></span>`
      + `<b class="pm-card-val ${cls || ''}">${value}</b>`
      + `<span class="pm-card-sub">${sub}</span></div>`;
  }

  function render() {
    const host = document.getElementById('pm-ai-body');
    const sub = document.getElementById('pm-ai-sub');
    if (!host) return;

    const d = S.data;
    if (!d) {
      if (sub) sub.textContent = S.error ? '연결 안 됨' : '불러오는 중…';
      host.innerHTML = S.error
        ? emptyBox('백엔드 모의매매 상태를 아직 못 받았습니다', `${S.error} — 루프가 기록을 저장소에 올리면 자동으로 표시됩니다`)
        : emptyBox('백엔드 모의매매 기록을 불러오는 중…', '');
      return;
    }

    const stats = d.stats || {};
    const kill = d.killswitch || {};
    const last = d.last_scan || {};
    const pos = d.position || null;
    const equity = isNum(d.equity) ? Number(d.equity) : null;
    const start = isNum(d.initial_equity) ? Number(d.initial_equity) : 10000;
    const retPct = equity != null && start > 0 ? ((equity - start) / start) * 100 : null;
    const trades = Number(stats.trades) || 0;

    if (sub) {
      const when = kst(d.updated_at || (last && last.ts));
      sub.textContent = `최근 갱신 ${when} · ${num(d.scan_count)}회 스캔 · 30분마다 자동 갱신`;
    }

    const killOk = kill.tripped !== true && kill.ok !== false;
    const cards = [
      card('모의 자본', 'USDT', `${num(equity, 2)}`,
        `시작 <b>${num(start)}</b> → 현재 <b>${num(equity, 2)}</b> · ${pct(retPct)}`,
        retPct > 0 ? 'up' : retPct < 0 ? 'down' : ''),
      card('마지막 스캔', '30분봉 확정',
        `${actionChip(last.action)}`,
        `가격 <b>${num(last.price, 1)}</b> · 봉 <b>${esc(kst(last.bar))}</b>`,
        'pm-ai-val-inline'),
      card('모의 성적', trades ? `${stats.wins || 0}승 ${stats.losses || 0}패` : '기록 없음',
        trades && isNum(stats.win_rate_pct) ? `${Number(stats.win_rate_pct).toFixed(1)}%` : '—',
        trades
          ? `<b>${num(trades)}건</b> · PF <b>${isNum(stats.profit_factor) ? Number(stats.profit_factor).toFixed(2) : '—'}</b> · 평균 R <b>${isNum(stats.avg_r) ? Number(stats.avg_r).toFixed(2) : '—'}</b>`
          : '모의 진입이 체결되면 집계됩니다',
        isNum(stats.net_pnl) && stats.net_pnl > 0 ? 'up' : isNum(stats.net_pnl) && stats.net_pnl < 0 ? 'down' : ''),
      card('킬스위치', '일 손실 3% · 연속 3패',
        killOk ? '<span class="pm-ai-ok">정상</span>' : '<span class="pm-ai-warn">발동</span>',
        esc(killOk
          ? `오늘 ${num(kill.trades_today)}건 · 실현 ${num(kill.realized_pnl, 2)} USDT · 진입 가능`
          : `${kill.reason || '진입 차단'}${kill.tripped_until ? ` · ${kst(kill.tripped_until)}까지` : ''}`),
        'pm-ai-val-inline'),
    ];

    let posHtml = '';
    if (pos) {
      posHtml = '<div class="pm-ai-pos">'
        + `<span class="pm-ai-pos-tag">보유 중</span>`
        + `<b class="${pos.side === 'LONG' ? 'up' : 'down'}">${esc(pos.side || '—')}</b>`
        + `<span>진입 <b>${num(pos.entry, 1)}</b></span>`
        + `<span>손절 <b>${num(pos.stop, 1)}</b></span>`
        + `<span>목표 <b>${num(pos.target, 1)}</b></span>`
        + `<span>위험 <b>$${num(pos.risk_usd, 2)}</b></span>`
        + `<span>경과 <b>${num(pos.bars)}봉</b></span>`
        + `<span class="pm-ai-pos-note">AI ${esc(pos.ai || '—')}</span>`
        + '</div>';
    } else {
      posHtml = '<div class="pm-ai-pos empty"><span class="pm-ai-pos-tag">대기</span>'
        + '<span>지금은 열린 모의 포지션이 없습니다 — 신호가 조건을 다 넘기면 진입합니다</span></div>';
    }

    const rows = S.log.slice(0, ROWS).map((rec) => {
      const filter = rec.filter || {};
      const ai = filter.decision
        ? `<span class="pm-ai-ai ${String(filter.decision).toUpperCase() === 'PASS' ? 'pass' : 'reject'}">${esc(filter.decision)}</span>`
        : '<span class="pm-ai-ai">—</span>';
      const events = Array.isArray(rec.events) ? rec.events.filter(Boolean) : [];
      let note = events.join(' · ');
      if (!note && filter.reason) note = String(filter.reason);
      if (!note && rec.signal) note = `${rec.signal.kind || ''} ${rec.signal.label || ''}`.trim();
      if (!note) note = String(rec.action || '').toUpperCase() === 'HOLD' ? 'M1 신호 없음 — 진입 조건 미충족' : (rec.source ? `${String(rec.source).toUpperCase()} 캔들` : '');
      return '<tr>'
        + `<td>${esc(shortTime(rec.bar_kst || kst(rec.bar || rec.ts)))}</td>`
        + `<td>${num(rec.price, 1)}</td>`
        + `<td>${actionChip(rec.action)}</td>`
        + `<td>${ai}</td>`
        + `<td class="pm-ai-note">${esc(note)}</td>`
        + '</tr>';
    }).join('');

    const table = rows
      ? `<table class="pm-table pm-ai-table"><thead><tr><th>봉(30분)</th><th>가격</th><th>판단</th><th>Gemini AI</th><th>사유</th></tr></thead><tbody>${rows}</tbody></table>`
      : emptyBox('아직 판단 로그가 없습니다', '루프가 한 번 돌면 봉마다 판단(관망·진입·청산)이 여기에 쌓입니다');

    const rule = d.rule || {};
    const ruleLine = [
      `${esc(d.symbol || 'BTCUSDT')} ${num(d.interval)}분봉`,
      rule.name ? esc(rule.name) : '',
      Array.isArray(rule.kinds) && rule.kinds.length ? esc(rule.kinds.join('·')) : '',
      rule.session === 'asia' ? '아시아 세션 진입' : '',
      isNum(rule.stop_atr) ? `${num(rule.stop_atr, 1)} ATR 손절` : '',
      isNum(rule.target_r) ? `${num(rule.target_r, 1)}R 목표` : '',
      isNum(rule.max_bars) ? `${num(rule.max_bars)}봉 시간청산` : '',
    ].filter(Boolean).join(' · ');

    host.innerHTML = `<div class="pm-cards pm-ai-cards">${cards.join('')}</div>`
      + posHtml
      + `<div class="pm-ai-log">${table}</div>`
      + `<div class="pm-ai-foot">규칙: ${ruleLine}</div>`
      + `<div class="pm-ai-foot dim">backend/paper_loop.py가 남긴 기록을 저장소에서 그대로 읽습니다 · 모의 계좌(실주문 없음) · Gemini 키는 서버 .env에만 있습니다</div>`;
  }

  // 모달이 열려 있을 때만 새로고침한다(닫혀 있으면 네트워크를 쓰지 않는다)
  function visible() {
    const modal = document.getElementById('pro-modal');
    return !!modal && !modal.classList.contains('hidden');
  }

  function tick(force) {
    if (!document.getElementById('pm-ai-body')) return;
    if (!visible() && !force) return;
    if (!force && S.at && Date.now() - S.at < TTL - 1000) return;
    load();
  }

  function init() {
    // 성적표 버튼·닫기·새로고침 — 모달은 pro.js가 나중에 만들기 때문에 문서 단위로 받는다
    document.addEventListener('click', (event) => {
      const target = event.target;
      if (!target || !target.closest) return;
      if (target.closest('.pro-stats-btn')) { setTimeout(() => tick(true), 60); return; }
      if (target.closest('#pm-ai-reload')) { event.preventDefault(); tick(true); }
    });
    setInterval(() => tick(false), 15000);
    tick(true);
  }

  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
  }

  return { load, render, tick, init, STATE_URL, LOG_URL };
});
