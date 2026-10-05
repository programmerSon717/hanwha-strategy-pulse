"""🚨 주요이슈 토픽 재정렬 + 누락분 추가.

하는 일
  1. 지금 기준(events.decisive / events.looks_incident)으로 주요이슈 자격을
     다시 매긴다. 자격을 잃은 건 내린다.
  2. 자격이 있는데 안 올라간 건을 올린다.
  3. **기사 발행시각 순으로 다시 쌓는다** — 최신이 맨 아래.

텔레그램은 메시지를 옮길 수 없다. 그래서 '지우고 다시 올리는' 방식이고,
48시간이 지난 메시지는 지울 수 없어 건드리지 않는다(위쪽에 그대로 남는다).
48시간 이내 것들만 재정렬 대상이다.

사용법:  venv/bin/python tools/resort_key_issues.py        # 계획만 출력
         venv/bin/python tools/resort_key_issues.py --go   # 실제 반영
"""
import os, sys, time, sqlite3, asyncio
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


def qualifies(h, et, score, ki):
    d = {"event_type": et, "headline": h}
    if events.looks_incident(h, d):
        return True
    return bool(ki) and (score or 0) >= settings.key_issue_threshold \
        and events.decisive(h, d)


def plan():
    c = sqlite3.connect(DB)
    rows = c.execute(
        "SELECT message_id, mirror_ids, headline, event_type, strategic_score,"
        "       is_key_issue, sent_at, origin_at, text, main_entities FROM published"
        " WHERE message_id IS NOT NULL AND text IS NOT NULL AND text!=''"
        # 중복으로 내린 건은 주요이슈에도 올리지 않는다(집합론 수칙).
        "   AND (cs_top10_date IS NULL OR cs_top10_date!='DELETED-DUP')").fetchall()
    now = time.time()
    drop, add, keep_old = [], [], []
    for mid, mir, h, et, sc, ki, sa, oa, tx, ents in rows:
        ok = qualifies(h, et, sc, ki)
        has = bool(mir and str(mir).strip())
        fresh = (now - float(sa)) / 3600 < FRESH_H
        d = {"main_entities": [x.strip() for x in (ents or "").split(",") if x.strip()],
             "event_type": et, "title_ko": h}
        r = dict(mid=mid, mir=str(mir).strip(), h=h, oa=oa or sa, tx=tx,
                 fresh=fresh, d=d, cov=events.coverage(d, h))
        if has and fresh:
            drop.append(r)                 # 일단 내리고, 자격 있으면 순서대로 다시 올린다
            if ok:
                add.append(r)
        elif has and not fresh:
            keep_old.append((r, ok))       # 48h 초과 — 손댈 수 없다
        elif ok and fresh:
            add.append(r)                  # 누락분
    # **집합론.** 같은 사건이 주요이슈에 여러 번 뜨지 않게, 가장 포괄적인
    # 것 하나만 남긴다. 전이 없이 직접 쌍만 본다(연쇄 병합은 무관한 기사까지
    # 묶는다 — tools/delete_dups.py 주석 참고).
    beaten = set()
    for a in add:
        for b in add:
            if a is b or a["cov"] >= b["cov"]:
                continue
            if events.same_event(events.parts(a["d"], a["h"]), a["h"],
                                 events.parts(b["d"], b["h"]), b["h"]):
                beaten.add(a["mid"])
                break
    folded = [r for r in add if r["mid"] in beaten]
    add = [r for r in add if r["mid"] not in beaten]
    add.sort(key=lambda r: float(r["oa"]))
    return c, drop, add, keep_old, folded


async def main():
    go = "--go" in sys.argv
    c, drop, add, keep_old, folded = plan()
    import datetime
    KST = datetime.timezone(datetime.timedelta(hours=9))
    f = lambda t: datetime.datetime.fromtimestamp(float(t), KST).strftime("%m-%d %H:%M")

    print(f"\n=== 내렸다가 다시 쌓을 것 ({len(drop)}건, 48h 이내)")
    for r in drop:
        print(f"  mirror={r['mir']}  {r['h'][:50]}")
    print(f"\n=== 기사시각 순으로 다시 올릴 것 ({len(add)}건) — 위에서 아래로")
    for r in add:
        print(f"  {f(r['oa'])}  msg={r['mid']}  {r['h'][:50]}")
    if folded:
        print(f"\n=== 같은 사건이라 접은 것 ({len(folded)}건)")
        for r in folded:
            print(f"  msg={r['mid']} cov={r['cov']}  {r['h'][:50]}")
    bad = [r for r, ok in keep_old if not ok]
    print(f"\n=== 48h 초과라 못 내리는 것 중, 지금 기준으론 자격 없는 것 ({len(bad)}건)")
    for r in bad:
        print(f"  mirror={r['mir']}  {r['h'][:50]}   ← 직접 삭제 필요")

    if not go:
        print("\n실제로 반영하려면 --go 를 붙이세요.")
        return
    if not (TOKEN and CHAT and KTID):
        print("\n[중단] 토큰/채널/주요이슈 토픽 ID 를 읽지 못했습니다.")
        return

    async with httpx.AsyncClient(timeout=30) as cl:
        for r in drop:
            d = (await cl.post(f"https://api.telegram.org/bot{TOKEN}/deleteMessage",
                               json={"chat_id": CHAT, "message_id": int(r["mir"])})).json()
            print(f"  내림 mirror={r['mir']}: {'ok' if d.get('ok') else d.get('description')}")
            c.execute("UPDATE published SET mirror_ids='' WHERE message_id=?", (r["mid"],))
        c.commit()
        for r in add:
            d = (await cl.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                               json={"chat_id": CHAT, "message_thread_id": KTID,
                                     "text": r["tx"], "parse_mode": "HTML",
                                     "link_preview_options": {"is_disabled": True}})).json()
            if d.get("ok"):
                new = d["result"]["message_id"]
                c.execute("UPDATE published SET mirror_ids=? WHERE message_id=?",
                          (str(new), r["mid"]))
                print(f"  올림 msg={r['mid']} → mirror={new}  {r['h'][:40]}")
            else:
                print(f"  실패 msg={r['mid']}: {d.get('description')}")
            await asyncio.sleep(1.2)       # 텔레그램 전송 제한
    c.commit()
    print("\n완료 — 기사 발행시각 순으로 다시 쌓았습니다(최신이 맨 아래).")

asyncio.run(main())
