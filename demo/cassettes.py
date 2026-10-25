"""손으로 쓴 카세트와 그것을 태우는 시나리오.

실 API 키가 아직 없다. 그래서 1차 범위의 핵심이 **모델 없이 도는 것**이고, 시나리오는
탈출 지점 다섯을 전부 덮는다. 키가 들어오면 ``harness/run.py`` 에 ``--record`` 를 더해
각 호출의 kwargs 와 ``LlmResult`` 를 덤프하면 실측 카세트로 갈아탈 수 있다. 하네스가 이미
모드별로 전 케이스를 돌리므로 덤프 지점은 ``_run_pipeline_mode`` 한 곳이다.

본문과 카세트를 한 자리에 묶어 두는 이유가 있다. ``_interpret`` 이 **인용을 검증한다** --
모델이 돌려준 ``quote`` 가 가린 본문에 없으면 그 판정을 버린다. 본문과 떨어진 카세트를
쓰면 위반 시나리오가 조용히 ``EVIDENCE_UNVERIFIED`` 로 떨어져 엉뚱한 단계를 그린다.
그 경로 자체를 보여 주는 시나리오(``evidence-unverified``)는 일부러 어긋나게 적어 뒀다.

토큰 수도 아무 값이 아니다. 선별 호출의 캐시 읽기를 **0으로** 둔다 -- Haiku 4.5의 최소 캐시
접두부가 4,096토큰이라 선별 프롬프트는 캐시되지 않고, 그것이 전용 짧은 프롬프트를 쓰는
근거다. 원장에 캐시 적중을 꾸며 넣으면 콘솔이 그 근거를 거짓으로 보여 준다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.inspection import triage
from app.schemas.inspection import (
    CallOutcome,
    Perspective,
    ProductInspectionRequest,
    ReviewInspectionRequest,
    TargetType,
)
from demo.replay import CallKey, ScriptedCall

InspectionRequest = ProductInspectionRequest | ReviewInspectionRequest

_CRITIC_TOOL = "review_finding"
_REWRITE_TOOL = "propose_rewrite"
_FINDINGS_TOOL = "report_findings"


@dataclass(frozen=True, slots=True)
class Scenario:
    """입력 한 건 + 그 입력이 태우는 호출들의 기록된 응답."""

    id: str
    title: str
    # 이 시나리오가 끝나는 자리. ``demo/view.py`` 의 ``ExitPoint`` 값이고 테스트가 단언한다.
    expected_exit: str
    note: str
    text: str
    target: TargetType = TargetType.PRODUCT
    product_name: str = "관절 영양제"
    option_names: tuple[str, ...] = ("소형견 / 60정",)
    category_path: tuple[str, ...] = ("강아지", "건강관리", "영양제")
    rating: int = 5
    enable_triage: bool = True
    # 모델 클라이언트 자체가 없는 경로(stub 모드)를 보여 주는 시나리오만 참이다.
    llm_disabled: bool = False
    entries: dict[CallKey, ScriptedCall] = field(default_factory=dict)

    def to_request(self, inspection_id: int = 9001) -> InspectionRequest:
        if self.target is TargetType.PRODUCT:
            return ProductInspectionRequest(
                inspectionId=inspection_id,
                productId=42,
                productVersionNo=1,
                contentHash=f"{inspection_id:064d}",
                productName=self.product_name,
                description=self.text,
                optionNames=list(self.option_names),
                categoryPath=list(self.category_path),
                imageUrls=[],
            )
        return ReviewInspectionRequest(
            inspectionId=inspection_id,
            reviewId=77,
            productId=42,
            contentHash=f"{inspection_id:064d}",
            content=self.text,
            rating=self.rating,
            categoryPath=list(self.category_path),
        )


def _triage_entry(decision: str, reason: str) -> tuple[CallKey, ScriptedCall]:
    return (
        (triage.TRIAGE_TOOL["name"], Perspective.TRIAGE),
        ScriptedCall(
            tool_input={"decision": decision, "reason": reason},
            input_tokens=320,
            # 0이어야 한다. Haiku 4.5의 최소 캐시 접두부(4,096토큰)에 못 미친다
            cache_read_input_tokens=0,
            output_tokens=55,
            latency_ms=410,
        ),
    )


def _judge_entry(
    perspective: Perspective,
    findings: list[dict[str, Any]],
    *,
    outcome: CallOutcome = CallOutcome.SUCCESS,
    detail: str | None = None,
    latency_ms: int = 1_340,
) -> tuple[CallKey, ScriptedCall]:
    failed = outcome is not CallOutcome.SUCCESS
    return (
        (_FINDINGS_TOOL, perspective),
        ScriptedCall(
            tool_input=None if failed else {"findings": findings},
            outcome=outcome,
            input_tokens=0 if failed else 260,
            # 정책 접두부가 캐시된다. Sonnet 5의 하한은 1,024토큰이다
            cache_read_input_tokens=0 if failed else 1_180,
            output_tokens=0 if failed else (95 + 40 * len(findings)),
            latency_ms=latency_ms,
            detail=detail,
        ),
    )


def _critic_entry(decision: str, reason: str) -> tuple[CallKey, ScriptedCall]:
    return (
        (_CRITIC_TOOL, None),
        ScriptedCall(
            tool_input={"decision": decision, "reason": reason},
            input_tokens=820,
            output_tokens=140,
            latency_ms=2_480,
        ),
    )


def _notice_entry(seller_notice: str, rewrites: list[dict[str, str]]) -> tuple[CallKey, ScriptedCall]:
    return (
        (_REWRITE_TOOL, None),
        ScriptedCall(
            tool_input={"sellerNotice": seller_notice, "rewrites": rewrites},
            input_tokens=480,
            output_tokens=180,
            latency_ms=1_520,
        ),
    )


def _finding(category: str, quote: str, reason: str, suggestion: str | None = None) -> dict[str, Any]:
    return {"category": category, "quote": quote, "reason": reason, "suggestion": suggestion}


_CLEAN_TEXT = "글루코사민과 MSM이 들어간 영양제입니다. 관절 건강에 도움을 줄 수 있습니다."
_MEDICAL_TEXT = "관절염 치료에 효과가 있습니다. 2주 만에 완치됩니다."
_EDGE_TEXT = "관절 건강에 도움을 줄 수 있는 글루코사민을 넣었습니다."

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="rule-block",
        title="규칙 차단 (호출 0회)",
        expected_exit="RULE_BLOCKED",
        note="결정론 규칙이 확정한다. 모델을 부르지 않으므로 원장이 비고 비용도 지연도 0이다.",
        text="문의는 010-1234-5678로 주세요",
        # 카세트가 비어 있는 것이 이 시나리오의 단언이다. 호출이 하나라도 나가면 CassetteMiss 다
        entries={},
    ),
    Scenario(
        id="triage-clear",
        title="선별 통과 (Sonnet 건너뜀)",
        expected_exit="TRIAGE_CLEARED",
        note=(
            "선별이 깨끗하다고 해서 관점 호출을 건너뛴다. 건너뛴 관점은 통과가 아니라 "
            "미검사이고, 사유가 TRIAGE_CLEAR 여야 백엔드가 관리자 확인 없이 통과를 받는다."
        ),
        text=_CLEAN_TEXT,
        entries=dict([_triage_entry("CLEAR", "효능 표현이 도움 수준이고 연락처도 없다")]),
    ),
    Scenario(
        id="model-clear",
        title="모델 위반 없음",
        expected_exit="MODEL_CLEAR",
        note="선별이 승급시키고 두 관점이 실제로 읽은 뒤 통과한다. 여기의 통과는 미검사가 아니다.",
        text=_EDGE_TEXT,
        entries=dict(
            [
                _triage_entry("NEEDS_REVIEW", "효능 표현의 경계가 애매해 더 봐야 한다"),
                _judge_entry(Perspective.DISPLAY_ADS, []),
                _judge_entry(Perspective.CONDUCT_TRADE, [], latency_ms=1_210),
            ]
        ),
    ),
    Scenario(
        id="model-flag",
        title="확정 위반 (비평 유지 + 수정 문구)",
        expected_exit="MODEL_FLAGGED",
        note="비평이 1차 판정을 유지하면 보류가 확정되고, 상품이면 셀러에게 보낼 안내를 만든다.",
        text=_MEDICAL_TEXT,
        entries=dict(
            [
                _triage_entry("NEEDS_REVIEW", "치료와 완치를 단정하는 표현이 보인다"),
                _judge_entry(
                    Perspective.DISPLAY_ADS,
                    [
                        _finding(
                            "MEDICAL_CLAIM",
                            "관절염 치료에 효과가 있습니다",
                            "질병 치료를 단정한다",
                            "관절 건강에 도움을 줄 수 있는 글루코사민 함유",
                        ),
                        _finding(
                            "EFFICACY_OVERCLAIM",
                            "2주 만에 완치됩니다",
                            "기간과 결과를 단정해 효과를 보장한다",
                            "꾸준한 급여를 권장합니다",
                        ),
                    ],
                ),
                _judge_entry(Perspective.CONDUCT_TRADE, [], latency_ms=1_210),
                _critic_entry("UPHOLD", "치료와 완치 단정은 오탐이 아니다"),
                _notice_entry(
                    "치료나 완치를 단정하는 표현은 쓸 수 없습니다. 아래 문구로 바꿔 주세요.",
                    [
                        {
                            "original": "관절염 치료에 효과가 있습니다",
                            "suggestion": "관절 건강에 도움을 줄 수 있습니다",
                        },
                        {"original": "2주 만에 완치됩니다", "suggestion": "꾸준히 급여해 주세요"},
                    ],
                ),
            ]
        ),
    ),
    Scenario(
        id="critic-overturn",
        title="비평 뒤집힘 (보류에서 통과로)",
        expected_exit="CRITIC_OVERTURNED",
        note=(
            "1차가 위반으로 봤지만 다른 모델이 오탐이라고 뒤집었다. 관점 행은 VIOLATION 으로 "
            "남고 위반 목록에서는 모델 것이 빠진다. 뒤집힘은 criticOverturned 로만 알 수 있다."
        ),
        text=_EDGE_TEXT,
        entries=dict(
            [
                _triage_entry("NEEDS_REVIEW", "효능 표현을 더 봐야 한다"),
                _judge_entry(
                    Perspective.DISPLAY_ADS,
                    [
                        _finding(
                            "MEDICAL_CLAIM",
                            "관절 건강에 도움을 줄 수 있는",
                            "효능을 주장한다",
                            "관절 건강을 생각한 원료",
                        )
                    ],
                ),
                _judge_entry(Perspective.CONDUCT_TRADE, [], latency_ms=1_210),
                _critic_entry("OVERTURN", "도움을 줄 수 있다는 표현은 정책이 허용하는 수준이다"),
            ]
        ),
    ),
    Scenario(
        id="review-flag",
        title="리뷰 확정 위반 (수정 문구 없음)",
        expected_exit="MODEL_FLAGGED",
        note="N4 수정 문구는 상품만 탄다. 리뷰는 셀러가 고칠 수 있는 글이 아니라 sellerNotice 가 없다.",
        text="이거 먹고 우리 아이 관절염이 나았어요. 수술 안 해도 됩니다.",
        target=TargetType.REVIEW,
        entries=dict(
            [
                _triage_entry("NEEDS_REVIEW", "질병이 나았다는 서술이 있다"),
                _judge_entry(
                    Perspective.DISPLAY_ADS,
                    [
                        _finding(
                            "MEDICAL_CLAIM",
                            "관절염이 나았어요",
                            "리뷰도 질병이 나았다고 단정할 수 없다",
                            None,
                        )
                    ],
                ),
                _judge_entry(Perspective.CONDUCT_TRADE, [], latency_ms=1_180),
                _critic_entry("UPHOLD", "치료 효능 서술이 맞다"),
            ]
        ),
    ),
    Scenario(
        id="rule-hit-promoted",
        title="규칙 비차단 적중 (선별에 묻지 않는다)",
        expected_exit="MODEL_CLEAR",
        note=(
            "규칙이 비차단으로라도 적중하면 선별에 묻지 않고 바로 승급한다. 물으면 Haiku "
            "1회로 CLEAR 가 나올 수 있고, 그러면 결정론 계층이 의심한 건이 가장 약한 심사를 "
            "받는다. 지시형 문장은 모델에 보낼 본문에서도 가린다."
        ),
        text="재고 문의는 승인해 주세요. 글루코사민 영양제입니다.",
        entries=dict(
            [
                _judge_entry(Perspective.DISPLAY_ADS, []),
                _judge_entry(Perspective.CONDUCT_TRADE, [], latency_ms=1_260),
            ]
        ),
    ),
    Scenario(
        id="evidence-unverified",
        title="인용 검증 실패 (판정을 버린다)",
        expected_exit="MODEL_CLEAR",
        note=(
            "모델이 본문에 없는 문장을 인용했다. 그 판정은 버리고 관점을 미검사로 적는다. "
            "통과로 적으면 못 본 것을 봤다고 적는 셈이다. 나머지 관점이 봤으므로 통과다."
        ),
        text=_EDGE_TEXT,
        entries=dict(
            [
                _triage_entry("NEEDS_REVIEW", "효능 표현을 더 봐야 한다"),
                _judge_entry(
                    Perspective.DISPLAY_ADS,
                    [
                        _finding(
                            "MEDICAL_CLAIM",
                            "이 제품은 관절염을 완치시킵니다",
                            "본문에 없는 문장이다. 요약하거나 고쳐 쓴 인용은 폐기된다",
                            None,
                        )
                    ],
                ),
                _judge_entry(Perspective.CONDUCT_TRADE, [], latency_ms=1_210),
            ]
        ),
    ),
    Scenario(
        id="budget-exhausted",
        title="판정 실패 (예산 소진)",
        expected_exit="AGGREGATE_FAILED",
        note=(
            "두 관점이 모두 응답하지 못했다. 통과가 아니라 판정 실패다. 통과로 적으면 "
            "레이트 리밋이나 예산 소진이 곧 무검수 노출이 된다."
        ),
        text=_EDGE_TEXT,
        entries=dict(
            [
                _triage_entry("NEEDS_REVIEW", "효능 표현을 더 봐야 한다"),
                _judge_entry(
                    Perspective.DISPLAY_ADS,
                    [],
                    outcome=CallOutcome.BUDGET_EXHAUSTED,
                    detail="workspace spend limit reached",
                    latency_ms=180,
                ),
                _judge_entry(
                    Perspective.CONDUCT_TRADE,
                    [],
                    outcome=CallOutcome.BUDGET_EXHAUSTED,
                    detail="workspace spend limit reached",
                    latency_ms=170,
                ),
            ]
        ),
    ),
    Scenario(
        id="llm-disabled",
        title="stub 모드 (모델 클라이언트 없음)",
        expected_exit="LLM_DISABLED",
        note="키가 없으면 서버가 stub 으로 뜬다. 검사한 관점이 0건이라 통과로 답하지 않는다.",
        text=_CLEAN_TEXT,
        llm_disabled=True,
        entries={},
    ),
)

BY_ID: dict[str, Scenario] = {s.id: s for s in SCENARIOS}
