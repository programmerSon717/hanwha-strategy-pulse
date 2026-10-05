"""발행 이력 저장소. URL(또는 소스별 고유 ID) 기준으로 중복 발행을 막는다."""
import hashlib
import re
import sqlite3
import time
from contextlib import contextmanager
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 같은 기사인데 URL 만 달라 보이게 만드는 추적 파라미터.
# 예: 블록미디어는 RSS 링크에 ?utm_source=general&utm_medium=rss 를 붙인다.
_TRACKING = re.compile(r"^(utm_.*|fbclid|gclid)$")


def normalize_url(url: str) -> str:
    """같은 기사면 같은 문자열이 되도록 URL 을 정리한다.

    한 매체를 여러 피드로 등록하면(일반 피드 + 규제 피드 등) 같은 기사가
    두 번 들어온다. 중복제거 키는 '소스이름+고유값' 해시라 소스 이름이 다르면
    같은 기사도 다른 키가 되어 두 번 발행된다. 그걸 막으려고 URL 을 정규화해
    소스와 무관한 두 번째 잣대로 쓴다.

    스킴(http/https)과 www, 끝 슬래시, 추적 파라미터, 프래그먼트를 떼어낸다.
    기사 식별에 쓰이는 일반 쿼리(?id=123 등)는 남긴다.
    """
    if not url:
        return ""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                       if not _TRACKING.match(k)])
    path = p.path.rstrip("/") or "/"
    return urlunsplit(("", host, path, query, "")).lstrip("/") or url.strip().lower()


def _as_float(v) -> float | None:
    """origin_at 은 ALTER TABLE 로 붙인 TEXT 컬럼이라 문자열로 돌아온다."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class Store:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS seen (
                    key TEXT PRIMARY KEY,
                    source TEXT,
                    title TEXT,
                    published_at REAL
                )"""
            )
            # 발행한 메시지의 id. 나중에 형식을 고쳐 수정(editMessageText)하거나
            # 잘못 나간 글을 지우려면 id가 있어야 한다. 없으면 손댈 방법이 없다.
            c.execute(
                """CREATE TABLE IF NOT EXISTS published (
                    key TEXT PRIMARY KEY,
                    message_id INTEGER,
                    thread_id INTEGER,
                    source_url TEXT,
                    headline TEXT,
                    published_at REAL
                )"""
            )
            # 시간별 다이제스트를 만들려면 헤드라인만으로는 부족해 분류·요약문도 남긴다.
            # 이미 만들어진 DB에도 적용되도록 없을 때만 컬럼을 추가한다.
            cols = {r[1] for r in c.execute("PRAGMA table_info(published)")}
            # text 는 발행 원문(HTML). 나중에 다른 탭으로 옮길 때 그대로 다시 쓸 수 있다.
            # extra_ids: 한 글이 사진+본문 두 메시지로 나갈 때 나머지 id(쉼표 구분)
            # 재정렬(--resort)에 필요한 것들:
            #   photo_file_id — 사진을 다시 올릴 때 재업로드 없이 그대로 재사용
            #   origin_at     — 기사/트윗의 원래 게시 시각. 정렬 기준(발행 시각이 아님)
            # mirror_ids: 같은 글을 다른 탭에도 올렸을 때 그쪽 message_id(쉼표 구분).
            # extra_ids 와 섞으면 안 된다 — extra_ids 는 '같은 탭의 딸린 메시지'라
            # --resort 가 한 탭으로 다시 몰아넣는다. 미러는 다른 탭에 있어야 한다.
            for col in ("category", "lede", "text", "extra_ids",
                        "photo_file_id", "origin_at", "mirror_ids"):
                if col not in cols:
                    c.execute(f"ALTER TABLE published ADD COLUMN {col} TEXT")

            # ── Strategy Pulse 컬럼 (스펙 §34) ──
            # 기존 스키마를 파괴하지 않고 필요한 것만 덧붙인다. 기존 행(크립토 봇이
            # 남긴 발행 이력)은 이 컬럼이 전부 NULL 로 남는다 — 그게 곧 구분자다.
            # Morning Brief·주요이슈 조회는 strategic_score IS NOT NULL 로 거른다.
            for col, typ in (
                ("canonical_url", "TEXT"),      # 정규화 URL
                ("collected_at", "REAL"),       # 수집 시각(KST epoch)
                ("sent_at", "REAL"),            # 전송 시각
                ("primary_topic", "TEXT"),
                ("secondary_topics", "TEXT"),   # 쉼표 구분
                ("strategic_score", "INTEGER"),
                ("is_key_issue", "INTEGER"),
                ("event_cluster_id", "TEXT"),   # events.fingerprint
                ("event_type", "TEXT"),
                ("main_entities", "TEXT"),      # 쉼표 구분(정식명)
                ("content_hash", "TEXT"),
                ("daily_brief_date", "TEXT"),   # 브리프에 실린 날짜(YYYY-MM-DD)
                ("cs_top10_date", "TEXT"),      # 📌 A팀 Top10 에 실린 날짜
                ("why_it_matters", "TEXT"),
                ("confidence", "REAL"),
            ):
                if col not in cols:
                    c.execute(f"ALTER TABLE published ADD COLUMN {col} {typ}")
            for idx, expr in (
                ("idx_pub_score", "published(strategic_score)"),
                ("idx_pub_cluster", "published(event_cluster_id)"),
                ("idx_pub_brief", "published(daily_brief_date)"),
                ("idx_pub_top10", "published(cs_top10_date)"),
            ):
                c.execute(f"CREATE INDEX IF NOT EXISTS {idx} ON {expr}")

            # ── 판정 감사 로그 (§35) ──
            # 왜 기사가 포함되거나 제외됐는지 나중에 확인할 수 있어야 한다.
            # 발행되지 않은 기사도 남는다 — 그게 이 표의 존재 이유다.
            c.execute(
                """CREATE TABLE IF NOT EXISTS judgment (
                    key TEXT PRIMARY KEY,
                    ts REAL,
                    title TEXT,
                    source TEXT,
                    url TEXT,
                    relevant INTEGER,
                    score INTEGER,
                    primary_topic TEXT,
                    reason TEXT,
                    duplicate_event TEXT,
                    sent INTEGER
                )"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_judg_ts ON judgment(ts)")
            # 소스 이름과 무관하게 '이 기사를 이미 봤는가'를 판정하는 색인.
            # seen 은 소스이름+고유값 해시라 같은 기사가 다른 피드로 들어오면 못 잡는다.
            c.execute(
                """CREATE TABLE IF NOT EXISTS seen_urls (
                    url_key TEXT PRIMARY KEY,
                    source TEXT,
                    title TEXT,
                    first_seen REAL
                )"""
            )
            # 이미 발행된 글들을 색인에 한 번 채워 넣는다(표가 비었을 때만).
            # 이게 없으면 도입 직후 과거 기사가 다시 발행될 수 있다.
            if not c.execute("SELECT 1 FROM seen_urls LIMIT 1").fetchone():
                rows = c.execute(
                    "SELECT source_url, headline FROM published WHERE source_url IS NOT NULL"
                ).fetchall()
                now = time.time()
                c.executemany(
                    "INSERT OR IGNORE INTO seen_urls (url_key, source, title, first_seen)"
                    " VALUES (?,?,?,?)",
                    [(normalize_url(u), "backfill", t or "", now)
                     for u, t in rows if normalize_url(u)],
                )
                if rows:
                    print(f"[store] 기존 발행 {len(rows)}건을 URL 색인에 등록")

            # 다이제스트 중복 발행 방지용 — 어느 구간까지 요약했는지 기록
            c.execute(
                """CREATE TABLE IF NOT EXISTS digest_log (
                    scope TEXT,
                    window_end REAL,
                    message_id INTEGER,
                    PRIMARY KEY (scope, window_end)
                )"""
            )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def make_key(source: str, unique: str) -> str:
        return hashlib.sha256(f"{source}::{unique}".encode()).hexdigest()

    def is_seen(self, key: str) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone()
            return row is not None

    def is_url_seen(self, url: str) -> bool:
        """소스 이름과 무관하게, 이 기사를 이미 처리했는가."""
        k = normalize_url(url)
        if not k:
            return False
        with self._conn() as c:
            return c.execute("SELECT 1 FROM seen_urls WHERE url_key=?", (k,)).fetchone() is not None

    def mark_url_seen(self, url: str, source: str, title: str):
        k = normalize_url(url)
        if not k:
            return
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO seen_urls (url_key, source, title, first_seen)"
                " VALUES (?,?,?,?)",
                (k, source, title, time.time()),
            )

    def forget_unpublished(self, keys: list, urls: list) -> int:
        """**발행되지 않은** 기사의 '봤음' 기록을 지운다. 소급 수집 재실행용.

        process_items 는 나이·예산 판정보다 **먼저** mark_seen 을 한다. 그래서
        소급 수집이 중간에 한 번 어긋나면(예: 나이 제한에 전부 걸림) 284건이
        전부 '봤음' 으로 남아 다시 돌려도 통째로 건너뛴다(2026-10-05).

        발행까지 간 것은 건드리지 않는다 — 그건 지워야 할 기억이 아니다.
        """
        if not keys and not urls:
            return 0
        with self._conn() as c:
            pub = {r[0] for r in c.execute("SELECT key FROM published")}
            tgt = [k for k in keys if k not in pub]
            n = 0
            for i in range(0, len(tgt), 400):
                chunk = tgt[i:i + 400]
                q = ",".join("?" * len(chunk))
                n += c.execute(f"DELETE FROM seen WHERE key IN ({q})", chunk).rowcount
            pub_urls = {normalize_url(r[0]) for r in
                        c.execute("SELECT source_url FROM published") if r[0]}
            uk = [normalize_url(u) for u in urls]
            uk = [u for u in uk if u and u not in pub_urls]
            for i in range(0, len(uk), 400):
                chunk = uk[i:i + 400]
                q = ",".join("?" * len(chunk))
                c.execute(f"DELETE FROM seen_urls WHERE url_key IN ({q})", chunk)
            return n

    def mark_seen(self, key: str, source: str, title: str):
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO seen (key, source, title, published_at) VALUES (?,?,?,?)",
                (key, source, title, time.time()),
            )

    def record_published(self, key: str, message_id: int, thread_id: int | None,
                         source_url: str, headline: str,
                         category: str = "", lede: str = "", text: str = "",
                         extra_ids: list | None = None, photo_file_id: str = "",
                         origin_at: float | None = None,
                         mirror_ids: list | None = None):
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO published
                   (key, message_id, thread_id, source_url, headline,
                    published_at, category, lede, text, extra_ids,
                    photo_file_id, origin_at, mirror_ids)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, message_id, thread_id, source_url, headline, time.time(),
                 category, lede, text, ",".join(str(i) for i in (extra_ids or [])),
                 photo_file_id, origin_at,
                 ",".join(str(i) for i in (mirror_ids or []))),
            )

    def all_published(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                """SELECT key, message_id, thread_id, category, headline, lede, text,
                          extra_ids, photo_file_id, origin_at, mirror_ids
                   FROM published ORDER BY published_at"""
            ).fetchall()
        return [
            {"key": r[0], "message_id": r[1], "thread_id": r[2], "category": r[3] or "",
             "headline": r[4] or "", "lede": r[5] or "", "text": r[6] or "",
             "extra_ids": [int(x) for x in (r[7] or "").split(",") if x.strip()],
             "photo_file_id": r[8] or "", "origin_at": _as_float(r[9]),
             "mirror_ids": [int(x) for x in (r[10] or "").split(",") if x.strip()]}
            for r in rows
        ]

    def update_published_ids(self, key: str, message_id: int, extra_ids: list):
        """재정렬로 메시지를 다시 올린 뒤 새 id 로 갱신한다."""
        with self._conn() as c:
            c.execute(
                "UPDATE published SET message_id=?, extra_ids=? WHERE key=?",
                (message_id, ",".join(str(i) for i in extra_ids), key),
            )

    def update_published_location(self, key: str, message_id: int,
                                  thread_id: int | None, category: str):
        with self._conn() as c:
            c.execute(
                "UPDATE published SET message_id=?, thread_id=?, category=? WHERE key=?",
                (message_id, thread_id, category, key),
            )

    def published_between(self, start: float, end: float) -> list[dict]:
        """구간 안에 발행된 글 목록(오래된 순). 다이제스트 재료."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT category, headline, lede, source_url, message_id, published_at
                   FROM published
                   WHERE published_at >= ? AND published_at < ?
                   ORDER BY published_at""",
                (start, end),
            ).fetchall()
        return [
            {"category": r[0] or "이슈", "headline": r[1], "lede": r[2] or "",
             "source_url": r[3] or "", "message_id": r[4], "published_at": r[5]}
            for r in rows
        ]

    def recent_for_dedup(self, hours: int = 12, limit: int = 10) -> list[dict]:
        """중복 판정에 쓸 최근 발행 목록.

        모델에게 "이미 이런 게 나갔다"고 알려주려는 것이라 헤드라인과 한 줄 요약만
        보낸다. 본문까지 실으면 토큰만 늘고 판정은 나아지지 않는다.
        """
        since = time.time() - hours * 3600
        with self._conn() as c:
            rows = c.execute(
                """SELECT headline, lede, published_at, source_url
                   FROM published WHERE published_at >= ?
                   ORDER BY published_at DESC LIMIT ?""",
                (since, limit),
            ).fetchall()
        out = []
        for headline, lede, ts, url in rows:
            host = ""
            if url:
                host = url.split("//")[-1].split("/")[0].replace("www.", "")
            out.append({
                "headline": headline or "",
                "lede": lede or "",
                "when": time.strftime("%m-%d %H:%M", time.localtime(ts)),
                "source": host or "이전 발행",
            })
        return out

    def digest_done(self, scope: str, window_end: float) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM digest_log WHERE scope=? AND window_end=?",
                (scope, window_end),
            ).fetchone()
            return row is not None

    def record_digest(self, scope: str, window_end: float, message_id: int):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO digest_log (scope, window_end, message_id) VALUES (?,?,?)",
                (scope, window_end, message_id),
            )

    def drop_published(self, key: str):
        """발행 기록만 지운다. seen 은 남겨 다시 수집되지 않게 한다."""
        with self._conn() as c:
            c.execute("DELETE FROM published WHERE key=?", (key,))

    def forget(self, key: str):
        """재발행할 수 있도록 이력에서 지운다."""
        with self._conn() as c:
            c.execute("DELETE FROM seen WHERE key=?", (key,))
            c.execute("DELETE FROM published WHERE key=?", (key,))


    # ─────────────────────────────────────────────────────────
    # Strategy Pulse 전용 조회/기록 (스펙 §34, §35)
    # 기존 메서드는 손대지 않는다.
    # ─────────────────────────────────────────────────────────
    def log_judgment(self, key: str, *, title: str, source: str, url: str,
                     relevant: bool, score: int | None, primary_topic: str | None,
                     reason: str, duplicate_event: str | None = None,
                     sent: bool = False):
        """기사 한 건의 판정 결과를 남긴다 (§35).

        LLM 원문 프롬프트나 시크릿은 절대 넣지 않는다 (§35, §41).
        """
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO judgment"
                " (key, ts, title, source, url, relevant, score, primary_topic,"
                "  reason, duplicate_event, sent)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (key, time.time(), title[:300], source, url,
                 int(bool(relevant)), score, primary_topic,
                 (reason or "")[:300], duplicate_event, int(bool(sent))),
            )

    def save_bsp_fields(self, key: str, data: dict, *, cluster_id: str | None,
                        canonical: str, collected_at: float):
        """발행 직후 BSP 판정 결과를 published 행에 채운다."""
        with self._conn() as c:
            c.execute(
                "UPDATE published SET canonical_url=?, collected_at=?, sent_at=?,"
                " primary_topic=?, secondary_topics=?, strategic_score=?,"
                " is_key_issue=?, event_cluster_id=?, event_type=?, main_entities=?,"
                " why_it_matters=?, confidence=? WHERE key=?",
                (canonical, collected_at, time.time(),
                 data.get("primary_topic"),
                 ",".join(data.get("secondary_topics") or []),
                 data.get("strategic_score"),
                 int(bool(data.get("is_key_issue"))),
                 cluster_id, data.get("event_type"),
                 ",".join(data.get("main_entities") or []),
                 (data.get("why_it_matters") or "")[:500],
                 data.get("confidence"), key),
            )

    def cluster_seen(self, cluster_id: str, within_sec: float) -> tuple | None:
        """같은 Event 를 이미 발행했는가 (§21). (headline, score, sent_at) 또는 None."""
        if not cluster_id:
            return None
        with self._conn() as c:
            return c.execute(
                "SELECT headline, strategic_score, sent_at FROM published"
                " WHERE event_cluster_id=? AND sent_at > ?"
                " ORDER BY sent_at DESC LIMIT 1",
                (cluster_id, time.time() - within_sec),
            ).fetchone()

    def recent_events(self, within_sec: float) -> list:
        """최근 발행분의 (제목, 엔티티, 종류, 발행시각).

        **회차 간 중복 판정용.** cluster_seen 은 event_cluster_id 문자열
        완전일치만 본다. 그런데 모델이 같은 사건의 main_entities 를 기사마다
        다르게 적어 fingerprint 가 갈리므로, 회차가 바뀌면 같은 사건이 그대로
        또 나간다. 실측(2026-10-05): 아부다비 디지털금융 건이 17:24 와 19:01
        에 두 번 발행됐다. 애큐온 딜은 cluster_id 가 5가지로 갈렸다.

        그래서 실제 값을 돌려주고 events.same_event 로 다시 비교한다.
        """
        with self._conn() as c:
            return c.execute(
                "SELECT headline, main_entities, event_type, sent_at"
                "  FROM published"
                " WHERE sent_at > ? AND headline IS NOT NULL AND headline <> ''"
                " ORDER BY sent_at DESC LIMIT 400",
                (time.time() - within_sec,)).fetchall()

    def key_issues_since(self, since_ts: float) -> int:
        """하루 주요이슈 발행 건수 — 과도 발행 방지 (§20)."""
        with self._conn() as c:
            return c.execute(
                "SELECT COUNT(*) FROM published"
                " WHERE is_key_issue=1 AND sent_at > ?", (since_ts,)
            ).fetchone()[0]

    def brief_candidates(self, since_ts: float, until_ts: float,
                         min_score: int) -> list:
        """Morning Brief 후보 (§24-1). cutoff window 기반.

        strategic_score IS NOT NULL 조건이 크립토 봇 시절 행을 자동으로 배제한다.
        """
        with self._conn() as c:
            return c.execute(
                "SELECT key, headline, source_url, primary_topic, secondary_topics,"
                "       strategic_score, is_key_issue, event_cluster_id, main_entities,"
                "       lede, why_it_matters, sent_at, event_type, origin_at, text"
                "  FROM published"
                " WHERE strategic_score IS NOT NULL"
                "   AND strategic_score >= ?"
                "   AND sent_at > ? AND sent_at <= ?"
                "   AND (daily_brief_date IS NULL OR daily_brief_date='')"
                " ORDER BY strategic_score DESC, sent_at DESC",
                (min_score, since_ts, until_ts),
            ).fetchall()

    def cstop10_candidates(self, since_ts: float, until_ts: float,
                           min_score: int, by_origin: bool = False) -> list:
        """📌 A팀 Top10 후보.

        brief_candidates 와 다른 점: **이미 Top10 에 실린 기사를 뺀다.**
        Morning Brief 의 daily_brief_date 와는 별개 컬럼이다 — 두 탭은 선정
        기준이 달라 한쪽에 실렸다고 다른 쪽에서 빼면 안 된다.

        by_origin: 창을 **기사 원문 발행시각**으로 자른다. 소급 생성 전용이다.
          평소에는 sent_at(봇이 내보낸 시각)이 맞다 — Top10 은 "그날 팀에
          나간 것 중의 Top10" 이기 때문이다. 그런데 봇이 멈춰 있던 날은
          sent_at 이 통째로 비어 후보가 0건이 된다(10/4·10/5 실측). 그런 날을
          뒤늦게 만들 때는 "그날 **보도된** 기사" 로 잡아야 뜻이 맞는다.
        """
        _col = "origin_at" if by_origin else "sent_at"
        with self._conn() as c:
            return c.execute(
                "SELECT key, headline, source_url, primary_topic, secondary_topics,"
                "       strategic_score, is_key_issue, event_cluster_id, main_entities,"
                "       lede, why_it_matters, sent_at, event_type, origin_at, text"
                "  FROM published"
                " WHERE strategic_score IS NOT NULL"
                "   AND strategic_score >= ?"
                f"   AND {_col} > ? AND {_col} <= ?"
                "   AND (cs_top10_date IS NULL OR cs_top10_date='')"
                # 본문을 못 읽어 확신이 낮은 건은 Top10 에도 올리지 않는다
                # (2026-10-05 사용자 지정). 제목·요약만으로 정리한 글은
                # 내용을 보증할 수 없어 Top10 에 실을 값이 없다.
                "   AND (confidence IS NULL OR confidence >= 0.5)"
                " ORDER BY strategic_score DESC, sent_at DESC",
                (min_score, since_ts, until_ts),
            ).fetchall()

    def key_issue_cluster_seen(self, cluster_id: str,
                               hours: int = 24) -> bool:
        """그 사건이 **오늘 이미 주요이슈로 나갔는가.**

        주요이슈는 집계 토픽이라 원 토픽과 중복 게시를 허용한다. 그러나
        같은 사건의 다른 보도까지 다 올리면 큐레이션이 아니라 중복이 된다
        (2026-10-05 감사: 하루 23건, 그중 12건이 같은 딜).
        """
        if not cluster_id:
            return False
        import time as _t
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM published"
                " WHERE event_cluster_id = ?"
                # 주요이슈는 이제 **그 탭 하나에만** 올린다(미러 폐지).
                # 옛 데이터의 미러도 함께 센다.
                "   AND (primary_topic = 'key_issues' OR category = 'key_issues'"
                "        OR (mirror_ids IS NOT NULL AND mirror_ids != ''))"
                "   AND sent_at > ?"
                " LIMIT 1",
                (cluster_id, _t.time() - hours * 3600)).fetchone()
        return bool(row)

    def published_clusters_before(self, before_ts: float,
                                  since_ts: float | None = None) -> list:
        """**창 시작 전에 이미 일반 탭으로 나간** 사건들의 (제목, 엔티티, 종류).

        Top10 의 기게재 제외가 '이전 Top10 에 실렸던 것'만 봐서, 일반 탭에
        여러 번 나간 애큐온 인수 건이 Top10 에 또 올라갔다(2026-10-05 지적).
        팀은 이미 그 사건을 봤으므로 Top10 에서 다시 볼 이유가 없다.

        창 **안**에서 발행된 것은 제외하지 않는다 — 그건 오늘 처음 전한
        뉴스이고, Top10 은 원래 그날 나간 것 중에서 고르는 물건이다.
        """
        since_ts = since_ts if since_ts is not None else before_ts - 14 * 24 * 3600
        with self._conn() as c:
            return c.execute(
                "SELECT headline, main_entities, event_type FROM published"
                " WHERE sent_at < ? AND sent_at >= ?"
                "   AND headline IS NOT NULL AND headline <> ''",
                (before_ts, since_ts)).fetchall()

    def top10_article_keys(self, exclude_date: str = "") -> set:
        """이미 어느 날짜든 Top10 에 실린 기사들의 **기사 식별키** 집합.

        키는 "호스트#기사번호" 다. 같은 기사라도 봇은 구글뉴스 리디렉터를,
        사람이 큐레이션한 행은 풀린 원문 주소를 저장해 URL 문자열 비교가
        통하지 않는다. 엔티티 비교(_same)도 main_entities 가 빈 행에서는
        무조건 '다른 사건' 으로 빠진다 — 실제로 뱅크샐러드 기사가 그래서
        10/4 와 10/5 양쪽에 실렸다(2026-10-05 감사).

        그래서 주소에서 기사 번호만 뽑아 맞춰 본다. 이건 엔티티·제목과
        무관하게 **같은 기사면 반드시 걸린다.**
        """
        import re as _re
        pat = _re.compile(r"idxno=(\d+)|newsId=(\w+)|/v/(\d+)|ncode=(\w+)"
                          r"|AKR(\d+)|articles/(\d+)|key=(\w+)")
        out = set()
        with self._conn() as c:
            rows = c.execute(
                "SELECT cs_top10_date, source_url, canonical_url FROM published"
                " WHERE cs_top10_date LIKE '____-__-__'").fetchall()
        # **풀린 주소까지 본다.** 봇은 구글뉴스 리디렉터를 저장하므로 두 컬럼만
        # 보면 기사번호를 못 뽑는다 — Top10 39건 중 27건(69%)이 그랬고, 그래서
        # 같은 기사가 다른 날 Top10 에 다시 실렸다(2026-10-05 감사).
        with self._conn() as c:
            resolved = {a: b for a, b in
                        c.execute("SELECT src, dst FROM resolved_url")}
        for d, su, cu in rows:
            if exclude_date and d == exclude_date:
                continue
            for u in (su, cu, resolved.get(su), resolved.get(cu)):
                if not u:
                    continue
                m = pat.search(u)
                if not m:
                    continue
                num = next((g for g in m.groups() if g), "")
                host = _re.sub(r"^https?://", "", u).split("/")[0]
                host = _re.sub(r"^(www\.|m\.|view\.|news\.|biz\.)", "", host)
                if num and "google" not in host:
                    out.add(f"{host}#{num}")
        return out

    def save_top10_draft(self, publish_date: str, keys: list,
                         quality: float, built_at: float):
        """다음 발행분 **초안**을 저장한다. 같은 날짜는 덮어쓴다.

        왜 초안을 미리 만드나(2026-10-05 사용자 지정): 06:50 에 즉석에서 뽑으면
        "이상하게 만들어지거나·전날자를 못 가져오거나·10건이 안 되는" 사고를
        그 자리에서 알 수 없다. 전날 18:00·22:00·당일 04:00 에 미리 만들어 두고,
        **뒤에 만든 것이 더 좋으면 갈아치운다.** 06:50 은 확정된 초안을 내보내기만
        한다. 품질도 올라가고, 사고가 나도 06:50 전에 드러난다.
        """
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS top10_draft (
                             publish_date TEXT PRIMARY KEY,
                             keys TEXT, quality REAL, built_at REAL)""")
            c.execute("INSERT OR REPLACE INTO top10_draft"
                      " (publish_date, keys, quality, built_at) VALUES (?,?,?,?)",
                      (publish_date, ",".join(keys), quality, built_at))

    def get_top10_draft(self, publish_date: str):
        """(keys, quality, built_at) 또는 None."""
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS top10_draft (
                             publish_date TEXT PRIMARY KEY,
                             keys TEXT, quality REAL, built_at REAL)""")
            row = c.execute("SELECT keys, quality, built_at FROM top10_draft"
                            " WHERE publish_date=?", (publish_date,)).fetchone()
        if not row:
            return None
        return ([k for k in (row[0] or "").split(",") if k], row[1], row[2])

    def rows_by_keys(self, keys: list) -> list:
        """키 목록을 cstop10_candidates 와 **같은 컬럼 순서**로 되돌린다.

        초안은 키만 저장한다. 본문·요약은 published 에 이미 있으므로 발행
        시점에 다시 읽는다 — 초안에 본문을 복사해 두면 그 사이 갱신된 내용을
        놓친다.
        """
        if not keys:
            return []
        q = ",".join("?" * len(keys))
        with self._conn() as c:
            # **후보 쿼리와 같은 관문을 건다.** 예전엔 키만 보고 그대로 꺼내서,
            # 초안을 만든 뒤 그 기사가 다른 경로로 이미 Top10 에 실려도 06:50 에
            # 또 나갔다. 본문 못 읽는 기사도 그대로 통과했다(2026-10-05 감사).
            rows = c.execute(
                "SELECT key, headline, source_url, primary_topic, secondary_topics,"
                "       strategic_score, is_key_issue, event_cluster_id, main_entities,"
                "       lede, why_it_matters, sent_at, event_type, origin_at, text"
                f"  FROM published WHERE key IN ({q})"
                "   AND (cs_top10_date IS NULL OR cs_top10_date='')"
                "   AND (confidence IS NULL OR confidence >= 0.5)",
                keys).fetchall()
        order = {k: i for i, k in enumerate(keys)}
        return sorted(rows, key=lambda r: order.get(r[0], 999))

    def mark_cstop10(self, keys: list, date_str: str):
        with self._conn() as c:
            c.executemany("UPDATE published SET cs_top10_date=? WHERE key=?",
                          [(date_str, k) for k in keys])

    def cstop10_recent_clusters(self, since_ts: float) -> list:
        """최근 Top10 에 실린 기사들의 (제목, 엔티티, event_type).

        저장된 cluster_id 로는 같은 사건을 못 묶는다 — 모델이 main_entities 를
        기사마다 다르게 적어 fingerprint 가 갈린다(애큐온캐피탈 건은 5가지로
        갈렸다). 그래서 실제 값으로 다시 비교한다.
        """
        with self._conn() as c:
            return c.execute(
                "SELECT headline, main_entities, event_type FROM published"
                # 실제 날짜(YYYY-MM-DD)만 센다. 중복으로 지운 글에 찍는
                # 'DELETED-DUP' 같은 표식까지 세면 "이전 Top10 에 실렸다"로
                # 오인해 멀쩡한 후보를 떨군다(2026-10-05: 10/5 분이 0건이 됐다).
                " WHERE cs_top10_date LIKE '____-__-__'"
                "   AND sent_at > ?", (since_ts,)).fetchall()

    def mark_briefed(self, keys: list, date_str: str):
        with self._conn() as c:
            c.executemany("UPDATE published SET daily_brief_date=? WHERE key=?",
                          [(date_str, k) for k in keys])

    def last_brief_cutoff(self) -> float | None:
        """직전 Morning Brief 의 cutoff (§22). 없으면 None."""
        with self._conn() as c:
            row = c.execute(
                "SELECT window_end FROM digest_log WHERE scope='morning_brief'"
                " ORDER BY window_end DESC LIMIT 1"
            ).fetchone()
        return row[0] if row else None

    def agg_ran_on(self, scope: str, day_start: float, day_end: float) -> bool:
        """그 날 안에 해당 집계 탭이 발행한 적이 있는가."""
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM digest_log WHERE scope=?"
                "   AND window_end >= ? AND window_end < ? LIMIT 1",
                (f"msg:{scope}", day_start, day_end)).fetchone()
        return bool(row)

    def get_setting(self, key: str) -> str | None:
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS setting ("
                      "  k TEXT PRIMARY KEY, v TEXT)")
            row = c.execute("SELECT v FROM setting WHERE k=?", (key,)).fetchone()
        return row[0] if row else None

    def put_setting(self, key: str, val: str):
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS setting ("
                      "  k TEXT PRIMARY KEY, v TEXT)")
            c.execute("INSERT OR REPLACE INTO setting (k, v) VALUES (?,?)",
                      (key, val))

    def get_iv_url(self, key: str) -> str | None:
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS iv_page ("
                      "  key TEXT PRIMARY KEY, url TEXT, ts REAL)")
            row = c.execute("SELECT url FROM iv_page WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def put_iv_url(self, key: str, url: str):
        import time
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS iv_page ("
                      "  key TEXT PRIMARY KEY, url TEXT, ts REAL)")
            c.execute("INSERT OR REPLACE INTO iv_page (key, url, ts)"
                      " VALUES (?,?,?)", (key, url, time.time()))

    def get_resolved_url(self, src: str) -> str | None:
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS resolved_url ("
                      "  src TEXT PRIMARY KEY, dst TEXT, ts REAL)")
            row = c.execute("SELECT dst FROM resolved_url WHERE src=?",
                            (src,)).fetchone()
        return row[0] if row else None

    def put_resolved_url(self, src: str, dst: str):
        import time
        with self._conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS resolved_url ("
                      "  src TEXT PRIMARY KEY, dst TEXT, ts REAL)")
            c.execute("INSERT OR REPLACE INTO resolved_url (src, dst, ts)"
                      " VALUES (?,?,?)", (src, dst, time.time()))

    def record_agg_message(self, scope: str, ts: float, message_id: int | None):
        """집계 탭(🚨·☀️·📌) 발행분의 message_id 를 남긴다.

        **왜 남기나.** 2026-10-01 에 📌 A팀 Top10 테스트 발행을 지우려다
        message_id 가 없어 탭을 통째로 삭제·재생성해야 했다. thread_id 가 바뀌어
        .env·topics.json·GitHub Secret 을 전부 고쳐야 했다. 기록해 두면
        해당 메시지만 지우면 된다.
        """
        if message_id is None:
            return
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO digest_log (scope, window_end, message_id)"
                " VALUES (?, ?, ?)", (f"msg:{scope}", ts, message_id))

    def agg_messages(self, scope: str, limit: int = 50) -> list[tuple]:
        """해당 집계 탭이 발행한 (시각, message_id) 목록. 최신순."""
        with self._conn() as c:
            return c.execute(
                "SELECT window_end, message_id FROM digest_log"
                " WHERE scope=? ORDER BY window_end DESC LIMIT ?",
                (f"msg:{scope}", limit)).fetchall()

    def forget_agg_message(self, scope: str, message_id: int):
        with self._conn() as c:
            c.execute("DELETE FROM digest_log WHERE scope=? AND message_id=?",
                      (f"msg:{scope}", message_id))

    def last_pr_pick(self) -> float | None:
        """📌 A팀 Top10 에 홍보성 기사를 마지막으로 실은 시각. 없으면 None.

        A팀은 한화 홍보성 기사도 공유하지만 매일은 아니다. 이틀에 한 건,
        그중 가장 큰 것만 싣는다(2026-10-01 사용자 지정).
        """
        with self._conn() as c:
            row = c.execute(
                "SELECT window_end FROM digest_log WHERE scope='cstop10_pr'"
                " ORDER BY window_end DESC LIMIT 1"
            ).fetchone()
        return row[0] if row else None

    def record_pr_pick(self, ts: float, message_id: int | None = None):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO digest_log (scope, window_end, message_id)"
                " VALUES ('cstop10_pr', ?, ?)", (ts, message_id))

    def record_brief(self, cutoff: float, message_id: int | None):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO digest_log (scope, window_end, message_id)"
                " VALUES ('morning_brief', ?, ?)", (cutoff, message_id))
