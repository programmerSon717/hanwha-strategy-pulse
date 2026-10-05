/* Strategy Pulse Portal — 공통 런타임.
   레퍼런스(Xangle Portal)의 헤더·푸터·드롭다운·카드·페이지네이션을 그대로 옮겼다.
   정적 호스팅이라 서버가 없다. 모든 화면은 docs/data/*.json 을 읽어 그린다. */

export const DATA = './data';

/* 토픽별 고정 색. 썸네일 이미지가 없으므로 레퍼런스의 '보라 카드' 자리를
   토픽 색 그라데이션으로 채운다 — 같은 토픽은 항상 같은 색이어야 눈에 익는다. */
export const TOPIC_HUE = {
  hanwha_group:        ['#6D28D9', '#A855F7'],
  ma_governance:       ['#1E3A8A', '#3B82F6'],
  insurance_finance:   ['#065F46', '#10B981'],
  regulation_policy:   ['#9A3412', '#F59E0B'],
  competitors_bigtech: ['#0F172A', '#475569'],
  digital_newbiz:      ['#831843', '#EC4899'],
  global_finance:      ['#134E4A', '#14B8A6'],
  key_issues:          ['#7F1D1D', '#EF4444'],
  '':                  ['#4C1D95', '#8B5CF6'],
};
export const hue = t => TOPIC_HUE[t] || TOPIC_HUE[''];
export const grad = t => { const [a, b] = hue(t);
  return `background:linear-gradient(132deg,${a} 0%,${b} 100%)`; };

export const esc = s => String(s ?? '').replace(/[&<>"']/g,
  m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[m]));

export const fmtDate = s => s ? String(s).slice(0, 10).replace(/-/g, '.') : '';
export const fmtTime = s => s ? String(s).slice(11, 16) : '';
export const fmtFull = s => s ? `${fmtDate(s)} ${fmtTime(s)}` : '—';
export const num = n => (n ?? 0).toLocaleString('ko-KR');

export function ago(iso) {
  if (!iso) return '—';
  const m = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (m < 1) return '방금';
  if (m < 60) return `${m}분 전`;
  if (m < 1440) return `${Math.floor(m / 60)}시간 전`;
  return `${Math.floor(m / 1440)}일 전`;
}

export async function load(name) {
  const r = await fetch(`${DATA}/${name}.json`, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${name}.json ${r.status}`);
  return r.json();
}

/* 토픽 이름에서 선행 이모지를 뗀 글자. 아바타·칩에 쓴다.
   \W 로 자르면 안 된다 — JS 의 \w 는 ASCII 라서 한글까지 통째로 날아간다
   ("🏦 보험 · 금융" → ""). 그림문자와 변형 선택자만 정확히 집어 뗀다. */
export const plain = n => String(n || '')
  .replace(/^[\p{Extended_Pictographic}\uFE0F\u200D\s]+/u, '').trim();
export const icon = n => (String(n || '').match(/^\p{Extended_Pictographic}/u) || ['●'])[0];

const MARK = `<svg class="mk" viewBox="0 0 24 24" fill="none" aria-hidden="true">
  <path d="M4 4l7 8-7 8M20 4l-7 8 7 8" stroke="#7C3AED" stroke-width="3.1"
        stroke-linecap="round" stroke-linejoin="round"/></svg>`;

const NAV = [
  ['index.html',    '홈'],
  ['top10.html',    'Top10 발행'],
  ['articles.html', '수집 기사'],
  ['pipeline.html', '파이프라인'],
  ['draft.html',    '초안 · 스케줄'],
];

export function header(current) {
  const nav = NAV.map(([h, t]) =>
    `<a href="${h}"${h === current ? ' aria-current="page"' : ''}>${t}</a>`).join('');
  document.body.insertAdjacentHTML('afterbegin', `
  <header class="hd"><div class="wrap hd-in">
    <a class="logo" href="index.html">${MARK}Strategy <b>Pulse</b></a>
    <nav class="nav">${nav}</nav>
    <div class="hd-r">
      <label class="search">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="2"/>
          <path d="M20 20l-3.5-3.5" stroke="currentColor" stroke-width="2"
                stroke-linecap="round"/></svg>
        <input id="gq" placeholder="검색" aria-label="검색">
        <kbd>⌘K</kbd>
      </label>
      <button class="ghost" id="themeBtn" title="밝게/어둡게">◐</button>
    </div>
  </div></header>`);

  const q = document.getElementById('gq');
  addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); q.focus(); }
  });
  q.addEventListener('keydown', e => {
    if (e.key === 'Enter' && q.value.trim())
      location.href = `articles.html?q=${encodeURIComponent(q.value.trim())}`;
  });

  const btn = document.getElementById('themeBtn');
  const saved = (() => { try { return localStorage.getItem('sp-theme'); } catch { return null; } })();
  if (saved) document.documentElement.dataset.theme = saved;
  btn.onclick = () => {
    const cur = document.documentElement.dataset.theme
      || (matchMedia('(prefers-color-scheme:dark)').matches ? 'dark' : 'light');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('sp-theme', next); } catch { /* 사생활 보호 모드 */ }
  };
}

export function footer(meta) {
  const col = (h, ls) => `<div><h5>${h}</h5><ul>${
    ls.map(([a, t]) => `<li><a href="${a}">${t}</a></li>`).join('')}</ul></div>`;
  document.body.insertAdjacentHTML('beforeend', `
  <footer class="ft"><div class="wrap">
    <div class="ft-in">
      <div>
        <a class="logo" href="index.html">${MARK}Strategy <b>Pulse</b></a>
        <div class="about">
          금융·전략 뉴스 자동 수집·선별 파이프라인의 공개 대시보드입니다.<br>
          화면의 모든 수치는 봇이 매 회차 끝에 내보낸 상태 파일에서 옵니다.<br>
          ${meta ? `마지막 갱신 ${esc(fmtFull(meta))}` : ''}
        </div>
      </div>
      ${col('화면', [['top10.html', 'Top10 발행'], ['articles.html', '수집 기사'],
                     ['pipeline.html', '파이프라인'], ['draft.html', '초안 · 스케줄']])}
      ${col('데이터', [['data/summary.json', 'summary.json'],
                       ['data/top10.json', 'top10.json'],
                       ['data/articles.json', 'articles.json'],
                       ['data/pipeline.json', 'pipeline.json']])}
      ${col('기준', [['top10.html', '선정 기준'], ['pipeline.html', '제외 사유'],
                     ['draft.html', '발행 스케줄']])}
      ${col('운영', [['pipeline.html', '가동 상태'], ['draft.html', '초안 교체 이력']])}
    </div>
    <div class="ft-b">
      <span>© Strategy Pulse. 수집 기사의 저작권은 각 언론사에 있습니다.</span>
      <button class="ghost top-btn" onclick="scrollTo({top:0,behavior:'smooth'})">
        ↑ 위로 가기</button>
    </div>
  </div></footer>`);
}

/* 드롭다운 — 레퍼런스의 체크표시 선택행 그대로. */
export function select(el, items, value, onPick) {
  const label = () => (items.find(i => i.v === value) || items[0] || {}).t || '전체';
  el.classList.add('sel');
  el.innerHTML = `
    <button class="sel-btn" type="button">
      <span class="sel-t">${esc(label())}</span>
      <svg class="cv" width="13" height="13" viewBox="0 0 24 24" fill="none"
           aria-hidden="true"><path d="M6 9l6 6 6-6" stroke="currentColor"
           stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>
    </button>
    <div class="sel-menu" role="listbox">${items.map(i => `
      <button type="button" role="option" data-v="${esc(i.v)}"
        aria-selected="${i.v === value}">${esc(i.t)}
        ${i.v === value ? `<svg width="14" height="14" viewBox="0 0 24 24" fill="none">
          <path d="M5 13l4 4 10-10" stroke="currentColor" stroke-width="2.4"
          stroke-linecap="round" stroke-linejoin="round"/></svg>` : ''}</button>`).join('')}
    </div>`;
  el.querySelector('.sel-btn').onclick = e => {
    e.stopPropagation();
    document.querySelectorAll('.sel.on').forEach(s => s !== el && s.classList.remove('on'));
    el.classList.toggle('on');
  };
  el.querySelectorAll('.sel-menu button').forEach(b => b.onclick = e => {
    e.stopPropagation(); el.classList.remove('on');
    onPick(b.dataset.v);
  });
}
addEventListener('click', () =>
  document.querySelectorAll('.sel.on').forEach(s => s.classList.remove('on')));

/* 카드 — 레퍼런스의 리서치 카드. 썸네일은 토픽 색 + 제목. */
export function card(a, kind) {
  const nm = a.topic_name || '';
  return `<a class="card" href="${esc(a.url || '#')}" target="_blank" rel="noopener">
    <div class="thumb" style="${grad(a.topic)}">
      <span class="badge">${esc(kind || plain(nm) || '수집')}</span>
      <span class="t-main">${esc(a.title)}</span>
      <span class="t-sub">${esc(fmtDate(a.origin_at))}</span>
    </div>
    <h3>${esc(a.title)}</h3>
    <p class="sum">${esc(a.lede || a.why || '')}</p>
    <div class="tags">${[nm && plain(nm), ...(a.entities || []).slice(0, 2)]
      .filter(Boolean).map(t => `<span class="tag">${esc(t)}</span>`).join('')}</div>
    <div class="foot">
      <span class="who"><span class="av" style="${grad(a.topic)}">${icon(nm)}</span>
        ${esc(plain(nm) || '미분류')}</span>
      <time>${esc(fmtDate(a.origin_at))}</time>
    </div>
  </a>`;
}

export function listRow(a, kind) {
  const nm = a.topic_name || '';
  return `<a class="row-item" href="${esc(a.url || '#')}" target="_blank" rel="noopener">
    <div class="thumb" style="${grad(a.topic)}">
      <span class="badge">${esc(kind || plain(nm) || '수집')}</span>
      <span class="t-main">${esc(a.title)}</span>
    </div>
    <div>
      <time>${esc(fmtFull(a.origin_at))}</time>
      <h3>${esc(a.title)}</h3>
      <p class="sum">${esc(a.lede || a.why || '')}</p>
      <div class="tags">${[nm && plain(nm), ...(a.entities || []).slice(0, 3)]
        .filter(Boolean).map(t => `<span class="tag">${esc(t)}</span>`).join('')}</div>
      <div class="foot"><span class="who">
        <span class="av" style="${grad(a.topic)}">${icon(nm)}</span>
        ${esc(plain(nm) || '미분류')}</span></div>
    </div>
  </a>`;
}

/* 페이지네이션 — 레퍼런스와 같은 모양(‹ 1 2 3 ›). */
export function pager(host, page, pages, go) {
  if (pages <= 1) { host.innerHTML = ''; return; }
  const win = [], from = Math.max(1, Math.min(page - 2, pages - 4)),
        to = Math.min(pages, from + 4);
  for (let i = from; i <= to; i++) win.push(i);
  host.innerHTML = `<div class="chips" style="justify-content:center;margin:34px 0 0">
    <button ${page === 1 ? 'disabled' : ''} data-p="${page - 1}">‹</button>
    ${win.map(i => `<button data-p="${i}" aria-pressed="${i === page}">${i}</button>`).join('')}
    <button ${page === pages ? 'disabled' : ''} data-p="${page + 1}">›</button></div>`;
  host.querySelectorAll('button[data-p]').forEach(b => b.onclick = () => {
    const p = +b.dataset.p;
    if (p >= 1 && p <= pages) { go(p); scrollTo({ top: 0, behavior: 'smooth' }); }
  });
}
