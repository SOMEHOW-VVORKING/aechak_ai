"""모델 호출 래퍼. Anthropic Messages API 와 OpenAI 호환 로컬 서버 둘을 같은 모양으로 감싼다.

계약 네 가지를 코드로 못박는다.

1. ``temperature``·``top_p``·``top_k``를 보내지 않는다. 현세대 모델에서 400이고,
   SDK 1.15의 ``messages.create`` 시그니처에 애초에 그 파라미터가 없다.
   결정성 레버는 ``output_config.effort``다.
2. 구조화 출력은 ``strict: true`` 툴을 강제 호출해 받는다. 본문 텍스트 파싱을 하지 않는다.
3. 오류를 세 갈래로 가른다. 레이트 429 / 티어 상한 429 / **워크스페이스 사용자 지정
   상한은 400 invalid_request_error**. 세 번째를 빼면 예산 소진이 스키마 오류로 숨는다.
4. ``max_tokens``로 끊긴 200 응답을 성공으로 적지 않는다. 끊긴 호출이 성공으로 섞이면
   승급률 같은 지표가 조용히 오염된다.

모델 ID 가 ``local/`` 로 시작하면 로컬 llama-server 로 간다(``local_base_url`` 의
``/v1/chat/completions``). 로컬 모델에는 tool use 가 없어서 구조화 출력을
``response_format`` 의 json_schema 로 받고 본문 JSON 을 파싱하지만, **돌려주는 것은 같은
``LlmResult``** 다 -- 호출부는 분기를 모른다. 위 계약 1 은 Anthropic 쪽 이야기고 로컬에는
``temperature`` 를 보낸다(아래 ``_call_local`` 주석).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx2

from app.schemas.inspection import CallOutcome

log = logging.getLogger(__name__)

# 로컬 모델 ID 접두. ``local/ax-4.0-light`` 의 뒷부분은 llama-server 가 쓰지 않지만(가중치가
# 기동 인자로 고정돼 있다) 추적과 하네스 표에 어느 가중치였는지 남기는 이름으로 쓴다.
LOCAL_MODEL_PREFIX = "local/"


def is_local_model(model_id: str) -> bool:
    return model_id.startswith(LOCAL_MODEL_PREFIX)


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
    """설정된 모델 ID 전부가 가격표에 있는지 본다. 모르면 예외를 던진다.

    ``local/`` 접두는 면제한다. 로컬 가중치는 단가가 0이라 가격표에 올릴 값이 없고, 올리면
    모델 하나를 더 받을 때마다 표를 고쳐야 기동한다.
    """
    unknown = sorted(
        {model_id for model_id in model_ids if model_id not in _PRICING and not is_local_model(model_id)}
    )
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
    영원히 침묵한다. **``local/`` 접두만 예외로 0이다** -- 내 맥에서 도는 가중치라 토큰에
    붙는 돈이 없다. 이 예외를 두지 않으면 로컬 선별이 최고 단가로 계상돼 비용 경보가
    쓰지도 않은 돈으로 울린다.
    """
    if is_local_model(model_id):
        return 0
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
    def __init__(self, *, api_key: str, timeout_seconds: float, local_base_url: str = "") -> None:
        # max_retries를 0으로 못박는다. SDK 기본값이 2라서 두지 않으면 429 한 건이 HTTP 3회가
        # 되고, 레이트 리밋 상황에서 리밋을 3배로 민다. 재시도가 필요한 자리는 파이프라인이
        # 관점 호출 하나당 한 번만 직접 돌린다 — 그래야 재시도 예산이 한곳에서 보인다.
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_seconds, max_retries=0)
        # 비어 있으면 로컬 경로를 쓸 수 없다. 기본값을 여기 박지 않는 이유는 그 값의 정본이
        # ``Settings.local_llm_base_url`` 하나여야 하기 때문이다 -- 양쪽에 같은 URL 을 적어
        # 두면 한쪽만 고치는 날 호출이 조용히 엉뚱한 포트로 간다.
        self._local_base_url = local_base_url.rstrip("/")
        # httpx2 는 anthropic SDK 가 이미 끌고 오는 패키지다. 새 의존성을 더하지 않으려고
        # 그것을 쓴다(dev 쪽 httpx 0.28 과 respx 는 별 패키지라 이 클라이언트를 못 가로챈다).
        # 재시도는 기본으로 없다 -- Anthropic 쪽 max_retries=0 과 같은 정책이다.
        self._http = httpx2.AsyncClient(timeout=timeout_seconds)

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

        모델 ID 가 ``local/`` 로 시작하면 로컬 llama-server 로 보낸다. ``effort`` 는 그 경로에서
        쓰이지 않는다.
        """
        if is_local_model(model):
            return await self._call_local(
                model=model,
                max_tokens=max_tokens,
                system_blocks=system_blocks,
                user_content=user_content,
                tool=tool,
                request_id=request_id,
                timeout_seconds=timeout_seconds,
            )

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

    async def _call_local(
        self,
        *,
        model: str,
        max_tokens: int,
        system_blocks: list[dict[str, Any]],
        user_content: list[dict[str, Any]],
        tool: dict[str, Any],
        request_id: str,
        timeout_seconds: float | None,
    ) -> LlmResult:
        """로컬 llama-server 의 OpenAI 호환 경로. 같은 ``LlmResult``를 돌려준다.

        tool use 가 없어서 툴의 ``input_schema`` 를 ``response_format`` 의 json_schema 로
        옮기고 본문 JSON 을 파싱한다. 그래서 ``_extract_tool_input`` 이 보던 "툴 블록이 없다"
        자리가 여기서는 "본문이 JSON 이 아니다"가 되고, 둘 다 ``SCHEMA_INVALID`` 로 간다.

        ``temperature`` 를 0으로 **보낸다.** Anthropic 쪽에 샘플링 파라미터를 보내지 않는 것은
        그 API 가 400을 내기 때문이고, llama.cpp 의 기본값은 0.8 이다 -- 안 보내면 판정이 둘뿐인
        선별이 같은 입력에 다른 답을 내고 그 흔들림이 승급률 측정에 그대로 섞인다.
        """
        call_id = f"{request_id}-{uuid.uuid4().hex[:8]}"
        started = time.monotonic()

        def failed(outcome: CallOutcome, detail: str) -> LlmResult:
            log.warning(
                "로컬 모델 호출 실패. call_id=%s model=%s outcome=%s detail=%s",
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
                detail=detail[:300],
            )

        if not self._local_base_url:
            return failed(CallOutcome.ERROR, "local_base_url 이 비어 있다")
        try:
            system_text = _flatten_text(system_blocks)
            user_text = _flatten_text(user_content)
        except ValueError as error:
            return failed(CallOutcome.SCHEMA_INVALID, str(error))

        body: dict[str, Any] = {
            # 가중치는 기동 인자로 고정돼 있어 llama-server 는 이 이름을 보지 않는다.
            "model": model.removeprefix(LOCAL_MODEL_PREFIX),
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_text},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": tool["name"],
                    "strict": True,
                    "schema": tool["input_schema"],
                },
            },
        }

        timeout: Any = httpx2.USE_CLIENT_DEFAULT if timeout_seconds is None else timeout_seconds
        try:
            response = await self._http.post(
                f"{self._local_base_url}/v1/chat/completions", json=body, timeout=timeout
            )
        except httpx2.TimeoutException:
            return failed(CallOutcome.TIMEOUT, "request timeout")
        except httpx2.RequestError as error:
            # 서버가 안 떠 있을 때 여기로 온다. 로컬에는 예산 소진이 없으므로 전부 ERROR 다.
            return failed(CallOutcome.ERROR, f"{type(error).__name__}: {error}")

        if response.status_code != 200:
            # llama-server 는 받지 못하는 json_schema 를 400 으로 거부한다. Anthropic 쪽
            # BadRequestError 를 SCHEMA_INVALID 로 적는 것과 같은 자리라 같게 적는다.
            outcome = CallOutcome.SCHEMA_INVALID if response.status_code == 400 else CallOutcome.ERROR
            return failed(outcome, f"status {response.status_code}: {response.text}")

        try:
            payload = response.json()
        except ValueError as error:
            return failed(CallOutcome.ERROR, f"응답이 JSON 이 아니다: {error}")

        usage = payload.get("usage") or {}
        input_tokens = _as_int(usage.get("prompt_tokens"))
        output_tokens = _as_int(usage.get("completion_tokens"))

        choices = payload.get("choices") or []
        if not choices:
            return failed(CallOutcome.SCHEMA_INVALID, "choices 가 비어 있다")
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        content = (message or {}).get("content")

        outcome = CallOutcome.SUCCESS
        detail: str | None = None
        tool_input: dict[str, Any] | None = None
        if choice.get("finish_reason") == "length":
            # 문법 제약 디코딩이라 끊기면 JSON 도 깨져 있다. 그래도 SCHEMA_INVALID 로 적지
            # 않는다 -- 상한을 올려야 하는 것을 스키마 오류로 적으면 알 수 없다.
            outcome, detail = CallOutcome.TRUNCATED, f"max_tokens={max_tokens}"
        else:
            tool_input = _parse_json_object(content)
            if tool_input is None:
                outcome = CallOutcome.SCHEMA_INVALID
                detail = f"본문이 JSON 객체가 아니다: {str(content)[:200]}"
            else:
                missing = _missing_required(tool["input_schema"], tool_input)
                if missing:
                    # 객체이기만 하면 통과시키면 ``{}`` 가 SUCCESS 로 올라간다. 그러면 호출부가
                    # 없는 ``findings`` 를 빈 목록으로 읽어 **미검사가 통과로 둔갑한다**.
                    # llama-server 의 문법 제약 디코딩은 required 를 항상 강제하지는 않으므로
                    # 여기서 한 번 더 본다.
                    outcome = CallOutcome.SCHEMA_INVALID
                    detail = f"필수 키 누락: {', '.join(missing)}"
                    tool_input = None

        if outcome is not CallOutcome.SUCCESS:
            log.warning(
                "로컬 모델 응답 불량. call_id=%s model=%s outcome=%s detail=%s",
                call_id,
                model,
                outcome,
                detail,
            )

        return LlmResult(
            call_id=call_id,
            outcome=outcome,
            model_id=model,
            tool_input=tool_input,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            # 로컬 가중치는 단가가 0이다. 그래도 같은 함수를 지나게 둔다 -- 접두 예외가
            # 한 자리에만 있어야 비용 0 이 우연이 아니라 규칙으로 남는다.
            cost_micro_usd=cost_micro_usd(
                model,
                input_tokens=input_tokens,
                cache_read=0,
                cache_creation=0,
                output_tokens=output_tokens,
            ),
            latency_ms=int((time.monotonic() - started) * 1000),
            detail=detail,
        )

    async def aclose(self) -> None:
        await self._client.close()
        await self._http.aclose()


def _flatten_text(blocks: list[dict[str, Any]]) -> str:
    """Anthropic 블록 목록을 한 문자열로 접는다. text 가 아닌 블록은 거부한다.

    조용히 버리면 이미지를 보낸 호출이 이미지 없이 돌고도 성공으로 보인다.
    """
    parts: list[str] = []
    for block in blocks:
        kind = block.get("type")
        if kind != "text":
            raise ValueError(f"로컬 경로는 text 블록만 보낸다. 받은 종류={kind!r}")
        parts.append(str(block.get("text", "")))
    return "\n\n".join(parts)


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _parse_json_object(content: Any) -> dict[str, Any] | None:
    if not isinstance(content, str):
        return None
    try:
        parsed = json.loads(content)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _missing_required(schema: Any, payload: dict[str, Any]) -> list[str]:
    """응답에서 빠져 있으면 안 되는 최상위 키를 돌려준다.

    ``required`` 를 그대로 쓰지 않는다. ``strict`` 모드가 선언한 속성 **전부** 를 거기 넣으라고
    요구해서, 우리 툴들은 선택 항목도 ``required`` 에 적고 대신 타입을 ``["string", "null"]``
    로 넓혀 둔다(``triage.TRIAGE_TOOL`` 머리에 그 사정을 적었다). 그래서 ``required`` 는
    "반드시 있어야 한다" 가 아니라 "선언했다" 는 뜻이고, 실제로 빠지면 안 되는 것은
    **null 을 허용하지 않는** 속성이다. 선별이 ``CLEAR`` 를 낼 때 ``category`` 와 ``quote`` 를
    비우는 것은 정상 응답이므로 그 둘을 요구하면 멀쩡한 호출이 실패로 적힌다.

    중첩까지 보지 않는다. 항목 안쪽은 호출부가 이미 방어적으로 읽고(``item.get("quote")
    or ""``) 인용 검증이 한 번 더 걸러서 누락이 판정을 뒤집지 못한다. 최상위는 다르다 --
    ``findings`` 하나가 비면 "위반 없음" 과 구별되지 않는다.
    """
    if not isinstance(schema, dict):
        return []
    required = schema.get("required")
    properties = schema.get("properties")
    if not isinstance(required, list) or not isinstance(properties, dict):
        return []
    missing: list[str] = []
    for key in required:
        if not isinstance(key, str) or key in payload:
            continue
        declared = properties.get(key)
        types = declared.get("type") if isinstance(declared, dict) else None
        nullable = types == "null" or (isinstance(types, list) and "null" in types)
        if not nullable:
            missing.append(key)
    return missing


def _extract_tool_input(response: Any, tool_name: str) -> dict[str, Any] | None:
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            value = getattr(block, "input", None)
            if isinstance(value, dict):
                return value
    return None
