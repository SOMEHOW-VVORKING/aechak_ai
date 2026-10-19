"""Anthropic Messages API 래퍼.

계약 세 가지를 코드로 못박는다.

1. ``temperature``·``top_p``·``top_k``를 보내지 않는다. 현세대 모델에서 400이고,
   SDK 1.15의 ``messages.create`` 시그니처에 애초에 그 파라미터가 없다.
   결정성 레버는 ``output_config.effort``다.
2. 구조화 출력은 ``strict: true`` 툴을 강제 호출해 받는다. 본문 텍스트 파싱을 하지 않는다.
3. 오류를 세 갈래로 가른다. 레이트 429 / 티어 상한 429 / **워크스페이스 사용자 지정
   상한은 400 invalid_request_error**. 세 번째를 빼면 예산 소진이 스키마 오류로 숨는다.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import anthropic

from app.schemas.inspection import CallOutcome

log = logging.getLogger(__name__)

# 100만 토큰당 USD. 설정이 아니라 상수로 두되 모델 확정 시 공식 단가로 재확인한다.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}
_CACHE_WRITE_MULTIPLIER = 1.25
_CACHE_READ_MULTIPLIER = 0.1


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
    """
    in_price, out_price = _PRICING.get(model_id, (0.0, 0.0))
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
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_seconds)

    async def call_tool(
        self,
        *,
        model: str,
        effort: str,
        max_tokens: int,
        system_blocks: list[dict[str, Any]],
        user_content: list[dict[str, Any]],
        tool: dict[str, Any],
        request_id: str,
    ) -> LlmResult:
        call_id = f"{request_id}-{uuid.uuid4().hex[:8]}"
        started = time.monotonic()
        try:
            response = await self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                # temperature를 넣지 않는다. 결정성은 effort로만 조절한다.
                output_config={"effort": effort},
                system=system_blocks,
                messages=[{"role": "user", "content": user_content}],
                tools=[tool],
                tool_choice={"type": "tool", "name": tool["name"]},
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
