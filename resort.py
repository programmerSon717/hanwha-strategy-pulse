"""탭 안의 글을 원 게시일 기준으로 다시 정렬한다.

텔레그램은 메시지 순서를 바꾸는 API가 없고 새 글을 무조건 맨 아래에 붙인다.
그래서 실시간 글이 쌓이면 '최신이 위' 순서가 어긋난다.
이 명령은 저장해둔 발행 원문을 지우고 원하는 순서로 다시 올려 정렬을 되돌린다.

    python main.py --resort                  # 모든 탭
    python main.py --resort --only 이슈       # 특정 탭만
    python main.py --resort --dry-run        # 어떤 순서가 될지만 출력

모델을 다시 부르지 않는다. 저장된 본문과 사진 file_id 를 그대로 재사용하므로
비용이 들지 않고 내용도 그대로다.
"""
import asyncio
import html
from datetime import datetime, timedelta, timezone

import httpx

import publisher
import topics

KST = timezone(timedelta(hours=9))

# 텔레그램은 탭을 열면 **맨 아래**로 이동한다.
# 따라서 열자마자 최신을 보려면 최신이 맨 아래에 있어야 한다 = 과거가 위.
# (최신을 위에 놓으면 탭을 열 때 가장 오래된 글이 먼저 보인다)
NEWEST_ON_TOP = False


def _sort_key(row: dict) -> float:
    """원 게시 시각 기준. 없으면 발행 순서를 대신 쓴다."""
    return row.get("origin_at") or 0.0


def _caption(row: dict) -> str:
    e = html.escape
    parts = [f"📰 <b>{e(row['headline'])}</b>"]
    if row.get("origin_at"):
        dt = datetime.fromtimestamp(row["origin_at"], KST)
        parts += ["", f"🕒 {dt:%Y-%m-%d %H:%M} KST 게시"]
    return "\n".join(parts)


async def run(client: httpx.AsyncClient, store, only: str | None = None,
              dry_run: bool = False) -> None:
    rows = [r for r in store.all_published() if r["text"]]
    if only:
        rows = [r for r in rows if r["category"] == only]
    if not rows:
        print("[resort] 정렬할 글이 없습니다 (발행 원문이 저장된 글만 대상)")
        return

    by_cat: dict[str, list] = {}
    for r in rows:
        by_cat.setdefault(r["category"] or "이슈", []).append(r)

    for category, group in by_cat.items():
        group.sort(key=_sort_key, reverse=NEWEST_ON_TOP)
        order = "최신 → 과거" if NEWEST_ON_TOP else "과거 → 최신"
        print(f"\n[{category}] {len(group)}건 재정렬 ({order})")

        if dry_run:
            for r in group:
                when = (datetime.fromtimestamp(r["origin_at"], KST).strftime("%m-%d %H:%M")
                        if r.get("origin_at") else "  ?  ")
                print(f"   {when}  {r['headline'][:50]}")
            continue

        thread_id = topics.thread_id_for(category)

        # 먼저 전부 지운다. 하나씩 지우고 올리면 중간에 순서가 섞인다.
        for r in group:
            for mid in [r["message_id"], *r.get("extra_ids", [])]:
                await publisher.delete(client, mid)
                await asyncio.sleep(0.3)

        # 원하는 순서대로 다시 올린다
        for r in group:
            photo_id = None
            if r.get("photo_file_id"):
                photo_id = await publisher.send_photo_by_id(
                    client, r["photo_file_id"], _caption(r), thread_id
                )
            body_id = await publisher.send_raw(client, r["text"], thread_id,
                                               reply_to=photo_id)
            if not body_id and not photo_id:
                print(f"   [실패] {r['headline'][:40]}")
                continue
            ids = [i for i in (photo_id, body_id) if i]
            store.update_published_ids(r["key"], ids[0], ids[1:])
            await asyncio.sleep(5)

        print(f"[{category}] 완료")
