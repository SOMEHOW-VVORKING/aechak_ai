from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.llm.client import LlmResult
from app.schemas.inspection import CallOutcome
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


def failed(outcome: CallOutcome = CallOutcome.RATE_LIMITED) -> LlmResult:
    return LlmResult(
        call_id="call-failed",
        outcome=outcome,
        model_id="claude-sonnet-5",
        tool_input=None,
        latency_ms=50,
    )
