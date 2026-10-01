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
                ("why_it_matters", "TEXT"),
                ("confidence", "REAL"),
            ):
                if col not in cols:
                    c.execute(f"ALTER TABLE published ADD COLUMN {col} {typ}")
            for idx, expr in (
                ("idx_pub_score", "published(strategic_score)"),
                ("idx_pub_cluster", "published(event_cluster_id)"),
                ("idx_pub_brief", "published(daily_brief_date)"),
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
                "       lede, why_it_matters, sent_at"
                "  FROM published"
                " WHERE strategic_score IS NOT NULL"
                "   AND strategic_score >= ?"
                "   AND sent_at > ? AND sent_at <= ?"
                "   AND (daily_brief_date IS NULL OR daily_brief_date='')"
                " ORDER BY strategic_score DESC, sent_at DESC",
                (min_score, since_ts, until_ts),
            ).fetchall()

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

    def record_brief(self, cutoff: float, message_id: int | None):
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO digest_log (scope, window_end, message_id)"
                " VALUES ('morning_brief', ?, ?)", (cutoff, message_id))
