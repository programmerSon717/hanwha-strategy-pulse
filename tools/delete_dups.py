"""중복 발행분 삭제. 집합이 더 작은 쪽을 지우고 큰 쪽만 남긴다.

사용법:  venv/bin/python tools/delete_dups.py          # 목록만 보여준다
         venv/bin/python tools/delete_dups.py --go     # 실제로 지운다

판정은 events.same_event 직접 쌍(전이 없음) + events.coverage 다.
텔레그램은 48시간이 지난 메시지를 봇으로 지울 수 없다 — 그건 목록만 낸다.
"""
import os, sys, time, sqlite3, asyncio
import httpx
from dotenv import load_dotenv
load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import events

from config import settings          # .env 키 이름을 코드와 한 곳에서 맞춘다
TOKEN = settings.telegram_bot_token
CHAT = settings.telegram_channel_id
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "botstate.sqlite3")


# 같은 사건으로 볼 최대 시차. events.parts() 의 시간 버킷은 절대 격자라,
# 과거 행을 지금 시각으로 다시 계산하면 전부 같은 버킷이 된다 — 88시간
# 떨어진 서로 다른 정책이 "같은 사건" 이 됐다(2026-10-05 감사).
MAX_GAP_H = 36.0


def losers():
    c = sqlite3.connect(DB)
    rows = c.execute(
        "SELECT message_id, thread_id, headline, main_entities, event_type,"
        "       sent_at, mirror_ids, extra_ids FROM published"
        " WHERE message_id IS NOT NULL AND headline IS NOT NULL").fetchall()
    it = []
    for mid, tid, h, e, et, sa, mir, ext in rows:
        ents = [x.strip() for x in (e or "").split(",") if x.strip()]
        d = {"main_entities": ents, "event_type": et, "title_ko": h}
        # 확정 사실(본계약·승인·제재 …)은 삭제 후보에서 뺀다. 발행 쪽이
        # material 로 허용한 것을 삭제 쪽이 지우면 두 규칙이 모순된다.
        if events.looks_material(h, d):
            continue
        it.append(dict(mid=mid, tid=tid, h=h, d=d, sa=sa, mir=mir, ext=ext,
                       cov=events.coverage(d, h)))
    out = {}
    for a in it:
        for b in it:
            if a is b or a["cov"] >= b["cov"]:
                continue
            # **시차 제한.** parts() 의 시간 버킷은 절대 격자라, 과거 행을
            # 지금 시각으로 다시 계산하면 전부 같은 버킷이 된다. 그래서
            # 88시간 떨어진 서로 다른 정책이 "같은 사건" 이 됐다(2026-10-05).
            if abs(float(a["sa"]) - float(b["sa"])) > MAX_GAP_H * 3600:
                continue
            if not events.same_event(events.parts(a["d"], a["h"]), a["h"],
                                     events.parts(b["d"], b["h"]), b["h"]):
                continue
            if a["mid"] not in out or b["cov"] > out[a["mid"]][0]["cov"]:
                out[a["mid"]] = (b, a)

    # **고정점 검사.** '남길 기사' 자신이 삭제 대상이면 둘 다 지워져 사건이
    # 채널에서 통째로 사라진다. 실측 9건이었다(2026-10-05 감사).
    # 승자가 살아남을 때까지 사슬을 따라 올라가고, 끝내 못 찾으면 뺀다.
    fixed = {}
    for mid, (w, l) in out.items():
        seen, cur = {mid}, w
        while cur["mid"] in out and cur["mid"] not in seen:
            seen.add(cur["mid"])
            cur = out[cur["mid"]][0]
        if cur["mid"] in out:
            continue            # 사슬이 닫혔다 — 건드리지 않는다
        fixed[mid] = (cur, l)
    return c, fixed


async def main():
    go = "--go" in sys.argv
    c, lose = losers()
    now = time.time()
    fresh, old = [], []
    for mid, (w, l) in lose.items():
        (fresh if (now - float(l["sa"])) / 3600 < 47.0 else old).append((w, l))
    fresh.sort(key=lambda x: x[1]["sa"])

    print(f"\n=== 48시간 내 · 지울 수 있음 ({len(fresh)}건)")
    for w, l in fresh:
        print(f"  삭제 msg={l['mid']}  {l['h'][:52]}")
        print(f"       남김 msg={w['mid']}  {w['h'][:48]}")
    print(f"\n=== 48시간 초과 · 봇으로 못 지움, 직접 삭제 ({len(old)}건)")
    print("  " + ", ".join(str(l["mid"]) for _, l in sorted(old, key=lambda x: x[1]["mid"])))

    if not go:
        print("\n실제로 지우려면 끝에 --go 를 붙여 다시 실행하세요.")
        return
    if not (TOKEN and CHAT):
        print("\n[중단] .env 에 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 없습니다.")
        return

    done = failed = 0
    async with httpx.AsyncClient(timeout=20) as cl:
        for w, l in fresh:
            gone = True                 # 이 기사의 메시지를 전부 지웠는가
            ids = [l["mid"]]
            for col in (l["mir"], l["ext"]):      # 주요이슈 사본·추가 메시지도 함께
                if col:
                    ids += [int(x) for x in str(col).replace(",", " ").split()
                            if x.strip().isdigit()]
            for i in dict.fromkeys(ids):
                r = await cl.post(
                    f"https://api.telegram.org/bot{TOKEN}/deleteMessage",
                    json={"chat_id": CHAT, "message_id": i})
                d = r.json()
                if d.get("ok"):
                    done += 1
                    print(f"  지움 msg={i}")
                else:
                    failed += 1
                    gone = False
                    print(f"  실패 msg={i}: {d.get('description', '')}")
            # **지운 것만 표시한다.** 실패분까지 message_id 를 비우면 메시지는
            # 남아 있는데 재시도할 번호를 잃는다(2026-10-05 실측: 4건).
            if gone:
                c.execute("UPDATE published SET message_id=NULL,"
                          " cs_top10_date='DELETED-DUP' WHERE message_id=?",
                          (l["mid"],))
            else:
                print(f"       → msg={l['mid']} 표시 보류(메시지가 남아 있을 수 있음)")
    c.commit()
    print(f"\n성공 {done}건 · 실패 {failed}건 — DB 표시 완료")

asyncio.run(main())
