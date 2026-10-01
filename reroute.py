"""이미 발행된 글을 다시 분류해 올바른 탭으로 옮긴다.

카테고리를 새로 추가하면 그 전에 발행된 글들은 옛 분류(대개 '이슈')에 남는다.
텔레그램은 메시지를 다른 토픽으로 **옮기는 API가 없으므로**, 지우고 다시 올린다.

    python main.py --reroute --dry-run   # 어떻게 옮겨질지만 확인
    python main.py --reroute             # 실제 이동

발행 원문(text)이 저장돼 있으면 그대로 다시 올려 내용이 그대로 보존된다.
저장 이전에 발행된 글은 원문이 없어 옮길 수 없다 — 무엇이 남았는지 출력한다.
"""
import asyncio

import httpx

import country
import publisher
import topics
from prompts import build_reroute_prompt, REROUTE_SYSTEM_PROMPT
from summarizer import generate_json


BATCH = 25   # 한 번에 판정시킬 건수. 너무 크면 뒷부분을 빠뜨린다.


async def classify_all(rows: list) -> dict[int, str]:
    """전체를 묶어서 판정한다. {행 인덱스: 카테고리}"""
    out: dict[int, str] = {}
    for base in range(0, len(rows), BATCH):
        chunk = rows[base:base + BATCH]
        data = await generate_json(
            REROUTE_SYSTEM_PROMPT, build_reroute_prompt(chunk),
            max_tokens=4000,
        )
        if not data:
            print(f"  [경고] {base}~{base + len(chunk) - 1}번 판정 실패 — 그대로 둡니다")
            continue
        for r in data.get("results", []):
            try:
                idx = base + int(r["i"])
            except (KeyError, ValueError, TypeError):
                continue
            if idx < len(rows):
                out[idx] = (r.get("category") or "").strip()
        print(f"  판정 {len(out)}/{len(rows)}건")
    return out


async def run(client: httpx.AsyncClient, store, dry_run: bool = False,
              only: set[str] | None = None) -> None:
    """only 가 주어지면 그 카테고리로 옮겨지는 건만 처리한다."""
    rows = store.all_published()
    print(f"[reroute] 발행 이력 {len(rows)}건 검토")

    moved = skipped = unchanged = 0
    stuck: list[str] = []
    regen: list[str] = []

    verdicts = await classify_all(rows)

    for idx, row in enumerate(rows):
        new_cat = verdicts.get(idx)
        if not new_cat or new_cat == row["category"]:
            unchanged += 1
            continue
        # 모델 판정에도 국가 보정을 적용한다(프롬프트만으로는 나라를 틀린다)
        fixed, why = country.enforce(new_cat, {"headline": row["headline"],
                                               "lede": row["lede"], "bullets": []})
        if why:
            print(f"     보정: {why}")
            new_cat = fixed
        if new_cat not in topics.CATEGORIES:
            print(f"  [무시] 알 수 없는 분류 '{new_cat}': {row['headline'][:40]}")
            unchanged += 1
            continue
        if only and new_cat not in only:
            unchanged += 1
            continue

        print(f"  {row['category'] or '(없음)'} → {new_cat}: {row['headline'][:45]}")
        if dry_run:
            moved += 1
            continue

        if not row["text"]:
            # 원문 저장 이전에 발행된 글. 지우고 발행 이력에서 빼두면
            # 다음 수집 때 원본에서 다시 만들어져 새 분류로 올라간다.
            if await publisher.delete(client, row["message_id"]):
                store.forget(row["key"])
                regen.append(row["headline"][:45])
                moved += 1
            else:
                stuck.append(f"msg {row['message_id']} · {row['headline'][:45]}")
                skipped += 1
            await asyncio.sleep(0.5)
            continue

        thread_id = topics.thread_id_for(new_cat)
        new_id = await publisher.send_raw(client, row["text"], thread_id)
        if not new_id:
            skipped += 1
            continue

        if not await publisher.delete(client, row["message_id"]):
            print(f"    [경고] 옛 글 삭제 실패(msg {row['message_id']}) — 중복이 남습니다")
        store.update_published_location(row["key"], new_id, thread_id, new_cat)
        moved += 1
        await asyncio.sleep(2)

    print(f"\n[reroute] 이동 {moved}건 / 그대로 {unchanged}건 / 실패·보류 {skipped}건"
          f"{' (dry-run)' if dry_run else ''}")
    if regen:
        print(f"\n{len(regen)}건은 지우고 이력에서 뺐습니다 — 다시 수집하면 새 탭으로 올라갑니다:")
        for s in regen:
            print(f"  - {s}")
        print("\n  이어서 실행: python main.py --tg-since 2026-08-10")
    if stuck:
        print("\n삭제에 실패해 남아 있는 글 — 텔레그램에서 직접 처리하세요:")
        for s in stuck:
            print(f"  - {s}")
