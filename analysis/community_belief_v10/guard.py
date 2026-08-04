"""무과금 강제. 모든 분석 스크립트가 첫 import로 이 모듈을 부른다.

이유: 사용자 지시(2026-08-03) — 이 분석은 어떤 유료 API도 호출하지 않는다.
.env를 절대 로드하지 않으며, 이미 환경에 있는 키도 제거한다.
"""

import os

PAID_KEY_NAMES = (
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
)


def enforce_no_paid_api(offline: bool = False) -> None:
    for name in PAID_KEY_NAMES:
        os.environ.pop(name, None)
    if offline:
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"


def assert_no_openai_package() -> None:
    try:
        import openai  # noqa: F401
    except ImportError:
        return
    raise RuntimeError(
        "openai 패키지가 analysis venv에 설치되어 있다. "
        "무과금 제약 위반 가능성이 있으므로 제거할 것."
    )


enforce_no_paid_api()
