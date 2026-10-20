"""실제 요청 본문을 가로채 파라미터 계약을 고정한다.

``FakeLlmClient``는 ``call_tool``의 인자만 본다. 그런데 틀리는 자리는 그 인자가 SDK를 거쳐
**본문에 어떻게 실리는가**다. ``output_config={"effort": None}``은 인자 수준에서는 멀쩡해
보이지만 본문에 ``{"effort": null}``로 실려 400이 된다. 그래서 전송 계층에서 단언한다.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx2
import pytest

from app.inspection.triage import TRIAGE_TOOL
from app.llm.client import LlmClient, UnknownModelPricing, cost_micro_usd, validate_pricing
from app.settings import Settings

TOOL: dict[str, Any] = {
    "name": "report_findings",
    "description": "검수 결과를 보고한다.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["findings"],
        "properties": {"findings": {"type": "array", "items": {"type": "string"}}},
    },
}


class BodyRecorder:
    """MockTransport로 본문과 호출당 타임아웃을 받아 둔다."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []
        self.timeouts: list[Any] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        self.timeouts.append(request.extensions.get("timeout"))
        return httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-4-5",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "triage_content",
                        "input": {"decision": "CLEAR"},
                    }
                ],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 300, "output_tokens": 40},
            },
        )


@pytest.fixture
def recorder() -> BodyRecorder:
    return BodyRecorder()


@pytest.fixture
def client(recorder: BodyRecorder) -> LlmClient:
    llm = LlmClient(api_key="test", timeout_seconds=60.0)
    # SDK 클라이언트만 가짜 전송으로 바꿔 끼운다. 프로덕션에 테스트용 주입구를 내지 않기 위해서다.
    llm._client = anthropic.AsyncAnthropic(
        api_key="test",
        timeout=60.0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(recorder)),
    )
    return llm


async def test_선별_호출은_output_config를_보내지_않는다(client: LlmClient, recorder: BodyRecorder) -> None:
    await client.call_tool(
        model="claude-haiku-4-5",
        effort=None,
        max_tokens=512,
        system_blocks=[{"type": "text", "text": "선별 프롬프트"}],
        user_content=[{"type": "text", "text": "본문"}],
        tool=TRIAGE_TOOL,
        request_id="insp-1",
    )

    assert "output_config" not in recorder.bodies[0], (
        "effort=None을 그냥 넘기면 {'effort': null}이 실려 400이 된다"
    )


async def test_등급을_주면_output_config가_실린다(client: LlmClient, recorder: BodyRecorder) -> None:
    await client.call_tool(
        model="claude-sonnet-5",
        effort="low",
        max_tokens=8192,
        system_blocks=[{"type": "text", "text": "정책"}],
        user_content=[{"type": "text", "text": "본문"}],
        tool=TOOL,
        request_id="insp-2",
    )

    assert recorder.bodies[0]["output_config"] == {"effort": "low"}, (
        "조건부 조립이 한쪽 방향으로만 맞으면 관점 호출이 모델 기본 등급으로 조용히 올라간다"
    )


async def test_선별_호출은_thinking을_보내지_않는다(client: LlmClient, recorder: BodyRecorder) -> None:
    await client.call_tool(
        model="claude-haiku-4-5",
        effort=None,
        max_tokens=512,
        system_blocks=[{"type": "text", "text": "선별 프롬프트"}],
        user_content=[{"type": "text", "text": "본문"}],
        tool=TRIAGE_TOOL,
        request_id="insp-3",
    )

    assert "thinking" not in recorder.bodies[0], (
        "Haiku 4.5는 adaptive를 받으면 400이고, 생략이 곧 사고 없음이다"
    )


async def test_샘플링_파라미터는_본문에도_없다(client: LlmClient, recorder: BodyRecorder) -> None:
    await client.call_tool(
        model="claude-sonnet-5",
        effort="low",
        max_tokens=8192,
        system_blocks=[{"type": "text", "text": "정책"}],
        user_content=[{"type": "text", "text": "본문"}],
        tool=TOOL,
        request_id="insp-4",
    )

    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in recorder.bodies[0]


async def test_선별_프롬프트에는_cache_control이_없다(client: LlmClient, recorder: BodyRecorder) -> None:
    from app.inspection.triage import triage_system_blocks

    await client.call_tool(
        model="claude-haiku-4-5",
        effort=None,
        max_tokens=512,
        system_blocks=triage_system_blocks(),
        user_content=[{"type": "text", "text": "본문"}],
        tool=TRIAGE_TOOL,
        request_id="insp-5",
    )

    for block in recorder.bodies[0]["system"]:
        assert "cache_control" not in block


async def test_호출당_타임아웃이_전역보다_좁아진다(client: LlmClient, recorder: BodyRecorder) -> None:
    await client.call_tool(
        model="claude-haiku-4-5",
        effort=None,
        max_tokens=512,
        system_blocks=[{"type": "text", "text": "선별 프롬프트"}],
        user_content=[{"type": "text", "text": "본문"}],
        tool=TRIAGE_TOOL,
        request_id="insp-6",
        timeout_seconds=15.0,
    )

    assert recorder.timeouts[0]["read"] == 15.0, "선별이 전역 60초를 붙잡으면 지연 이득이 사라진다"


def test_미지_모델은_비용_0으로_적히지_않는다(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("ERROR"):
        micro = cost_micro_usd(
            "모르는모델", input_tokens=1000, cache_read=0, cache_creation=0, output_tokens=100
        )

    assert micro > 0, "0으로 적으면 일일 비용 경보가 영원히 침묵한다"
    assert any("가격표에 없는 모델" in r.message for r in caplog.records)


def test_정식_Haiku_ID가_가격표에_있다() -> None:
    canonical = cost_micro_usd(
        "claude-haiku-4-5", input_tokens=300, cache_read=1200, cache_creation=0, output_tokens=100
    )
    aliased = cost_micro_usd(
        "claude-haiku-4-5-20251001",
        input_tokens=300,
        cache_read=1200,
        cache_creation=0,
        output_tokens=100,
    )

    assert canonical == aliased, "어느 ID를 써도 같은 값이어야 설정 표기가 비용을 바꾸지 않는다"
    assert canonical == 920


def test_설정된_모든_모델이_가격표에_있다() -> None:
    validate_pricing(Settings().model_ids())

    with pytest.raises(UnknownModelPricing):
        validate_pricing(Settings(triage_model="없는모델").model_ids())
