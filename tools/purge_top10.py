#!/usr/bin/env python
"""📌 경전실 Top10 발행분 삭제 유틸.

예전엔 message_id 를 남기지 않아 탭을 통째로 지워야 했다(2026-10-01).
이제는 발행할 때 store.record_agg_message 로 남기므로 해당 메시지만 지운다.

    venv/bin/python tools/purge_top10.py --list        무엇이 지워질지만 본다
    venv/bin/python tools/purge_top10.py --delete      실제로 지운다
    venv/bin/python tools/purge_top10.py --delete --keep 1   최신 1건만 남긴다
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

import publisher
import store as store_mod
from config import settings

KST = timezone(timedelta(hours=9))


def _arg(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


async def main():
    st = store_mod.Store(getattr(settings, "db_path", "botstate.sqlite3"))
    keep = int(_arg("--keep", "0"))
    rows = st.agg_messages("cs_top10")
    targets = rows[keep:]

    print(f"기록된 발행분 {len(rows)}건 · 유지 {keep}건 · 삭제 대상 {len(targets)}건\n")
    for ts, mid in targets:
        when = datetime.fromtimestamp(ts, KST).strftime("%m-%d %H:%M")
        print(f"  msg {mid}  ({when} KST)")

    if "--delete" not in sys.argv:
        print("\n실제로 지우려면 --delete 를 붙이세요.")
        return
    if not targets:
        return

    ok = 0
    async with httpx.AsyncClient() as c:
        for ts, mid in targets:
            if await publisher.delete(c, mid):
                st.forget_agg_message("cs_top10", mid)
                ok += 1
            else:
                print(f"  ⚠️ msg {mid} 삭제 실패 (이미 지워졌을 수 있음)")
                st.forget_agg_message("cs_top10", mid)
    print(f"\n{ok}/{len(targets)}건 삭제")


if __name__ == "__main__":
    asyncio.run(main())
