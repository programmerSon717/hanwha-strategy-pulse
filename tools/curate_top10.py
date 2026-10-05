#!/usr/bin/env python
"""사람이 고른 기사 목록으로 📌 경전실 Top10 과 🔗 링크용을 발행한다.

    venv/bin/python tools/curate_top10.py urls.txt 2026-10-05T06:50

**봇이 뽑은 게 아니라 사람이 직접 고른 날** 쓴다(2026-10-05). 봇이 멈춰 있던
날은 후보 창이 비어 Top10 이 성립하지 않는데, 그날 아침 과장님이 이미 10건을
공유해 두었다면 그걸 그대로 Top10 으로 올리는 게 맞다.

  1) 각 주소를 평소와 **같은 요약 경로**(summarizer.summarize)로 돌린다
  2) cstop10 이 쓰는 행 모양으로 만들어 render_all / render_links 에 넘긴다
  3) 발행 후 published 에 남긴다 — 다음 회차가 같은 기사를 또 뽑지 않게

**일반 탭에는 올리지 않는다.** 그 기사들은 이미 사람이 공유했다. 봇이 탭마다
또 뿌리면 중복이다. 그래서 message_id 는 Top10 메시지 것으로만 남긴다.
"""
import asyncio
import html as H
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

import csfit
import cstop10 as K
import gnews
import publisher
import telegraph
import topics
from config import settings
from models import NewsItem
from store import Store, normalize_url
from summarizer import summarize

KST = K.KST
UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")}


# 기사 발행시각을 뽑는 자리들. 앞에서부터 먼저 맞는 것을 쓴다.
_DATE_PATS = [
    r'property=["\']article:published_time["\'][^>]*content=["\']([^"\']+)',
    r'content=["\']([^"\']+)["\'][^>]*property=["\']article:published_time',
    r'itemprop=["\']datePublished["\'][^>]*content=["\']([^"\']+)',
    r'"datePublished"\s*:\s*"([^"]+)"',
    r'입력\s*:?\s*(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})[^\d]{1,4}(\d{1,2}):(\d{2})',
    r'(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})\s+(\d{1,2}):(\d{2})',
]


def _parse_published(html_text: str) -> float | None:
    """기사 원문 발행시각(epoch). 못 찾으면 None.

    **창 끝 시각을 대신 넣지 않는다.** 예전에 asof 를 그대로 origin_at 에
    박았더니 10건이 전부 '10-05 06:50' 로 찍혀, 인스턴트뷰의 기사발행시각이
    전부 같아졌다(2026-10-05 지적).
    """
    import datetime as _dt
    for pat in _DATE_PATS:
        m = re.search(pat, html_text, re.I)
        if not m:
            continue
        g = m.groups()
        try:
            if len(g) == 1:
                t = _dt.datetime.fromisoformat(g[0].strip().replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=KST)
                return t.timestamp()
            y, mo, d, hh, mm = (int(x) for x in g)
            return _dt.datetime(y, mo, d, hh, mm, tzinfo=KST).timestamp()
        except (ValueError, TypeError):
            continue
    return None


async def _title_of(client, url: str) -> tuple[str, str, float | None]:
    try:
        r = await client.get(url, headers=UA, timeout=20, follow_redirects=True)
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
        if not m:
            return "", ""
        t = H.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        ts = _parse_published(r.text)
        parts = re.split(r"\s+[-|<]\s+", t)
        if len(parts) > 1 and len(parts[-1]) <= 20:
            return " ".join(parts[:-1]).strip(), parts[-1].strip(), ts
        return t, "", ts
    except Exception:
        return "", "", None


def _row(data: dict, url: str, origin_at: float, text: str) -> list:
    """cstop10 의 K_* 색인에 맞춘 행. 컬럼 순서는 store.cstop10_candidates 와 같다."""
    r = [None] * (K.K_TEXT + 1)
    r[K.K_KEY] = Store.make_key("curated", normalize_url(url))
    r[K.K_HEAD] = data.get("headline") or ""
    r[K.K_URL] = url
    r[K.K_PRI] = data.get("category") or ""
    r[K.K_SEC] = ",".join(data.get("secondary_topics") or [])
    r[K.K_SCORE] = data.get("strategic_score") or 0
    r[K.K_ISKEY] = 1 if data.get("is_key_issue") else 0
    r[K.K_CLUSTER] = data.get("event_cluster_id") or ""
    r[K.K_ENT] = ",".join(data.get("main_entities") or [])
    r[K.K_LEDE] = data.get("lede") or ""
    r[K.K_WHY] = data.get("why_it_matters") or ""
    r[K.K_SENT] = time.time()
    r[K.K_ETYPE] = data.get("event_type") or ""
    r[K.K_ORIGIN] = origin_at
    r[K.K_TEXT] = text
    return r


def _store_rows(store, picked):
    """published 에 적어 둔다.

    **sent_at 을 반드시 채운다.** record_published 는 published_at 만 쓰는데,
    Top10 의 중복 검사(store.cstop10_recent_clusters / published_clusters_before)는
    sent_at 으로 거른다. 비워 두면 큐레이션한 기사가 중복 검사망에서 통째로
    빠져, 같은 기사가 다른 날 Top10 에 또 실린다(2026-10-05 실측: 뱅크샐러드·
    생보사 유동성 2건이 그렇게 겹쳤다).
    """
    import sqlite3
    now = time.time()
    for fit, r in picked:
        store.record_published(r[K.K_KEY], None, None, r[K.K_URL], r[K.K_HEAD],
                               category=r[K.K_PRI], lede=r[K.K_LEDE],
                               text=r[K.K_TEXT], origin_at=r[K.K_ORIGIN])
        with sqlite3.connect(settings.db_path) as c:
            c.execute("""UPDATE published
                            SET sent_at=?, main_entities=?, event_type=?,
                                strategic_score=?, primary_topic=?, why_it_matters=?
                          WHERE key=?""",
                      (now, r[K.K_ENT], r[K.K_ETYPE], r[K.K_SCORE],
                       r[K.K_PRI], r[K.K_WHY], r[K.K_KEY]))


async def main():
    if len(sys.argv) < 3:
        sys.exit("사용법: curate_top10.py <urls.txt> <asof: 2026-10-05T06:50>")
    urls = [l.strip() for l in open(sys.argv[1], encoding="utf-8")
            if l.strip() and not l.startswith("#")]
    from datetime import datetime
    asof = datetime.fromisoformat(sys.argv[2]).replace(tzinfo=KST).timestamp()
    label = datetime.fromtimestamp(asof, KST).strftime("%Y.%m.%d %a")
    store = Store(settings.db_path)
    dry = settings.dry_run
    print(f"[curate] {len(urls)}건 · asof {sys.argv[2]} KST · DRY_RUN={dry}")

    picked = []
    async with httpx.AsyncClient() as client:
        for i, url in enumerate(urls, 1):
            real = gnews.resolve(url, store) or url
            title, src, origin = await _title_of(client, real)
            if not title:
                print(f"  {i:>2}. 제목 실패 — 건너뜀: {real[:58]}")
                continue
            paras, why_fail = telegraph.fetch_article(real, title=title)
            item = NewsItem(source=src or "경전실 공유", unique_id=real, title=title,
                            url=real, body="\n".join(paras)[:6000],
                            region_hint="국내")
            data = await summarize(item)
            if not data:
                print(f"  {i:>2}. 요약 실패 — 건너뜀: {title[:44]}")
                continue
            data["headline"] = data.get("title_ko") or title
            data["lede"] = data.get("summary") or ""
            data["category"] = topics.normalize_topic(data.get("primary_topic"))
            data["_posted_label"] = None
            text = publisher.render(data, real)
            fit, _ = csfit.score(data["headline"], "", data["category"],
                                 data.get("strategic_score"))
            print(f"  {i:>2}. 적합{fit:>3} [{data['category']:<20}] {data['headline'][:42]}")
            if origin is None:
                print(f"       ⚠️ 발행시각을 못 찾았다 — 창 끝 시각으로 대신한다")
            picked.append((fit, _row(data, real, origin or asof, text)))

    if not picked:
        sys.exit("발행할 것이 없습니다.")

    # --register-only: 발행하지 않고 **기록만** 남긴다.
    # 날짜 순서대로 올리려면, 사람이 고른 날을 먼저 등록해 두고 봇이 뽑는 날이
    # 그 기사들을 피해 가게 해야 한다. 그다음 날짜 순서로 발행한다.
    if "--register-only" in sys.argv:
        _store_rows(store, picked)
        store.mark_cstop10([r[K.K_KEY] for _, r in picked],
                           datetime.fromtimestamp(asof, KST).strftime("%Y-%m-%d"))
        print(f"[curate] 등록만 완료 — {len(picked)}건 (발행 안 함)")
        return
    msgs, previews = K.render_all(picked, label, store)
    print(f"[curate] 본문 {len(msgs)}건 · 링크 {len(picked)}건")
    if dry:
        for m in msgs:
            print("─" * 60); print(m)
        return

    import publisher as P
    thread = topics.thread_id_for("cs_top10")
    d = datetime.fromtimestamp(asof, KST)
    async with httpx.AsyncClient() as client:
        await P.send_raw(client,
                         f"📌 <b>{d.month}월 {d.day}일자 경전실 Top10 발행 시작합니다</b>",
                         thread)
        await asyncio.sleep(0.5)
        for i, m in enumerate(msgs):
            mid = await P.send_raw(client, m, thread,
                                   preview_url=previews[i] if i < len(previews) else None)
            store.record_agg_message("cs_top10", asof + i, mid)
            await asyncio.sleep(0.6)
        lt = topics.thread_id_for("cs_top10_links")
        if lt:
            await P.send_raw(client,
                             f"📌 <b>{d.month}월 {d.day}일자 top10 링크용 발행 시작합니다</b>", lt)
            await asyncio.sleep(0.5)
            for j, (t, u) in enumerate(K.render_links(picked, label, store)):
                mid = await P.send_raw(client, t, lt, preview_url=u)
                store.record_agg_message("cs_top10_links", asof + 100 + j, mid)
                await asyncio.sleep(0.6)
    # 다음 회차가 같은 기사를 또 뽑지 않게 기록한다
    _store_rows(store, picked)
    store.mark_cstop10([r[K.K_KEY] for _, r in picked],
                       datetime.fromtimestamp(asof, KST).strftime("%Y-%m-%d"))
    print(f"[curate] 발행 완료 · {len(picked)}건 기록")


if __name__ == "__main__":
    asyncio.run(main())
