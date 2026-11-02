"""콘솔이 그릴 모양을 만든다. 판정 + (있으면) 추적을 받아 화면 모델 하나로 접는다.

여기가 데모의 판단 코드다. 두 가지를 소유한다.

1. **탈출 지점 판별.** 여덟 자리를 ``ContentVerdict`` 하나만으로 가른다. 관측 지점을 더하지
   않아도 "어디서 끝났는가"는 알아볼 수 있고, 그래서 추적이 없는 얕은 재생에서도 배지가 나온다.
2. **통과와 미검사의 분리.** 단계 줄마다 ``status``(돌았는지)와 ``outcome``(무엇으로
   판정됐는지)을 따로 싣고 라벨도 따로 준다. 한 값으로 합치면 콘솔이 "안 봤다"를 "문제
   없다"와 같은 색으로 그린다 -- 이 프로젝트가 반복해 고친 결함이 그것이다.

백엔드 게이트 술어(``backend_accepts``)도 여기 있다. 정본은 백엔드
``InspectionFacade.modelVerified()`` 이고, ``tests/conftest.py`` 가 같은 규칙의 사본을 들고
있다. ``tests/test_demo.py`` 가 두 사본이 같은 답을 내는지 단언해 둘이 갈라지는 것을 막는다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from app.inspection.trace import Stage, StageStatus, Trace
from app.inspection.triage import TRIAGE_SKIP_REASON
from app.schemas.inspection import (
    CallOutcome,
    ContentVerdict,
    Perspective,
    PerspectiveOutcome,
    PerspectiveResult,
    Verdict,
)

# 원화 환산. 하네스의 손익분기표와 같은 전제를 쓴다(harness/run.py 의 KRW_PER_USD).
# 두 숫자가 갈라지면 콘솔이 보여 주는 비용으로 손익분기를 이야기할 수 없다.
KRW_PER_USD = 1_400

# 백엔드가 "모델이 내용을 읽었다"로 세는 관점. 정본은 InspectionFacade.MODEL_PERSPECTIVES 다.
BACKEND_MODEL_PERSPECTIVES = (Perspective.ALL, Perspective.DISPLAY_ADS, Perspective.CONDUCT_TRADE)

# 모의 모드의 결과 패널 머리줄에 띄우는 경고.
SIMULATED_NOTE = (
    "이 판정은 모의 판정기의 키워드 표가 낸 것이다. 모델 판단이 아니고 비용과 토큰도 추정값이다. "
    "여기서 볼 것은 판정 품질이 아니라 파이프라인의 분기와 단계다."
)

# 단계 순서. 파이프라인이 도는 순서와 같고, 기록이 없는 단계는 "도달하지 않음"으로 채운다.
CANONICAL_STAGES: tuple[tuple[Stage, str], ...] = (
    (Stage.RULE, "N0 규칙"),
    (Stage.TRIAGE, "N0.5 선별"),
    (Stage.JUDGE, "N1 관점 판정"),
    (Stage.AGGREGATE, "N2 집계"),
    (Stage.CRITIC, "N3 비평"),
    (Stage.NOTICE, "N4 수정 문구"),
)

NOT_REACHED = "NOT_REACHED"

_STATUS_LABELS = {
    StageStatus.RAN.value: "돌았다",
    StageStatus.SKIPPED.value: "건너뜀",
    StageStatus.FAILED.value: "걸림",
    NOT_REACHED: "도달하지 않음",
}

_OUTCOME_LABELS = {
    PerspectiveOutcome.CLEAR.value: "통과",
    PerspectiveOutcome.UNCHECKED.value: "미검사",
    PerspectiveOutcome.VIOLATION.value: "위반",
}

_SKIP_REASON_LABELS = {
    TRIAGE_SKIP_REASON: "선별이 깨끗하다고 해서 보지 않았다",
    "PROMOTED": "선별이 더 보라고 넘겼다",
    "RULE_HIT": "규칙이 적중해 선별에 묻지 않았다",
    "TRIAGE_DISABLED": "선별이 꺼져 있다",
    "LLM_DISABLED": "모델 클라이언트가 없다",
    "ESCALATION_DISABLED": "비평이 꺼져 있다",
    "NOTICE_DISABLED": "수정 문구가 꺼져 있다",
    "EVIDENCE_UNVERIFIED": "인용이 본문에 없어 판정을 버렸다",
    CallOutcome.TIMEOUT.value: "호출이 시간 안에 끝나지 않았다",
    CallOutcome.RATE_LIMITED.value: "레이트 리밋에 걸렸다",
    CallOutcome.BUDGET_EXHAUSTED.value: "예산 상한에 걸렸다",
    CallOutcome.REFUSED.value: "모델이 응답을 거부했다",
    CallOutcome.SCHEMA_INVALID.value: "툴 응답이 스키마를 지키지 않았다",
    CallOutcome.TRUNCATED.value: "상한에 걸려 응답이 끊겼다",
    CallOutcome.ERROR.value: "호출이 실패했다",
}

_VERDICT_LABELS = {
    Verdict.PASS.value: "통과",
    Verdict.FLAG.value: "보류 제안",
    Verdict.FAILED.value: "판정 실패",
}


class ExitPoint(StrEnum):
    RULE_BLOCKED = "RULE_BLOCKED"
    TRIAGE_CLEARED = "TRIAGE_CLEARED"
    MODEL_CLEAR = "MODEL_CLEAR"
    # 쏜 관점 가운데 일부만 판정한 통과. MODEL_CLEAR 와 반드시 갈라야 한다 --
    # 둘을 합치면 "의료·효능을 보는 유일한 관점이 아무 판정도 못 했다"가 머리줄에서
    # "관점이 실제로 읽고 위반을 찾지 못했다"로 뒤집힌다.
    MODEL_PARTIAL = "MODEL_PARTIAL"
    CRITIC_OVERTURNED = "CRITIC_OVERTURNED"
    MODEL_FLAGGED = "MODEL_FLAGGED"
    # 표에 없는 경로 둘. 같은 방식으로 판정 하나에서 구분된다
    AGGREGATE_FAILED = "AGGREGATE_FAILED"
    LLM_DISABLED = "LLM_DISABLED"


_EXIT_LABELS: dict[str, tuple[str, str]] = {
    ExitPoint.RULE_BLOCKED.value: (
        "규칙 차단",
        "결정론 규칙이 확정했다. 모델을 부르지 않으므로 원장이 비어 있다.",
    ),
    ExitPoint.TRIAGE_CLEARED.value: (
        "선별 통과",
        "선별이 깨끗하다고 해서 관점 호출을 건너뛰었다. 건너뛴 관점은 통과가 아니라 미검사다.",
    ),
    ExitPoint.MODEL_CLEAR.value: (
        "모델 위반 없음",
        "쏜 관점이 전부 실제로 읽고 위반을 찾지 못했다. 이 통과는 미검사가 아니다.",
    ),
    # 설명문을 고정하지 않는다. 몇 건 중 몇 건을 읽었는지가 이 자리의 내용이고,
    # build() 가 관점 행에서 세어 채운다. 아래 문장은 셀 수 없을 때의 대비값이다.
    ExitPoint.MODEL_PARTIAL.value: (
        "부분 미검사 통과",
        "쏜 관점 가운데 일부만 판정했다. 나머지는 통과가 아니라 미검사다.",
    ),
    ExitPoint.CRITIC_OVERTURNED.value: (
        "비평 뒤집힘",
        "1차가 위반으로 봤지만 다른 모델이 오탐이라고 뒤집었다. 어드민 라우팅이 보는 신호다.",
    ),
    ExitPoint.MODEL_FLAGGED.value: (
        "확정 위반",
        "모델 위반이 비평을 통과해 확정됐다. 상품이면 셀러 안내가 함께 나간다.",
    ),
    ExitPoint.AGGREGATE_FAILED.value: (
        "판정 실패",
        "쏜 관점이 하나도 응답하지 못했다. 통과가 아니라 실패로 적는다.",
    ),
    ExitPoint.LLM_DISABLED.value: (
        "stub 모드",
        "모델 클라이언트가 없다. 검사한 관점이 0건이라 통과로 답하지 않는다.",
    ),
}

# 판정 메시지만으로는 되살릴 수 없는 것들. 얕은 재생에서 콘솔이 이 목록을 그대로 띄운다.
WITHOUT_TRACE_MISSING: tuple[str, ...] = (
    "선별이 적은 사유 (reason). 계약에는 관점 행의 결과만 남는다",
    "비평의 결정 (UPHOLD / INSUFFICIENT). 뒤집힘만 criticOverturned 로 남는다",
    "호출 실패 상세 (max_tokens 초과, 스키마 오류 본문). 열거값만 남는다",
    "비평과 수정문구의 단계 구분. 원장에는 둘 다 ALL 관점으로 적힌다",
    "인용 검증으로 떨어낸 구절과 모델이 적은 사유",
    "수정 문구 후보 배열. 계약에는 sellerNotice 한 줄만 남는다",
    "가린 본문. 위반 구간은 가린 본문 기준 오프셋이라 하이라이트를 찍을 수 없다",
    "규칙 적중의 사유와 차단 여부",
)


@dataclass(frozen=True, slots=True)
class FindingView:
    category: str
    quote: str
    reason: str | None
    suggestion: str | None
    verified: bool
    verified_label: str


@dataclass(frozen=True, slots=True)
class RewriteView:
    original: str
    suggestion: str


@dataclass(frozen=True, slots=True)
class RuleHitView:
    category: str
    quote: str
    reason: str
    blocking: bool
    blocking_label: str
    span_start: int | None
    span_end: int | None


@dataclass(frozen=True, slots=True)
class StageView:
    stage: str
    label: str
    status: str
    status_label: str
    outcome: str | None = None
    outcome_label: str | None = None
    skip_reason: str | None = None
    skip_reason_label: str | None = None
    decision: str | None = None
    reason: str | None = None
    detail: str | None = None
    note: str | None = None
    perspective: str | None = None
    model_id: str | None = None
    latency_ms: int | None = None
    cost_micro_usd: int | None = None
    evidence_dropped: int = 0
    dispatched: int | None = None
    checked: int | None = None
    findings: list[FindingView] = field(default_factory=list)
    rewrites: list[RewriteView] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ViolationView:
    category: str
    perspective: str
    quote: str | None
    span_start: int | None
    span_end: int | None
    suggestion: str | None
    from_rule: bool


@dataclass(frozen=True, slots=True)
class CallView:
    perspective: str
    model_id: str
    input_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    output_tokens: int
    cost_micro_usd: int
    cost_krw: float
    latency_ms: int
    outcome: str
    outcome_label: str
    cache_hit: bool


@dataclass(frozen=True, slots=True)
class LedgerView:
    calls: list[CallView]
    total_cost_micro_usd: int
    total_cost_krw: float
    total_latency_ms: int
    total_input_tokens: int
    total_cache_read_tokens: int
    total_output_tokens: int
    cache_hits: int
    triage_cost_micro_usd: int
    triage_cost_krw: float


@dataclass(frozen=True, slots=True)
class ConsoleView:
    mode: str
    source: str
    trace_available: bool
    missing_without_trace: list[str]
    inspection_id: int
    target: str
    target_ref: int
    verdict: str
    verdict_label: str
    exit_point: str
    exit_label: str
    exit_note: str
    backend_accepts: bool
    # 게이트가 이 판정에 적용되는가. 통과에만 적용된다
    backend_gate_applies: bool
    backend_note: str
    # 이 판정이 모델의 판단인가, 대역의 키워드 표가 낸 것인가.
    # 참이면 콘솔이 머리줄에 경고를 띄우고 백엔드 게이트 배지를 중립색으로 내린다.
    judgment_simulated: bool
    simulated_note: str
    critic_overturned: bool
    prompt_version: str
    seller_notice: str | None
    text: str | None
    redacted: str | None
    highlight_base: str
    rule_hits: list[RuleHitView]
    stages: list[StageView]
    violations: list[ViolationView]
    ledger: LedgerView

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_exit(verdict: ContentVerdict) -> ExitPoint:
    """판정 하나만 보고 어디서 끝났는지 가른다. 추적이 없어도 돈다.

    순서가 중요하다. 뒤집힘은 ``FLAG`` 를 ``PASS`` 로 바꾼 뒤의 상태라 통과 분기보다 먼저
    봐야 하고, 규칙 차단은 "원장이 비어 있다"로만 모델 보류와 갈라진다.
    """
    rows = {p.perspective: p for p in verdict.perspectives}
    model_rows = _model_rows(verdict.perspectives)

    if verdict.criticOverturned:
        return ExitPoint.CRITIC_OVERTURNED

    if verdict.verdict is Verdict.FAILED:
        if any(p.skipReason == "LLM_DISABLED" for p in model_rows):
            return ExitPoint.LLM_DISABLED
        return ExitPoint.AGGREGATE_FAILED

    if verdict.verdict is Verdict.FLAG:
        rule_row = rows.get(Perspective.RULE)
        if not verdict.calls and rule_row is not None and rule_row.outcome is PerspectiveOutcome.VIOLATION:
            return ExitPoint.RULE_BLOCKED
        return ExitPoint.MODEL_FLAGGED

    triage_row = rows.get(Perspective.TRIAGE)
    if triage_row is not None and triage_row.outcome is PerspectiveOutcome.CLEAR:
        # 선별이 통과시킨 것 자체가 탈출 지점이다. 그 모양이 백엔드 게이트를 넘는지는
        # 별 축이고 backend_accepts 가 답한다 -- 넘지 못하면 전건이 관리자 검토 큐로 간다.
        return ExitPoint.TRIAGE_CLEARED

    # 통과이지만 쏜 관점 가운데 판정하지 못한 것이 있으면 "다 읽었다"로 적지 않는다.
    #
    # 집계가 통과를 내는 조건은 "**한** 관점이라도 판정했고 위반이 없다"다. 그래서 의료·효능
    # 주장을 보는 유일한 관점이 인용 검증 실패나 레이트 리밋으로 죽어도 판정은 PASS 다.
    # N2 집계 줄에 수치가 있지만 발표장에서 읽히는 것은 배지와 그 밑 한 줄이다.
    if any(p.outcome is PerspectiveOutcome.UNCHECKED for p in model_rows):
        return ExitPoint.MODEL_PARTIAL
    return ExitPoint.MODEL_CLEAR


def backend_accepts(verdict: ContentVerdict) -> bool:
    """백엔드가 이 판정을 "모델이 내용을 읽었다"로 받아들이는가.

    정본은 ``InspectionFacade.modelVerified()`` 다. 셋이 동시에 참이어야 선별이 건너뛴
    통과가 관리자 손을 거치지 않는다 -- ``TRIAGE`` 행이 ``CLEAR``, 모델 관점 행이 비어 있지
    않음, 그 행 전부의 사유가 ``TRIAGE_CLEAR``.
    """
    model_rows = _model_rows(verdict.perspectives)
    if any(p.outcome is not PerspectiveOutcome.UNCHECKED for p in model_rows):
        return True
    triage_cleared = any(
        p.perspective is Perspective.TRIAGE and p.outcome is PerspectiveOutcome.CLEAR
        for p in verdict.perspectives
    )
    return triage_cleared and bool(model_rows) and all(p.skipReason == TRIAGE_SKIP_REASON for p in model_rows)


def build(
    verdict: ContentVerdict,
    *,
    mode: str,
    source: str,
    trace: Trace | None = None,
) -> ConsoleView:
    exit_point = classify_exit(verdict)
    exit_label, exit_note = _EXIT_LABELS[exit_point.value]
    if exit_point is ExitPoint.MODEL_PARTIAL:
        exit_note = _partial_note(verdict)
    accepted = backend_accepts(verdict)
    # 모의 모드의 판정은 키워드 표가 낸 것이고 모델의 판단이 아니다. 그 사실이 결과 패널
    # 머리줄에 없으면, 사람이 자기 문장을 넣어 본 결과가 "이 시스템은 이것을 이렇게
    # 판정한다"로 읽힌다. 비용·토큰이 추정값이라는 줄은 원장 밑에 따로 있다.
    simulated = mode == "mock"
    # 게이트가 가르는 것은 **통과를 사람 손 없이 반영할지**다. 보류와 판정 실패는 어차피
    # 사람이 보므로 이 축이 적용되지 않는다. 적용되지 않는 자리에 불통과 배지를 띄우면
    # 규칙이 차단한 건이 마치 잘못된 것처럼 보인다.
    gate_applies = verdict.verdict is Verdict.PASS
    stages = _stages_from_trace(trace) if trace is not None else _stages_from_verdict(verdict)

    return ConsoleView(
        mode=mode,
        source=source,
        trace_available=trace is not None,
        missing_without_trace=[] if trace is not None else list(WITHOUT_TRACE_MISSING),
        inspection_id=verdict.inspectionId,
        target=verdict.targetType.value,
        target_ref=verdict.targetRef,
        verdict=verdict.verdict.value,
        verdict_label=_VERDICT_LABELS[verdict.verdict.value],
        exit_point=exit_point.value,
        exit_label=exit_label,
        exit_note=exit_note,
        backend_accepts=accepted,
        backend_gate_applies=gate_applies,
        backend_note=_gate_note(gate_applies=gate_applies, accepted=accepted),
        judgment_simulated=simulated,
        simulated_note=SIMULATED_NOTE if simulated else "",
        critic_overturned=verdict.criticOverturned,
        prompt_version=verdict.promptVersion,
        seller_notice=verdict.sellerNotice,
        text=trace.text if trace is not None else None,
        redacted=trace.redacted if trace is not None else None,
        # 위반 구간은 **가린 본문** 기준 오프셋이다. 원문에 찍으면 엉뚱한 구간을 가리킨다.
        highlight_base="REDACTED" if trace is not None else "NONE",
        rule_hits=[
            RuleHitView(
                category=str(hit.category),
                quote=hit.quote,
                reason=hit.reason,
                blocking=hit.blocking,
                blocking_label="차단" if hit.blocking else "비차단(모델에 넘김)",
                span_start=hit.start,
                span_end=hit.end,
            )
            for hit in (trace.rule_hits if trace is not None else [])
        ],
        stages=stages,
        violations=[
            ViolationView(
                category=v.category.value,
                perspective=v.perspective.value,
                quote=v.quote,
                span_start=v.spanStart,
                span_end=v.spanEnd,
                suggestion=v.suggestion,
                from_rule=v.perspective is Perspective.RULE,
            )
            for v in verdict.violations
        ],
        ledger=_ledger(verdict),
    )


def _partial_note(verdict: ContentVerdict) -> str:
    """부분 미검사 통과의 설명문을 관점 행에서 세어 만든다.

    고정 문장을 쓰면 "몇 건이 미검사인지"가 빠지고, 그 수치가 이 배지의 내용 전부다.
    """
    model_rows = _model_rows(verdict.perspectives)
    checked = [p for p in model_rows if p.outcome is not PerspectiveOutcome.UNCHECKED]
    unchecked = [p for p in model_rows if p.outcome is PerspectiveOutcome.UNCHECKED]
    reasons = sorted(
        {_SKIP_REASON_LABELS.get(p.skipReason or "", p.skipReason or "사유 없음") for p in unchecked}
    )
    return (
        f"쏜 관점 {len(model_rows)}건 중 {len(checked)}건만 읽었다. 나머지 {len(unchecked)}건은 "
        f"통과가 아니라 미검사다 ({', '.join(reasons)})."
    )


def _gate_note(*, gate_applies: bool, accepted: bool) -> str:
    if not gate_applies:
        return "통과가 아니라 게이트가 가르는 자리가 아니다. 보류와 판정 실패는 어차피 사람이 본다."
    if accepted:
        return "백엔드가 모델 판정의 증거를 확인했다. 관리자 확인 없이 상태가 바뀐다."
    return (
        "백엔드는 이 통과를 증거 없는 통과로 본다. 전건이 관리자 검토 큐로 가고, "
        "선별은 호출만 아끼고 사람 손은 그만큼 더 쓰는 장치가 된다."
    )


def _model_rows(perspectives: list[PerspectiveResult]) -> list[PerspectiveResult]:
    return [p for p in perspectives if p.perspective in BACKEND_MODEL_PERSPECTIVES]


def _ledger(verdict: ContentVerdict) -> LedgerView:
    rows = [
        CallView(
            perspective=call.perspective.value,
            model_id=call.modelId,
            input_tokens=call.inputTokens,
            cache_read_input_tokens=call.cacheReadInputTokens,
            cache_creation_input_tokens=call.cacheCreationInputTokens,
            output_tokens=call.outputTokens,
            cost_micro_usd=call.costMicroUsd,
            cost_krw=_krw(call.costMicroUsd),
            latency_ms=call.latencyMs,
            outcome=call.outcome.value,
            outcome_label=_SKIP_REASON_LABELS.get(call.outcome.value, "성공")
            if call.outcome is not CallOutcome.SUCCESS
            else "성공",
            # 캐시 적중은 읽은 토큰이 있는지로 본다. 미달이면 에러 없이 조용히 0이 된다
            cache_hit=call.cacheReadInputTokens > 0,
        )
        for call in verdict.calls
    ]
    triage_cost = sum(c.cost_micro_usd for c in rows if c.perspective == Perspective.TRIAGE.value)
    total = sum(c.cost_micro_usd for c in rows)
    return LedgerView(
        calls=rows,
        total_cost_micro_usd=total,
        total_cost_krw=_krw(total),
        # 관점 호출이 직렬이라 합이 곧 그 건의 모델 대기 시간이다
        total_latency_ms=sum(c.latency_ms for c in rows),
        total_input_tokens=sum(c.input_tokens for c in rows),
        total_cache_read_tokens=sum(c.cache_read_input_tokens for c in rows),
        total_output_tokens=sum(c.output_tokens for c in rows),
        cache_hits=sum(1 for c in rows if c.cache_hit),
        triage_cost_micro_usd=triage_cost,
        triage_cost_krw=_krw(triage_cost),
    )


def _krw(micro_usd: int) -> float:
    return round(micro_usd * KRW_PER_USD / 1_000_000, 3)


def _stages_from_trace(trace: Trace) -> list[StageView]:
    """추적이 있으면 기록된 그대로 그린다. 기록이 없는 단계는 도달하지 않은 것이다."""
    views: list[StageView] = []
    for stage, label in CANONICAL_STAGES:
        recorded = [s for s in trace.stages if s.stage is stage]
        if not recorded:
            views.append(
                StageView(
                    stage=stage.value,
                    label=label,
                    status=NOT_REACHED,
                    status_label=_STATUS_LABELS[NOT_REACHED],
                    note="앞 단계에서 끝나 이 단계는 돌지 않았다",
                )
            )
            continue
        for record in recorded:
            views.append(
                StageView(
                    stage=record.stage.value,
                    label=record.label,
                    status=record.status.value,
                    status_label=_STATUS_LABELS[record.status.value],
                    outcome=record.outcome.value if record.outcome is not None else None,
                    outcome_label=(
                        _OUTCOME_LABELS[record.outcome.value] if record.outcome is not None else None
                    ),
                    skip_reason=record.skip_reason,
                    skip_reason_label=_skip_label(record.skip_reason),
                    decision=record.decision,
                    reason=record.reason,
                    detail=record.detail,
                    note=record.note,
                    perspective=record.perspective.value if record.perspective is not None else None,
                    model_id=record.model_id,
                    latency_ms=record.latency_ms,
                    cost_micro_usd=record.cost_micro_usd,
                    evidence_dropped=record.evidence_dropped,
                    dispatched=record.dispatched,
                    checked=record.checked,
                    findings=[
                        FindingView(
                            category=f.category,
                            quote=f.quote,
                            reason=f.reason,
                            suggestion=f.suggestion,
                            verified=f.verified,
                            verified_label=("인용 확인" if f.verified else "인용이 본문에 없어 폐기"),
                        )
                        for f in record.findings
                    ],
                    rewrites=[
                        RewriteView(original=r.original, suggestion=r.suggestion) for r in record.rewrites
                    ],
                )
            )
    return views


def _stages_from_verdict(verdict: ContentVerdict) -> list[StageView]:
    """추적 없이 판정만으로 단계를 되살린다. 얕은 재생이 쓰는 길이다.

    모양은 되살아나지만 이유는 상당 부분 남지 않는다. 무엇이 비는지는
    ``WITHOUT_TRACE_MISSING`` 이 그대로 들고 있고 콘솔이 그 목록을 띄운다.
    """
    rows = {p.perspective: p for p in verdict.perspectives}
    model_rows = _model_rows(verdict.perspectives)
    # **마지막 호출로 덮는다.** 재시도가 난 관점은 원장에 두 행이 있고, 단계 줄이 보여야
    # 하는 것은 판정에 쓴 호출이다(깊은 추적도 그 하나를 적는다). 첫 행으로 두면 재시도한
    # 관점의 단계 줄이 버려진 시도의 비용·지연을 띄운다.
    calls_by_perspective: dict[str, Any] = {}
    for call in verdict.calls:
        calls_by_perspective[call.perspective.value] = call

    views: list[StageView] = []

    rule_row = rows.get(Perspective.RULE)
    if rule_row is None:
        views.append(_not_reached(Stage.RULE, "N0 규칙", "판정에 규칙 관점 행이 없다"))
    else:
        views.append(
            StageView(
                stage=Stage.RULE.value,
                label="N0 규칙",
                status=StageStatus.RAN.value,
                status_label=_STATUS_LABELS[StageStatus.RAN.value],
                outcome=rule_row.outcome.value,
                outcome_label=_OUTCOME_LABELS[rule_row.outcome.value],
                perspective=Perspective.RULE.value,
                note="적중 상세와 사유는 판정에 남지 않는다",
            )
        )

    triage_row = rows.get(Perspective.TRIAGE)
    triage_call = calls_by_perspective.get(Perspective.TRIAGE.value)
    if triage_row is None:
        views.append(_not_reached(Stage.TRIAGE, "N0.5 선별", "선별 관점 행이 없다. 꺼져 있었거나 못 갔다"))
    else:
        cleared = triage_row.outcome is PerspectiveOutcome.CLEAR
        ran = cleared or triage_call is not None
        views.append(
            StageView(
                stage=Stage.TRIAGE.value,
                label="N0.5 선별",
                status=(StageStatus.RAN if ran else StageStatus.SKIPPED).value,
                status_label=_STATUS_LABELS[(StageStatus.RAN if ran else StageStatus.SKIPPED).value],
                outcome=triage_row.outcome.value,
                outcome_label=_OUTCOME_LABELS[triage_row.outcome.value],
                skip_reason=triage_row.skipReason,
                skip_reason_label=_skip_label(triage_row.skipReason),
                decision="CLEAR" if cleared else None,
                perspective=Perspective.TRIAGE.value,
                model_id=getattr(triage_call, "modelId", None),
                latency_ms=getattr(triage_call, "latencyMs", None),
                cost_micro_usd=getattr(triage_call, "costMicroUsd", None),
                note="선별이 적은 사유는 판정에 남지 않는다",
            )
        )

    if not model_rows:
        views.append(
            _not_reached(
                Stage.JUDGE,
                "N1 관점 판정",
                "모델 관점 행이 아예 없다. 백엔드는 이것을 증거 없는 통과로 본다",
            )
        )
    for row in model_rows:
        call = calls_by_perspective.get(row.perspective.value)
        if row.outcome is not PerspectiveOutcome.UNCHECKED:
            status = StageStatus.RAN
        elif row.skipReason in (TRIAGE_SKIP_REASON, "LLM_DISABLED"):
            status = StageStatus.SKIPPED
        else:
            status = StageStatus.FAILED
        views.append(
            StageView(
                stage=Stage.JUDGE.value,
                label=f"N1 {row.perspective.value}",
                status=status.value,
                status_label=_STATUS_LABELS[status.value],
                outcome=row.outcome.value,
                outcome_label=_OUTCOME_LABELS[row.outcome.value],
                skip_reason=row.skipReason,
                skip_reason_label=_skip_label(row.skipReason),
                perspective=row.perspective.value,
                model_id=getattr(call, "modelId", None),
                latency_ms=getattr(call, "latencyMs", None),
                cost_micro_usd=getattr(call, "costMicroUsd", None),
            )
        )

    checked = sum(1 for p in model_rows if p.outcome is not PerspectiveOutcome.UNCHECKED)
    views.append(
        StageView(
            stage=Stage.AGGREGATE.value,
            label="N2 집계",
            status=StageStatus.RAN.value,
            status_label=_STATUS_LABELS[StageStatus.RAN.value],
            decision=verdict.verdict.value,
            dispatched=len(model_rows),
            checked=checked,
            note="판정 결과에서 되살린 셈이다. 쏜 관점 수는 관점 행 수로 본다",
        )
    )

    if verdict.criticOverturned:
        views.append(
            StageView(
                stage=Stage.CRITIC.value,
                label="N3 비평",
                status=StageStatus.RAN.value,
                status_label=_STATUS_LABELS[StageStatus.RAN.value],
                decision="OVERTURN",
                note="뒤집힘만 계약에 남는다. 유지(UPHOLD)와 판단 불가(INSUFFICIENT)는 구분되지 않는다",
            )
        )
    elif verdict.verdict is Verdict.FLAG:
        views.append(
            StageView(
                stage=Stage.CRITIC.value,
                label="N3 비평",
                status=StageStatus.RAN.value,
                status_label=_STATUS_LABELS[StageStatus.RAN.value],
                decision=None,
                note="유지된 것은 알 수 있지만 UPHOLD 인지 INSUFFICIENT 인지는 판정에 남지 않는다",
            )
        )
    else:
        views.append(_not_reached(Stage.CRITIC, "N3 비평", "위반 판정이 아니라 비평을 부르지 않았다"))

    if verdict.sellerNotice:
        views.append(
            StageView(
                stage=Stage.NOTICE.value,
                label="N4 수정 문구",
                status=StageStatus.RAN.value,
                status_label=_STATUS_LABELS[StageStatus.RAN.value],
                reason=verdict.sellerNotice,
                note="후보 문구 배열은 판정에 실리지 않는다",
            )
        )
    else:
        views.append(_not_reached(Stage.NOTICE, "N4 수정 문구", "셀러 안내가 없다"))

    return views


def _not_reached(stage: Stage, label: str, note: str) -> StageView:
    return StageView(
        stage=stage.value,
        label=label,
        status=NOT_REACHED,
        status_label=_STATUS_LABELS[NOT_REACHED],
        note=note,
    )


def _skip_label(reason: str | None) -> str | None:
    if reason is None:
        return None
    return _SKIP_REASON_LABELS.get(reason, reason)
