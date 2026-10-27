"""요약 단계 추적. **판정 계약에 실리지 않는 것만 모은다.**

``app/inspection/trace.py`` 와 같은 이유로 존재하고 같은 규약을 지킨다 -- 데모 콘솔이
보여 줘야 하는 것(단계 라벨, 왜 그 길로 갔는지, 떨어진 항목과 그 사유)이 결과 메시지에
없기 때문이다. 운영 경로는 추적기를 주지 않으므로 동작이 바뀌지 않는다.

``app/inspection/trace.py`` 의 ``Stage`` 를 재사용하지 않는다. 그 열거는 검수 6단계
(``RULE``/``TRIAGE``/``JUDGE``/``AGGREGATE``/``CRITIC``/``NOTICE``)로 고정이고 콘솔의
``CANONICAL_STAGES`` 가 그 순서에 묶여 있다. 값을 더하면 검수 화면 모델이 요약 단계를
"도달하지 않음"으로 그린다.

여기서도 **통과와 미검사를 가른다.** ``status`` 는 돌았는지, ``outcome`` 은 무엇이
나왔는지다. 한 값으로 합치면 콘솔이 "안 봤다"를 "없다"와 같은 색으로 그린다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.llm.client import LlmResult


class SummaryStage(StrEnum):
    GATE = "GATE"
    EXTRACT = "EXTRACT"
    TALLY = "TALLY"
    NARRATE = "NARRATE"
    OUTPUT_GATE = "OUTPUT_GATE"


class SummaryStageStatus(StrEnum):
    RAN = "RAN"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class DroppedItem:
    """떨어진 항목 하나. **무엇이 왜 떨어졌는지**가 콘솔의 핵심 화면이다."""

    where: str
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class SummaryStageRecord:
    stage: SummaryStage
    label: str
    status: SummaryStageStatus
    note: str | None = None
    scope: str | None = None
    # 모델 호출이 있던 단계만 채운다. 없으면 0원 단계라는 뜻이다.
    call_id: str | None = None
    model_id: str | None = None
    latency_ms: int | None = None
    cost_micro_usd: int | None = None
    input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    output_tokens: int | None = None
    outcome: str | None = None
    detail: str | None = None
    counts: dict[str, int] = field(default_factory=dict)
    dropped: list[DroppedItem] = field(default_factory=list)


@dataclass(slots=True)
class SummaryTrace:
    stages: list[SummaryStageRecord] = field(default_factory=list)
    envelope_text: str | None = None
    tally_text: dict[str, str] = field(default_factory=dict)


class SummaryTraceRecorder:
    """추적기. 운영 경로는 ``None`` 을 받으므로 이 객체가 없으면 아무 일도 하지 않는다."""

    def __init__(self) -> None:
        self.trace = SummaryTrace()

    def stage(
        self,
        stage: SummaryStage,
        label: str,
        status: SummaryStageStatus,
        *,
        note: str | None = None,
        scope: str | None = None,
        result: LlmResult | None = None,
        counts: dict[str, int] | None = None,
        dropped: list[DroppedItem] | None = None,
    ) -> None:
        self.trace.stages.append(
            SummaryStageRecord(
                stage=stage,
                label=label,
                status=status,
                note=note,
                scope=scope,
                call_id=result.call_id if result else None,
                model_id=result.model_id if result else None,
                latency_ms=result.latency_ms if result else None,
                cost_micro_usd=result.cost_micro_usd if result else None,
                input_tokens=result.input_tokens if result else None,
                cache_read_input_tokens=result.cache_read_input_tokens if result else None,
                output_tokens=result.output_tokens if result else None,
                outcome=result.outcome.value if result else None,
                detail=result.detail if result else None,
                counts=dict(counts or {}),
                dropped=list(dropped or []),
            )
        )

    def envelope(self, text: str) -> None:
        self.trace.envelope_text = text

    def tally_block(self, scope: str, text: str) -> None:
        self.trace.tally_text[scope] = text


def as_dict(record: SummaryStageRecord) -> dict[str, Any]:
    return {
        "stage": record.stage.value,
        "label": record.label,
        "status": record.status.value,
        "note": record.note,
        "scope": record.scope,
        "call_id": record.call_id,
        "model_id": record.model_id,
        "latency_ms": record.latency_ms,
        "cost_micro_usd": record.cost_micro_usd,
        "input_tokens": record.input_tokens,
        "cache_read_input_tokens": record.cache_read_input_tokens,
        "output_tokens": record.output_tokens,
        "outcome": record.outcome,
        "detail": record.detail,
        "counts": dict(record.counts),
        "dropped": [{"where": d.where, "reason": d.reason, "detail": d.detail} for d in record.dropped],
    }
