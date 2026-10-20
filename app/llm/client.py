"""Anthropic Messages API 래퍼.

계약 세 가지를 코드로 못박는다.

1. ``temperature``·``top_p``·``top_k``를 보내지 않는다. 현세대 모델에서 400이고,
   SDK 1.15의 ``messages.create`` 시그니처에 애초에 그 파라미터가 없다.
   결정성 레버는 ``output_config.effort``다.
2. 구조화 출력은 ``strict: true`` 툴을 강제 호출해 받는다. 본문 텍스트 파싱을 하지 않는다.
3. 오류를 세 갈래로 가른다. 레이트 429 / 티어 상한 429 / **워크스페이스 사용자 지정
   상한은 400 invalid_request_error**. 세 번째를 빼면 예산 소진이 스키마 오류로 숨는다.
4. ``max_tokens``로 끊긴 200 응답을 성공으로 적지 않는다. 끊긴 호출이 성공으로 섞이면
   승급률 같은 지표가 조용히 오염된다.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import anthropic

from app.schemas.inspection import CallOutcome

log = logging.getLogger(__name__)

# 100만 토큰당 USD. 설정이 아니라 상수로 두되 모델 확정 시 공식 단가로 재확인한다.
# Haiku 4.5는 정식 ID와 날짜 접미 별칭이 둘 다 실재한다. 어느 쪽을 설정에 적어도
# 비용이 0으로 기록되지 않게 둘 다 올려 둔다.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
_CACHE_WRITE_MULTIPLIER = 1.25
_CACHE_READ_MULTIPLIER = 0.1

# 미지 모델에 쓰는 단가. 표 최고값으로 두면 그보다 비싼 등급이 여전히 과소 계상되므로,
# 표와 무관하게 넉넉히 잡는다. 과대 계상은 경보를 당기고 과소 계상은 경보를 침묵시킨다.
_FALLBACK_PRICING: tuple[float, float] = (15.0, 75.0)


class UnknownModelPricing(RuntimeError):
    """가격표에 없는 모델 ID. 부팅에서 기동을 거부하는 데 쓴다."""


def validate_pricing(model_ids: Iterable[str]) -> None:
    """설정된 모델 ID 전부가 가격표에 있는지 본다. 모르면 예외를 던진다."""
    unknown = sorted({model_id for model_id in model_ids if model_id not in _PRICING})
    if unknown:
        raise UnknownModelPricing(f"가격표에 없는 모델이 설정됐다: {', '.join(unknown)}")


@dataclass(slots=True)
class LlmResult:
    """한 번의 모델 호출 결과와 그 사용량."""

    call_id: str
    outcome: CallOutcome
    model_id: str
    tool_input: dict[str, Any] | None
    input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    output_tokens: int = 0
    cost_micro_usd: int = 0
    latency_ms: int = 0
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is CallOutcome.SUCCESS and self.tool_input is not None


def cost_micro_usd(
    model_id: str,
    *,
    input_tokens: int,
    cache_read: int,
    cache_creation: int,
    output_tokens: int,
) -> int:
    """입력 토큰 셋을 따로 받아 비용을 만든다.

    전체 입력이 세 값의 합이라서 나누지 않으면 비용을 되살릴 수 없다.
    캐시 쓰기는 1.25배, 캐시 읽기는 0.1배로 과금된다.

    가격표에 없는 모델은 0이 아니라 최고 단가로 과대 계상한다. 0으로 적으면 비용 경보가
    영원히 침묵한다.
    """
    pricing = _PRICING.get(model_id)
    if pricing is None:
        log.error("가격표에 없는 모델이다. 최고 단가로 과대 계상한다. model=%s", model_id)
        pricing = _FALLBACK_PRICING
    in_price, out_price = pricing
    usd = (
        input_tokens * in_price
        + cache_creation * in_price * _CACHE_WRITE_MULTIPLIER
        + cache_read * in_price * _CACHE_READ_MULTIPLIER
        + output_tokens * out_price
    ) / 1_000_000
    return round(usd * 1_000_000)


def _classify(error: Exception) -> tuple[CallOutcome, str]:
    """오류를 세 갈래로 가른다. 예산 소진을 스키마 오류로 묻지 않는 것이 목적이다."""
    if isinstance(error, anthropic.APITimeoutError):
        return CallOutcome.TIMEOUT, "request timeout"
    if isinstance(error, anthropic.RateLimitError):
        code = _error_code(error)
        if code == "enforced_spend_limit_reached":
            return CallOutcome.BUDGET_EXHAUSTED, "tier spend cap reached"
        return CallOutcome.RATE_LIMITED, f"rate limited ({code or 'unknown'})"
    if isinstance(error, anthropic.BadRequestError):
        message = str(error)
        # 워크스페이스 사용자 지정 spend limit은 429가 아니라 400으로 온다
        if "spend limit" in message.lower() or "credit balance" in message.lower():
            return CallOutcome.BUDGET_EXHAUSTED, "workspace spend limit reached"
        return CallOutcome.SCHEMA_INVALID, message[:300]
    if isinstance(error, anthropic.APIStatusError):
        return CallOutcome.ERROR, f"status {error.status_code}"
    return CallOutcome.ERROR, type(error).__name__


def _error_code(error: anthropic.APIStatusError) -> str | None:
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict):
            details = inner.get("details")
            if isinstance(details, dict):
                value = details.get("error_code")
                if isinstance(value, str):
                    return value
    return None


class LlmClient:
    def __init__(self, *, api_key: str, timeout_seconds: float) -> None:
        # max_retries를 0으로 못박는다. SDK 기본값이 2라서 두지 않으면 429 한 건이 HTTP 3회가
        # 되고, 레이트 리밋 상황에서 리밋을 3배로 민다. 재시도가 필요한 자리는 파이프라인이
        # 관점 호출 하나당 한 번만 직접 돌린다 — 그래야 재시도 예산이 한곳에서 보인다.
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_seconds, max_retries=0)

    async def call_tool(
        self,
        *,
        model: str,
        effort: str | None,
        max_tokens: int,
        system_blocks: list[dict[str, Any]],
        user_content: list[dict[str, Any]],
        tool: dict[str, Any],
        request_id: str,
        timeout_seconds: float | None = None,
    ) -> LlmResult:
        """``effort``에 기본값을 두지 않는다. 호출부가 반드시 등급을 명시하거나 None을 준다.

        ``thinking``을 보내지 않는다. Sonnet 5와 Opus 5는 생략이 곧 adaptive이고,
        Haiku 4.5는 생략하면 사고하지 않는다 -- 그 세대는 budget_tokens를 받아 adaptive가 400이다.
        """
        call_id = f"{request_id}-{uuid.uuid4().hex[:8]}"
        started = time.monotonic()
        # output_config를 조건부로 조립한다. effort=None을 그냥 넘기면 SDK가 키를 생략하지 않고
        # {"effort": null}을 본문에 실어 400이 된다.
        #
        # 빈 문자열도 미지정으로 접는다. 설정 계층이 이미 접지만, 400은 전건에 걸리고 조용해서
        # 전송 계층에도 둔다. Settings를 거치지 않는 하네스와 스크립트가 같은 보호를 받는다.
        grade = effort.strip() if effort is not None else None
        options: dict[str, Any] = {}
        if grade:
            # temperature를 넣지 않는다. 결정성은 effort로만 조절한다.
            options["output_config"] = {"effort": grade}

        client = self._client
        if timeout_seconds is not None:
            client = client.with_options(timeout=timeout_seconds)

        try:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_blocks,
                messages=[{"role": "user", "content": user_content}],
                tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
                **options,
            )
        except Exception as error:
            outcome, detail = _classify(error)
            log.warning(
                "모델 호출 실패. call_id=%s model=%s outcome=%s detail=%s",
                call_id,
                model,
                outcome,
                detail,
            )
            return LlmResult(
                call_id=call_id,
                outcome=outcome,
                model_id=model,
                tool_input=None,
                latency_ms=int((time.monotonic() - started) * 1000),
                detail=detail,
            )

        usage = response.usage
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0

        tool_input = _extract_tool_input(response, tool["name"])
        outcome = CallOutcome.SUCCESS
        detail = None
        if response.stop_reason == "refusal":
            outcome, detail = CallOutcome.REFUSED, "model refused"
        elif response.stop_reason == "max_tokens":
            # 상한에 걸려 끊긴 응답이다. tool_use.input이 dict로 보여도 중간까지만 채워져 있다.
            # SUCCESS로 적으면 끊긴 호출이 승급률 분자에서 "모델이 더 보라고 했다"와 같아지고,
            # 스키마 오류로 적으면 상한을 올려야 하는 것을 알 수 없다.
            outcome, detail = CallOutcome.TRUNCATED, f"max_tokens={max_tokens}"
        elif tool_input is None:
            outcome, detail = CallOutcome.SCHEMA_INVALID, f"stop_reason={response.stop_reason}"

        if cache_read == 0 and cache_creation == 0:
            # 캐시 미달은 에러 없이 조용히 발생한다. 비용이 그대로 올라가므로 남긴다.
            log.info("프롬프트 캐시 미적중. call_id=%s model=%s", call_id, model)

        return LlmResult(
            call_id=call_id,
            outcome=outcome,
            model_id=model,
            tool_input=tool_input,
            input_tokens=input_tokens,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_creation,
            output_tokens=output_tokens,
            cost_micro_usd=cost_micro_usd(
                model,
                input_tokens=input_tokens,
                cache_read=cache_read,
                cache_creation=cache_creation,
                output_tokens=output_tokens,
            ),
            latency_ms=int((time.monotonic() - started) * 1000),
            detail=detail,
        )

    async def aclose(self) -> None:
        await self._client.close()


def _extract_tool_input(response: Any, tool_name: str) -> dict[str, Any] | None:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            value = getattr(block, "input", None)
            if isinstance(value, dict):
                return value
    return None
