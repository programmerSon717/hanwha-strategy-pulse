"""공개 텔레그램 채널을 웹 미리보기(t.me/s/<채널>)로 수집한다.

Telethon(개인 계정 로그인)이 필요 없다는 게 핵심이다. 공개 채널은 로그인 없이
HTML로 열람할 수 있고, 첨부 사진도 텔레그램 CDN에서 그대로 받을 수 있다.
따라서 이 수집기는 GitHub Actions 같은 클라우드에서도 그대로 동작한다.

한 페이지에 최근 20건이 담기고, `?before=<메시지id>` 로 과거로 거슬러 올라간다.

주의: 비공개 채널에는 쓸 수 없다. 그 경우에만 telegram_channels.py(Telethon)를 쓴다.
"""
import asyncio
import html
import re

import httpx

from models import NewsItem

BASE = "https://t.me/s/{channel}"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

# 한 메시지 블록. data-post 로 시작해 다음 data-post 직전까지를 한 덩어리로 본다.
_MSG_SPLIT = re.compile(r'(?=<div class="tgme_widget_message[^"]*"[^>]*data-post=")')
_POST_ID = re.compile(r'data-post="([^"/]+)/(\d+)"')
_DATETIME = re.compile(r'datetime="([^"]+)"')
_PHOTO = re.compile(r"background-image:url\('(https://cdn\d*\.telesco\.pe/file/[^']+)'\)")
_TEXT = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.DOTALL
)
_TAG = re.compile(r"<[^>]+>")
_HREF = re.compile(r'href="(https?://[^"]+)"')

# 출처로 쓸 수 없는 링크: 텔레그램 내부(채널 자기 자신, 해시태그 검색 등)
_INTERNAL = ("t.me/", "telegram.me/", "telegram.org/")


def _origin_links(block: str) -> list[str]:
    """메시지 안의 외부 링크. 채널이 붙여둔 '공지원문' 같은 원문 주소를 건진다."""
    out = []
    for u in _HREF.findall(block):
        u = html.unescape(u)
        if any(k in u for k in _INTERNAL):
            continue
        if u not in out:
            out.append(u)
    return out


def _clean_text(raw: str) -> str:
    """메시지 본문 HTML → 평문. <br>은 줄바꿈으로 살린다."""
    s = re.sub(r"<br\s*/?>", "\n", raw)
    s = _TAG.sub("", s)
    return html.unescape(s).strip()


def parse_page(page_html: str) -> list[dict]:
    """웹 미리보기 HTML → 메시지 dict 목록(오래된 것부터)."""
    out = []
    for block in _MSG_SPLIT.split(page_html):
        m = _POST_ID.search(block)
        if not m:
            continue
        channel, msg_id = m.group(1), int(m.group(2))

        dt = _DATETIME.search(block)
        text_m = _TEXT.search(block)
        photos = _PHOTO.findall(block)

        out.append({
            "channel": channel,
            "id": msg_id,
            "datetime": dt.group(1) if dt else None,
            "text": _clean_text(text_m.group(1)) if text_m else "",
            "photos": photos,
            "links": _origin_links(block),
        })
    return out


async def _download(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        r = await client.get(url, headers=UA, timeout=30, follow_redirects=True)
        r.raise_for_status()
        return r.content
    except Exception as e:
        print(f"[tg_web] 이미지 다운로드 실패: {type(e).__name__} {str(e)[:60]}")
        return None


async def fetch_since(client: httpx.AsyncClient, channel: str, since_epoch: float,
                      with_image: bool = True, max_pages: int = 40) -> list[NewsItem]:
    """지정 시각 이후의 글을 전부 수집한다(오래된 것 → 최신 순으로 정렬해 반환).

    max_pages 는 안전장치다. 채널이 예상보다 활발해도 무한히 거슬러 올라가지 않는다.
    """
    channel = channel.lstrip("@")
    collected: dict[int, dict] = {}
    before: int | None = None
    reached = False

    for page in range(max_pages):
        url = BASE.format(channel=channel) + (f"?before={before}" if before else "")
        try:
            r = await client.get(url, headers=UA, timeout=30, follow_redirects=True)
            r.raise_for_status()
        except Exception as e:
            print(f"[tg_web:{channel}] 페이지 요청 실패: {e}")
            break

        msgs = parse_page(r.text)
        if not msgs:
            break

        for m in msgs:
            collected[m["id"]] = m

        oldest = min(m["id"] for m in msgs)
        oldest_dt = min((m["datetime"] or "9999") for m in msgs)
        # 이 페이지의 가장 오래된 글이 기준 시각보다 과거면 더 갈 필요가 없다
        if oldest_dt != "9999":
            from datetime import datetime
            if datetime.fromisoformat(oldest_dt).timestamp() < since_epoch:
                reached = True
                break
        if before == oldest:      # 더 이상 과거가 없음
            break
        before = oldest

    if not reached:
        print(f"[tg_web:{channel}] {max_pages}페이지까지 훑음 — 기준일까지 못 갔을 수 있음")

    from datetime import datetime

    items: list[NewsItem] = []
    for m in sorted(collected.values(), key=lambda x: x["id"]):
        if not m["datetime"]:
            continue
        ts = datetime.fromisoformat(m["datetime"]).timestamp()
        if ts < since_epoch:
            continue

        text = m["text"]
        # 사진도 없고 본문도 짧으면 정보가 없는 글
        if not m["photos"] and len(text) < 40:
            continue

        photo_url = m["photos"][0] if m["photos"] else ""
        image = None
        if photo_url and with_image:
            image = await _download(client, photo_url)
            await asyncio.sleep(0.3)   # CDN 예의상 간격

        items.append(
            NewsItem(
                source=f"TG:@{m['channel']}",
                unique_id=f"{m['channel']}:{m['id']}",
                title=(text.split("\n")[0][:120] if text
                       else f"[트위터 캡처] {m['channel']}:{m['id']}"),
                url=f"https://t.me/{m['channel']}/{m['id']}",
                body=text[:2000],
                region_hint="",
                published_at=ts,
                image=image,
                image_mime="image/jpeg",
                image_url=photo_url,
                origin_url=m["links"][0] if m["links"] else "",
            )
        )

    print(f"[tg_web:{channel}] {len(collected)}건 훑음 → 기준일 이후 {len(items)}건 수집")
    return items
