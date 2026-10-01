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
K_ETYPE, K_ORIGIN = 12, 13          # origin_at = 기사 자체의 발행시각

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


def render(picked: list, label: str) -> str:
    e = html.escape
    parts = [f"📌 <b>{e(settings.bot_name)} | 경전실 Top10</b>", "", e(label), ""]
    for i, (s, r) in enumerate(picked, 1):
        tag = " · 홍보" if csfit.is_pr(r[K_HEAD] or "") else ""
        parts.append(f"<b>{i}. [{e(topics.display_name(r[K_PRI] or ''))}]{tag}</b>")
        parts.append(f'<a href="{e(r[K_URL] or "")}">{e(r[K_HEAD] or "")}</a>')

        # 주요 내용 — 모델이 쓴 1~2문장 요약
        lede = (r[K_LEDE] or "").strip()
        if lede:
            parts.append(f"✅ {e(lede[:170])}")

        why = (r[K_WHY] or "").strip()
        if why:
            parts.append(f"🐧 {e(why[:150])}")

        # 기사 자체의 발행시각. 없으면 봇이 받은 시각으로 대체한다.
        # origin_at 은 행에 따라 TEXT 로 들어 있어 float 변환이 필요하다.
        ts = _ts(r[K_ORIGIN] if len(r) > K_ORIGIN else None) or _ts(r[K_SENT])
        if ts:
            parts.append(f"🕒 {datetime.fromtimestamp(ts, KST):%m-%d %H:%M} KST")
        parts.append("")
    return "\n".join(parts)


async def run(client, store, dry_run: bool | None = None) -> int | None:
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
    text = render(picked, label)
    if dry:
        print("─" * 60)
        print(text)
        print("─" * 60)
        print("[cstop10] DRY_RUN — 발행하지 않았습니다")
        return None
    mid = await publisher.send_raw(client, text, topics.thread_id_for("cs_top10"))
    # 홍보성을 실었으면 기록한다 — 이틀 간격을 지키기 위해.
    if any(csfit.is_pr(r[K_HEAD] or "") for _, r in picked):
        store.record_pr_pick(until, mid)
        print("[cstop10] 홍보성 1건 게재 — 이틀간 보류")
    return mid
