/* PIXEL TRADING FLOOR — 판정 리포트 아카이브 (브라우저)
   ◆ 정밀 분석이 끝날 때마다 판정 한 장을 localStorage에 남기고(최근 10장),
   판정 패널의 [📄 리포트] 버튼에서 다시 보고 복사·저장(.md)할 수 있게 한다.
   실주문·계좌 정보는 담지 않는다 — 이 브라우저의 모의 판정 기록뿐이다. */

(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.LiteReports = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const KEY = 'pixel-lite-reports-v1';
  const MAX = 10;

  function storageOf(win) {
    try {
      const s = win.localStorage;
      s.setItem(KEY + ':probe', '1');
      s.removeItem(KEY + ':probe');
      return s;
    } catch (_) { return null; }
  }

  function read(win) {
    const s = storageOf(win);
    if (!s) return [];
    try {
      const v = JSON.parse(s.getItem(KEY) || '[]');
      return Array.isArray(v) ? v : [];
    } catch (_) { return []; }
  }

  function write(win, rows) {
    const s = storageOf(win);
    if (!s) return;
    try { s.setItem(KEY, JSON.stringify(rows.slice(0, MAX))); } catch (_) { /* 저장 실패는 무시 */ }
  }

  // decision 이벤트 → 한 장(리포트)
  function toReport(ev, extra) {
    const x = extra || {};
    const act = String(ev.action || 'HOLD').toUpperCase();
    return {
      ts: x.ts || Date.now(),
      symbol: x.symbol || (ev.display ? String(ev.display) : ''),
      action: act,
      confidence: Number(ev.confidence) || 0,
      source: x.source || '체험 규칙 B안',
      entry: ev.entry || '—', stop: ev.stop || '—', target: ev.target || '—',
      rr: ev.rr == null ? null : ev.rr,
      rationale: ev.rationale || '',
      aiNote: ev.aiNote || null,
      verdict: ev.verdict || null,
      risk: Array.isArray(ev.riskReasons) ? ev.riskReasons.slice(0, 4) : [],
      regime: ev.context && ev.context.regime ? ev.context.regime : null,
      inputs: ev.context && ev.context.inputs ? ev.context.inputs : null,
      account: ev.context && ev.context.account ? ev.context.account : null,
      lessons: ev.context && Array.isArray(ev.context.lessons) ? ev.context.lessons : null,
    };
  }

  function save(win, rep) {
    const rows = read(win);
    rows.unshift(rep);
    write(win, rows);
    return rows;
  }

  function stamp(ms) {
    const d = new Date(ms);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  function toMarkdown(rep) {
    const lines = [
      `# ${rep.symbol || 'BTC'} 판정 리포트 · ${rep.action} ${rep.confidence}%`,
      '',
      `- 시각: ${stamp(rep.ts)}`,
      `- 판정 출처: ${rep.source}`,
      `- 진입 ${rep.entry} · 손절 ${rep.stop} · 목표 ${rep.target}${rep.rr != null ? ` · R:R ${rep.rr}` : ''}`,
    ];
    if (rep.verdict) lines.push(`- PM 검토: ${rep.verdict}`);
    if (rep.regime) lines.push(`- 시장 국면: ${rep.regime}`);
    if (rep.inputs) {
      lines.push('- 입력 상태: ' + Object.entries(rep.inputs).map(([k, v]) => `${k} ${v}`).join(' · '));
    }
    if (rep.account) lines.push(`- 내 모의계좌: 잔고 ${rep.account.balance} USDT · 보유 ${rep.account.openCount}건`);
    if (rep.lessons && rep.lessons.length) lines.push(`- 최근 교훈: ${rep.lessons.join(' / ')}`);
    lines.push('', '## 판정 근거', rep.rationale || '—', '');
    if (rep.aiNote) lines.push(`> ${rep.aiNote}`, '');
    if (rep.risk.length) lines.push('## 리스크 게이트', ...rep.risk.map((r) => `- ${r}`), '');
    lines.push('---', '_모의 판정입니다. 주문은 나가지 않았고 투자 권유가 아닙니다._', '');
    return lines.join('\n');
  }

  function copy(win, text) {
    try {
      if (win.navigator && win.navigator.clipboard && win.navigator.clipboard.writeText) {
        win.navigator.clipboard.writeText(text).catch(() => {});
        return true;
      }
    } catch (_) { /* 폴백 */ }
    try {
      const ta = win.document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      win.document.body.appendChild(ta);
      ta.select();
      win.document.execCommand('copy');
      win.document.body.removeChild(ta);
      return true;
    } catch (_) { return false; }
  }

  function download(win, rep) {
    try {
      const blob = new win.Blob([toMarkdown(rep)], { type: 'text/markdown;charset=utf-8' });
      const url = win.URL.createObjectURL(blob);
      const a = win.document.createElement('a');
      const d = new Date(rep.ts);
      const p = (n) => String(n).padStart(2, '0');
      a.href = url;
      a.download = `report-${rep.symbol || 'BTC'}-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}.md`;
      win.document.body.appendChild(a);
      a.click();
      win.document.body.removeChild(a);
      win.setTimeout(() => win.URL.revokeObjectURL(url), 4000);
    } catch (_) { /* 저장 실패는 무시 */ }
  }

  // ---- 화면(모달) — index.html·CSS를 건드리지 않게 여기서 만든다
  function ensureModal(win) {
    const doc = win.document;
    if (doc.getElementById('lite-report-modal')) return doc.getElementById('lite-report-modal');
    const wrap = doc.createElement('div');
    wrap.id = 'lite-report-modal';
    wrap.style.cssText = 'position:fixed;inset:0;z-index:9999;display:none;align-items:center;justify-content:center;'
      + 'background:rgba(4,8,18,.72);padding:16px;font-family:inherit;';
    wrap.innerHTML = ''
      + '<div style="width:min(560px,100%);max-height:82vh;overflow:auto;background:#0d1424;color:#dbe6ff;'
      + 'border:1px solid #2b3a5c;border-radius:10px;padding:14px 16px;box-shadow:0 12px 40px rgba(0,0,0,.5)">'
      + '<div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px">'
      + '<strong style="font-size:14px;letter-spacing:.02em">📄 최근 판정 리포트</strong>'
      + '<button type="button" data-close="1" style="background:transparent;border:1px solid #2b3a5c;color:#9fb0d0;'
      + 'border-radius:6px;padding:2px 8px;cursor:pointer">닫기</button></div>'
      + '<div data-list="1" style="font-size:12px;line-height:1.55"></div>'
      + '<div style="margin-top:10px;color:#6f7f9e;font-size:11px">이 브라우저에만 저장됩니다(최근 10장). '
      + '모의 판정이며 주문은 나가지 않습니다.</div></div>';
    doc.body.appendChild(wrap);
    wrap.addEventListener('click', (e) => {
      if (e.target === wrap || (e.target && e.target.getAttribute && e.target.getAttribute('data-close'))) {
        wrap.style.display = 'none';
      }
    });
    return wrap;
  }

  function paintList(win, wrap) {
    const box = wrap.querySelector('[data-list="1"]');
    const rows = read(win);
    if (!rows.length) {
      box.innerHTML = '<div style="color:#8b9bbd">아직 저장된 리포트가 없습니다 — ◆ 정밀 분석을 실행하면 자동으로 남습니다.</div>';
      return;
    }
    box.innerHTML = rows.map((r, i) => {
      const col = r.action === 'BUY' ? '#3ad18c' : r.action === 'SELL' ? '#ff6b6b' : '#f5c451';
      return `<div style="border-top:1px solid #1e2a44;padding:8px 0">`
        + `<div style="display:flex;justify-content:space-between;gap:8px">`
        + `<span><b style="color:${col}">${r.action}</b> ${r.confidence}% · ${r.symbol || 'BTC'} `
        + `<span style="color:#7d8fb3">${stamp(r.ts)}</span></span>`
        + `<span style="color:#7d8fb3">${r.source || ''}</span></div>`
        + `<div style="color:#9fb0d0;margin:2px 0">${(r.rationale || '').slice(0, 140)}</div>`
        + `<div style="color:#7d8fb3">진입 ${r.entry} · 손절 ${r.stop} · 목표 ${r.target}`
        + (r.regime ? ` · 국면 ${r.regime}` : '') + `</div>`
        + `<div style="margin-top:4px;display:flex;gap:6px">`
        + `<button type="button" data-copy="${i}" style="background:#16213a;border:1px solid #2b3a5c;color:#cfe0ff;`
        + `border-radius:5px;padding:2px 8px;cursor:pointer;font-size:11px">복사</button>`
        + `<button type="button" data-save="${i}" style="background:#16213a;border:1px solid #2b3a5c;color:#cfe0ff;`
        + `border-radius:5px;padding:2px 8px;cursor:pointer;font-size:11px">.md 저장</button>`
        + `</div></div>`;
    }).join('');
    box.querySelectorAll('[data-copy]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const r = rows[Number(btn.getAttribute('data-copy'))];
        if (r && copy(win, toMarkdown(r))) btn.textContent = '복사됨';
      });
    });
    box.querySelectorAll('[data-save]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const r = rows[Number(btn.getAttribute('data-save'))];
        if (r) download(win, r);
      });
    });
  }

  function open(win) {
    const wrap = ensureModal(win);
    paintList(win, wrap);
    wrap.style.display = 'flex';
  }

  // 브라우저에서 자동 배선 — 판정이 나오면 저장하고, 버튼을 달아 둔다
  function install(win) {
    if (!win || !win.document) return null;
    const doc = win.document;
    let lastAi = null;
    if (win.LITE && win.LITE.bus) {
      win.LITE.bus.subscribe((ev) => {
        if (!ev) return;
        if (ev.type === 'ai') lastAi = ev.ok ? String(ev.model || 'gemini') : null;
        if (ev.type === 'run:start') lastAi = null;
        if (ev.type === 'decision') {
          const symbolEl = doc.querySelector('#symbol-input');
          const src = lastAi ? `✨ Gemini AI · ${lastAi.replace(/^gemini-/, '').replace(/-/g, ' ')}` : '체험 규칙 B안';
          save(win, toReport(ev, {
            symbol: symbolEl && symbolEl.value ? symbolEl.value.trim().toUpperCase() : (ev.display || ''),
            source: src,
          }));
        }
      });
    }
    const btn = doc.getElementById('dp-report-btn');
    if (btn) btn.addEventListener('click', () => open(win));
    const mbtn = doc.getElementById('mv-report-btn');
    if (mbtn) mbtn.addEventListener('click', () => open(win));   // 휴대폰 레이아웃
    return { open: () => open(win), list: () => read(win), save: (rep) => save(win, rep) };
  }

  if (typeof window !== 'undefined' && window.document && !window.LITE_NO_BOOT) {
    const boot = () => { if (window.LITE && window.LITE.bus) install(window); };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
    else window.setTimeout(boot, 0);
  }

  return { KEY, MAX, install, open, read, save, toReport, toMarkdown, copy, download };
});
