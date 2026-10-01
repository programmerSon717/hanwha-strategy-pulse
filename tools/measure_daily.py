#!/usr/bin/env python3
"""하루치 발행량·한도·필터 실적을 재서 리포트 파일로 남긴다.

**클로드와 무관하게 돈다.** 맥의 launchd 가 정해진 시각에 이 스크립트를 실행하고,
결과는 `reports/YYYY-MM-DD.md` 에 쌓인다. 터미널을 닫든 세션을 새로 열든 남는다.

왜 필요한가: 2026-09-04 에 발행 범위를 규제 중심으로 바꾸고 가격 필터를 넣었다.
그 효과를 하루 뒤에 재기로 했는데, 클로드 세션 예약은 터미널이 닫히면 사라진다.

읽기만 한다 — 봇의 코드도 상태도 건드리지 않는다.

    python3 tools/measure_daily.py           # 오늘 기준으로 재고 파일로 남긴다
    python3 tools/measure_daily.py --stdout  # 파일 대신 화면에 출력
"""
import datetime
import os
import pathlib
import sqlite3
import subprocess
import sys
import collections

ROOT = pathlib.Path(__file__).resolve().parent.parent
KST = datetime.timezone(datetime.timedelta(hours=9))

BOTS = [
    ("한국어판", ROOT / "crypto-news-bot"),
    ("영문판",   ROOT / "crypto-news-bot-en"),
]
REPORT_BOT = ROOT / "crypto-news-report"

# 변경 전 기준선 (2026-09-03 실측)
BASELINE = {"한국어판": 357, "영문판": 273}
PREDICTED = {"한국어판": 214, "영문판": 156}


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True).stdout.strip()


def pull_db(repo: pathlib.Path) -> pathlib.Path | None:
    """원격 DB 를 임시로 받아온다. 로컬 작업물을 건드리지 않는다."""
    sh("git", "fetch", "-q", "origin", cwd=repo)
    dest = pathlib.Path("/tmp") / f"measure_{repo.name}.sqlite3"
    out = subprocess.run(["git", "show", "origin/main:botstate.sqlite3"],
                         cwd=repo, capture_output=True)
    if out.returncode != 0 or not out.stdout:
        return None
    dest.write_bytes(out.stdout)
    return dest


def publishes(db: pathlib.Path, day: datetime.date):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = [(cat or "?", ts) for cat, ts in
            con.execute("SELECT category, published_at FROM published")]
    con.close()
    same = [c for c, t in rows
            if datetime.datetime.fromtimestamp(t, KST).date() == day]
    return same, len(rows)


def quota(repo: pathlib.Path) -> str:
    """7개 모델 중 몇 개가 살아 있는지. 모델당 1회씩만 부른다."""
    code = (
        "import re, sys\n"
        "from google import genai\n"
        "from google.genai import types\n"
        "from config import settings\n"
        "import summarizer\n"
        "c = genai.Client(api_key=settings.gemini_api_key,"
        " http_options=types.HttpOptions(timeout=45000))\n"
        "cfg = types.GenerateContentConfig(response_mime_type='application/json',"
        " max_output_tokens=60)\n"
        "ok, dead = 0, []\n"
        "for m in summarizer._candidates():\n"
        "    try:\n"
        "        c.models.generate_content(model=m, contents='{\"ok\":true} 만', config=cfg)\n"
        "        ok += 1\n"
        "    except Exception as e:\n"
        "        q = re.search(r\"'quotaId': '([^']+)'\", str(e))\n"
        "        dead.append(m + (' 일일' if q and 'PerDay' in q.group(1) else ' 분당'))\n"
        "print(f'{ok}/7' + (' · 소진: ' + ', '.join(dead) if dead else ''))\n"
    )
    env = dict(os.environ, PYTHONPATH=str(repo))
    r = subprocess.run([str(repo / "venv/bin/python"), "-c", code],
                       cwd=repo, capture_output=True, text=True, env=env)
    line = [l for l in r.stdout.splitlines() if "/7" in l]
    return line[-1] if line else f"측정 실패 ({r.stderr.strip()[-120:]})"


def price_filter_rate(repo: pathlib.Path, db: pathlib.Path, day: datetime.date) -> str:
    """그날 발행분에 가격 필터를 돌려 몇 %가 걸리는지. 모델을 부르지 않는다."""
    code = (
        "import sqlite3, datetime, sys\n"
        "import prefilter\n"
        "from models import NewsItem\n"
        "KST = datetime.timezone(datetime.timedelta(hours=9))\n"
        f"day = datetime.date.fromisoformat('{day.isoformat()}')\n"
        f"con = sqlite3.connect('file:{db}?mode=ro', uri=True)\n"
        "rows = [h for h, t in con.execute('SELECT headline, published_at FROM published')\n"
        "        if datetime.datetime.fromtimestamp(t, KST).date() == day]\n"
        "mk = lambda h: NewsItem(source='x', unique_id='x', title=h, url='u',"
        " body='', region_hint='')\n"
        "hit = [h for h in rows if prefilter.is_price_story(mk(h))]\n"
        "print(f'발행 {len(rows)}건 중 가격으로 걸릴 것 {len(hit)}건'"
        " + (f' ({len(hit)/len(rows)*100:.0f}%)' if rows else ''))\n"
        "for h in hit[:5]: print('    · ' + h[:60])\n"
    )
    env = dict(os.environ, PYTHONPATH=str(repo))
    r = subprocess.run([str(repo / "venv/bin/python"), "-c", code],
                       cwd=repo, capture_output=True, text=True, env=env)
    return r.stdout.strip() or f"측정 실패 ({r.stderr.strip()[-120:]})"


def main():
    now = datetime.datetime.now(KST)
    today, yday = now.date(), now.date() - datetime.timedelta(days=1)
    out = [f"# 발행량·한도 측정 — {now:%Y-%m-%d %H:%M} KST", ""]
    out.append("2026-09-04 에 발행 범위를 규제 중심으로 바꾸고 가격 사전 필터를 넣었다.")
    out.append("그 효과를 재기 위한 자동 측정이다. 읽기만 하며 봇을 건드리지 않는다.")
    out.append("")

    for name, repo in BOTS:
        out.append(f"## {name}")
        db = pull_db(repo)
        if db is None:
            out.append("  DB 를 받지 못했다.\n")
            continue
        t_rows, total = publishes(db, today)
        y_rows, _ = publishes(db, yday)
        base, pred = BASELINE.get(name), PREDICTED.get(name)
        out.append(f"- 누적 {total}건")
        out.append(f"- 어제({yday}) {len(y_rows)}건 · 오늘({today}, 진행 중) {len(t_rows)}건")
        if base:
            d = len(y_rows) - base
            out.append(f"- 변경 전 기준선 09-03 {base}건 → 어제 {len(y_rows)}건 "
                       f"({d:+d}, {len(y_rows)/base*100:.0f}%)")
            out.append(f"- 예측치 {pred}건 대비 {len(y_rows)-pred:+d}건")
        cnt = collections.Counter(y_rows)
        out.append(f"- 어제 탭별: {dict(cnt.most_common(8))}")
        out.append(f"- 한도: {quota(repo)}")
        out.append(f"- 가격 필터: {price_filter_rate(repo, db, yday)}")
        out.append("")

    if REPORT_BOT.exists():
        out.append("## 리포트봇")
        out.append(f"- 한도: {quota(REPORT_BOT)}")
        rp = REPORT_BOT / "reportstate.sqlite3"
        if rp.exists():
            con = sqlite3.connect(f"file:{rp}?mode=ro", uri=True)
            rows = con.execute("SELECT kind, label FROM reports ORDER BY published_at DESC LIMIT 5").fetchall()
            con.close()
            out.append(f"- 최근 보고서: {[f'{k}:{l}' for k, l in rows] or '없음'}")
        out.append("")

    out.append("---")
    out.append("이 파일은 launchd 가 자동으로 만든 것이다. 클로드 세션과 무관하게 남는다.")
    text = "\n".join(out)

    if "--stdout" in sys.argv:
        print(text)
        return
    d = ROOT / "reports"
    d.mkdir(exist_ok=True)
    f = d / f"{today}.md"
    f.write_text(text, encoding="utf-8")
    print(f"작성: {f}")


if __name__ == "__main__":
    main()
