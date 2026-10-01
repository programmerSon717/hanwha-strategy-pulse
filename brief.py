"""☀️ Morning Brief (스펙 §23~25) — Strategy Pulse 에서 가장 중요한 산출물.

    "아침 7시 전후, 경영전략실이 볼 기사 약 10개"

**최신 기사 10개가 아니다. Strategic Intelligence Ranking Top 10 이다.**

cutoff 기반 window (§22):
    직전 Morning Brief cutoff ~ 이번 cutoff 사이에 수집된 Material Event.
    단순 calendar date 가 아니다 — 06:55 에 돌면 전날 06:55 이후가 대상이다.

선정 (§24):
    1) cutoff window 내 후보 확보
    2) threshold 미달 제거
    3) event clustering
    4) 각 Event 대표기사 선정
    5) Strategic Score 순 ranking
    6) 동일 사건/동일 회사 과도 중복 보정
    7) Top N
    8) Key Signals 추출

    단순 ORDER BY score DESC LIMIT 10 이 아니라 Diversified Ranking 이다.
    단, **Quality > Quota** — 다양성을 맞추려고 낮은 품질 기사를 억지로 넣지 않는다.
    한화그룹에 중요한 사건이 4개면 4개 모두 들어갈 수 있다.
"""
import html
import time
from datetime import datetime, timedelta, timezone

import topics
from config import settings

KST = timezone(timedelta(hours=9))


def cutoff_window(store, now: float | None = None) -> tuple[float, float, str]:
    """(시작, 끝, 날짜라벨). 직전 브리프 cutoff 부터 지금까지 (§22)."""
    now = now or time.time()
    prev = store.last_brief_cutoff()
    if prev is None or prev >= now:
        # 첫 실행이거나 기록이 어긋났다. 24시간으로 잡는다.
        prev = now - 24 * 3600
    # 너무 오래 멈췄다가 살아난 경우 — 3일치를 한꺼번에 쏟아내지 않는다.
    prev = max(prev, now - 3 * 24 * 3600)
    label = datetime.fromtimestamp(now, KST).strftime("%Y.%m.%d %a")
    return prev, now, label


# ── 경영전략실 추천 서칭 순서 (2026-10-01 사용자 지정) ──────────
# 브리프 Top10 은 점수만으로 뽑지 않는다. 아래 순서가 **1차 정렬키**이고
# strategic_score 는 같은 티어 안에서의 2차 정렬키다.
#
#   1  한화그룹 관련
#   2  진행중인 M&A, 보험 관련
#   3  규제(공정위·금융위 등), 지배구조
#   4  한화금융 계열사 관련
#   5  기타 (네이버·카카오·토스·메리츠 등)
#
# 1 과 4 를 가르는 기준 (2026-10-01 사용자 확정):
#   **기사의 핵심이 한화면 그룹이든 금융계열사든 전부 티어 1 이다.**
#   티어 4 는 한화가 곁다리로만 언급된 기사(예: 금감원 규제 기사에 한화생명이
#   업계 사례로 들어간 경우)를 위해 남겨 둔다.
# 이렇게 하지 않으면 한화생명 애큐온캐피탈 인수(95점) 같은 최대 뉴스가
# 한화투자증권 유상증자(88점) 아래로 밀린다. 실제로 그렇게 나왔다.
# 사용자가 지정한 순서이므로 임의로 바꾸지 마라.

_HANWHA_FIN = ("한화생명", "한화손해보험", "한화투자증권", "한화자산운용",
               "한화저축은행", "한화생명금융서비스", "캐롯손해보험", "피플라이프")
_HANWHA_GRP = ("한화그룹", "한화에어로스페이스", "한화솔루션", "한화오션",
               "한화시스템", "한화호텔앤드리조트", "김동관", "김동원")
_DEAL_TYPES = ("acquisition", "merger", "divestiture", "stake_change", "investment")


def tier(primary: str, entities: str, event_type: str = "") -> int:
    """추천 서칭 순서상의 티어 (1 이 가장 앞)."""
    ents = [e.strip() for e in (entities or "").split(",") if e.strip()]
    has_grp = any(e in _HANWHA_GRP for e in ents)
    has_fin = any(e in _HANWHA_FIN for e in ents)

    # 1) 한화 — 그룹이든 금융계열사든, 기사의 핵심이 한화면 최우선
    if has_grp or has_fin or primary == "hanwha_group":
        return 1
    # 2) 진행중인 M&A · 보험
    if primary == "ma_governance" or (event_type in _DEAL_TYPES) \
            or primary == "insurance_finance":
        return 2
    # 3) 규제 · 지배구조
    if primary == "regulation_policy":
        return 3
    # 4) 한화가 곁다리로 언급된 기사 — 위에서 이미 걸러졌으므로 여기엔
    #    secondary_topics 로만 한화가 걸린 경우가 온다.
    if "hanwha" in (primary or ""):
        return 4
    # 5) 기타
    return 5


def select(rows: list) -> list:
    """§24 Diversified Ranking. rows 는 store.brief_candidates() 결과."""
    # rows: (key, headline, url, primary, secondary, score, is_key, cluster,
    #        entities, lede, why, sent_at)
    K_KEY, K_HEAD, K_URL, K_PRI, K_SEC, K_SCORE = 0, 1, 2, 3, 4, 5
    K_ISKEY, K_CLUSTER, K_ENT, K_LEDE, K_WHY = 6, 7, 8, 9, 10

    # 3~4) Event 별 대표기사 — 같은 cluster 에서 점수가 가장 높은 것 하나
    best: dict[str, tuple] = {}
    for r in rows:
        cid = r[K_CLUSTER] or f"_solo:{r[K_KEY]}"
        cur = best.get(cid)
        if cur is None or (r[K_SCORE] or 0) > (cur[K_SCORE] or 0):
            best[cid] = r

    # 5) 추천 서칭 순서(티어) → 같은 티어 안에서 Strategic Score 순
    # rows 끝에 event_type 이 붙어 있다(store.brief_candidates). 없으면 빈 값으로 둔다.
    def _etype(r):
        return r[12] if len(r) > 12 else ""

    def _sortkey(r):
        return (tier(r[K_PRI] or "", r[K_ENT] or "", _etype(r) or ""),
                -(r[K_SCORE] or 0))

    ranked = sorted(best.values(), key=_sortkey)

    # 6) 동일 회사 과도 중복 보정 — 한 엔티티가 브리프를 독식하지 않게.
    #    단, 상한은 넉넉하다. 한화 사건이 4개면 4개 다 들어가는 게 맞다(§23).
    cap = settings.daily_brief_max_per_entity
    used: dict[str, int] = {}
    picked: list = []
    deferred: list = []

    for r in ranked:
        ents = [e for e in (r[K_ENT] or "").split(",") if e]
        head = ents[0] if ents else r[K_PRI] or "_"
        if used.get(head, 0) >= cap:
            deferred.append(r)
            continue
        used[head] = used.get(head, 0) + 1
        picked.append(r)
        if len(picked) >= settings.daily_brief_count:
            break

    # 자리가 남으면 상한에 걸려 밀렸던 것을 점수순으로 채운다.
    # (Quality > Quota — 빈자리를 억지로 낮은 점수로 메우지는 않는다)
    for r in deferred:
        if len(picked) >= settings.daily_brief_count:
            break
        picked.append(r)

    return picked


def key_signals(rows: list, picked: list) -> list[str]:
    """§25 Key Signals. 브리프에 실린 것 중 주요이슈로 꼽힌 건의 한 줄 요약."""
    K_HEAD, K_ISKEY, K_WHY, K_PRI = 1, 6, 10, 3
    out = []
    for r in picked:
        if not r[K_ISKEY]:
            continue
        why = (r[K_WHY] or "").strip()
        # 한 줄로 자른다 — Key Signals 가 길어지면 브리프가 안 읽힌다.
        line = why.split(". ")[0].strip().rstrip(".")
        out.append(line or (r[K_HEAD] or "")[:60])
    return out[:5]


def render(picked: list, signals: list[str], label: str) -> str:
    """§25 Morning Brief 발행 양식.

    각 기사는 Headline + 1줄 핵심 + 1줄 Why it matters 로 제한한다 —
    텔레그램에서 스크롤이 지나치게 길어지지 않게.
    """
    e = html.escape
    K_HEAD, K_URL, K_PRI, K_LEDE, K_WHY = 1, 2, 3, 9, 10

    parts = [f"☀️ <b>{e(settings.bot_name)} | Morning Brief</b>", "", e(label), ""]

    if not picked:
        parts += ["오늘 구간에는 보고할 만한 전략 이슈가 없습니다.", ""]
        return "\n".join(parts)

    for i, r in enumerate(picked, 1):
        icon = _icon(r[K_PRI])
        name = _name(r[K_PRI])
        head = e(r[K_HEAD] or "")
        url = (r[K_URL] or "").strip()
        title = f'<a href="{e(url)}">{head}</a>' if url else head

        parts.append(f"<b>{i}. {icon} [{e(name)}]</b>")
        parts.append(title)

        core = _one_line(r[K_LEDE])
        if core:
            parts += ["", f"<b>핵심</b>  {e(core)}"]
        why = _one_line(r[K_WHY])
        if why:
            parts.append(f"🐧 {e(why)}")
        parts.append("")

    if signals:
        parts += ["━━━━━━━━━━━━━━", "", "🚨 <b>Key Signals</b>", ""]
        parts += [f"• {e(s)}" for s in signals]

    return "\n".join(parts)


def _one_line(text: str | None, limit: int = 110) -> str:
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    return t[:limit].rsplit(" ", 1)[0] + "…"


def _icon(topic_id: str | None) -> str:
    name = topics.CATEGORIES.get(topic_id or "", "")
    return name.split(" ", 1)[0] if name else "📰"


def _name(topic_id: str | None) -> str:
    name = topics.CATEGORIES.get(topic_id or "", topic_id or "")
    return name.split(" ", 1)[1] if " " in name else name


async def run(client, store, dry_run: bool | None = None) -> int | None:
    """Morning Brief 를 만들어 ☀️ 탭에 발행한다. message_id 또는 None."""
    import publisher

    dry = settings.dry_run if dry_run is None else dry_run
    since, until, label = cutoff_window(store)

    rows = store.brief_candidates(since, until, settings.general_topic_threshold)
    picked = select(rows)
    signals = key_signals(rows, picked)
    text = render(picked, signals, label)

    span = f"{datetime.fromtimestamp(since, KST):%m-%d %H:%M} ~ {datetime.fromtimestamp(until, KST):%m-%d %H:%M}"
    print(f"[brief] 구간 {span} · 후보 {len(rows)}건 → 선정 {len(picked)}건 "
          f"· Key Signals {len(signals)}건")

    if dry:
        print("─" * 60)
        print(text)
        print("─" * 60)
        print("[brief] DRY_RUN — 발행하지 않았습니다")
        return None

    thread_id = topics.thread_id_for("daily_brief")
    mid = await publisher.send_raw(client, text, thread_id)
    if mid:
        store.mark_briefed([r[0] for r in picked],
                           datetime.fromtimestamp(until, KST).strftime("%Y-%m-%d"))
        store.record_brief(until, mid)
        print(f"[brief] 발행 완료 (message_id={mid})")
    return mid
