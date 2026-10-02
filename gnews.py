"""구글뉴스 리디렉터 URL → 실제 기사 주소.

왜 필요한가. 발행 링크의 86%가 `news.google.com/rss/articles/CBMi...` 형태다.
구글뉴스 수집 쿼리를 쓰기 때문이다. 이 주소는 두 가지 문제가 있다.
  1) 텔레그램이 미리보기 카드를 못 만든다 — 자바스크립트 리디렉터라 메타태그가 없다
  2) 누르면 구글뉴스를 한 번 거친다

단순 GET 으로는 안 풀린다(리디렉션이 JS 로 일어난다). 페이지에서 서명
파라미터(data-n-a-sg · data-n-a-ts)를 뽑아 batchexecute 에 물어봐야 한다.

**비싸다.** 기사 1건당 GET 약 600KB + POST 1회다. 그래서 결과를 DB 에 캐시하고,
실패하면 원래 주소를 그대로 쓴다. 해석이 안 돼도 발행은 멈추지 않는다.
"""
import json
import re

import httpx

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120 Safari/537.36")
_H = {"User-Agent": _UA}
_BATCH = "https://news.google.com/_/DotsSplashUi/data/batchexecute"

_mem: dict[str, str] = {}


def _parse(text: str) -> str | None:
    """batchexecute 응답에서 실제 주소를 꺼낸다.

    **문자열 치환으로 풀지 마라.** 응답은 JSON 안에 JSON 이 문자열로 들어 있는
    이중 구조라 `?idxno\\u003d7275` 처럼 두 겹으로 escape 돼 있다. 정규식으로
    긁으면 `?idxno` 에서 잘린 주소가 나오고, 그 주소는 404 가 뜬다(2026-10-02).
    json.loads 를 두 번 돌려야 제대로 풀린다.
    """
    body = text.lstrip()
    if body.startswith(")]}'"):
        body = body[4:]
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            outer = json.loads(line)
        except json.JSONDecodeError:
            continue
        for row in outer:
            if not isinstance(row, list):
                continue
            for cell in row:
                if not (isinstance(cell, str) and "garturlres" in cell):
                    continue
                try:
                    inner = json.loads(cell)
                except json.JSONDecodeError:
                    continue
                for v in inner:
                    if isinstance(v, str) and v.startswith("http") \
                            and "news.google" not in v:
                        return v
    return None


def is_gnews(url: str) -> bool:
    return "news.google.com" in (url or "")


def resolve(url: str, store=None, timeout: float = 25) -> str:
    """실제 기사 주소. 못 풀면 받은 주소를 그대로 돌려준다."""
    if not is_gnews(url):
        return url
    if url in _mem:
        return _mem[url]
    if store is not None:
        hit = store.get_resolved_url(url)
        if hit:
            _mem[url] = hit
            return hit

    out = url
    try:
        aid = url.split("/articles/")[1].split("?")[0]
        r = httpx.get(url, headers=_H, timeout=timeout, follow_redirects=True)
        sg = re.search(r'data-n-a-sg="([^"]+)"', r.text)
        ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
        if sg and ts:
            req = ["Fbv4je", json.dumps([
                "garturlreq",
                [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                  None, None, None, None, None, 0, 1],
                 "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                aid, int(ts.group(1)), sg.group(1)])]
            rr = httpx.post(
                _BATCH, headers={**_H, "Content-Type":
                                 "application/x-www-form-urlencoded;charset=UTF-8"},
                data={"f.req": json.dumps([[req]])}, timeout=timeout)
            out = _parse(rr.text) or url
    except Exception:
        pass        # 해석 실패는 치명적이지 않다. 원래 주소로 간다.

    _mem[url] = out
    if store is not None and out != url:
        store.put_resolved_url(url, out)
    return out
