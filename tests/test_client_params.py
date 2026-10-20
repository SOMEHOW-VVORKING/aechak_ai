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
from pydantic import ValidationError

from app.inspection.triage import TRIAGE_TOOL
from app.llm.client import LlmClient, UnknownModelPricing, cost_micro_usd, validate_pricing
from app.schemas.inspection import CallOutcome
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


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
async def test_빈_effort는_output_config를_보내지_않는다(
    client: LlmClient, recorder: BodyRecorder, blank: str
) -> None:
    """ECS taskdef·.env·SSM에서 "값을 비운다"가 None이 아니라 빈 문자열로 들어온다.

    그걸 그대로 실으면 ``{"effort": ""}``가 전송돼 선별 전건이 400이고, SCHEMA_INVALID로
    분류돼 스키마 오류로 숨는다. 모델 ID 오타는 부팅에서 잡히지만 effort 값은 아무도 안 본다.
    """
    await client.call_tool(
        model="claude-haiku-4-5",
        effort=blank,
        max_tokens=512,
        system_blocks=[{"type": "text", "text": "선별 프롬프트"}],
        user_content=[{"type": "text", "text": "본문"}],
        tool=TRIAGE_TOOL,
        request_id="insp-blank",
    )

    assert "output_config" not in recorder.bodies[0], f"effort={blank!r}가 본문에 실렸다"


def test_빈_환경변수는_선별_effort를_미지정으로_접는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AECHAK_AI_TRIAGE_EFFORT", "")

    assert Settings().triage_effort is None


def test_모르는_effort_등급은_부팅에서_거부된다() -> None:
    """``none``·``LOW``·오타가 조용히 400을 만들지 않게 설정 검증에서 떨어뜨린다."""
    for bad in ("none", "null", "LOW", "lowish"):
        with pytest.raises(ValidationError):
            Settings(triage_effort=bad)


def test_관점_effort를_비우면_부팅에서_거부된다() -> None:
    """관점 호출의 등급을 비우면 모델 기본값(high)으로 조용히 올라간다. 400보다 더 조용하다."""
    with pytest.raises(ValidationError):
        Settings(screening_effort="")


async def test_SDK_재시도를_끈다(recorder: BodyRecorder) -> None:
    """선별은 재시도하지 않는다는 독스트링이 사실이어야 한다.

    SDK 기본값이 ``max_retries=2``라서 두지 않으면 429 한 건이 HTTP 3회가 된다 —
    근거로 든 레이트 리밋 상황에서 리밋을 3배로 미는 셈이다.
    """
    hits = 0

    def rate_limited(request: httpx2.Request) -> httpx2.Response:
        nonlocal hits
        hits += 1
        return httpx2.Response(429, json={"type": "error", "error": {"type": "rate_limit_error"}})

    llm = LlmClient(api_key="test", timeout_seconds=60.0)
    llm._client = llm._client.with_options(
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(rate_limited))
    )

    result = await llm.call_tool(
        model="claude-haiku-4-5",
        effort=None,
        max_tokens=512,
        system_blocks=[{"type": "text", "text": "선별 프롬프트"}],
        user_content=[{"type": "text", "text": "본문"}],
        tool=TRIAGE_TOOL,
        request_id="insp-429",
    )

    assert result.outcome is CallOutcome.RATE_LIMITED
    assert hits == 1, f"HTTP {hits}회. SDK 재시도가 살아 있다"


async def test_상한에_걸려_끊긴_응답은_SUCCESS가_아니다() -> None:
    """``stop_reason=max_tokens``면 ``tool_use.input``이 dict로 보여도 중간까지만 채워져 있다.

    SUCCESS로 적으면 끊긴 호출이 승급률 분자에서 "모델이 더 보라고 했다"와 같아진다.
    """

    def truncated(request: httpx2.Request) -> httpx2.Response:
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
                        "input": {"reason": "효능 표현이 의"},
                    }
                ],
                "stop_reason": "max_tokens",
                "stop_sequence": None,
                "usage": {"input_tokens": 300, "output_tokens": 512},
            },
        )

    llm = LlmClient(api_key="test", timeout_seconds=60.0)
    llm._client = llm._client.with_options(
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(truncated))
    )

    result = await llm.call_tool(
        model="claude-haiku-4-5",
        effort=None,
        max_tokens=512,
        system_blocks=[{"type": "text", "text": "선별 프롬프트"}],
        user_content=[{"type": "text", "text": "본문"}],
        tool=TRIAGE_TOOL,
        request_id="insp-trunc",
    )

    assert result.outcome is CallOutcome.TRUNCATED
    assert result.ok is False, "끊긴 응답을 ok로 읽으면 반쪽 tool_input을 판정에 쓴다"
