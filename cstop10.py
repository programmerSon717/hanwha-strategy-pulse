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


def window(now: float | None = None) -> tuple[float, float, str]:
    """전날 06:55 ~ 오늘 06:55."""
    now = now or datetime.now(KST).timestamp()
    until = now
    since = now - 24 * 3600
    label = datetime.fromtimestamp(now, KST).strftime("%Y.%m.%d %a")
    return since, until, label


def select(rows: list, count: int | None = None) -> list:
    """적합도 순 Top N. 같은 Event 는 대표기사 하나만."""
    count = count or settings.daily_brief_count

    best: dict[str, tuple] = {}
    for r in rows:
        cid = r[K_CLUSTER] or f"_solo:{r[K_KEY]}"
        s = csfit.score(r[K_HEAD] or "", r[K_ENT] or "", r[K_PRI] or "", r[K_SCORE])[0]
        cur = best.get(cid)
        if cur is None or s > cur[0]:
            best[cid] = (s, r)

    ranked = sorted(best.values(), key=lambda x: -x[0])

    # 한 회사가 독식하지 않게. Morning Brief 와 같은 상한을 쓴다.
    cap = settings.daily_brief_max_per_entity
    used: dict[str, int] = {}
    picked, deferred = [], []
    for s, r in ranked:
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
    return picked


def render(picked: list, label: str) -> str:
    e = html.escape
    parts = [f"📌 <b>{e(settings.bot_name)} | 경전실 Top10</b>", "", e(label), ""]
    for i, (s, r) in enumerate(picked, 1):
        # display_name 에 이미 이모지가 들어 있다. 앞에 또 붙이지 않는다.
        parts.append(f"<b>{i}. [{e(topics.display_name(r[K_PRI] or ''))}]</b>")
        parts.append(f'<a href="{e(r[K_URL] or "")}">{e(r[K_HEAD] or "")}</a>')
        why = (r[K_WHY] or "").strip()
        if why:
            parts.append(f"🐧 {e(why[:150])}")
        parts.append("")
    return "\n".join(parts)


async def run(client, store, dry_run: bool | None = None) -> int | None:
    import publisher
    dry = settings.dry_run if dry_run is None else dry_run
    since, until, label = window()
    rows = store.brief_candidates(since, until, settings.discard_threshold)
    picked = select(rows)
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
    return await publisher.send_raw(client, text, topics.thread_id_for("cs_top10"))
