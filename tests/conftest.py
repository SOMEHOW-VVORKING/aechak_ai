from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.inspection.triage import TRIAGE_SKIP_REASON
from app.llm.client import LlmResult
from app.schemas.inspection import (
    CallOutcome,
    ContentVerdict,
    Perspective,
    PerspectiveOutcome,
)
from app.settings import Settings

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


# 백엔드 게이트 규칙을 그대로 옮긴 술어.
#
# 정본은 백엔드 ``InspectionFacade.modelVerified()`` 다. 선별이 건너뛴 통과를 관리자 손을
# 거치지 않고 받으려면 셋이 동시에 참이어야 한다 -- ``TRIAGE`` 행이 ``CLEAR``, 모델 관점 행이
# 비어 있지 않음, 그 행 전부의 사유가 ``TRIAGE_CLEAR``.
#
# 가운데 조건을 여기 베껴 두는 이유가 전부다. 건너뛴 관점의 행을 생략하면 백엔드는 그것을
# 증거 없는 통과로 보고 전건을 관리자 검토 큐로 보낸다. 그러면 선별은 Sonnet 호출만 아끼고
# 사람 손은 그만큼 더 쓰는 장치가 된다. 생산자 쪽 테스트가 행의 존재만 보면 그 결과를 잡지
# 못하므로, 소비자의 판단 규칙 자체를 테스트가 소유한다.
BACKEND_MODEL_PERSPECTIVES = (
    Perspective.ALL,
    Perspective.DISPLAY_ADS,
    Perspective.CONDUCT_TRADE,
)


def backend_model_verified(verdict: ContentVerdict) -> bool:
    """백엔드가 이 판정을 "모델이 내용을 읽었다"로 받아들이는가."""
    model_rows = [p for p in verdict.perspectives if p.perspective in BACKEND_MODEL_PERSPECTIVES]
    if any(p.outcome is not PerspectiveOutcome.UNCHECKED for p in model_rows):
        return True

    triage_cleared = any(
        p.perspective is Perspective.TRIAGE and p.outcome is PerspectiveOutcome.CLEAR
        for p in verdict.perspectives
    )
    return triage_cleared and bool(model_rows) and all(p.skipReason == TRIAGE_SKIP_REASON for p in model_rows)
