"""한국어판 출력 지문. 영문판 작업이 한국어판을 건드리지 않았음을 '증명'한다.

  venv/bin/python tests/ko_snapshot.py --save     지문 새로 뜨기 (작업 시작 전에 한 번)
  venv/bin/python tests/ko_snapshot.py            지문과 대조 (변경할 때마다)

대조 대상은 실제로 발행된 글이다. 모델을 부르지 않으므로 한도를 쓰지 않고,
네트워크도 타지 않는다. 렌더 결과가 한 글자라도 달라지면 실패로 잡는다.
"""
import hashlib
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
SNAP = os.path.join(ROOT, "tests", "ko_snapshot.json")

# 렌더에 들어가는 모든 갈래를 한 번씩은 밟도록 고른 표본.
# (탭별 해시태그 · 출처 매체명 정리 · 업데이트 블록 · 펭귄 코멘트)
SAMPLE_SQL = """
    SELECT category, headline, lede, text, source_url
    FROM published
    WHERE text IS NOT NULL AND text != ''
    ORDER BY published_at DESC LIMIT 40
"""


def cases():
    con = sqlite3.connect(os.path.join(ROOT, "botstate.sqlite3"))
    return list(con.execute(SAMPLE_SQL))


def fingerprint():
    """발행 당시 저장해 둔 본문(text)과 렌더 부품들의 지문."""
    import publisher
    import topics

    out = {"tabs": {}, "labels": {}, "tags": {}, "rows": [], "render": {}}

    # 탭 표시 이름과 해시태그 — 언어 분기가 처음 닿는 곳
    for key, (name, _color) in topics.CATEGORIES.items():
        out["tabs"][key] = name
        out["tags"][key] = publisher._tab_tag(key)

    # 출처 매체명 정리 규칙
    for raw in ("규제:미국(Bloomberg.com)", "CoinPost(일본)", "블록미디어",
                "Cointelegraph", "규제:한국(Decenter)", ""):
        out["labels"][raw] = publisher.source_label(raw)

    # 렌더 결과 자체. 저장된 본문만 보면 render() 가 바뀐 것을 못 잡는다.
    from fixtures import CASES
    for name, url, data in CASES:
        out["render"][name] = hashlib.sha256(
            (publisher.render(data, url) + "\x1e" + publisher.render_caption(data)).encode()
        ).hexdigest()[:16]

    for category, headline, lede, text, url in cases():
        out["rows"].append({
            "headline": headline,
            "category": category,
            # 발행된 원문 그대로 — 이게 바뀌면 독자가 보는 글이 바뀐 것이다
            "text_sha": hashlib.sha256((text or "").encode()).hexdigest()[:16],
            "len": len(text or ""),
        })
    return out


def main():
    fp = fingerprint()
    if "--save" in sys.argv:
        with open(SNAP, "w", encoding="utf-8") as f:
            json.dump(fp, f, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"지문 저장: 탭 {len(fp['tabs'])}개 · 발행글 {len(fp['rows'])}건")
        return 0

    if not os.path.exists(SNAP):
        print("지문이 없다. 먼저 --save 로 떠라.", file=sys.stderr)
        return 2

    old = json.load(open(SNAP, encoding="utf-8"))
    bad = []
    for section in ("tabs", "tags", "labels", "render"):
        for k, v in old[section].items():
            now = fp[section].get(k)
            if now != v:
                bad.append(f"{section}[{k}]: {v!r} → {now!r}")
    old_rows = {r["headline"]: r for r in old["rows"]}
    for r in fp["rows"]:
        o = old_rows.get(r["headline"])
        if o and (o["text_sha"] != r["text_sha"] or o["category"] != r["category"]):
            bad.append(f"발행글 바뀜: {r['headline'][:40]}")

    if bad:
        print("한국어판이 바뀌었다 — 되돌려라:", file=sys.stderr)
        for b in bad:
            print("  " + b, file=sys.stderr)
        return 1
    print(f"한국어판 그대로 (탭 {len(fp['tabs'])}개 · 발행글 {len(fp['rows'])}건 대조)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
