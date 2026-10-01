"""이미 발행된 글 중 크립토/블록체인과 무관한 것을 걸러 삭제한다.

분류 기준이 느슨하던 시절에 발행된 일반 뉴스(반도체·정치·잡담 등)가 탭에 남아 있다.
헤드라인과 요약문만으로 한 번에 판정해 지운다.

    python main.py --purge --dry-run     # 뭐가 지워질지만 확인
    python main.py --purge               # 실제 삭제
    python main.py --purge --only 이슈    # 특정 탭만
"""
import asyncio

import publisher
from prompts import PURGE_SYSTEM_PROMPT, build_purge_prompt
from summarizer import generate_json

BATCH = 25


async def judge(rows: list) -> dict[int, bool]:
    """{행 인덱스: 크립토 관련 여부}. 판정 못 한 건 True(보존)로 둔다."""
    out: dict[int, bool] = {}
    for base in range(0, len(rows), BATCH):
        chunk = rows[base:base + BATCH]
        data = await generate_json(PURGE_SYSTEM_PROMPT, build_purge_prompt(chunk),
                                   max_tokens=3000)
        if not data:
            print(f"  [경고] {base}~{base + len(chunk) - 1}번 판정 실패 — 보존합니다")
            continue
        for r in data.get("results", []):
            try:
                idx = base + int(r["i"])
            except (KeyError, ValueError, TypeError):
                continue
            if idx < len(rows):
                out[idx] = bool(r.get("crypto", True))
        print(f"  판정 {len(out)}/{len(rows)}건")
    return out


async def run(client, store, only: str | None = None, dry_run: bool = False) -> None:
    rows = store.all_published()
    if only:
        rows = [r for r in rows if r["category"] == only]
    if not rows:
        print("[purge] 검토할 글이 없습니다")
        return

    print(f"[purge] 발행 이력 {len(rows)}건 검토")
    verdicts = await judge(rows)

    drop = [r for i, r in enumerate(rows) if verdicts.get(i, True) is False]
    if not drop:
        print("[purge] 무관한 글이 없습니다")
        return

    print(f"\n[purge] 무관 판정 {len(drop)}건"
          f"{' (dry-run — 지우지 않음)' if dry_run else ''}")
    for r in drop:
        print(f"   [{r['category']}] {r['headline'][:55]}")

    if dry_run:
        return

    gone = 0
    for r in drop:
        ok = False
        # 미러(다른 탭에 올린 사본)도 함께 지운다. 안 지우면 한쪽 탭에만 남는다.
        for mid in [r["message_id"], *r.get("extra_ids", []), *r.get("mirror_ids", [])]:
            if await publisher.delete(client, mid):
                ok = True
            await asyncio.sleep(0.3)
        if ok:
            # 이력에서 지우면 다음 수집 때 다시 올라오므로, seen 은 남기고 발행기록만 뺀다.
            store.drop_published(r["key"])
            gone += 1

    print(f"\n[purge] {gone}건 삭제 완료")
