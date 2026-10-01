"""봇 운영 상태를 한 덩어리 JSON 으로 모은다. 웹 어드민 페이지의 원료.

네트워크가 막히거나 형식이 바뀌어도 **부분 실패로 끝나야** 한다 —
한 항목이 죽어도 나머지는 채운다. 상태판이 통째로 안 뜨는 게 최악이다.
"""
import collections
import datetime
import json
import os
import re
import subprocess
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KST = datetime.timezone(datetime.timedelta(hours=9))
REPO_API = "https://api.github.com/repos/programmerSon717/crypto-news-bot"
BACKUPS = os.path.expanduser("~/Desktop/HanwhaDAPnews/backups")


def sh(*args, cwd=ROOT):
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True,
                              text=True, timeout=30).stdout.strip()
    except Exception:
        return ""


def kst(ts):
    return datetime.datetime.fromtimestamp(ts, KST).isoformat()


def get_json(url):
    try:
        import httpx
        r = httpx.get(url, headers={"Accept": "application/vnd.github+json"}, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"_error": f"{type(e).__name__}: {e}"[:160]}


def collect_git():
    sh("git", "fetch", "-q", "origin")
    log = sh("git", "log", "origin/main", "--format=%H%x1f%ct%x1f%s", "-40")
    commits = []
    for line in log.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3:
            commits.append({"sha": parts[0][:7], "ts": int(parts[1]),
                            "when": kst(int(parts[1])), "subject": parts[2]})
    return {
        "head": sh("git", "rev-parse", "--short", "HEAD"),
        "remote_head": sh("git", "rev-parse", "--short", "origin/main"),
        "dirty": bool(sh("git", "status", "--porcelain")),
        "commits": commits,
    }


# 되돌리기가 '보존'하는 경로. rollback.yml 의 목록과 반드시 같아야 한다.
# 이 경로만 바꾼 커밋은 되돌려도 봇 동작이 하나도 안 바뀌므로 버전 목록에서 뺀다.
# (안 그러면 문서만 고친 커밋에 '적용'을 눌렀는데 아무 일도 안 일어난다)
PRESERVED = ("botstate.sqlite3", "topics.json", "docs/", "admin/",
             "HANDOFF.md", "RULES.md", ".github/workflows/rollback.yml")


def collect_versions(limit=25):
    """되돌릴 수 있는 '코드 버전' 목록.

    발행 이력 커밋이 20분마다 쌓이므로 그냥 git log 를 보여주면 실제로 무엇이
    바뀐 버전인지 사람이 못 고른다. 코드·설정·워크플로를 건드린 커밋만 남긴다.
    """
    raw = sh("git", "log", "origin/main", "-200",
             "--format=%x1e%H%x1f%ct%x1f%s%x1f%b%x1f", "--name-only")
    head = sh("git", "rev-parse", "HEAD")
    out = []
    for block in raw.split("\x1e"):
        if not block.strip():
            continue
        parts = block.split("\x1f")
        if len(parts) < 5:
            continue
        sha, ts, subject, body = parts[0].strip(), parts[1], parts[2], parts[3]
        files = [f for f in parts[4].splitlines() if f.strip()]
        code = [f for f in files if not f.startswith(PRESERVED)]
        if not code:
            continue
        note = next((l.strip() for l in body.splitlines() if l.strip()), "")
        out.append({
            "sha": sha,
            "short": sha[:7],
            "ts": int(ts),
            "when": kst(int(ts)),
            "subject": subject,
            "note": note[:160],
            "files": code[:12],
            "file_count": len(code),
            "revert": subject.startswith("revert:"),
            "current": False,
        })
        if len(out) >= limit:
            break

    # '지금 돌고 있는 버전'은 HEAD 커밋이 아니다. HEAD 가 문서·상태 커밋이면
    # 실제로 적용 중인 로직은 그보다 앞선 코드 커밋이다. HEAD 의 조상 중
    # 가장 최근 코드 버전을 찾아 표시한다.
    for v in out:
        if _is_ancestor(v["sha"], head):
            v["current"] = True
            break
    return out


def _is_ancestor(sha, head):
    return subprocess.run(["git", "merge-base", "--is-ancestor", sha, head],
                          cwd=ROOT, capture_output=True).returncode == 0


def collect_runs():
    data = get_json(f"{REPO_API}/actions/runs?per_page=15")
    if "_error" in data:
        return {"error": data["_error"], "runs": []}
    out = []
    for r in data.get("workflow_runs", []):
        s = datetime.datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
        e = datetime.datetime.fromisoformat(r["updated_at"].replace("Z", "+00:00"))
        out.append({
            "number": r["run_number"], "status": r["status"],
            "conclusion": r["conclusion"], "event": r["event"],
            "sha": r["head_sha"][:7], "started": s.timestamp(),
            "when": kst(s.timestamp()),
            "minutes": round((e - s).total_seconds() / 60, 1),
        })
    return {"error": None, "runs": out}


def collect_db(path):
    if not os.path.exists(path):
        return {"error": "botstate.sqlite3 없음"}
    c = sqlite3.connect(path)
    try:
        pub = list(c.execute(
            "select message_id, category, headline, published_at, source_url, mirror_ids "
            "from published order by published_at desc limit 60"))
        cats = dict(c.execute(
            "select category, count(*) from published group by category"))
        per_day = collections.Counter()
        for (ts,) in c.execute("select published_at from published"):
            per_day[datetime.datetime.fromtimestamp(ts, KST).strftime("%m-%d")] += 1
        srcs = dict(c.execute(
            "select source, count(*) from seen group by source order by count(*) desc limit 15"))
        return {
            "error": None,
            "seen": c.execute("select count(*) from seen").fetchone()[0],
            "published": c.execute("select count(*) from published").fetchone()[0],
            "urls_indexed": c.execute("select count(*) from seen_urls").fetchone()[0]
                if c.execute("select name from sqlite_master where name='seen_urls'").fetchone() else 0,
            "by_category": cats,
            "per_day": dict(sorted(per_day.items())),
            "top_sources": srcs,
            "recent": [{"id": r[0], "category": r[1], "headline": r[2],
                        "ts": r[3], "when": kst(r[3]),
                        "url": r[4] or "", "mirrored": bool(r[5])} for r in pub],
        }
    finally:
        c.close()


def collect_backups():
    out = []
    if os.path.isdir(BACKUPS):
        for name in sorted(os.listdir(BACKUPS), reverse=True):
            p = os.path.join(BACKUPS, name)
            if not os.path.isdir(p):
                continue
            size = int(sh("du", "-sk", p).split("\t")[0] or 0)
            out.append({
                "name": name, "path": p, "mb": round(size / 1024, 1),
                "has_restore": os.path.exists(os.path.join(p, "RESTORE.md")),
                "has_archive": os.path.exists(os.path.join(p, "project.tar.gz")),
                "when": kst(os.path.getmtime(p)),
            })
    tags = [t for t in sh("git", "tag", "-l").splitlines() if t]
    return {"dirs": out, "tags": tags, "root": BACKUPS}


def poll_rhythm(commits):
    """발행 이력 커밋 간격 = 실제 폴링 리듬."""
    marks = [c for c in commits if "발행 이력" in c["subject"]]
    gaps = []
    for a, b in zip(marks, marks[1:]):
        gaps.append(round((a["ts"] - b["ts"]) / 60, 1))
    return {"count": len(marks), "gaps": gaps[:20],
            "last": marks[0]["when"] if marks else None}


# ── 히스토리 저장소 ──
# 별도 DB(Mongo 등)를 두지 않는다. 리포가 public 이라 여기 쌓으면
#   · 누구나 웹에서 읽을 수 있고(Pages)
#   · 나중에 이 저장소를 여는 사람도 자격증명 없이 그대로 읽는다.
# 한 줄 = 한 시점. 오래된 것부터 잘라 파일이 무한정 커지지 않게 한다.
DOCS = os.path.join(ROOT, "docs")
HISTORY = os.path.join(DOCS, "history.jsonl")
HISTORY_MAX = 3000          # 20분 간격이면 약 40일치


def append_history(snap):
    """상태판에 그릴 만한 값만 추려 한 줄 남긴다. 원본 전체를 쌓으면 금방 수십 MB 가 된다."""
    db, runs = snap.get("db", {}), (snap.get("actions", {}).get("runs") or [])
    live = next((r for r in runs if r["status"] == "in_progress"), None)
    recent = (db.get("recent") or [{}])[0]
    row = {
        "at": snap["generated_at"],
        "published": db.get("published"),
        "seen": db.get("seen"),
        "last_publish": recent.get("when"),
        "last_headline": (recent.get("headline") or "")[:90],
        "run": live["number"] if live else None,
        "run_sha": live["sha"] if live else None,
        "head": snap.get("git", {}).get("remote_head"),
        "gap_median": _median(snap.get("rhythm", {}).get("gaps") or []),
        "by_category": db.get("by_category") or {},
    }
    os.makedirs(DOCS, exist_ok=True)
    lines = []
    if os.path.exists(HISTORY):
        with open(HISTORY, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
    # 같은 분(minute)에 두 번 돌면 덮어쓴다 — 중복 점이 그래프를 망친다
    stamp = row["at"][:16]
    lines = [ln for ln in lines if not ln.startswith(f'{{"at": "{stamp}')]
    lines.append(json.dumps(row, ensure_ascii=False))
    with open(HISTORY, "w", encoding="utf-8") as f:
        f.write("\n".join(lines[-HISTORY_MAX:]) + "\n")
    return len(lines)


def _median(xs):
    if not xs:
        return None
    s = sorted(xs)
    return s[len(s) // 2]


def main():
    sys.path.insert(0, ROOT)
    snap = {
        "generated_at": kst(datetime.datetime.now(KST).timestamp()),
        "repo": REPO_API.rsplit("/repos/", 1)[-1],
        "git": collect_git(),
        "actions": collect_runs(),
        "db": collect_db(os.path.join(ROOT, "botstate.sqlite3")),
        "backups": collect_backups(),
        "versions": collect_versions(),
    }
    snap["rhythm"] = poll_rhythm(snap["git"]["commits"])
    try:
        import topics
        snap["tabs"] = {k: {"name": v[0], "thread": None} for k, v in topics.CATEGORIES.items()}
        tj = os.path.join(ROOT, "topics.json")
        if os.path.exists(tj):
            for k, v in json.load(open(tj)).items():
                if k in snap["tabs"]:
                    snap["tabs"][k]["thread"] = v
    except Exception as e:
        snap["tabs"] = {"_error": str(e)[:120]}
    out = json.dumps(snap, ensure_ascii=False)
    if "--write" in sys.argv:
        os.makedirs(DOCS, exist_ok=True)
        with open(os.path.join(DOCS, "status.json"), "w", encoding="utf-8") as f:
            f.write(out)
        n = append_history(snap)
        print(f"docs/status.json 갱신 · 히스토리 {n}줄", file=sys.stderr)
    else:
        print(out)


if __name__ == "__main__":
    main()
