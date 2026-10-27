"""초안 단계 추적. **판정 계약에도 초안 응답에도 실리지 않는 것만 모은다.**

``app/inspection/trace.py`` 의 ``Stage`` 와 ``app/review_summary/trace.py`` 의
``SummaryStage`` 를 **둘 다 재사용하지 않는다.** 두 열거는 각각 검수 6단계와 요약 5단계로
고정이고, 각 콘솔의 ``CANONICAL_*_STAGES`` 가 그 순서에 묶여 있다. 값을 더하면 저쪽 화면
모델이 초안 단계를 "도달하지 않음"으로 그린다.

여기서도 **돌았는지(``status``)와 무엇이 나왔는지(``outcome``)를 가른다.** 한 값으로 합치면
콘솔이 "안 불렀다"를 "실패했다"와 같은 색으로 그리고, 초안에서 그 둘은 비용이 0원과
유료만큼 다르다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.llm.client import LlmResult


class DraftStage(StrEnum):
    GATE = "GATE"
    IMAGE = "IMAGE"
    GENERATE = "GENERATE"
    OUTPUT_GUARD = "OUTPUT_GUARD"


class DraftStageStatus(StrEnum):
    RAN = "RAN"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class DraftDroppedItem:
    where: str
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class DraftStageRecord:
    stage: DraftStage
    label: str
    status: DraftStageStatus
    note: str | None = None
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
    dropped: list[DraftDroppedItem] = field(default_factory=list)


@dataclass(slots=True)
class DraftTrace:
    stages: list[DraftStageRecord] = field(default_factory=list)
    envelope_text: str | None = None
    # 이미지를 실제로 실었는가. 봉투 텍스트에는 나타나지 않아 따로 든다.
    image_attached: bool = False


class DraftTraceRecorder:
    """운영 경로는 ``None`` 을 받으므로 이 객체가 없으면 아무 일도 하지 않는다."""

    def __init__(self) -> None:
        self.trace = DraftTrace()

    def stage(
        self,
        stage: DraftStage,
        label: str,
        status: DraftStageStatus,
        *,
        note: str | None = None,
        result: LlmResult | None = None,
        counts: dict[str, int] | None = None,
        dropped: list[DraftDroppedItem] | None = None,
    ) -> None:
        self.trace.stages.append(
            DraftStageRecord(
                stage=stage,
                label=label,
                status=status,
                note=note,
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

    def envelope(self, text: str, *, image_attached: bool) -> None:
        self.trace.envelope_text = text
        self.trace.image_attached = image_attached


def as_dict(record: DraftStageRecord) -> dict[str, Any]:
    return {
        "stage": record.stage.value,
        "label": record.label,
        "status": record.status.value,
        "note": record.note,
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


__all__ = [
    "DraftDroppedItem",
    "DraftStage",
    "DraftStageRecord",
    "DraftStageStatus",
    "DraftTrace",
    "DraftTraceRecorder",
    "as_dict",
]
