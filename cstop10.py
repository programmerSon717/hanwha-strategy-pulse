"""📌 경전실 Top10 — 매일 KST 06:55.

☀️ Morning Brief(07:00)와 **선정 기준이 다르다.**
  Morning Brief : 모델이 매긴 strategic_score 순 + 추천 서칭 순서 티어
  경전실 Top10  : csfit.score() — 경전실이 실제 공유한 125건의 주제 분포 가중치

같은 기사가 양쪽에 다 나올 수 있다. 둘 다 Aggregation Topic 이라 중복 허용이다.
"""
import html
import re
from datetime import datetime, timedelta, timezone

import brief
import csfit
import events
import gnews
import telegraph
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


def _stem_overlap(ta: set, tb: set, minlen: int = 2) -> bool:
    """어간이 겹치는 낱말이 있는가.

    **한국어 조사 때문에 정확히 일치하는 토큰 비교로는 안 된다.**
    같은 딜 기사 둘이 "자본확충" 과 "자본확충으로" 로 갈려 공통 낱말이
    0개로 나왔다(2026-10-02). 공백으로 자르면 조사가 붙은 채로 남는다.
    한쪽이 다른 쪽의 앞부분이면 같은 낱말로 본다.
    """
    for x in ta:
        if len(x) < minlen:
            continue
        for y in tb:
            if len(y) < minlen:
                continue
            if x == y or x.startswith(y) or y.startswith(x):
                return True
    return False


def _same(a_title, a_ents, a_type, b_title, b_ents, b_type) -> bool:
    """두 기사가 같은 사건인가. **신호를 합산해서** 판정한다.

    저장된 cluster_id 를 믿으면 안 된다 — 모델이 main_entities 를 기사마다
    다르게 적어 fingerprint 가 갈린다. 애큐온캐피탈 인수 한 건이 실제로
    5가지 cluster_id 로 저장돼 Top10 에 3건이 나란히 실렸다(2026-10-02).

    제목 유사도 단독으로도 안 된다. 같은 딜을 다룬 기사들의 Jaccard 가
    0.13~0.14 로 나와 events.SOFT_TITLE_SIMILARITY(0.25)에도 못 미쳤다.
    매체마다 제목을 전혀 다르게 뽑기 때문이다.

    그래서 네 신호를 합산한다. 엔티티가 하나도 안 겹치면 무조건 다른 사건이다.
        엔티티 2개 이상 겹침  +2      엔티티 1개 겹침       +1
        action 그룹 같음      +1      제목 유사도 0.25 이상 +1
        회사명 말고 겹치는 낱말이 있음  +1
    합이 3 이상이면 같은 사건으로 본다.

    틀렸을 때의 대가가 비대칭이라 이 정도로 과감하게 잡는다 — 잘못 묶으면
    Top10 에서 기사 하나가 빠질 뿐이고, 못 묶으면 같은 뉴스가 3~4건 실린다.
    """
    def ents_of(ents, title):
        return events._entity_set(
            {"main_entities": [e for e in (ents or "").split(",") if e]}, title)

    ea, eb = ents_of(a_ents, a_title), ents_of(b_ents, b_title)
    inter = ea & eb
    if not inter:
        return False

    score = 2 if len(inter) >= 2 else 1

    ga = events.ACTION_GROUPS.get(a_type or "other", "other")
    gb = events.ACTION_GROUPS.get(b_type or "other", "other")
    if ga == gb:
        score += 1

    if events.similarity(a_title, b_title) >= events.SOFT_TITLE_SIMILARITY:
        score += 1

    # 회사명을 뺀 낱말이 겹치는가 (인수 · 자본확충 · 유상증자 …)
    ta, tb = events._tokens(a_title), events._tokens(b_title)
    names = {events._norm(x) for x in (ea | eb)}
    if _stem_overlap(ta - names, tb - names):
        score += 1

    return score >= 3


# 추천 서칭 순서를 **가산점**으로 반영한다.
#
# 처음엔 티어를 절대 1차 정렬키로 뒀다. 그랬더니 69점짜리 규제 기사
# ("보험사 GA 관리 평가 지표…K-ICS 반영")가 45점짜리 T2 기사 뒤로 밀려
# Top10 에서 아예 빠졌다. 추천 순서는 **어디부터 찾아볼지**의 우선순위지
# 품질 판단을 뒤집으라는 뜻이 아니다(2026-10-02).
# 가산점이면 티어가 낮아도 내용이 좋으면 올라온다.
TIER_BONUS = {1: 25, 2: 15, 3: 10, 4: 5, 5: 0}


def tier_of(r) -> int:
    """추천 서칭 순서상의 티어. **한화 여부는 제목으로 판단한다.**

        1 한화그룹 관련          2 진행중인 M&A · 보험
        3 규제 · 지배구조        4 한화 금융계열사(곁다리 언급)
        5 기타 (네이버 · 카카오 · 토스 · 메리츠 등)

    brief.tier 는 entities 로 판단한다. Morning Brief 에서는 그게 맞다 —
    사용자가 "한화면 무조건 1순위"로 확정했다. 다만 Top10 에 그대로 쓰면
    모델이 main_entities 에 한화생명을 폭넓게 적는 탓에 "퇴직연금 기금화"
    같은 기사까지 T1 이 돼 10건 중 7건이 1티어로 몰린다. 티어가 아무것도
    가르지 못한다. 그래서 여기서는 **기사의 핵심이 한화인지**를 제목으로 본다.
    """
    title = r[K_HEAD] or ""
    ents = r[K_ENT] or ""
    etype = r[K_ETYPE] if len(r) > K_ETYPE else ""
    pri = r[K_PRI] or ""

    if csfit.primary_category(title) == "한화":
        return 1
    if pri == "ma_governance" or etype in events.ACTION_GROUPS and \
            events.ACTION_GROUPS.get(etype) == "deal":
        return 2
    if pri == "insurance_finance":
        return 2
    if pri == "regulation_policy":
        return 3
    if "한화" in ents:
        return 4
    return 5


def select(rows: list, count: int | None = None, store=None,
           now: float | None = None) -> list:
    """적합도 순 Top N.

    같은 사건은 **한 건만** 싣는다. 저장된 cluster_id 로 1차로 묶고,
    그것만으로는 갈리는 건들을 _same() 으로 2차로 묶는다.

    홍보성 기사는 이틀에 한 건, 가장 큰 것 하나만 넣는다.
    """
    import time
    count = count or settings.daily_brief_count
    now = now or time.time()

    # 1차: 저장된 cluster_id
    best: dict[str, tuple] = {}
    for r in rows:
        cid = r[K_CLUSTER] or f"_solo:{r[K_KEY]}"
        sc = csfit.score(r[K_HEAD] or "", r[K_ENT] or "", r[K_PRI] or "", r[K_SCORE])[0]
        cur = best.get(cid)
        if cur is None or sc > cur[0]:
            best[cid] = (sc, r)

    # 2차: 실제 값으로 같은 사건 재판정. 점수 높은 쪽을 남긴다.
    merged: list = []
    for sc, r in sorted(best.values(), key=lambda x: -x[0]):
        dup = False
        for i, (sc2, r2) in enumerate(merged):
            if _same(r[K_HEAD] or "", r[K_ENT] or "", r[K_ETYPE] if len(r) > K_ETYPE else "",
                     r2[K_HEAD] or "", r2[K_ENT] or "",
                     r2[K_ETYPE] if len(r2) > K_ETYPE else ""):
                dup = True
                break
        if not dup:
            merged.append((sc, r))

    dropped = len(best) - len(merged)
    if dropped:
        print(f"[cstop10] 같은 사건 {dropped}건 접음")

    # 3차: 최근 Top10 에 이미 실린 사건 제외
    if store is not None:
        recent = store.cstop10_recent_clusters(now - 7 * 24 * 3600)
        if recent:
            keep = []
            for sc, r in merged:
                hit = any(_same(r[K_HEAD] or "", r[K_ENT] or "",
                                r[K_ETYPE] if len(r) > K_ETYPE else "",
                                h or "", e or "", t or "")
                          for h, e, t in recent)
                if hit:
                    print(f"[cstop10] 기게재 사건 제외: {(r[K_HEAD] or '')[:40]}")
                else:
                    keep.append((sc, r))
            merged = keep

    # 경영 판단에 쓸 데 없는 체인 기술·시세 기사는 뺀다 (사용자 지정).
    before = len(merged)
    merged = [(sc, r) for sc, r in merged
              if not csfit.is_crypto_tech(r[K_HEAD] or "")]
    if before != len(merged):
        print(f"[cstop10] 크립토 기술·시세 {before - len(merged)}건 제외")

    # **1차 정렬키는 사용자가 지정한 추천 서칭 순서다.**
    #   1 한화  2 M&A·보험  3 규제·지배구조  4 한화 곁다리  5 기타
    # 125건 실측 가중치(csfit)는 **같은 티어 안에서의** 2차 정렬키다.
    # 기준의 위계가 그렇다 — 추천 순서가 틀이고, 125건은 그 안에서 과장님들이
    # 실제로 무엇을 골랐는지 보여주는 성향이다(2026-10-02 사용자 설명).
    def rank_key(x):
        return -(x[0] + TIER_BONUS.get(tier_of(x[1]), 0))

    ranked = sorted(merged, key=rank_key)

    pr = [(sc, r) for sc, r in ranked if csfit.is_pr(r[K_HEAD] or "")]
    normal = [(sc, r) for sc, r in ranked if not csfit.is_pr(r[K_HEAD] or "")]

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
    cat_used: dict[str, int] = {}
    picked, deferred = [], []

    # 최소 보장석을 **먼저** 채운다. 점수 경쟁에 맡기면 영영 못 들어온다.
    reserved_keys = set()
    for catg, floor in csfit.CATEGORY_FLOOR.items():
        got = 0
        for sc, r in normal:
            if got >= floor:
                break
            if sc < csfit.FLOOR_MIN_SCORE:
                continue
            if csfit.primary_category(r[K_HEAD] or "") != catg:
                continue
            picked.append((sc, r))
            reserved_keys.add(r[K_KEY])
            cat_used[catg] = cat_used.get(catg, 0) + 1
            ents = [e for e in (r[K_ENT] or "").split(",") if e]
            h = ents[0] if ents else (r[K_PRI] or "_")
            used[h] = used.get(h, 0) + 1
            got += 1
            print(f"[cstop10] {catg} 보장석: {(r[K_HEAD] or '')[:40]}")

    for sc, r in pr_pick + normal:
        if r[K_KEY] in reserved_keys:
            continue
        ents = [e for e in (r[K_ENT] or "").split(",") if e]
        head = ents[0] if ents else (r[K_PRI] or "_")
        # 범주 상한 — 125건 실측 분포에 맞춘다. 안 걸면 국내 보험·GA 가 독식한다.
        catg = csfit.primary_category(r[K_HEAD] or "", r[K_ENT] or "")
        ccap = csfit.CATEGORY_CAP.get(catg, 2)
        if cat_used.get(catg, 0) >= ccap:
            deferred.append((sc, r))
            continue
        if used.get(head, 0) >= cap:
            deferred.append((sc, r))
            continue
        cat_used[catg] = cat_used.get(catg, 0) + 1
        used[head] = used.get(head, 0) + 1
        picked.append((sc, r))
        if len(picked) >= count:
            break
    for sc, r in deferred:
        if len(picked) >= count:
            break
        picked.append((sc, r))

    # 최종 배열도 추천 순서를 따른다. 홍보성은 맨 아래.
    picked.sort(key=lambda x: (csfit.is_pr(x[1][K_HEAD] or ""), rank_key(x)))
    return picked


TG_LIMIT = 4096          # 텔레그램 한 메시지 상한. **보이는 텍스트** 기준이다
SAFE_LIMIT = 4060        # 여유분 36자. visible_len 이 정확해 더 줄일 이유가 없다

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


def _ts(v) -> float | None:
    """epoch 로 쓸 수 있는 값이면 float, 아니면 None. DB 에 TEXT 로 들어온 행이 있다."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _cut(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _iv_url(r, real_url: str, store) -> str | None:
    """이 기사의 telegra.ph 페이지 주소. 없으면 만든다.

    제목을 여기로 링크하면 **눌렀을 때 텔레그램 안에서 Instant View 로 열린다.**
    원래 기사 주소로 링크하면 매체마다 복불복이다 — IV 템플릿이 등록된
    도메인만 되고(글로벌이코노믹 ○), 아닌 곳은 "Open this link?" 가 뜨면서
    브라우저로 나간다(newsis AMP ×). telegra.ph 는 항상 된다.
    """
    if store is None:
        return None
    key = r[K_KEY]
    hit = store.get_iv_url(key)
    if hit:
        return hit

    body = (r[K_TEXT] or "") if len(r) > K_TEXT else ""
    bullets = []
    m = re.search(r"<blockquote>(.*?)</blockquote>", body, re.S)
    if m:
        bullets = [html.unescape(re.sub(r"<[^>]+>", "", b)).strip(" •").strip()
                   for b in m.group(1).split("\n") if b.strip()]
    src_name = ""
    m = re.search(r"</a>\s*-\s*(.+?)(?:\n|$)", body)
    if m:
        nm = html.unescape(m.group(1)).strip()
        mm = re.match(r"^.+?\((.+)\)$", nm)
        src_name = (mm.group(1) if mm else nm).strip()

    content = telegraph.build_content(
        summary=(r[K_LEDE] or "").strip(),
        bullets=bullets,
        why=(r[K_WHY] or "").strip(),
        excerpt=telegraph.fetch_excerpt(real_url),
        source_url=real_url,
        source_name=src_name,
    )
    url = telegraph.create_page(store, r[K_HEAD] or "", content,
                                author=src_name or settings.bot_name)
    if url:
        store.put_iv_url(key, url)
    return url


def _link_headline(body: str, url: str) -> str:
    """제목 줄을 눌러 기사로 갈 수 있게 링크로 감싼다.

    원문의 제목 줄은 `🏢 <b>제목</b>` 형태라 눌러도 아무 일이 없다. 링크는
    맨 아래 "기사 원문" 에만 있어서 Top10 처럼 여러 건이 이어진 글에서는
    제목에서 바로 넘어가는 게 자연스럽다(2026-10-02 사용자 지정).

    이미 <a> 로 감싸여 있으면 건드리지 않는다.
    """
    if not url:
        return body
    lines = body.split("\n")
    for i, ln in enumerate(lines):
        if "<a " in ln:
            break
        m = re.match(r"^(\S+)\s+<b>(.+)</b>\s*$", ln)
        if m:
            icon, title = m.group(1), m.group(2)
            lines[i] = f'{icon} <a href="{html.escape(url, quote=True)}"><b>{title}</b></a>'
            break
    return "\n".join(lines)


def render_item(r, url: str | None = None, iv: str | None = None,
                **_ignored) -> str:
    """기사 1건. **발행 당시 원문을 그대로 쓴다.**

    원문(published.text)에는 ✅ 핵심 · 📂 주요 내용(불릿) · 🐧 · 🕒 · 기사 원문 ·
    해시태그가 이미 완성된 형태로 들어 있다. 실시간 탭에 나간 바로 그 글이다.

    **다시 조립하지 않는다.** 두 번 데였다.
      1) 섹션을 뜯어 재조립하면서 html.escape 를 한 번 더 걸었다. 원문은 이미
         이스케이프돼 있어서 `&#x27;` 가 화면에 그대로 찍혔다(2026-10-02).
      2) 한 메시지에 맞추려고 불릿을 2개로 깎고 문장에 상한을 걸었더니
         내용 품질이 눈에 띄게 떨어졌다.

    여기서는 맨 윗줄(봇 이름)만 떼고 카테고리 줄로 바꿔 끼운다. 그뿐이다.
    길이가 넘치면 render_all 이 **메시지를 나눈다** — 내용을 깎지 않는다.
    """
    e = html.escape
    body = (r[K_TEXT] or "").strip() if len(r) > K_TEXT else ""
    cat = topics.display_name(r[K_PRI] or "") or ""
    tag = " · 홍보" if csfit.is_pr(r[K_HEAD] or "") else ""
    head = f"<b>[{e(cat)}]</b>{tag}"

    if body:
        lines = body.split("\n")
        if lines and settings.bot_name in lines[0]:
            lines = lines[1:]
            while lines and not lines[0].strip():
                lines = lines[1:]
        body = "\n".join(lines)
        # 옛 메시지에 남은 영문 라벨만 펭귄으로 맞춘다.
        body = body.replace("💡 <b>Why it matters</b>\n", "🐧 ")
        # 본문 안의 구글뉴스 주소를 전부 실제 기사 주소로 바꾼다.
        # 제목뿐 아니라 맨 아래 "기사 원문" 도 바로 가야 한다.
        src = r[K_URL] or ""
        if url and src and url != src:
            body = body.replace(html.escape(src, quote=True), html.escape(url, quote=True))
            body = body.replace(src, url)
        # 제목은 Instant View 되는 telegra.ph 로, 본문 속 "기사 원문" 은 실제 기사로.
        body = _link_headline(body, iv or url or src)
        return head + "\n\n" + body

    # 원문이 없는 옛 행은 가진 필드로 최소 형태를 만든다. 여기서만 escape 한다.
    parts = [head, "", f'<a href="{e(r[K_URL] or "")}"><b>{e(r[K_HEAD] or "")}</b></a>', ""]
    if (r[K_LEDE] or "").strip():
        parts += ["✅ <b>핵심</b>", e(r[K_LEDE].strip()), ""]
    if (r[K_WHY] or "").strip():
        parts += [f"🐧 {e(r[K_WHY].strip())}", ""]
    ts = _ts(r[K_ORIGIN] if len(r) > K_ORIGIN else None) or _ts(r[K_SENT])
    if ts:
        parts.append(f"🕒 {datetime.fromtimestamp(ts, KST):%Y-%m-%d %H:%M} KST")
    return "\n".join(parts)


def _first_urls(msgs: list[str], items: list[str], urls: list[str]) -> list[str]:
    """조각마다 **맨 앞 기사**의 주소. 미리보기는 메시지당 하나뿐이라 대표를 고른다."""
    out, idx = [], 0
    for m in msgs:
        taken = sum(1 for it in items if it in m)
        out.append(urls[idx] if idx < len(urls) else "")
        idx += max(taken, 1)
    return out


def render_all(picked: list, label: str, _store=None) -> tuple[list[str], list[str]]:
    """메시지 목록. **내용을 깎지 않는다 — 넘치면 나눈다.**

    한 판으로 보내려고 불릿을 줄이고 문장을 자르던 것을 그만뒀다. 기사 하나가
    약 460자(보이는 길이)라 10건이면 4,600자다. 텔레그램 상한이 4,096자이니
    2개로 나뉜다. 11개로 쪼개던 때와는 다르다.
    """
    e = html.escape
    n_pr = sum(1 for _, r in picked if csfit.is_pr(r[K_HEAD] or ""))
    head = f"📌 <b>{e(settings.bot_name)} | 경전실 Top10</b>\n{e(label)}"
    if n_pr:
        head += f"  ·  홍보 {n_pr}건 포함"

    # 구글뉴스 리디렉터를 실제 기사 주소로 바꾼다. 발행 링크의 86% 가 그것이다.
    # 미리보기 카드가 붙으려면 메타태그가 있는 실제 기사 주소여야 한다.
    urls = [gnews.resolve(r[K_URL] or "", _store) for _, r in picked]
    ivs = [_iv_url(r, u, _store) for (_, r), u in zip(picked, urls)]
    items = [render_item(r, url=u, iv=iv)
             for (_, r), u, iv in zip(picked, urls, ivs)]

    # 몇 조각이 필요한지 먼저 센 뒤, 그 수에 맞춰 **고르게** 나눈다.
    # 그냥 채우면 3,999자 + 731자 처럼 한쪽으로 쏠려 보기 나쁘다.
    def pack(limit: int) -> list[str] | None:
        out, cur = [], head
        for it in items:
            cand = cur + ITEM_GAP + it
            if visible_len(cand) > limit and cur != head:
                out.append(cur)
                cur = it
            else:
                cur = cand
        out.append(cur)
        return out if all(visible_len(m) <= SAFE_LIMIT for m in out) else None

    base = pack(SAFE_LIMIT)
    n = len(base)
    if n == 1:
        return base, []
    # 조각 수를 늘리지 않는 선에서 한도를 조여 균등하게 만든다.
    total = visible_len(head) + sum(visible_len(i) + len(ITEM_GAP) for i in items)
    for limit in range(total // n + 60, SAFE_LIMIT + 1, 40):
        trial = pack(limit)
        if trial and len(trial) == n:
            return trial, []
    return base, []


async def run(client, store, dry_run: bool | None = None) -> int | None:
    import asyncio
    import publisher
    dry = settings.dry_run if dry_run is None else dry_run
    since, until, label = window()
    rows = store.cstop10_candidates(since, until, settings.discard_threshold)
    picked = select(rows, store=store)
    span = (f"{datetime.fromtimestamp(since, KST):%m-%d %H:%M}"
            f" ~ {datetime.fromtimestamp(until, KST):%m-%d %H:%M}")
    print(f"[cstop10] 구간 {span} · 후보 {len(rows)}건 → 선정 {len(picked)}건")
    if not picked:
        print("[cstop10] 후보 없음 — 게시하지 않음")
        return None

    msgs, preview_urls = render_all(picked, label, store)
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
        mid = await publisher.send_raw(
            client, m, thread,
            preview_url=preview_urls[i] if i < len(preview_urls) else None)
        # 발행분을 기록해 둔다 — 나중에 이 메시지만 골라 지울 수 있게.
        store.record_agg_message("cs_top10", until + i, mid)
        first = first or mid
        if i < len(msgs) - 1:
            await asyncio.sleep(0.6)
    # 실린 기사를 표시해 둔다 — 다음 회차에서 다시 뽑히지 않게.
    store.mark_cstop10([r[K_KEY] for _, r in picked],
                       datetime.fromtimestamp(until, KST).strftime("%Y-%m-%d"))
    if any(csfit.is_pr(r[K_HEAD] or "") for _, r in picked):
        store.record_pr_pick(until, first)
        print("[cstop10] 홍보성 1건 게재 — 이틀간 보류")
    return first
