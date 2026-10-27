"""요약 콘솔의 화면 모델. **``demo/view.py`` 와 별 모듈이다.**

그 파일(721줄)은 검수 전용이다 -- ``CANONICAL_STAGES`` 가 ``N0~N4`` 6단계로 고정이고
``ExitPoint`` 8종과 ``_SKIP_REASON_LABELS`` 가 검수 사유 전건이다. 요약은 그 화면
모델에 맞지 않아서 거기 끼우면 요약 단계가 전부 "도달하지 않음"으로 그려진다.

**환율을 다시 적지 않는다.** ``demo/view.KRW_PER_USD`` 를 import 한다 -- 하네스와 콘솔의
두 숫자가 갈라지면 콘솔이 보여 주는 비용으로 손익분기를 이야기할 수 없고,
``tests/test_demo.py`` 가 그 둘을 등호로 묶어 두었다.

이 모듈이 소유하는 판단이 둘이다.

1. **탈출 지점 판별.** 요약이 어디서 끝났는가를 결과 메시지 하나로 가른다.
2. **"센 숫자는 있는데 문장이 없다"를 "없다"와 다른 색으로 그린다.** 검수 콘솔이
   통과와 미검사를 가르는 것과 **같은 종류의 구분**이고, 이 기능에서 가장 틀리기 쉬운
   자리다 -- 네 가지가 화면에서 서로 달라야 한다.

       반복되는 단점이 있다
       분석한 n건에 단점 언급이 **없었다**
       언급은 있었으나 반복이라 부르기엔 **적었다**
       요약하기에 리뷰가 아직 **적다**
       반복이라고 셌는데 **문장이 없다** (서술 실패 또는 모델이 안 씀)
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from app.review_summary.trace import SummaryStage, SummaryStageStatus, SummaryTrace
from app.review_summary.trace import as_dict as stage_as_dict
from app.schemas.summary import (
    ConsAbsenceReason,
    ExtractStatus,
    ReviewSummaryMessage,
    ScopeSummary,
    SummaryState,
)
from demo.view import KRW_PER_USD

# 단계 순서. 서비스가 도는 순서와 같고 기록이 없는 단계는 "도달하지 않음"으로 채운다.
CANONICAL_SUMMARY_STAGES: tuple[tuple[SummaryStage, str], ...] = (
    (SummaryStage.GATE, "N0 입력 게이트"),
    (SummaryStage.EXTRACT, "N1 추출"),
    (SummaryStage.NARRATE, "N3 서술"),
)

NOT_REACHED = "NOT_REACHED"

_STATUS_LABELS = {
    SummaryStageStatus.RAN.value: "돌았다",
    SummaryStageStatus.SKIPPED.value: "건너뜀",
    SummaryStageStatus.FAILED.value: "걸림",
    NOT_REACHED: "도달하지 않음",
}

# 단점이 비어 있는 이유를 **화면 문장으로** 옮긴다. 네 갈래가 서로 다른 말이어야 한다.
CONS_ABSENCE_SENTENCES = {
    ConsAbsenceReason.NO_NEGATIVE_MENTIONS_FOUND.value: "분석한 후기에서 단점 언급이 없었습니다",
    ConsAbsenceReason.BELOW_MENTION_THRESHOLD.value: ("단점 언급이 있었으나 반복이라 부르기엔 적습니다"),
    ConsAbsenceReason.NOT_ENOUGH_REVIEWS.value: "요약을 만들기에 리뷰가 아직 적습니다",
}

# 떨어진 것들의 사람 말. 계수 이름만 보여 주면 발표장에서 읽히지 않는다.
DROPPED_LABELS = {
    "evidenceUnverified": "인용이 본문이나 집계표에 없어 버렸다",
    "polarityConflict": "1점 리뷰의 장점이라 셈에서 뺐다",
    "polarityConflictNoted": "5점 리뷰의 단점이라 세기만 하고 남겼다",
    "piiRedacted": "연락처·지시형이 섞여 그 항목만 버렸다",
    "injectionExcluded": "판정기를 겨냥한 문장이 있어 추출 입력에서 뺐다",
    "duplicateFolded": "같은 문구라 한 건만 남겼다",
    "maskHeavyQuote": "마스킹 기호가 절반 이상인 인용을 버렸다",
    "truncated": "본문이 상한을 넘어 잘렸다",
    "unknownAspectCode": "열거 밖의 항목 코드라 버렸다",
    "belowThresholdRejected": "집계표의 반복 항목이 아니라 버렸다",
    "narrationMissing": "반복이라 셌는데 모델이 한 줄을 쓰지 않았다",
}

# 셈이 0 이 아니면 **반드시 화면에 띄워야 하는** 것들. 조용히 지나가면 요약이 거짓이 된다.
LOUD_DROPPED = ("narrationMissing", "polarityConflict", "piiRedacted", "injectionExcluded")


class SummaryExit(StrEnum):
    """요약이 끝난 자리. 결과 메시지 하나로 갈린다."""

    # 분모 미달. **모델을 부르지 않았다.**
    NOT_ENOUGH_REVIEWS = "NOT_ENOUGH_REVIEWS"
    # 추출은 돌았지만 서술 호출이 실패했다. 센 숫자는 있고 문장이 없다.
    NARRATE_FAILED = "NARRATE_FAILED"
    # 서술이 돌았지만 반복 항목에 한 줄이 안 붙었다.
    NARRATION_MISSING = "NARRATION_MISSING"
    # 장단점이 실렸다.
    SUMMARIZED = "SUMMARIZED"
    # 모델 호출이 전부 실패했다.
    FAILED = "FAILED"
    # 모델 클라이언트가 없다(stub 모드).
    LLM_DISABLED = "LLM_DISABLED"


_EXIT_LABELS: dict[str, tuple[str, str]] = {
    SummaryExit.NOT_ENOUGH_REVIEWS.value: (
        "분모 미달",
        "산문 요약을 만들지 않았다. 모델을 0회 불렀고 0원이다. "
        "센 항목 수를 그대로 보여 준다 -- '장단점 없음'으로 적으면 거짓이다.",
    ),
    SummaryExit.NARRATE_FAILED.value: (
        "서술 실패",
        "집계는 돌았고 서술 호출이 죽었다. 빈 장단점이 아니라 **문장이 없는 것**이다. "
        "센 항목은 건수째로 결과에 남는다.",
    ),
    SummaryExit.NARRATION_MISSING.value: (
        "문장 누락",
        "코드가 반복이라고 센 항목에 모델이 한 줄을 붙이지 않았다. "
        "셈은 코드의 것이므로 건수는 남는다 -- 화면이 '없다'로 그리면 거짓이다.",
    ),
    SummaryExit.SUMMARIZED.value: (
        "요약 완료",
        "임계를 넘은 항목에만 한 줄이 붙었고, 인용은 전부 리뷰 본문에서 검증된 것이다.",
    ),
    SummaryExit.FAILED.value: (
        "판정 실패",
        "모델 호출이 전부 실패했다. 결과를 발행하고 오프셋은 커밋한다 -- "
        "유료 호출 경로에서 커밋을 빼면 지출이 무한 루프가 된다.",
    ),
    SummaryExit.LLM_DISABLED.value: (
        "모델 없음",
        "모델 클라이언트가 없다. 전건이 UNEXTRACTED 이고 '항목 없음'이 아니다.",
    ),
}


@dataclass(frozen=True, slots=True)
class StageRow:
    stage: str
    label: str
    status: str
    status_label: str
    note: str | None
    scope: str | None
    model_id: str | None
    latency_ms: int | None
    cost_micro_usd: int | None
    cost_krw: float
    input_tokens: int | None
    cache_read_input_tokens: int | None
    output_tokens: int | None
    outcome: str | None
    detail: str | None
    counts: dict[str, int]
    dropped: list[dict[str, str]]
    # 모델을 부른 단계인가. 거짓이면 0원 단계다.
    paid: bool


@dataclass(frozen=True, slots=True)
class AspectRow:
    aspect_code: str
    headline: str
    mention_count: int
    mention_share: float
    quotes: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class CountedRow:
    """산문이 없는 항목. **반복인데 문장이 없는 것**과 **임계 미달**을 라벨로 가른다."""

    aspect_code: str
    polarity: str
    mention_count: int
    labels: list[str]
    kind: str
    kind_label: str


@dataclass(frozen=True, slots=True)
class ScopeRow:
    scope: str
    segment_key: str | None
    basis_review_count: int
    unextracted_review_count: int
    pros: list[AspectRow]
    cons: list[AspectRow]
    cons_absence_reason: str | None
    cons_absence_sentence: str | None
    not_enough_reason: str | None
    narrate_failure: str | None
    counted: list[CountedRow]
    tally_fingerprint: str


@dataclass(frozen=True, slots=True)
class ReviewRow:
    review_id: int
    status: str
    status_label: str
    aspects: list[dict[str, Any]]
    pet_context: dict[str, Any] | None
    exclude_reason: str | None
    truncated: bool
    note: str | None


@dataclass(frozen=True, slots=True)
class LedgerRow:
    calls: list[dict[str, Any]]
    total_cost_micro_usd: int
    total_cost_krw: float
    total_latency_ms: int
    total_input_tokens: int
    total_cache_read_tokens: int
    total_output_tokens: int
    extract_cost_micro_usd: int
    narrate_cost_micro_usd: int
    # Haiku 기본에서는 **0 이 정상이다.** 그 사실을 숫자로 남긴다.
    cache_reads: int


@dataclass(frozen=True, slots=True)
class SummaryConsoleView:
    mode: str
    source: str
    trace_available: bool
    request_id: str
    product_id: int
    state: str
    exit_point: str
    exit_label: str
    exit_note: str
    extract_version: str
    narrate_version: str
    reviews_in: int
    reviews_extracted: int
    reviews_unextracted: int
    envelope_text: str | None
    tally_text: dict[str, str]
    stages: list[StageRow] = field(default_factory=list)
    scopes: list[ScopeRow] = field(default_factory=list)
    reviews: list[ReviewRow] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    ledger: LedgerRow | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_exit(message: ReviewSummaryMessage) -> SummaryExit:
    """결과 메시지 하나로 어디서 끝났는지 가른다.

    순서가 중요하다. 전부 실패를 먼저 보고, 그 다음에 "모델을 아예 안 불렀다"를 보고,
    마지막에 산문 유무를 본다 -- 거꾸로 보면 분모 미달이 "요약 완료"로 그려진다.
    """
    if message.state is SummaryState.FAILED:
        return SummaryExit.FAILED
    if not message.calls and any(
        row.extracted is ExtractStatus.UNEXTRACTED and "모델 클라이언트가 없다" in (row.note or "")
        for row in message.extracted
    ):
        return SummaryExit.LLM_DISABLED

    scopes = message.summaries
    if scopes and all(scope.notEnoughReason for scope in scopes):
        return SummaryExit.NOT_ENOUGH_REVIEWS
    if scopes and any(scope.narrateFailure for scope in scopes):
        return SummaryExit.NARRATE_FAILED
    if message.dropped.narrationMissing:
        return SummaryExit.NARRATION_MISSING
    return SummaryExit.SUMMARIZED


def build(
    message: ReviewSummaryMessage,
    *,
    mode: str,
    source: str,
    reviews_in: int,
    trace: SummaryTrace | None = None,
) -> SummaryConsoleView:
    exit_point = classify_exit(message)
    label, note = _EXIT_LABELS[exit_point.value]
    return SummaryConsoleView(
        mode=mode,
        source=source,
        trace_available=trace is not None,
        request_id=message.requestId,
        product_id=message.productId,
        state=message.state.value,
        exit_point=exit_point.value,
        exit_label=label,
        exit_note=note,
        extract_version=message.extractVersion,
        narrate_version=message.narrateVersion,
        reviews_in=reviews_in,
        reviews_extracted=sum(1 for row in message.extracted if row.extracted is ExtractStatus.EXTRACTED),
        reviews_unextracted=sum(1 for row in message.extracted if row.extracted is ExtractStatus.UNEXTRACTED),
        envelope_text=trace.envelope_text if trace else None,
        tally_text=dict(trace.tally_text) if trace else {},
        stages=_stages(trace),
        scopes=[_scope(scope) for scope in message.summaries],
        reviews=[_review(row) for row in message.extracted],
        dropped=_dropped(message),
        ledger=_ledger(message),
    )


def _stages(trace: SummaryTrace | None) -> list[StageRow]:
    """추적이 없으면 빈 목록이다. **거짓 단계를 그리지 않는다.**

    검수 콘솔은 판정만으로 단계를 되살리는 얕은 재생을 할 수 있다(관점 행이 계약에
    있기 때문이다). 요약 결과에는 단계가 없어서 되살릴 재료가 없고, 억지로 만들면
    화면이 추측을 사실처럼 그린다.
    """
    if trace is None:
        return []
    recorded = [stage_as_dict(record) for record in trace.stages]
    seen = {row["stage"] for row in recorded}
    rows = [_stage_row(row) for row in recorded]
    for stage, label in CANONICAL_SUMMARY_STAGES:
        if stage.value not in seen:
            rows.append(
                StageRow(
                    stage=stage.value,
                    label=label,
                    status=NOT_REACHED,
                    status_label=_STATUS_LABELS[NOT_REACHED],
                    note="이 단계에 도달하지 않았다",
                    scope=None,
                    model_id=None,
                    latency_ms=None,
                    cost_micro_usd=None,
                    cost_krw=0.0,
                    input_tokens=None,
                    cache_read_input_tokens=None,
                    output_tokens=None,
                    outcome=None,
                    detail=None,
                    counts={},
                    dropped=[],
                    paid=False,
                )
            )
    return rows


def _stage_row(row: dict[str, Any]) -> StageRow:
    cost = row.get("cost_micro_usd")
    return StageRow(
        stage=row["stage"],
        label=row["label"],
        status=row["status"],
        status_label=_STATUS_LABELS.get(row["status"], row["status"]),
        note=row.get("note"),
        scope=row.get("scope"),
        model_id=row.get("model_id"),
        latency_ms=row.get("latency_ms"),
        cost_micro_usd=cost,
        cost_krw=_krw(cost or 0),
        input_tokens=row.get("input_tokens"),
        cache_read_input_tokens=row.get("cache_read_input_tokens"),
        output_tokens=row.get("output_tokens"),
        outcome=row.get("outcome"),
        detail=row.get("detail"),
        counts=dict(row.get("counts") or {}),
        dropped=list(row.get("dropped") or []),
        paid=row.get("call_id") is not None,
    )


def _scope(scope: ScopeSummary) -> ScopeRow:
    reason = scope.consAbsenceReason.value if scope.consAbsenceReason else None
    counted = [
        CountedRow(
            aspect_code=row.aspectCode.value,
            polarity=row.polarity.value,
            mention_count=row.mentionCount,
            labels=list(row.labelSamples),
            # **두 종류를 라벨로 가른다.** 섞으면 반복된 장단점이 "미달"로 읽힌다.
            kind="UNNARRATED",
            kind_label="반복이라고 셌는데 문장이 없다",
        )
        for row in scope.unnarratedAspects
    ] + [
        CountedRow(
            aspect_code=row.aspectCode.value,
            polarity=row.polarity.value,
            mention_count=row.mentionCount,
            labels=list(row.labelSamples),
            kind="MINOR",
            kind_label="언급이 적어 반복이라 부르지 않았다",
        )
        for row in scope.minorMentions
    ]
    return ScopeRow(
        scope=scope.scope.value,
        segment_key=scope.segmentKey,
        basis_review_count=scope.basisReviewCount,
        unextracted_review_count=scope.unextractedReviewCount,
        pros=[_aspect(item) for item in scope.pros],
        cons=[_aspect(item) for item in scope.cons],
        cons_absence_reason=reason,
        cons_absence_sentence=CONS_ABSENCE_SENTENCES.get(reason or ""),
        not_enough_reason=scope.notEnoughReason,
        narrate_failure=scope.narrateFailure,
        counted=counted,
        tally_fingerprint=scope.tallyFingerprint,
    )


def _aspect(item: Any) -> AspectRow:
    return AspectRow(
        aspect_code=item.aspectCode.value,
        headline=item.headline,
        mention_count=item.mentionCount,
        mention_share=item.mentionShare,
        quotes=[{"reviewId": q.reviewId, "text": q.text} for q in item.quotes],
    )


def _review(row: Any) -> ReviewRow:
    return ReviewRow(
        review_id=row.reviewId,
        status=row.extracted.value,
        # **"추출됨"과 "분석되지 않음"을 라벨로 가른다.** 뭉개면 못 본 리뷰가
        # "항목이 없는 리뷰"로 읽힌다.
        status_label=("추출됨" if row.extracted is ExtractStatus.EXTRACTED else "분석되지 않음"),
        aspects=[
            {
                "aspectCode": aspect.aspectCode.value,
                "aspectLabel": aspect.aspectLabel,
                "polarity": aspect.polarity.value,
                "quote": aspect.quote,
            }
            for aspect in row.aspects
        ],
        pet_context=(
            {
                "source": row.petContext.source.value,
                "species": row.petContext.species,
                "breedLabel": row.petContext.breedLabel,
                "weightKg": row.petContext.weightKg,
                "ageYears": row.petContext.ageYears,
                "quote": row.petContext.quote,
            }
            if row.petContext
            else None
        ),
        exclude_reason=row.excludeReason.value if row.excludeReason else None,
        truncated=row.truncated,
        note=row.note,
    )


def _dropped(message: ReviewSummaryMessage) -> list[dict[str, Any]]:
    """0 이 아닌 계수만 사람 말과 함께 돌려준다. ``loud`` 는 반드시 띄워야 하는 것이다."""
    raw = message.dropped.model_dump()
    return [
        {
            "key": key,
            "count": value,
            "label": DROPPED_LABELS.get(key, key),
            "loud": key in LOUD_DROPPED,
        }
        for key, value in raw.items()
        if value
    ]


def _ledger(message: ReviewSummaryMessage) -> LedgerRow:
    calls = [
        {
            "callId": call.callId,
            "stage": call.stage,
            "modelId": call.modelId,
            "inputTokens": call.inputTokens,
            "cacheReadInputTokens": call.cacheReadInputTokens,
            "outputTokens": call.outputTokens,
            "costMicroUsd": call.costMicroUsd,
            "costKrw": _krw(call.costMicroUsd),
            "latencyMs": call.latencyMs,
            "outcome": call.outcome,
        }
        for call in message.calls
    ]
    total = sum(call.costMicroUsd for call in message.calls)
    return LedgerRow(
        calls=calls,
        total_cost_micro_usd=total,
        total_cost_krw=_krw(total),
        total_latency_ms=sum(call.latencyMs for call in message.calls),
        total_input_tokens=sum(call.inputTokens for call in message.calls),
        total_cache_read_tokens=sum(call.cacheReadInputTokens for call in message.calls),
        total_output_tokens=sum(call.outputTokens for call in message.calls),
        extract_cost_micro_usd=sum(call.costMicroUsd for call in message.calls if call.stage == "EXTRACT"),
        narrate_cost_micro_usd=sum(call.costMicroUsd for call in message.calls if call.stage == "NARRATE"),
        cache_reads=sum(1 for call in message.calls if call.cacheReadInputTokens),
    )


def _krw(micro_usd: int) -> float:
    """원화 환산. **환율을 다시 적지 않고 ``demo/view`` 의 것을 쓴다.**"""
    return round(micro_usd * KRW_PER_USD / 1_000_000, 2)
