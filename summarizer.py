"""Google Gemini(무료 티어)로 뉴스를 채널 포맷 JSON으로 변환.

기존 Claude 버전과 인터페이스 동일: summarize(item) -> dict | None.
Gemini SDK는 동기라 asyncio.to_thread 로 감싸 async 인터페이스를 유지한다.

무료 티어는 503(과부하)이 잦으므로 지수 백오프 재시도 + 대체 모델 폴백을 둔다.
모델이 JSON 앞뒤에 설명을 붙이는 경우가 있어 본문에서 JSON 객체만 추출한다.
"""
import asyncio
import json
import os
import re
import time

from google import genai
from google.genai import types

from config import settings
from models import NewsItem
from prompts import (
    REPAIR_PROMPT,
    coerce,
    validate,
    BRIEFING_SYSTEM_PROMPT,
    INSIGHT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    TERM_FIX_SYSTEM_PROMPT,
    build_insight_prompt,
    build_term_fix_prompt,
    build_user_prompt,
)

# 타임아웃을 주지 않으면 응답이 끊겨도 영원히 대기한다.
# 실제로 백필이 이 상태로 7시간 멈춰 있었다. (단위: ms)
# 키가 없어도 **import 는 되어야 한다.** DRY_RUN 구조 검증·테스트·토픽 생성처럼
# 모델을 안 부르는 작업이 키 하나 때문에 통째로 막히면 안 된다.
# 실제 호출 시점에 만들고, 그때 키가 없으면 그때 실패한다.
_client = None


def _get_client():
    global _client
    if _client is None:
        if not settings.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY 가 비어 있습니다. .env 를 채우세요.")
        _client = genai.Client(
            api_key=settings.gemini_api_key,
            http_options=types.HttpOptions(timeout=90_000),
        )
    return _client


class _LazyClient:
    """기존 코드가 `client.models...` 로 쓰고 있어 형태를 유지한다."""
    def __getattr__(self, name):
        return getattr(_get_client(), name)


client = _LazyClient()

# 라이브러리 단 타임아웃이 안 먹는 경우를 대비한 상한(초).
CALL_TIMEOUT = 120

# 분당 한도는 **모델별**이다. 실측(2026-08-29):
#   quotaId = GenerateRequestsPerMinutePerProjectPerModel-FreeTier, quotaValue = 15
# 그래서 스로틀도 모델별로 건다. 예전에는 전역 하나였는데, 모델이 7개로 늘어난 뒤로는
# 그게 병목이 됐다 — 모델이 몇 개든 분당 12건에 묶여 대기 물량이 안 빠졌다.
RATE_LIMIT_RPM = 12          # 모델당. 실제 한도 15 에서 여유를 뒀다
_MIN_GAP = 60.0 / RATE_LIMIT_RPM
_gate = asyncio.Lock()
_last_call: dict[str, float] = {}


async def _throttle(model: str):
    """그 **모델의** 직전 호출과 간격을 벌린다."""
    async with _gate:
        now = asyncio.get_running_loop().time()
        wait = _MIN_GAP - (now - _last_call.get(model, 0.0))
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call[model] = asyncio.get_running_loop().time()


# 매번 같은 모델부터 시작하면 그 모델만 한도를 다 쓰고 나머지는 논다.
# 호출마다 시작 지점을 옮겨 부하를 고르게 편다.
_rr = 0


def _rotate(models: list[str]) -> list[str]:
    global _rr
    if len(models) < 2:
        return models
    _rr = (_rr + 1) % len(models)
    return models[_rr:] + models[:_rr]


# 한도에 걸린 모델과 **다시 시도해도 되는 시각**.
#
# 예전에는 집합(set)이라 한 번 걸리면 프로세스가 끝날 때까지 영영 제외했다.
# 실행이 2~8분이던 시절엔 맞았지만, 5시간 30분짜리 루프로 바뀐 뒤로는
# 새벽 한 번의 오류로 아침 내내 요약이 멈춘다(실측: 08:07 이후 3시간 발행 0).
#
# 게다가 구글이 주는 오류는 quotaId 가 PerDay 인데도 retryDelay 가 26초다.
# 즉 '오늘 끝'이 아니라 '잠시 뒤 다시'인 경우가 있다. 그래서 영구 배제 대신
# 일정 시간 쉬었다가 다시 써본다.
COOLDOWN_SEC = int(os.getenv("MODEL_COOLDOWN_SEC", "900"))   # 15분
_exhausted: dict[str, float] = {}


def _rest(model: str, seconds: float | None = None):
    _exhausted[model] = time.monotonic() + (seconds or COOLDOWN_SEC)


def _is_quota_exhausted(msg: str) -> bool:
    """분당 초과(잠시 후 회복)와 한도 소진(그날은 끝)을 구분한다.

    분당 한도 메시지에도 'PerMinutePerProject...' 처럼 Per 가 들어가므로
    PerDay 를 정확히 집어야 한다. 잘못 판정하면 살아 있는 모델을 배제해버린다.
    """
    if "RESOURCE_EXHAUSTED" not in msg:
        return False
    return "PerDay" in msg and "PerMinute" not in msg


def _candidates() -> list[str]:
    """시도할 모델 목록. 주 모델이 폴백 목록에도 있으면 중복 호출이 되므로 정리한다."""
    return list(dict.fromkeys([settings.gemini_model, *FALLBACK_MODELS]))


def _usable(models: list[str]) -> list[str]:
    """아직 쓸 수 있는 모델만. 쉬는 시간이 끝난 모델은 다시 포함된다.

    예전에는 전부 소진일 때 목록을 그대로 돌려줘 '어쩔 수 없이 다시 시도'했는데,
    그러면 건마다 모델 수만큼 429 를 받아낸다. _throttle() 이 전역이라 호출 하나당
    5초가 붙어, 소진된 날에는 100여 건 처리에 40분이 걸려 job 타임아웃에 잘렸다.
    살아 있는 모델이 없으면 그냥 포기하는 게 맞다.
    """
    now = time.monotonic()
    return [m for m in models if _exhausted.get(m, 0) <= now]


def all_exhausted() -> bool:
    """이번 실행에서 쓸 수 있는 모델이 하나도 안 남았는가.

    호출하는 쪽이 남은 항목을 '요약 실패'가 아니라 '손대지 않음'으로 처리해
    다음 실행에 넘길 수 있게 하려고 공개해 둔다.
    """
    return not _usable(_candidates())


def _retry_after(msg: str) -> float:
    """429 응답에 담긴 대기 시간을 뽑아낸다. 없으면 기본값."""
    m = re.search(r"retry in ([0-9.]+)s", msg) or re.search(r"'retryDelay': '(\d+)s'", msg)
    if m:
        try:
            return min(float(m.group(1)) + 2, 90)
        except ValueError:
            pass
    return 20.0

# 기본 모델이 소진·과부하일 때 순서대로 시도할 대체 모델.
#
# **한도는 모델마다 따로 매겨진다.** 그래서 모델을 늘리면 하루 처리량이 그만큼 는다.
# 2026-08-29 실측 — 무료 티어 일일 한도(429 응답의 quotaValue):
#   gemini-3.1-flash-lite   500건/일   ← 주 모델
#   gemini-flash-latest      20건/일
#   나머지 flash 계열         20건/일
#
# 아래 셋은 안 쓰던 모델이다. 같은 기사로 실제 요약을 시켜 품질을 비교해 골랐다
# (한국어·JSON 형식·수치 보존·인명 보존·코멘트 문장력 전부 확인).
#   gemini-3.6-flash          가장 최신 full flash. 문장이 매끄럽고 맥락 파악이 정확
#   gemini-flash-lite-latest  최신 lite 별칭. 3.1-flash-lite 와 한도 통이 분리돼 있다(실측)
#   gemini-3.5-flash-lite     안정적. 형식 준수 완벽
#
# 넣지 않은 것:
#   gemini-3.1-flash-lite-preview — 주 모델과 한도 통을 공유한다(같이 소진됨)
#   gemma-4-*                     — Gemini 계열이 아니고 형제 모델이 빈 응답을 준 적이 있다
#   gemini-2.5-*                  — 퇴역(generateContent 불가)
#
# 순서는 **품질 순**이다. 앞에서부터 쓰다가 한도에 걸리면 다음으로 넘어간다.
FALLBACK_MODELS = ["gemini-3.1-flash-lite", "gemini-3.6-flash",
                   "gemini-flash-lite-latest", "gemini-3.5-flash-lite",
                   "gemini-3.7-flash", "gemini-3.5-flash", "gemini-3-flash-preview"]

_config = types.GenerateContentConfig(
    system_instruction=SYSTEM_PROMPT,
    response_mime_type="application/json",
    temperature=0.7,
    max_output_tokens=1500,
)

# 트위터 캡처용. 배경·영향·지켜볼 포인트까지 쓰므로 출력 토큰을 넉넉히 준다.
# 사실 왜곡을 줄이려 temperature 를 낮춘다.
_insight_config = types.GenerateContentConfig(
    system_instruction=INSIGHT_SYSTEM_PROMPT,
    response_mime_type="application/json",
    temperature=0.4,
    max_output_tokens=3000,
)


def _retryable(err: Exception, msg: str) -> bool:
    """같은 모델로 다시 시도해볼 만한 오류인가.

    타임아웃은 응답이 늦은 것뿐이라 재시도 대상이다. 이걸 빠뜨리면
    한 번 느려졌을 때 곧장 대체 모델로 넘어가버린다.
    """
    if isinstance(err, (TimeoutError, asyncio.TimeoutError)):
        return True
    return ("503" in msg or "429" in msg or "UNAVAILABLE" in msg
            or "RESOURCE_EXHAUSTED" in msg)


def _extract_json(text: str) -> dict:
    """모델이 앞뒤에 설명을 붙여도 JSON 객체만 뽑아 파싱한다."""
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


def _generate_sync(model: str, user_prompt: str) -> str:
    resp = client.models.generate_content(
        model=model, contents=user_prompt, config=_config
    )
    return resp.text or ""


def _generate_vision_sync(model: str, user_prompt: str, image: bytes | None,
                          mime: str) -> str:
    contents = [user_prompt] if image is None else [
        types.Part.from_bytes(data=image, mime_type=mime),
        user_prompt,
    ]
    resp = client.models.generate_content(
        model=model, contents=contents, config=_insight_config
    )
    return resp.text or ""


async def generate_json(system_prompt: str, user_prompt: str,
                        max_tokens: int = 2000) -> dict | None:
    """임의의 시스템 프롬프트로 JSON 응답을 받는다(다이제스트 등 범용).

    요약·분류 경로와 동일하게 503/429 재시도 + 대체 모델 폴백을 적용한다.
    """
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        temperature=0.4,
        max_output_tokens=max_tokens,
    )

    def _call(model: str) -> str:
        resp = client.models.generate_content(
            model=model, contents=user_prompt, config=config
        )
        return resp.text or ""

    models = _rotate(_usable(_candidates()))
    if not models:
        return None                      # 오늘 쓸 수 있는 모델 없음 — 헛호출하지 않는다
    last_err = None
    for model in models:
        for attempt in range(3):
            try:
                await _throttle(model)
                return _extract_json(
                    await asyncio.wait_for(asyncio.to_thread(_call, model), CALL_TIMEOUT)
                )
            except Exception as e:
                last_err = e
                msg = str(e)
                if _is_quota_exhausted(msg):
                    # 한도에 걸렸다. 응답이 알려준 재시도 시각이 있으면 그만큼만 쉰다.
                    if _exhausted.get(model, 0) <= time.monotonic():
                        wait = max(_retry_after(msg), 60)
                        _rest(model, wait)
                        print(f"[모델] {model} 한도 — {wait/60:.0f}분 쉬었다 다시 시도")
                    break
                if _retryable(e, msg):
                    delay = _retry_after(msg) if "429" in msg or "RESOURCE_EXHAUSTED" in msg \
                        else 2 * (attempt + 1)
                    await asyncio.sleep(delay)
                    continue
                break

    print(f"[generate_json] 실패: {last_err}")
    return None


async def translate_terms(tokens: list[str]) -> dict[str, str] | None:
    """한국어로 안 옮겨진 조각들만 받아 한국어 표기를 돌려준다.

    None 은 **물어보지도 못했다**는 뜻이다(한도 소진·호출 실패). 빈 dict 는
    물어봤는데 고칠 것이 없다는 뜻이라 호출하는 쪽이 둘을 구분해야 한다.
    조각만 주고받으므로 토큰이 거의 들지 않는다 — 호출 수만 1 늘어난다.
    """
    if not tokens:
        return {}
    data = await generate_json(TERM_FIX_SYSTEM_PROMPT,
                               build_term_fix_prompt(tokens[:40]), max_tokens=800)
    if not data:
        return None
    terms = data.get("terms")
    if not isinstance(terms, dict):
        return None
    return {str(k): str(v) for k, v in terms.items() if k and v}


async def summarize_insight(item: NewsItem, posted_at: str) -> dict | None:
    """퍼온 글을 분석 인사이트 JSON으로 만든다.

    이미지가 있으면 비전으로 캡처를 읽고, 없으면 본문 텍스트만으로 처리한다.
    **이미지가 없다고 일반 뉴스 경로로 흘려보내면 안 된다** — 그 경로는 출처를
    '기사 원문 = 수집처 링크'로 표기해서, 퍼온 채널이 출처로 찍혀버린다.
    """
    user_prompt = build_insight_prompt(item.body, item.url, posted_at,
                                       has_image=bool(item.image))
    models = _rotate(_usable(_candidates()))
    if not models:
        return None                      # 오늘 쓸 수 있는 모델 없음 — 헛호출하지 않는다
    last_err = None

    for model in models:
        for attempt in range(3):
            try:
                await _throttle(model)
                text = await asyncio.wait_for(
                    asyncio.to_thread(_generate_vision_sync, model, user_prompt,
                                      item.image, item.image_mime),
                    CALL_TIMEOUT,
                )
                data = _extract_json(text)
                if not data.get("relevant"):
                    return None
                data["_insight"] = True
                return data
            except Exception as e:
                last_err = e
                msg = str(e)
                if _is_quota_exhausted(msg):
                    # 한도에 걸렸다. 응답이 알려준 재시도 시각이 있으면 그만큼만 쉰다.
                    if _exhausted.get(model, 0) <= time.monotonic():
                        wait = max(_retry_after(msg), 60)
                        _rest(model, wait)
                        print(f"[모델] {model} 한도 — {wait/60:.0f}분 쉬었다 다시 시도")
                    break
                if _retryable(e, msg):
                    delay = _retry_after(msg) if "429" in msg or "RESOURCE_EXHAUSTED" in msg \
                        else 2 * (attempt + 1)
                    await asyncio.sleep(delay)
                    continue
                break

    print(f"[insight] 실패 ({item.url}): {last_err}")
    return None


# 심층 요약용. bullet 이 10~16개라 출력 토큰을 넉넉히 준다.
# 사실 왜곡을 줄이려 temperature 를 낮춘다.
_briefing_config = types.GenerateContentConfig(
    system_instruction=BRIEFING_SYSTEM_PROMPT,
    response_mime_type="application/json",
    temperature=0.3,
    max_output_tokens=4000,
)


def _generate_briefing_sync(model: str, user_prompt: str) -> str:
    resp = client.models.generate_content(
        model=model, contents=user_prompt, config=_briefing_config
    )
    return resp.text or ""


async def summarize_briefing(item: NewsItem) -> dict | None:
    """FOMC·연준 연설처럼 최상위 정책 이벤트를 촘촘하게 요약한다.

    일반 경로(summarize)와 프롬프트·출력 길이만 다르고 나머지는 같다.
    """
    user_prompt = build_user_prompt(
        item.source, item.title, item.url, item.body, item.region_hint
    )
    models = _rotate(_usable(_candidates()))
    if not models:
        return None
    last_err = None
    for model in models:
        for attempt in range(3):
            try:
                await _throttle(model)
                text = await asyncio.wait_for(
                    asyncio.to_thread(_generate_briefing_sync, model, user_prompt),
                    CALL_TIMEOUT,
                )
                data = _extract_json(text)
                ok, why = validate(data)
                if not ok:
                    # §28 — 1회만 repair 한다. 그 뒤에도 실패하면 포기한다.
                    # 무한 retry 는 금지다. 한도를 그대로 태워 먹는다.
                    print(f"[스키마] {why} — 1회 교정 시도")
                    repair = REPAIR_PROMPT + text[:4000]
                    await _throttle(model)
                    fixed = await asyncio.wait_for(
                        asyncio.to_thread(_generate_sync, model, repair), CALL_TIMEOUT
                    )
                    data = _extract_json(fixed)
                    ok, why = validate(data)
                    if not ok:
                        print(f"[스키마] 교정 실패({why}) — 이 기사는 건너뛴다")
                        return None

                if not data.get("relevant"):
                    # 제외 사유를 남겨 나중에 왜 빠졌는지 확인할 수 있게 한다 (§35).
                    data["_reject_reason"] = data.get("relevance_reason") or "모델 제외 판정"
                    return data
                return coerce(data)
            except Exception as e:
                last_err = e
                msg = str(e)
                if _is_quota_exhausted(msg):
                    # _exhausted 는 dict 다. 예전 코드가 set 시절의 .add() 를
                    # 그대로 두고 있어서 여기 오면 AttributeError 로 터졌다 —
                    # 심층 요약(FOMC·연준 연설)이 한도에 걸리면 재시도도 못 하고
                    # '요약 실패'로 버려졌다.
                    if _exhausted.get(model, 0) <= time.monotonic():
                        wait = max(_retry_after(msg), 60)
                        _rest(model, wait)
                        print(f"[모델] {model} 한도 — {wait/60:.0f}분 쉬었다 다시 시도")
                    break
                if _retryable(e, msg):
                    delay = _retry_after(msg) if "429" in msg or "RESOURCE_EXHAUSTED" in msg \
                        else 2 * (attempt + 1)
                    await asyncio.sleep(delay)
                    continue
                break
    print(f"[briefing] 실패 ({item.title[:40]}): {last_err}")
    return None


async def summarize(item: NewsItem, recent: list | None = None) -> dict | None:
    """실패하거나 관련 없는 뉴스면 None 반환.

    recent 는 이미 발행한 글 목록이다. 요약 호출에 얹어 보내 모델이 중복 여부까지
    함께 판정하게 한다 — 따로 물어보면 한도를 두 배로 쓴다.
    """
    user_prompt = build_user_prompt(
        item.source, item.title, item.url, item.body, item.region_hint, recent
    )
    models = _rotate(_usable(_candidates()))
    if not models:
        return None                      # 오늘 쓸 수 있는 모델 없음 — 헛호출하지 않는다
    last_err = None

    for model in models:
        for attempt in range(3):
            try:
                await _throttle(model)
                text = await asyncio.wait_for(
                    asyncio.to_thread(_generate_sync, model, user_prompt), CALL_TIMEOUT
                )
                data = _extract_json(text)
                ok, why = validate(data)
                if not ok:
                    # §28 — 1회만 repair 한다. 그 뒤에도 실패하면 포기한다.
                    # 무한 retry 는 금지다. 한도를 그대로 태워 먹는다.
                    print(f"[스키마] {why} — 1회 교정 시도")
                    repair = REPAIR_PROMPT + text[:4000]
                    await _throttle(model)
                    fixed = await asyncio.wait_for(
                        asyncio.to_thread(_generate_sync, model, repair), CALL_TIMEOUT
                    )
                    data = _extract_json(fixed)
                    ok, why = validate(data)
                    if not ok:
                        print(f"[스키마] 교정 실패({why}) — 이 기사는 건너뛴다")
                        return None

                if not data.get("relevant"):
                    # 제외 사유를 남겨 나중에 왜 빠졌는지 확인할 수 있게 한다 (§35).
                    data["_reject_reason"] = data.get("relevance_reason") or "모델 제외 판정"
                    return data
                return coerce(data)
            except Exception as e:
                last_err = e
                msg = str(e)
                # 과부하/레이트리밋이면 잠시 쉬었다 재시도, 그 외 오류는 다음 모델로
                if _is_quota_exhausted(msg):
                    # 한도에 걸렸다. 응답이 알려준 재시도 시각이 있으면 그만큼만 쉰다.
                    if _exhausted.get(model, 0) <= time.monotonic():
                        wait = max(_retry_after(msg), 60)
                        _rest(model, wait)
                        print(f"[모델] {model} 한도 — {wait/60:.0f}분 쉬었다 다시 시도")
                    break
                if _retryable(e, msg):
                    delay = _retry_after(msg) if "429" in msg or "RESOURCE_EXHAUSTED" in msg \
                        else 2 * (attempt + 1)
                    await asyncio.sleep(delay)
                    continue
                break  # 파싱 실패 등 → 다음 모델 시도

    print(f"[summarizer] 실패 ({item.title[:40]}): {last_err}")
    return None
