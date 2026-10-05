"""프롬프트 진입점 — Strategy Pulse.

내용은 prompts_bsp.py 에 있다. summarizer.py 는 이 파일에서만 가져가므로
엔진 코드를 건드리지 않고 프롬프트 세트를 갈아끼울 수 있다.

크립토 봇 시절 프롬프트(prompts_ko.py)는 지우지 않고 그대로 두었다.
되돌려야 할 때 참고할 유일한 원본이고, 크립토 봇 두 리포와는 무관한 사본이다.
"""
from prompts_bsp import (          # noqa: F401
    SYSTEM_PROMPT,
    REPAIR_PROMPT,
    VALID_EVENT_TYPES,
    build_recent_block,
    build_user_prompt,
    coerce,
    validate,
)

# ── 구(舊) 엔진 호환 ────────────────────────────────────────
# summarizer.py 가 아래 이름들을 import 한다. BSP 에서는 쓰지 않는 경로
# (트위터 캡처 인사이트·FOMC 딥브리핑·용어교정)라서 프롬프트만 이어 둔다.
# 이름을 지우면 summarizer import 가 깨지므로 남긴다.
from prompts_ko import (           # noqa: F401
    BRIEFING_SYSTEM_PROMPT,
    INSIGHT_SYSTEM_PROMPT,
    TERM_FIX_SYSTEM_PROMPT,
    build_insight_prompt,
    build_term_fix_prompt,
)
