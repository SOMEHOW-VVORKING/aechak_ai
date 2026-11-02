"""모델 경계에서 되돌리는 재생 클라이언트.

판정 JSON 한 건을 다시 그리는 것(얕은 재생)으로는 콘솔이 보여 줘야 하는 것의 절반이
나오지 않는다. 선별 사유·비평 결정·단계 라벨·실패 상세가 판정 계약에 없기 때문이다.
그래서 **모델 호출 경계**에서 되돌린다. ``call_tool`` 만 구현한 가짜를 끼우면
``InspectionPipeline`` 이 **그대로 돌아** 추적 전체가 생기고, 결정론적이며 비용이 0이다.

이 이음매는 이미 열려 있었다 -- 파이프라인은 ``llm`` 을 덕 타이핑으로 ``call_tool`` 과
``aclose`` 만 부른다(``tests/conftest.py`` 의 ``FakeLlmClient`` 가 그 증거다). 운영 코드는
한 줄도 바꾸지 않는다. ``tests/`` 는 패키지가 아니라 콘솔이 import 할 수 없어 여기에 자기
것을 둔다.

카세트 키를 호출 순서로 두지 않는다. 순서로 두면 파이프라인 분기가 바뀔 때마다 조용히
어긋난 응답이 흘러 들어간다. **(툴 이름, 관점)** 으로 고르고, 없는 키는 조용히 통과 방향으로
떨어지지 않게 ``CassetteMiss`` 로 터뜨린다 -- 선별과 비평이 실패를 각각 승급과 유지로 접기
때문에, 빠진 카세트를 조용히 실패로 돌리면 콘솔이 거짓 단계를 그린다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.inspection import triage
from app.inspection.perspectives import specs_for
from app.llm.client import LlmResult, cost_micro_usd
from app.schemas.inspection import CallOutcome, Perspective, TargetType

# (툴 이름, 관점). 관점이 없는 호출(비평·수정문구)은 None 이다.
CallKey = tuple[str, Perspective | None]

_FINDINGS_TOOL = "report_findings"


class CassetteMiss(RuntimeError):
    """카세트에 없는 호출. 데모 중에 조용히 다른 길로 가지 않게 터뜨린다."""


@dataclass(frozen=True, slots=True)
class ScriptedCall:
    """기록된 응답 한 건. 토큰 수를 들고 있어야 원장의 비용이 실제 단가로 계산된다."""

    tool_input: dict[str, Any] | None = None
    outcome: CallOutcome = CallOutcome.SUCCESS
    input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    detail: str | None = None
    # 모델 ID를 비우면 호출부가 설정으로 고른 값을 쓴다. 비용은 그 ID의 단가로 계산된다.
    model_id: str | None = None


@dataclass(slots=True)
class ReplayLlmClient:
    """``call_tool`` 과 ``aclose`` 만 구현한다. 잠들지 않고 네트워크를 타지 않는다."""

    entries: dict[CallKey, ScriptedCall] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

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
        key = cassette_key(tool)
        scripted = self.entries.get(key)
        if scripted is None:
            raise CassetteMiss(
                f"카세트에 없는 호출이다. key={key[0]}/{key[1].value if key[1] else '-'} "
                f"model={model}. 시나리오의 entries 에 이 키를 더해라"
            )

        # 호출 순서대로 번호를 붙인다. 같은 입력이면 같은 call_id 가 나와야 재생이 결정론적이다.
        call_id = f"{request_id}-{len(self.calls):02d}-{key[0]}"
        self.calls.append(
            {
                "key": key,
                "model": model,
                "effort": effort,
                "max_tokens": max_tokens,
                "timeout_seconds": timeout_seconds,
                "tool": tool["name"],
                "system_blocks": system_blocks,
                "user_content": user_content,
            }
        )
        model_id = scripted.model_id or model
        return LlmResult(
            call_id=call_id,
            outcome=scripted.outcome,
            model_id=model_id,
            tool_input=scripted.tool_input if scripted.outcome is CallOutcome.SUCCESS else None,
            input_tokens=scripted.input_tokens,
            cache_read_input_tokens=scripted.cache_read_input_tokens,
            cache_creation_input_tokens=scripted.cache_creation_input_tokens,
            output_tokens=scripted.output_tokens,
            # 가격표는 운영 코드의 것을 그대로 쓴다. 데모가 자기 단가표를 들면 원장의 숫자가
            # 운영과 어긋나고, 그 숫자로 손익분기를 이야기할 수 없다.
            cost_micro_usd=cost_micro_usd(
                model_id,
                input_tokens=scripted.input_tokens,
                cache_read=scripted.cache_read_input_tokens,
                cache_creation=scripted.cache_creation_input_tokens,
                output_tokens=scripted.output_tokens,
            ),
            latency_ms=scripted.latency_ms,
            detail=scripted.detail,
        )

    async def aclose(self) -> None:
        return None


def cassette_key(tool: dict[str, Any]) -> CallKey:
    """툴 스키마에서 (툴 이름, 관점)을 뽑는다.

    관점 호출은 툴 이름이 둘 다 ``report_findings`` 라서 이름으로는 갈라지지 않는다.
    대신 ``findings_tool`` 이 관점마다 다른 분류 열거를 싣는다는 사실을 쓴다 -- 그 집합이
    관점의 지문이고, 캐시 엔트리가 관점별로 갈라지는 이유도 같은 것이다.
    """
    name = str(tool.get("name"))
    if name != _FINDINGS_TOOL:
        return (name, Perspective.TRIAGE if name == triage.TRIAGE_TOOL["name"] else None)

    schema = tool.get("input_schema") or {}
    properties = schema.get("properties") or {}
    items = (properties.get("findings") or {}).get("items") or {}
    categories = ((items.get("properties") or {}).get("category") or {}).get("enum") or []
    wanted = set(categories)

    for fanout in (True, False):
        for target in TargetType:
            for spec in specs_for(target, fanout=fanout):
                if {c.value for c in spec.categories} == wanted:
                    return (name, spec.perspective)
    raise CassetteMiss(f"관점을 알 수 없는 판정 툴이다. categories={sorted(wanted)}")
