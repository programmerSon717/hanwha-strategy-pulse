"""시간별 다이제스트.

지난 1시간 동안 발행된 글을 카테고리별로 묶어 각 탭에 요약을 올리고,
그 요약들을 다시 묶어 전체 브리핑을 General(=All 에서 보이는 곳)에 올린다.

    python main.py --digest              # 직전 정시 구간
    python main.py --digest --hours 3    # 3시간 구간
    python main.py --digest --dry-run    # 발행 없이 출력만

재료는 store 의 published 테이블이다. 즉 **실제로 발행된 글만** 요약 대상이며,
중요도 미달로 걸러진 글은 들어가지 않는다.
"""
import asyncio
import html
from datetime import datetime, timedelta, timezone

import httpx

import topics
from config import settings
from i18n import T
from prompts import (
    DIGEST_SYSTEM_PROMPT,
    OVERVIEW_SYSTEM_PROMPT,
    build_digest_prompt,
    build_overview_prompt,
)
from summarizer import generate_json

KST = timezone(timedelta(hours=9))
API = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"


def window_bounds(hours: int, now: datetime | None = None) -> tuple[float, float, str]:
    """직전 정시까지의 구간 [start, end) 와 사람이 읽을 라벨.

    진행 중인 시간대를 요약하면 뒤늦게 들어온 글이 빠지므로, 항상 정시로 끊는다.
    """
    now = now or datetime.now(tz=KST)
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=hours)
    label = (f"{start:%m월 %d일 %H시}~{end:%H시}" if hours == 1
             else f"{start:%m월 %d일 %H시}~{end:%m월 %d일 %H시}")
    return start.timestamp(), end.timestamp(), label


def tab_name(category: str) -> str:
    """내부 분류 키 → 독자에게 보이는 탭 이름.

    'Global Macro' 같은 내부 식별자가 그대로 발행되고 있었다. 탭 이름과 달라
    독자가 어느 탭 얘기인지 알 수 없다.
    """
    entry = topics.CATEGORIES.get(category)
    return entry[0] if entry else category


def render_category_digest(category: str, label: str, count: int, data: dict) -> str:
    e = html.escape
    bullets = "\n".join(f"• {e(b)}" for b in data.get("bullets", []))
    parts = [
        f"🗂 <b>{e(tab_name(category))} · {e(label)} {T('digest_title')}</b>",
        "",
        e(data.get("summary", "")),
    ]
    if bullets:
        parts += ["", f"<blockquote>{bullets}</blockquote>"]
    if data.get("takeaway"):
        parts += ["", f"🐧 {e(data['takeaway'])}"]
    parts += ["", f"<i>{T('digest_count').format(n=count)}</i>"]
    return "\n".join(parts)


def render_overview(label: str, total: int, data: dict) -> str:
    e = html.escape
    blocks = []
    for row in data.get("by_category", []):
        name = e(tab_name(row.get("category", "")))
        # 새 형식은 lines(여러 줄), 옛 형식은 line(한 줄) — 둘 다 받는다.
        items = row.get("lines") or ([row["line"]] if row.get("line") else [])
        if not items:
            continue
        blocks.append(f"<b>{name}</b>")
        blocks += [f"• {e(x)}" for x in items]
    lines = "\n".join(blocks)
    parts = [
        f"🕐 <b>{e(label)} {T('overview_title')}</b>",
        "",
        f"<b>{e(data.get('headline', ''))}</b>",
        "",
        e(data.get("summary", "")),
    ]
    if lines:
        parts += ["", f"📋 <b>{T('overview_by_cat')}</b>", f"<blockquote>{lines}</blockquote>"]
    if data.get("takeaway"):
        parts += ["", f"🐧 {e(data['takeaway'])}"]
    parts += ["", f"<i>{T('overview_count').format(n=total)}</i>"]
    return "\n".join(parts)


async def _send(client: httpx.AsyncClient, text: str, thread_id: int | None) -> int | None:
    payload = {
        "chat_id": settings.telegram_channel_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    if thread_id:
        payload["message_thread_id"] = thread_id

    r = await client.post(API, json=payload, timeout=20)
    if r.status_code != 200:
        print(f"[digest] 발행 실패: {r.status_code} {r.text[:200]}")
        return None
    return r.json().get("result", {}).get("message_id")


async def run(client: httpx.AsyncClient, store, hours: int = 1,
              dry_run: bool = False) -> None:
    start, end, label = window_bounds(hours)
    items = store.published_between(start, end)

    if not items:
        print(f"[digest] {label}: 발행된 글이 없어 건너뜀")
        return

    # 카테고리별로 묶기 (탭 순서를 따라 정렬해 항상 같은 순서로 나오게)
    order = list(topics.CATEGORIES)
    grouped: dict[str, list] = {}
    for it in items:
        grouped.setdefault(it["category"], []).append(it)
    ordered = sorted(grouped.items(),
                     key=lambda kv: order.index(kv[0]) if kv[0] in order else 99)

    print(f"[digest] {label}: 총 {len(items)}건 / 카테고리 {len(ordered)}개")

    summaries = []
    for category, group in ordered:
        scope = f"cat:{category}"
        if store.digest_done(scope, end):
            print(f"[digest] {category}: 이미 발행됨 — 건너뜀")
            continue

        data = await generate_json(
            DIGEST_SYSTEM_PROMPT, build_digest_prompt(category, label, group)
        )
        if data is None:
            print(f"[digest] {category}: 요약 실패")
            continue

        summaries.append({
            "category": category,
            "count": len(group),
            "summary": data.get("summary", ""),
            "bullets": data.get("bullets", []),
        })

        text = render_category_digest(category, label, len(group), data)
        if dry_run:
            print(f"\n--- [{category}] {len(group)}건 ---\n{text}\n")
            continue

        mid = await _send(client, text, topics.thread_id_for(category))
        if mid:
            store.record_digest(scope, end, mid)
            print(f"[digest] {category} 발행 완료 ({len(group)}건)")
        await asyncio.sleep(2)

    if not summaries:
        return

    # 전체 브리핑 — General 토픽(탭 없이 발행)에 올리면 All 에서 보인다
    scope = "overview"
    if store.digest_done(scope, end):
        print("[digest] 전체 브리핑: 이미 발행됨 — 건너뜀")
        return

    data = await generate_json(
        OVERVIEW_SYSTEM_PROMPT, build_overview_prompt(label, summaries)
    )
    if data is None:
        print("[digest] 전체 브리핑: 요약 실패")
        return

    text = render_overview(label, len(items), data)
    if dry_run:
        print(f"\n--- [전체 브리핑] ---\n{text}\n")
        return

    mid = await _send(client, text, None)
    if mid:
        store.record_digest(scope, end, mid)
        print(f"[digest] 전체 브리핑 발행 완료 (총 {len(items)}건)")
