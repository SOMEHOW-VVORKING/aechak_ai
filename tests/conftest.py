from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.llm.client import LlmResult
from app.schemas.inspection import CallOutcome
from app.settings import Settings
from demo import view

CONTRACT_DIR = Path(__file__).parent / "contract"


@pytest.fixture
def contract() -> Any:
    def load(name: str) -> dict[str, Any]:
        return json.loads((CONTRACT_DIR / name).read_text())

    return load


@pytest.fixture
def settings() -> Settings:
    return Settings(
        anthropic_api_key="test",
        kafka_bootstrap_servers="localhost:9092",
    )


class FakeLlmClient:
    """정해진 응답을 순서대로 돌려준다. 호출 기록을 남겨 호출 수를 단언할 수 있게 한다."""

    def __init__(self, scripted: list[LlmResult]) -> None:
        self._scripted: Iterator[LlmResult] = iter(scripted)
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, **kwargs: Any) -> LlmResult:
        self.calls.append(kwargs)
        try:
            return next(self._scripted)
        except StopIteration as exc:
            raise AssertionError(f"예정보다 많이 호출됐다. 지금까지 {len(self.calls)}회") from exc

    async def aclose(self) -> None:
        return None


def ok(tool_input: dict[str, Any], *, model: str = "claude-sonnet-5") -> LlmResult:
    return LlmResult(
        call_id=f"call-{len(tool_input)}",
        outcome=CallOutcome.SUCCESS,
        model_id=model,
        tool_input=tool_input,
        input_tokens=1200,
        cache_read_input_tokens=1500,
        output_tokens=200,
        cost_micro_usd=5000,
        latency_ms=900,
    )


def failed(
    outcome: CallOutcome = CallOutcome.RATE_LIMITED,
    *,
    model: str = "claude-sonnet-5",
) -> LlmResult:
    return LlmResult(
        call_id="call-failed",
        outcome=outcome,
        model_id=model,
        tool_input=None,
        latency_ms=50,
    )


# 백엔드 게이트 술어. **사본을 여기 또 두지 않는다.**
#
# 정본은 백엔드 ``ContentVerdictMessage.hasModelEvidence()`` 이고, 이 레포의 파이썬 거울은
# ``demo/view.py`` 의 ``backend_accepts`` 하나다. 선별이 건너뛴 통과를 관리자 손을 거치지 않고
# 받으려면 셋이 동시에 참이어야 한다 -- ``TRIAGE`` 행이 ``CLEAR``, 모델 관점 행이 비어 있지
# 않음, 그 행 전부의 사유가 ``TRIAGE_CLEAR``.
#
# 종전에는 같은 규칙을 여기에도 손으로 베껴 두고 "두 사본이 같은 답을 내는지"를 테스트가
# 단언했는데, 그 단언은 항진명제였다 -- 백엔드가 조건을 바꾸면 두 파이썬 사본은 여전히 서로
# 같아 전부 녹색이고, 콘솔은 백엔드가 거부할 통과에 "관리자 확인 없이 상태가 바뀐다"는 초록
# 배지를 계속 띄운다.
#
# 그래서 사본을 하나로 줄이고, 정본과의 어긋남은 **계약 표본에 대한 기대값**으로 잡는다
# (``tests/test_demo.py`` 의 ``test_계약_표본마다_게이트_통과_여부를_표로_고정한다``).
# 표본은 백엔드 레포가 소유하므로 정본이 바뀌면 그 표가 깨진다.
#
# 방향이 테스트 -> demo 인 이유: 거울이 필요한 쪽은 콘솔이고, 운영·데모 코드가 테스트
# 코드를 import 하게 만들 수는 없다.
BACKEND_MODEL_PERSPECTIVES = view.BACKEND_MODEL_PERSPECTIVES
backend_model_verified = view.backend_accepts
