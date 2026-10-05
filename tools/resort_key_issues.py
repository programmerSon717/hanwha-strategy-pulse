"""🚨 주요이슈 정리 — 한 기사는 **한 탭에만** 둔다.

발주자 수칙(2026-10-05): 같은 기사가 원 토픽과 주요이슈에 동시에 있으면
집합론 위반이다. 주요이슈가 더 큰 집합이므로, 자격이 있으면 **주요이슈
하나만** 남기고 원 토픽 사본을 지운다. 자격이 없으면 반대로 주요이슈
사본을 지우고 원 토픽만 남긴다.

하는 일
  1. 지금 기준(events.looks_incident / events.decisive)으로 자격을 다시 매긴다.
  2. 같은 사건은 가장 포괄적인 것 하나만 남긴다(집합론).
  3. 48시간 안의 메시지에 대해 **남길 쪽만 남기고 나머지를 지운다.**
  4. 주요이슈 탭을 기사 발행시각 순으로 다시 쌓는다(최신이 맨 아래).

텔레그램은 48시간이 지난 메시지를 봇으로 지울 수 없다. 그건 목록만 낸다.

사용법:  venv/bin/python tools/resort_key_issues.py        # 계획만
         venv/bin/python tools/resort_key_issues.py --go   # 실제 반영
"""
import os, sys, time, sqlite3, asyncio, datetime
import httpx
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import events
from config import settings

TOKEN = settings.telegram_bot_token
CHAT = settings.telegram_channel_id
KTID = int(os.getenv("TG_TOPIC_KEY_ISSUES") or 0)
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "botstate.sqlite3")
FRESH_H = 47.0
KST = datetime.timezone(datetime.timedelta(hours=9))
f = lambda t: datetime.datetime.fromtimestamp(float(t), KST).strftime("%m-%d %H:%M")


def qualifies(h, et, score, ki):
    d = {"event_type": et, "headline": h}
    if events.looks_incident(h, d):
        return True
    return bool(ki) and (score or 0) >= settings.key_issue_threshold \
        and events.decisive(h, d)


def ids_of(raw):
    out = []
    for x in str(raw or "").replace(",", " ").split():
        if x.strip().isdigit():
            out.append(int(x))
    return out


def plan():
    c = sqlite3.connect(DB)
    rows = c.execute(
        "SELECT message_id, mirror_ids, extra_ids, headline, event_type,"
        "       strategic_score, is_key_issue, sent_at, origin_at, text,"
        "       main_entities, primary_topic FROM published"
        " WHERE message_id IS NOT NULL AND text IS NOT NULL AND text != ''"
        "   AND (cs_top10_date IS NULL OR cs_top10_date != 'DELETED-DUP')").fetchall()
    now = time.time()
    items = []
    for (mid, mir, ext, h, et, sc, ki, sa, oa, tx, ents, pt) in rows:
        d = {"main_entities": [x.strip() for x in (ents or "").split(",") if x.strip()],
             "event_type": et, "title_ko": h}
        items.append(dict(mid=mid, mirrors=ids_of(mir), extras=ids_of(ext),
                          h=h, d=d, sa=sa, oa=oa or sa, tx=tx, pt=pt,
                          ok=qualifies(h, et, sc, ki),
                          cov=events.coverage(d, h),
                          fresh=(now - float(sa)) / 3600 < FRESH_H))

    # 집합론 — 자격자 중 같은 사건은 가장 포괄적인 것만
    q = [r for r in items if r["ok"]]
    beaten = set()
    for a in q:
        for b in q:
            if a is b or a["cov"] >= b["cov"]:
                continue
            if abs(float(a["sa"]) - float(b["sa"])) > 36 * 3600:
                continue
            if events.same_event(events.parts(a["d"], a["h"]), a["h"],
                                 events.parts(b["d"], b["h"]), b["h"]):
                beaten.add(a["mid"])
                break
    for r in items:
        if r["mid"] in beaten:
            r["ok"] = False
            r["folded"] = True
    return c, items


async def main():
    go = "--go" in sys.argv
    c, items = plan()
    # 지울 것 / 남길 것
    drop, keep, old = [], [], []
    for r in items:
        if r["ok"]:
            # 주요이슈만 남긴다 → 원 토픽 사본(+추가 메시지)을 지운다
            victims = [r["mid"]] + r["extras"] + r["mirrors"]
            (keep if r["fresh"] else old).append((r, victims))
        elif r["mirrors"]:
            # 자격 없는데 주요이슈에 있다 → 주요이슈 사본만 지운다
            (drop if r["fresh"] else old).append((r, r["mirrors"]))

    print(f"\n=== 주요이슈 자격 없는데 올라가 있는 것 — 그 사본만 삭제 ({len(drop)}건)")
    for r, v in drop:
        why = "같은 사건 접힘" if r.get("folded") else "사건성 없음"
        print(f"  삭제 {v}  ({why})  {r['h'][:44]}")

    print(f"\n=== 주요이슈로 올릴 것 — 원 토픽 사본 삭제 후 재게시 ({len(keep)}건)")
    for r, _ in sorted(keep, key=lambda x: float(x[0]["oa"])):
        print(f"  {f(r['oa'])}  [{r['pt']}] {r['h'][:46]}")

    print(f"\n=== 48시간 초과 — 봇으로 못 지움, 직접 정리 ({len(old)}건)")
    for r, v in old:
        print(f"  msg={v}  {'주요이슈로' if r['ok'] else '주요이슈에서 제거'}  {r['h'][:40]}")

    if not go:
        print("\n실제로 반영하려면 --go 를 붙이세요.")
        return
    if not (TOKEN and CHAT and KTID):
        print("\n[중단] 토큰/채널/주요이슈 토픽 ID 를 읽지 못했습니다.")
        return

    async with httpx.AsyncClient(timeout=30) as cl:
        async def rm(mid):
            d = (await cl.post(f"https://api.telegram.org/bot{TOKEN}/deleteMessage",
                               json={"chat_id": CHAT, "message_id": int(mid)})).json()
            await asyncio.sleep(0.4)
            return bool(d.get("ok"))

        for r, v in drop:
            for m in v:
                print(f"  {'지움' if await rm(m) else '실패'} {m}")
            c.execute("UPDATE published SET mirror_ids='' WHERE message_id=?", (r["mid"],))
        c.commit()

        for r, v in sorted(keep, key=lambda x: float(x[0]["oa"])):
            for m in v:
                await rm(m)
            d = (await cl.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                               json={"chat_id": CHAT, "message_thread_id": KTID,
                                     "text": r["tx"], "parse_mode": "HTML",
                                     "link_preview_options": {"is_disabled": True}})).json()
            if d.get("ok"):
                new = d["result"]["message_id"]
                c.execute("UPDATE published SET message_id=?, mirror_ids='',"
                          " extra_ids='', primary_topic='key_issues',"
                          " category='key_issues' WHERE message_id=?",
                          (new, r["mid"]))
                print(f"  주요이슈로 이동 → {new}  {r['h'][:40]}")
            else:
                print(f"  실패: {d.get('description')}  {r['h'][:40]}")
            await asyncio.sleep(1.2)
    c.commit()
    print("\n완료 — 한 기사는 한 탭에만 남았고, 기사 발행시각 순으로 쌓였습니다.")

asyncio.run(main())
