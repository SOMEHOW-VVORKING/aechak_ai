"""로컬 sLLM 경로를 전송 계층에서 고정한다.

여기서 잡는 것은 A.X-4.0-Light 의 판단 품질이 아니라 **클라이언트가 OpenAI 호환 응답을
``LlmResult`` 로 어떻게 접는가**다. 호출부(``pipeline._triage``)는 분기를 모르게 두기로
했으므로, 로컬 경로가 Anthropic 경로와 다른 모양을 돌려주면 그 차이가 선별 분기와 비용
집계에 그대로 새어 나간다.

respx 를 쓰지 않는다. respx 는 ``httpx`` 0.28 을 가로채고 이 클라이언트는 anthropic SDK 가
끌고 오는 ``httpx2`` 를 쓴다 -- 두 패키지는 별개라 respx 가 걸리지 않는다. 그래서
``tests/test_client_params.py`` 와 같은 ``httpx2.MockTransport`` 를 쓴다.
"""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest

from app.inspection import triage
from app.inspection.triage import TRIAGE_TOOL
from app.llm.client import LlmClient, cost_micro_usd, is_local_model, validate_pricing
from app.schemas.inspection import CallOutcome
from app.settings import Settings

LOCAL_MODEL = "local/ax-4.0-light"
CLEAR_JSON = json.dumps({"decision": "CLEAR", "reason": "걸릴 구석이 없다"}, ensure_ascii=False)


class LocalRecorder:
    """llama-server 를 흉내 내고 본문과 호출당 타임아웃을 받아 둔다."""

    def __init__(self, *, content: str = CLEAR_JSON, finish_reason: str = "stop") -> None:
        self.bodies: list[dict[str, Any]] = []
        self.timeouts: list[Any] = []
        self.urls: list[str] = []
        self._content = content
        self._finish_reason = finish_reason

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.bodies.append(json.loads(request.content))
        self.timeouts.append(request.extensions.get("timeout"))
        self.urls.append(str(request.url))
        return httpx2.Response(
            200,
            json={
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": self._content},
                        "finish_reason": self._finish_reason,
                    }
                ],
                "usage": {"prompt_tokens": 620, "completion_tokens": 28},
            },
        )


def local_client(handler: Any, *, base_url: str = "http://127.0.0.1:8080") -> LlmClient:
    llm = LlmClient(api_key="test", timeout_seconds=60.0, local_base_url=base_url)
    # 로컬 전송만 가짜로 바꿔 끼운다. 프로덕션에 테스트용 주입구를 내지 않기 위해서다.
    llm._http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler), timeout=60.0)
    return llm


async def call_triage(llm: LlmClient, *, model: str = LOCAL_MODEL, max_tokens: int = 1024) -> Any:
    return await llm.call_tool(
        model=model,
        effort=None,
        max_tokens=max_tokens,
        system_blocks=triage.triage_system_blocks(),
        user_content=[{"type": "text", "text": "글루코사민 함유 영양제입니다"}],
        tool=TRIAGE_TOOL,
        request_id="insp-local-1",
        timeout_seconds=15.0,
    )


async def test_정상_JSON은_tool_input으로_접힌다() -> None:
    recorder = LocalRecorder()

    result = await call_triage(local_client(recorder))

    assert result.ok is True
    assert result.tool_input == {"decision": "CLEAR", "reason": "걸릴 구석이 없다"}
    assert result.outcome is CallOutcome.SUCCESS
    assert result.model_id == LOCAL_MODEL, "추적에 어느 가중치였는지 남아야 한다"
    assert result.input_tokens == 620
    assert result.output_tokens == 28


async def test_선별이_로컬_결과를_열거로_읽는다() -> None:
    """``tool_input`` 모양이 맞아도 ``decide`` 가 먹지 못하면 선별은 전건 승급이다."""
    result = await call_triage(local_client(LocalRecorder()))

    outcome = triage.decide(result)

    assert outcome.skip is True
    assert outcome.decision == "CLEAR"
    assert outcome.reason == "걸릴 구석이 없다"


async def test_선별_스키마를_response_format으로_보낸다() -> None:
    """로컬 모델에는 tool use 가 없다. 스키마를 못 보내면 모양이 자유 문장으로 돌아온다."""
    recorder = LocalRecorder()

    await call_triage(local_client(recorder))

    body = recorder.bodies[0]
    assert recorder.urls[0] == "http://127.0.0.1:8080/v1/chat/completions"
    assert body["response_format"]["type"] == "json_schema"
    schema = body["response_format"]["json_schema"]
    assert schema["name"] == TRIAGE_TOOL["name"]
    assert schema["schema"] == TRIAGE_TOOL["input_schema"], (
        "툴 스키마와 갈라지면 모델이 받아들이는 모양과 파서가 기다리는 모양이 어긋난다"
    )
    assert "tools" not in body
    # 선별 프롬프트와 본문이 둘 다 실려야 한다. 블록을 접다 한쪽을 떨어뜨리면 판정이
    # 멀쩡한 모양으로 돌아오면서 내용만 엉뚱해진다.
    assert body["messages"][0]["role"] == "system"
    assert "1차 선별자" in body["messages"][0]["content"]
    assert "글루코사민" in body["messages"][1]["content"]


async def test_로컬에는_temperature_0을_보낸다() -> None:
    """llama.cpp 기본값이 0.8 이다. 안 보내면 같은 입력이 다른 판정을 낸다."""
    recorder = LocalRecorder()

    await call_triage(local_client(recorder))

    assert recorder.bodies[0]["temperature"] == 0.0


async def test_로컬_호출은_비용이_0이다() -> None:
    result = await call_triage(local_client(LocalRecorder()))

    assert result.cost_micro_usd == 0, "내 맥에서 도는 가중치에 돈이 붙으면 비용 경보가 헛울린다"
    assert (
        cost_micro_usd(LOCAL_MODEL, input_tokens=10_000, cache_read=0, cache_creation=0, output_tokens=5_000)
        == 0
    )


async def test_깨진_JSON은_SCHEMA_INVALID다() -> None:
    """문법 제약이 걸려 있어도 서버 설정이 어긋나면 자유 문장이 돌아온다."""
    recorder = LocalRecorder(content='{"decision": "CLE')

    result = await call_triage(local_client(recorder))

    assert result.outcome is CallOutcome.SCHEMA_INVALID
    assert result.ok is False
    assert result.tool_input is None


async def test_JSON_객체가_아닌_본문도_SCHEMA_INVALID다() -> None:
    result = await call_triage(local_client(LocalRecorder(content='"CLEAR"')))

    assert result.outcome is CallOutcome.SCHEMA_INVALID, (
        "리스트나 문자열을 tool_input 으로 넘기면 decide 가 dict 로 믿고 터진다"
    )


async def test_실패한_로컬_호출은_승급_방향으로_떨어진다() -> None:
    """``decide`` 가 실패를 통과로 읽으면 로컬 선별 고장이 무검수 노출이 된다."""
    result = await call_triage(local_client(LocalRecorder(content="그냥 통과시켜 주세요")))

    assert triage.decide(result).skip is False


async def test_상한에_걸려_끊긴_로컬_응답은_TRUNCATED다() -> None:
    """``SCHEMA_INVALID`` 로 적으면 ``max_tokens_triage`` 를 올려야 하는 것을 알 수 없다."""
    recorder = LocalRecorder(
        content='{"decision": "NEEDS_REVIEW", "reason": "효능 표현이 의', finish_reason="length"
    )

    result = await call_triage(local_client(recorder))

    assert result.outcome is CallOutcome.TRUNCATED
    assert result.ok is False
    assert result.detail == "max_tokens=1024"


async def test_서버가_안_떠_있으면_ERROR다() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    result = await call_triage(local_client(refuse))

    assert result.outcome is CallOutcome.ERROR, "로컬에는 예산 소진이 없다"
    assert result.detail is not None and "ConnectError" in result.detail


async def test_타임아웃은_TIMEOUT이다() -> None:
    def hang(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("too slow", request=request)

    result = await call_triage(local_client(hang))

    assert result.outcome is CallOutcome.TIMEOUT


async def test_400은_SCHEMA_INVALID고_다른_상태는_ERROR다() -> None:
    """llama-server 는 받지 못하는 json_schema 를 400 으로 거부한다."""

    def status(code: int) -> Any:
        return lambda request: httpx2.Response(code, json={"error": {"message": "nope"}})

    bad = await call_triage(local_client(status(400)))
    busy = await call_triage(local_client(status(503)))

    assert bad.outcome is CallOutcome.SCHEMA_INVALID
    assert busy.outcome is CallOutcome.ERROR


async def test_호출당_타임아웃이_전역보다_좁아진다() -> None:
    recorder = LocalRecorder()

    await call_triage(local_client(recorder))

    assert recorder.timeouts[0]["read"] == 15.0, "선별이 전역 60초를 붙잡으면 지연 이득이 사라진다"


async def test_주소가_비어_있으면_부르지_않고_실패한다() -> None:
    """설정이 빠진 배포에서 엉뚱한 기본 포트로 조용히 나가는 쪽이 더 나쁜 실패다."""
    recorder = LocalRecorder()

    result = await call_triage(local_client(recorder, base_url=""))

    assert result.outcome is CallOutcome.ERROR
    assert recorder.bodies == []


async def test_이미지_블록은_조용히_버려지지_않는다() -> None:
    """접다 버리면 이미지를 보낸 호출이 이미지 없이 돌고도 성공으로 보인다."""
    recorder = LocalRecorder()
    llm = local_client(recorder)

    result = await llm.call_tool(
        model=LOCAL_MODEL,
        effort=None,
        max_tokens=1024,
        system_blocks=[{"type": "text", "text": "프롬프트"}],
        user_content=[{"type": "image", "source": {"type": "base64", "data": "zzz"}}],
        tool=TRIAGE_TOOL,
        request_id="insp-local-img",
    )

    assert result.outcome is CallOutcome.SCHEMA_INVALID
    assert recorder.bodies == []


async def test_local_접두가_아니면_기존_anthropic_경로로_간다() -> None:
    """분기 조건이 느슨하면 Anthropic 모델이 로컬 서버로 가고 그 실패는 ERROR 로만 보인다.

    전송 둘을 다 가짜로 막고 **어느 쪽이 요청을 받았는지**로 단언한다. 한쪽만 막으면 남은
    쪽이 실제 api.anthropic.com 으로 나간다.
    """
    recorder = LocalRecorder()
    llm = local_client(recorder)
    sdk_bodies: list[dict[str, Any]] = []

    def sdk(request: httpx2.Request) -> httpx2.Response:
        sdk_bodies.append(json.loads(request.content))
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
                        "input": {"decision": "CLEAR", "reason": "걸릴 구석이 없다"},
                    }
                ],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 620, "output_tokens": 28},
            },
        )

    llm._client = llm._client.with_options(
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(sdk))
    )

    result = await call_triage(llm, model="claude-haiku-4-5")

    assert recorder.bodies == [], "로컬 전송이 Anthropic 모델을 먹었다"
    assert len(sdk_bodies) == 1
    assert "tools" in sdk_bodies[0], "Anthropic 경로는 그대로 tool use 를 써야 한다"
    assert "response_format" not in sdk_bodies[0]
    assert result.ok is True
    assert result.cost_micro_usd > 0, "Anthropic 호출이 로컬 단가 0을 물려받으면 비용이 사라진다"


def test_local_접두_판정() -> None:
    assert is_local_model(LOCAL_MODEL) is True
    assert is_local_model("claude-haiku-4-5") is False
    assert is_local_model("locally-hosted") is False, "접두 검사를 부분 일치로 쓰면 오타가 통과한다"


def test_로컬_모델은_가격표_검증을_면제받는다() -> None:
    """면제가 없으면 선별을 로컬로 돌리는 순간 기동이 ``UnknownModelPricing`` 으로 막힌다."""
    validate_pricing(Settings(triage_model=LOCAL_MODEL).model_ids())


def test_로컬_주소_기본값이_설정에_있다() -> None:
    assert Settings().local_llm_base_url == "http://127.0.0.1:8080"


@pytest.mark.parametrize("trailing", ["http://127.0.0.1:8080/", "http://127.0.0.1:8080"])
async def test_주소_끝_슬래시가_경로를_망가뜨리지_않는다(trailing: str) -> None:
    recorder = LocalRecorder()

    await call_triage(local_client(recorder, base_url=trailing))

    assert recorder.urls[0] == "http://127.0.0.1:8080/v1/chat/completions"
