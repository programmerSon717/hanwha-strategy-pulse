"""📌 경전실 Top10 — 매일 KST 06:55.

☀️ Morning Brief(07:00)와 **선정 기준이 다르다.**
  Morning Brief : 모델이 매긴 strategic_score 순 + 추천 서칭 순서 티어
  경전실 Top10  : csfit.score() — 경전실이 실제 공유한 125건의 주제 분포 가중치

같은 기사가 양쪽에 다 나올 수 있다. 둘 다 Aggregation Topic 이라 중복 허용이다.
"""
import html
import re
from datetime import datetime, timedelta, timezone

import csfit
import topics
from config import settings

KST = timezone(timedelta(hours=9))

# brief_candidates 컬럼 순서
K_KEY, K_HEAD, K_URL, K_PRI, K_SEC, K_SCORE = 0, 1, 2, 3, 4, 5
K_ISKEY, K_CLUSTER, K_ENT, K_LEDE, K_WHY, K_SENT = 6, 7, 8, 9, 10, 11
K_ETYPE, K_ORIGIN, K_TEXT = 12, 13, 14   # origin_at=기사 발행시각, text=발행 원문

# 홍보성 기사를 다시 실을 수 있게 되기까지의 간격 (2026-10-01 사용자 지정: 이틀)
PR_INTERVAL_SEC = 2 * 24 * 3600


def due(store, now: float | None = None) -> tuple[bool, str]:
    """지금 발행해야 하는가. (해야하나, 이유)

    **GitHub 의 schedule 에 기대지 않기 위해 있다.** 2026-10-02 06:55 에
    cstop10.yml 의 cron(21:55 UTC)이 아예 발화하지 않아 그날 Top10 이 누락됐다.
    실행 이력 0건. 리포 문서에도 적혀 있듯 GitHub 무료 티어의 예약 실행은
    best-effort 다(간격 중앙값 42분, 최대 11시간). 하루 한 번짜리는 통째로
    건너뛸 수 있다.

    그래서 **상시 도는 bot.yml 루프**가 매 회차 이것을 물어보고 띄운다.
    cron 워크플로는 백업으로 남겨 둔다 — 둘 다 와도 여기서 한 번만 나간다.
    """
    now = now or datetime.now(KST).timestamp()
    t = datetime.fromtimestamp(now, KST)
    hh, _, mm = settings.cs_top10_time.partition(":")
    sched = t.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    day0 = t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    day1 = day0 + 24 * 3600

    if t < sched:
        return False, f"아직 {settings.cs_top10_time} 전"
    if store.agg_ran_on("cs_top10", day0, day1):
        return False, "오늘 이미 발행함"
    return True, "발행 시각 지남 · 오늘 미발행"


def window(now: float | None = None) -> tuple[float, float, str]:
    """전날 06:55 ~ 오늘 06:55."""
    now = now or datetime.now(KST).timestamp()
    until = now
    since = now - 24 * 3600
    label = datetime.fromtimestamp(now, KST).strftime("%Y.%m.%d %a")
    return since, until, label


def select(rows: list, count: int | None = None, store=None,
           now: float | None = None) -> list:
    """적합도 순 Top N. 같은 Event 는 대표기사 하나만.

    홍보성 기사(csfit.is_pr)는 **이틀에 한 건, 가장 큰 것 하나만** 넣는다.
    경전실도 홍보성을 공유하지만 11일 125건 중 3건 수준이다. 그대로 두면
    한화 가중치(40)가 커서 홍보성이 매일 상위를 먹는다.
    """
    import time
    count = count or settings.daily_brief_count
    now = now or time.time()

    best: dict[str, tuple] = {}
    for r in rows:
        cid = r[K_CLUSTER] or f"_solo:{r[K_KEY]}"
        s = csfit.score(r[K_HEAD] or "", r[K_ENT] or "", r[K_PRI] or "", r[K_SCORE])[0]
        cur = best.get(cid)
        if cur is None or s > cur[0]:
            best[cid] = (s, r)

    ranked = sorted(best.values(), key=lambda x: -x[0])

    # 홍보성과 일반을 가른다.
    pr = [(s, r) for s, r in ranked if csfit.is_pr(r[K_HEAD] or "")]
    normal = [(s, r) for s, r in ranked if not csfit.is_pr(r[K_HEAD] or "")]

    allow_pr = True
    if store is not None:
        last = store.last_pr_pick()
        if last and (now - last) < PR_INTERVAL_SEC:
            allow_pr = False
    pr_pick = pr[:1] if (allow_pr and pr) else []
    if pr and not allow_pr:
        print(f"[cstop10] 홍보성 {len(pr)}건 보류 — 직전 게재 후 이틀 미경과")

    cap = settings.daily_brief_max_per_entity
    used: dict[str, int] = {}
    picked, deferred = [], []
    for s, r in pr_pick + normal:
        ents = [e for e in (r[K_ENT] or "").split(",") if e]
        head = ents[0] if ents else (r[K_PRI] or "_")
        if used.get(head, 0) >= cap:
            deferred.append((s, r))
            continue
        used[head] = used.get(head, 0) + 1
        picked.append((s, r))
        if len(picked) >= count:
            break
    for s, r in deferred:
        if len(picked) >= count:
            break
        picked.append((s, r))

    # 홍보성은 맨 아래로 — 상단은 전략 사안이 차지해야 한다.
    picked.sort(key=lambda x: (csfit.is_pr(x[1][K_HEAD] or ""), -x[0]))
    return picked


def _ts(v) -> float | None:
    """epoch 로 쓸 수 있는 값이면 float, 아니면 None. DB 에 TEXT 로 들어온 행이 있다."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


TG_LIMIT = 4096          # 텔레그램 한 메시지 상한. **보이는 텍스트** 기준이다
SAFE_LIMIT = 4000        # 여유분 96자

# 기사와 기사 사이. 섹션 사이가 한 줄이라 기사 경계는 더 벌려야 구분된다.
ITEM_GAP = "\n\n━━━━━\n\n"


def visible_len(s: str) -> int:
    """텔레그램이 세는 길이 — HTML 태그는 빼고 센다."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", s)))


def _sections(text: str) -> dict:
    """발행 원문에서 섹션을 뜯어낸다. 없으면 빈 값."""
    out = {"lede": "", "bullets": [], "why": "", "when": "", "source": ""}
    if not text:
        return out
    m = re.search(r"✅ <b>핵심</b>\n(.+?)(?:\n\n|$)", text, re.S)
    if m: out["lede"] = m.group(1).strip()
    m = re.search(r"<blockquote>(.*?)</blockquote>", text, re.S)
    if m:
        out["bullets"] = [b.strip(" •").strip()
                          for b in m.group(1).split("\n") if b.strip()]
    m = re.search(r"(?:🐧|💡 <b>Why it matters</b>\n)\s*(.+?)(?:\n\n|$)", text, re.S)
    if m: out["why"] = m.group(1).strip()
    m = re.search(r"🕒 (.+?)(?:\n|$)", text)
    if m: out["when"] = m.group(1).strip()
    m = re.search(r"</a>\s*-\s*(.+?)(?:\n|$)", text)
    if m:
        src = m.group(1).strip()
        # "한화 금융계열사(비즈니스포스트)" 처럼 내부 수집기 이름이 앞에 붙는다.
        # 독자에게 필요한 건 매체명뿐이다.
        mm = re.match(r"^.+?\((.+)\)$", src)
        out["source"] = (mm.group(1) if mm else src).strip()
    return out


def _cut(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def render_item(r, *, lede=120, bullets=3, blen=58, why=130) -> str:
    """기사 1건. 순위 번호는 붙이지 않는다(2026-10-01 사용자 지정).

    길이 상한을 인자로 받는다 — 한 판에 안 들어가면 호출부가 조여서 다시 부른다.
    """
    e = html.escape
    sec = _sections(r[K_TEXT] if len(r) > K_TEXT else "")
    cat = topics.display_name(r[K_PRI] or "") or ""
    tag = " · 홍보" if csfit.is_pr(r[K_HEAD] or "") else ""

    # 탭 이름을 그대로 붙인다(아이콘 포함). 어느 카테고리인지 한눈에 보여야 한다.
    parts = [f"<b>[{e(cat)}]</b>{tag}",
             f'<a href="{e(r[K_URL] or "")}"><b>{e(r[K_HEAD] or "")}</b></a>']

    ld = sec["lede"] or (r[K_LEDE] or "")
    if ld:
        parts.append(f"✅ {e(_cut(ld, lede))}")

    bs = sec["bullets"][:bullets]
    if bs:
        parts.append("<blockquote>"
                     + "\n".join(f"• {e(_cut(b, blen))}" for b in bs)
                     + "</blockquote>")

    wh = sec["why"] or (r[K_WHY] or "")
    if wh:
        parts.append(f"🐧 {e(_cut(wh, why))}")

    when = sec["when"]
    if not when:
        ts = _ts(r[K_ORIGIN] if len(r) > K_ORIGIN else None) or _ts(r[K_SENT])
        when = f"{datetime.fromtimestamp(ts, KST):%Y-%m-%d %H:%M} KST" if ts else ""
    foot = " · ".join(x for x in (when, sec["source"]) if x)
    if foot:
        parts.append(f"🕒 {e(foot)}")
    # 섹션 사이는 한 줄 띄운다 (2026-10-01 사용자 지정).
    # 빈 줄도 텔레그램 길이에 포함되므로 SAFE_LIMIT 여유를 그만큼 잡아 둬야 한다.
    return "\n\n".join(parts)


# 한 판에 안 들어갈 때 차례로 조여 보는 단계. 위에서부터 시도한다.
TIGHTEN = [
    dict(lede=120, bullets=3, blen=58, why=130),
    dict(lede=100, bullets=3, blen=50, why=110),
    dict(lede=90,  bullets=2, blen=48, why=95),
    dict(lede=80,  bullets=2, blen=42, why=80),
    dict(lede=70,  bullets=0, blen=0,  why=70),
]


def render_all(picked: list, label: str) -> list[str]:
    """**한 판**으로 만든다. 상한을 넘으면 단계적으로 조이고,
    그래도 안 되면 그때만 나눈다."""
    e = html.escape
    n_pr = sum(1 for _, r in picked if csfit.is_pr(r[K_HEAD] or ""))
    head = f"📌 <b>{e(settings.bot_name)} | 경전실 Top10</b>\n{e(label)}"
    if n_pr:
        head += f"  ·  홍보 {n_pr}건 포함"

    for opt in TIGHTEN:
        body = ITEM_GAP.join(render_item(r, **opt) for _, r in picked)
        msg = head + ITEM_GAP + body
        if visible_len(msg) <= SAFE_LIMIT:
            return [msg]

    # 최대로 조여도 안 들어가면 나눈다 (기사 경계에서만).
    opt = TIGHTEN[-1]
    msgs, cur = [], head
    for _, r in picked:
        item = render_item(r, **opt)
        cand = cur + ITEM_GAP + item
        if visible_len(cand) > SAFE_LIMIT and cur != head:
            msgs.append(cur)
            cur = item
        else:
            cur = cand
    msgs.append(cur)
    return msgs


async def run(client, store, dry_run: bool | None = None) -> int | None:
    import asyncio
    import publisher
    dry = settings.dry_run if dry_run is None else dry_run
    since, until, label = window()
    rows = store.brief_candidates(since, until, settings.discard_threshold)
    picked = select(rows, store=store)
    span = (f"{datetime.fromtimestamp(since, KST):%m-%d %H:%M}"
            f" ~ {datetime.fromtimestamp(until, KST):%m-%d %H:%M}")
    print(f"[cstop10] 구간 {span} · 후보 {len(rows)}건 → 선정 {len(picked)}건")
    if not picked:
        print("[cstop10] 후보 없음 — 게시하지 않음")
        return None

    msgs = render_all(picked, label)
    print(f"[cstop10] 메시지 {len(msgs)}건 "
          f"(보이는 길이 {[visible_len(m) for m in msgs]})")

    if dry:
        for m in msgs:
            print("─" * 60)
            print(m)
        print("─" * 60)
        print("[cstop10] DRY_RUN — 발행하지 않았습니다")
        return None

    thread = topics.thread_id_for("cs_top10")
    first = None
    for i, m in enumerate(msgs):
        mid = await publisher.send_raw(client, m, thread)
        # 발행분을 기록해 둔다 — 나중에 이 메시지만 골라 지울 수 있게.
        store.record_agg_message("cs_top10", until + i, mid)
        first = first or mid
        if i < len(msgs) - 1:
            await asyncio.sleep(0.6)
    if any(csfit.is_pr(r[K_HEAD] or "") for _, r in picked):
        store.record_pr_pick(until, first)
        print("[cstop10] 홍보성 1건 게재 — 이틀간 보류")
    return first
