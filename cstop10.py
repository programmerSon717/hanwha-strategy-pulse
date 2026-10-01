"""📌 경전실 Top10 — 매일 KST 06:55.

☀️ Morning Brief(07:00)와 **선정 기준이 다르다.**
  Morning Brief : 모델이 매긴 strategic_score 순 + 추천 서칭 순서 티어
  경전실 Top10  : csfit.score() — 경전실이 실제 공유한 125건의 주제 분포 가중치

같은 기사가 양쪽에 다 나올 수 있다. 둘 다 Aggregation Topic 이라 중복 허용이다.
"""
import html
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


def render_header(picked: list, label: str) -> str:
    e = html.escape
    n_pr = sum(1 for _, r in picked if csfit.is_pr(r[K_HEAD] or ""))
    line = f"{len(picked)}건"
    if n_pr:
        line += f" (홍보 {n_pr}건 포함)"
    return "\n".join([
        f"📌 <b>{e(settings.bot_name)} | 경전실 Top10</b>", "",
        e(label), "", e(line),
    ])


def render_item(rank: int, score: int, r, picked_len: int) -> str:
    """개별 기사 1건. **발행 당시 원문을 그대로 재사용한다.**

    원문(published.text)에는 ✅ 핵심 · 📂 주요 내용(불릿) · 🐧 · 🕒 · 기사 원문 ·
    해시태그가 이미 다 들어 있다. 다시 조립하면 실시간 탭과 양식이 어긋난다.
    여기서는 맨 윗줄(봇 이름)만 떼고 순위 줄로 바꿔 끼운다.
    """
    e = html.escape
    body = (r[K_TEXT] or "").strip() if len(r) > K_TEXT else ""

    tag = " · 홍보" if csfit.is_pr(r[K_HEAD] or "") else ""
    head = (f"<b>{rank}/{picked_len}. "
            f"[{e(topics.display_name(r[K_PRI] or ''))}]{tag}</b>")

    if body:
        # 첫 줄은 봇 이름이다. 떼어낸다.
        lines = body.split("\n")
        if lines and settings.bot_name in lines[0]:
            lines = lines[1:]
            while lines and not lines[0].strip():
                lines = lines[1:]
        body = "\n".join(lines)
        # 옛 메시지에 남아 있는 영문 라벨을 펭귄으로 맞춘다.
        body = body.replace("💡 <b>Why it matters</b>\n", "🐧 ")
        return head + "\n\n" + body

    # 원문이 없는 행(옛 데이터)은 가진 필드로 최소한만 만든다.
    parts = [head, "", f'<a href="{e(r[K_URL] or "")}">{e(r[K_HEAD] or "")}</a>', ""]
    if (r[K_LEDE] or "").strip():
        parts += ["✅ <b>핵심</b>", e(r[K_LEDE].strip()), ""]
    if (r[K_WHY] or "").strip():
        parts += [f"🐧 {e(r[K_WHY].strip())}", ""]
    ts = _ts(r[K_ORIGIN] if len(r) > K_ORIGIN else None) or _ts(r[K_SENT])
    if ts:
        parts.append(f"🕒 {datetime.fromtimestamp(ts, KST):%Y-%m-%d %H:%M} KST")
    return "\n".join(parts)


async def run(client, store, dry_run: bool | None = None) -> int | None:
    """헤더 1건 + 기사 10건을 **각각 별도 메시지로** 보낸다.

    한 메시지에 몰면 텔레그램 4096자 제한을 넘는다(10건 × 약 700자).
    개별 발행이라 실시간 탭과 읽는 느낌도 같아진다.
    """
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

    thread = topics.thread_id_for("cs_top10")
    msgs = [render_header(picked, label)]
    msgs += [render_item(i, s, r, len(picked))
             for i, (s, r) in enumerate(picked, 1)]

    if dry:
        for m in msgs:
            print("─" * 60)
            print(m)
        print("─" * 60)
        print(f"[cstop10] DRY_RUN — {len(msgs)}건 발행하지 않았습니다")
        return None

    first = None
    for i, m in enumerate(msgs):
        mid = await publisher.send_raw(client, m, thread)
        if first is None:
            first = mid
        if i < len(msgs) - 1:
            await asyncio.sleep(0.6)      # 텔레그램 rate limit 여유
    if any(csfit.is_pr(r[K_HEAD] or "") for _, r in picked):
        store.record_pr_pick(until, first)
        print("[cstop10] 홍보성 1건 게재 — 이틀간 보류")
    print(f"[cstop10] {len(msgs)}건 발행 완료")
    return first
