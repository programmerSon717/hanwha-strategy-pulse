"""상태 DB 를 **정적 웹이 읽을 JSON** 으로 내보낸다.

왜 정적인가: 발주자 수칙이 "무조건 무료" 이고 "컴퓨터가 꺼져도 7/24/365 돌아야"
한다. 서버를 띄우면 둘 다 깨진다. 그래서 GitHub Actions 가 매 회차 끝에 이
스크립트로 docs/data/*.json 을 다시 쓰고 커밋하면, GitHub Pages 가 그걸 그대로
서빙한다. 비용 0, 가동률은 GitHub 과 같다.

**공개 저장소에 올라간다.** 발주자 수칙: "회사명 같은 건 깃허브에서만 가려라."
그래서 조직·부서를 가리키는 말은 내보내지 않는다(scrub 참고). 기사 본문에
나오는 회사명은 뉴스 그 자체이므로 그대로 둔다.

사용법:  python tools/export_web.py [출력디렉터리]
"""
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = os.path.join(ROOT, "botstate.sqlite3")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "docs", "data")
KST = timezone(timedelta(hours=9))

# 토픽 표시 이름.
#
# **폴백을 반드시 둔다.** config 를 읽으려면 .env 의 설정값이 필요한데,
# 러너에는 그 비밀값이 없어 import 가 조용히 실패한다. 그러면 화면의 토픽이
# 전부 '미분류' 로 나온다(2026-10-05 실측: 172/172 건이 빈 이름). 표시용
# 문자열이라 여기 박아 두어도 안전하다 — 라우팅 키는 건드리지 않는다.
_FALLBACK = [
    ("hanwha_group",        "🏢 한화그룹"),
    ("ma_governance",       "🤝 M&A · 지배구조"),
    ("insurance_finance",   "🏦 보험 · 금융"),
    ("regulation_policy",   "⚖️ 규제 · 정책"),
    ("competitors_bigtech", "🔎 경쟁사 · Big Tech"),
    ("digital_newbiz",      "💡 디지털 · 신사업"),
    ("global_finance",      "🌐 Global"),
    ("key_issues",          "🚨 주요이슈"),
]
TOPIC_NAME = dict(_FALLBACK)
TOPIC_ORDER = [k for k, _ in _FALLBACK]
try:                                     # 설정을 읽을 수 있으면 그쪽이 우선이다
    from config import TOPIC_DEFS
    TOPIC_NAME = {t["id"]: t["telegram_topic_name"] for t in TOPIC_DEFS}
    TOPIC_ORDER = [t["id"] for t in TOPIC_DEFS]
except Exception:                                        # pragma: no cover
    print("[export] 설정을 읽지 못해 토픽 이름은 내장 목록을 씁니다")

# 수집 토픽만(집계 토픽 제외) 보여 줄 때 쓴다.
AGG = {"key_issues", "daily_brief", "cs_top10", "cs_top10_links"}

# 공개 저장소에 나가면 안 되는 말. 기사 본문의 회사명은 뉴스라 건드리지 않고,
# **우리 조직·부서를 가리키는 호칭만** 지운다.
_SCRUB = re.compile(r"A팀|경영전략실|경전실")


def scrub(s):
    return _SCRUB.sub("선정 대상", s) if isinstance(s, str) else s


def ts(v):
    """epoch(초/밀리초) 또는 문자열 → ISO8601(KST). 모르면 None."""
    if v in (None, ""):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    if f > 1e11:                      # 밀리초로 들어온 행이 있다
        f /= 1000.0
    try:
        return datetime.fromtimestamp(f, KST).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def day_of(v):
    i = ts(v)
    return i[:10] if i else None


def rows(c, sql, args=()):
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute(sql, args)]


def write(name, obj):
    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[export] {name}  {os.path.getsize(p):,} bytes")


def ents(v):
    return [x.strip() for x in (v or "").split(",") if x.strip()]


# ── 기사 ────────────────────────────────────────────────────────
def build_articles(c):
    out = []
    for r in rows(c, """
        SELECT key, headline, lede, why_it_matters, source_url, canonical_url,
               primary_topic, secondary_topics, strategic_score, confidence,
               main_entities, event_type, is_key_issue, cs_top10_date,
               origin_at, sent_at, collected_at, message_id
          FROM published
         WHERE headline IS NOT NULL AND headline != ''
         ORDER BY COALESCE(origin_at, sent_at) DESC"""):
        top10 = r["cs_top10_date"]
        # 'DELETED-DUP' · 'DROPPED-LOWCONF' 같은 표시값은 날짜가 아니다.
        if top10 and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", top10):
            top10 = None
        out.append({
            "key": r["key"],
            "title": scrub(r["headline"]),
            "lede": scrub(r["lede"] or ""),
            "why": scrub(r["why_it_matters"] or ""),
            "url": r["canonical_url"] or r["source_url"] or "",
            "topic": r["primary_topic"] or "",
            "topic_name": TOPIC_NAME.get(r["primary_topic"] or "", ""),
            "subtopics": ents(r["secondary_topics"]),
            "score": r["strategic_score"],
            "confidence": r["confidence"],
            "entities": ents(r["main_entities"]),
            "event_type": r["event_type"] or "",
            "key_issue": bool(r["is_key_issue"]),
            "top10_date": top10,
            "origin_at": ts(r["origin_at"]),
            "sent_at": ts(r["sent_at"]),
            "live": r["message_id"] is not None,
        })
    return out


# ── Top10 발행판 ────────────────────────────────────────────────
def build_top10(arts):
    by_day = {}
    for a in arts:
        if a["top10_date"]:
            by_day.setdefault(a["top10_date"], []).append(a)
    editions = []
    for d in sorted(by_day, reverse=True):
        items = sorted(by_day[d], key=lambda x: x["origin_at"] or "")
        editions.append({
            "date": d,
            "count": len(items),
            "topics": sorted({i["topic"] for i in items if i["topic"]}),
            "items": [{
                "rank": n,
                "key": i["key"], "title": i["title"], "lede": i["lede"],
                "why": i["why"], "url": i["url"], "topic": i["topic"],
                "topic_name": i["topic_name"], "entities": i["entities"],
                "score": i["score"], "origin_at": i["origin_at"],
            } for n, i in enumerate(items, 1)],
        })
    return editions


# ── 파이프라인(판정 로그) ───────────────────────────────────────
def build_pipeline(c):
    """수집 → 판정 → 발행 의 흐름. 쟁글의 '시장동향' 자리에 들어간다."""
    jr = rows(c, """
        SELECT ts, title, source, url, relevant, score, primary_topic,
               reason, duplicate_event, sent
          FROM judgment ORDER BY ts DESC LIMIT 600""")
    recent = [{
        "at": ts(j["ts"]),
        "title": scrub(j["title"] or ""),
        "source": j["source"] or "",
        "url": j["url"] or "",
        "relevant": bool(j["relevant"]),
        "score": j["score"],
        "topic": j["primary_topic"] or "",
        "topic_name": TOPIC_NAME.get(j["primary_topic"] or "", ""),
        "reason": scrub(j["reason"] or ""),
        "dup": bool(j["duplicate_event"]),
        "sent": bool(j["sent"]),
    } for j in jr]

    # 일자별 추이 — 판정 전체를 긁어 날짜로 접는다.
    daily = {}
    for (t, rel, sent) in c.execute(
            "SELECT ts, relevant, sent FROM judgment"):
        d = day_of(t)
        if not d:
            continue
        s = daily.setdefault(d, {"date": d, "judged": 0, "passed": 0, "sent": 0})
        s["judged"] += 1
        s["passed"] += 1 if rel else 0
        s["sent"] += 1 if sent else 0
    for (t,) in c.execute("SELECT published_at FROM seen"):
        d = day_of(t)
        if d and d in daily:
            daily[d]["seen"] = daily[d].get("seen", 0) + 1
    trend = [daily[d] for d in sorted(daily)][-30:]

    # 제외 사유 분포 — 왜 안 나갔는지가 운영자가 제일 궁금한 것이다.
    why = {}
    for j in jr:
        if j["relevant"]:
            continue
        r = scrub((j["reason"] or "사유 미기재").strip())
        r = (r[:40] + "…") if len(r) > 40 else r
        why[r] = why.get(r, 0) + 1
    drops = sorted(({"reason": k, "n": v} for k, v in why.items()),
                   key=lambda x: -x["n"])[:12]
    return {"recent": recent, "trend": trend, "drops": drops}


# ── 초안·스케줄 ─────────────────────────────────────────────────
def build_draft(c, arts):
    by_key = {a["key"]: a for a in arts}
    st = {k: v for k, v in c.execute("SELECT k, v FROM setting")}
    out = {"slot": st.get("draft_slot"), "heal": st.get("draft_heal"),
           "editions": []}
    for r in rows(c, "SELECT publish_date, keys, quality, built_at"
                     "  FROM top10_draft ORDER BY publish_date DESC"):
        keys = [k for k in (r["keys"] or "").split(",") if k]
        out["editions"].append({
            "date": r["publish_date"],
            "built_at": ts(r["built_at"]),
            "quality": round(r["quality"], 1) if r["quality"] else None,
            "count": len(keys),
            "items": [{
                "rank": n, "title": by_key[k]["title"] if k in by_key else "(본문 없음)",
                "topic_name": by_key[k]["topic_name"] if k in by_key else "",
                "origin_at": by_key[k]["origin_at"] if k in by_key else None,
                "url": by_key[k]["url"] if k in by_key else "",
            } for n, k in enumerate(keys, 1)],
        })
    # 올라간 초안 메시지 수(탭별) — '교체가 끝났는지' 를 눈으로 본다.
    posts = {}
    for k, v in st.items():
        if k.startswith("draft_msgs:") and v:
            posts[k.split(":", 1)[1]] = len([x for x in v.split(",") if x])
    out["posted"] = posts
    return out


# ── 요약 지표 ───────────────────────────────────────────────────
def build_summary(c, arts, pipe):
    one = lambda q: c.execute(q).fetchone()[0]
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")

    topic_counts = {}
    for a in arts:
        if a["topic"] and a["topic"] not in AGG:
            topic_counts[a["topic"]] = topic_counts.get(a["topic"], 0) + 1
    topics = [{"id": t, "name": TOPIC_NAME.get(t, t), "n": topic_counts.get(t, 0)}
              for t in TOPIC_ORDER if t not in AGG]

    last_seen = one("SELECT MAX(published_at) FROM seen")
    last_judge = one("SELECT MAX(ts) FROM judgment")
    last_pub = one("SELECT MAX(sent_at) FROM published")
    # '살아 있는가' 는 마지막 판정 시각으로 본다. 수집은 돌아도 기준에 맞는
    # 기사가 없으면 발행이 비는 것이 정상이라, 발행 시각으로 재면 오판한다.
    age_h = None
    if last_judge:
        try:
            age_h = round((time.time() - float(last_judge)) / 3600, 1)
        except (TypeError, ValueError):
            age_h = None
    health = "ok" if (age_h is not None and age_h < 2) else (
        "warn" if (age_h is not None and age_h < 6) else "down")

    return {
        "generated_at": now.isoformat(),
        "today": today,
        "health": health,
        "age_hours": age_h,
        "counts": {
            "seen": one("SELECT COUNT(*) FROM seen"),
            "judged": one("SELECT COUNT(*) FROM judgment"),
            "published": len(arts),
            "live": sum(1 for a in arts if a["live"]),
            "top10": sum(1 for a in arts if a["top10_date"]),
            "key_issues": sum(1 for a in arts if a["key_issue"]),
            "today_published": sum(1 for a in arts
                                   if (a["origin_at"] or "")[:10] == today),
        },
        "last": {"seen": ts(last_seen), "judged": ts(last_judge),
                 "published": ts(last_pub)},
        "topics": topics,
        "trend": pipe["trend"][-14:],
    }


def main():
    if not os.path.exists(DB):
        raise SystemExit(f"[export] DB 가 없습니다: {DB}")
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    arts = build_articles(c)
    pipe = build_pipeline(c)
    write("articles.json", arts)
    write("top10.json", build_top10(arts))
    write("pipeline.json", pipe)
    write("draft.json", build_draft(c, arts))
    write("summary.json", build_summary(c, arts, pipe))
    print(f"[export] 완료 — 기사 {len(arts)}건")


if __name__ == "__main__":
    main()
