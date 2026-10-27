"""초안 콘솔의 화면 모델. **``demo/view.py`` 와 ``demo/summary_view.py`` 와 별 모듈이다.**

``demo/view.py``(721줄)는 검수 전용이다 -- ``CANONICAL_STAGES`` 가 ``N0~N4`` 6단계로 고정이고
``ExitPoint`` 8종이 검수 사유 전건이다. ``summary_view.py`` 는 요약 3단계다. 초안은 둘 다에
맞지 않아서 거기 끼우면 초안 단계가 전부 "도달하지 않음"으로 그려진다.

**환율을 다시 적지 않는다.** ``demo/view.KRW_PER_USD`` 를 import 한다 -- 하네스와 콘솔의 두
숫자가 갈라지면 콘솔이 보여 주는 비용으로 손익분기를 이야기할 수 없고, 테스트가 그 둘을
등호로 묶어 두었다.

## 이 모듈이 소유하는 판단

1. **탈출 지점 판별.** 초안이 어디서 끝났는가를 응답 하나로 가른다. 0원으로 끝난 세 길
   (상품명 차단 / 품목 미식별 / 기능 꺼짐)을 서로 다른 라벨로 그린다.
2. **"왜 비었는가"를 네 종류로 그린다.** 이 기능에서 가장 틀리기 쉬운 자리다.

       설계상 채우지 않는다          (가격·재고·이미지 키)
       품목을 몰라 시도하지 않았다
       가드가 걸러 비웠다            (규칙 / 금지성분 / 효능 / 옵션 / 근거 / 길이)
       **호출이 죽어서 못 봤다**     <- "제안할 것이 없었다"로 그리면 거짓이다

3. **두 방식을 나란히 놓고 차이를 숫자로 낸다.** 비용·지연·초안이 한 화면에 있어야
   사람이 입력 방식을 고를 수 있다. 그 비교가 이 탭의 존재 이유다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from app.schemas.draft import (
    DRAFT_FIELDS,
    DraftState,
    FieldStatus,
    ImageUsability,
    SellerDraftResponse,
    SkipReason,
)
from app.seller_draft.trace import DraftTrace
from app.seller_draft.trace import as_dict as stage_as_dict
from demo.view import KRW_PER_USD

# 단계 순서. 서비스가 도는 순서와 같고 기록이 없는 단계는 "도달하지 않음"으로 채운다.
CANONICAL_DRAFT_STAGES: tuple[tuple[str, str], ...] = (
    ("GATE", "N0 어휘 게이트"),
    ("IMAGE", "N0 이미지 헤더"),
    ("GENERATE", "N1 초안 생성"),
    ("OUTPUT_GUARD", "N2 생성후 가드"),
)

NOT_REACHED = "NOT_REACHED"

_STATUS_LABELS = {
    "RAN": "돌았다",
    "SKIPPED": "건너뜀",
    "FAILED": "걸림",
    NOT_REACHED: "도달하지 않음",
}

# 필드가 빈 이유를 **화면 문장으로** 옮긴다. 네 종류가 서로 다른 말이어야 한다.
FIELD_STATUS_SENTENCES: dict[str, str] = {
    FieldStatus.FILLED.value: "채웠습니다",
    FieldStatus.KEPT_LOCKED.value: "직접 고친 값을 그대로 두었습니다",
    FieldStatus.EMPTY_NOT_ATTEMPTED.value: "자동완성이 채우지 않는 항목입니다 (가격·재고·이미지)",
    FieldStatus.EMPTY_ITEM_UNIDENTIFIED.value: "어떤 품목인지 확인되지 않아 채우지 않았습니다",
    FieldStatus.EMPTY_NAME_RULE_BLOCKED.value: "상품명이 정책에 걸려 초안을 만들지 않았습니다",
    FieldStatus.EMPTY_GUARD_RULE.value: "연락처·외부 링크가 섞여 비웠습니다",
    FieldStatus.EMPTY_GUARD_BANNED.value: "반려동물에게 위험한 성분이 적혀 비웠습니다",
    FieldStatus.EMPTY_GUARD_CLAIM.value: "효능을 단정하는 표현이 섞여 비웠습니다",
    FieldStatus.EMPTY_GUARD_OPTION_RULE.value: "제안한 옵션이 등록 규칙을 위반해 비웠습니다",
    FieldStatus.EMPTY_GUARD_BASIS_MISMATCH.value: "사진을 쓰지 못했는데 사진을 근거로 적어 비웠습니다",
    FieldStatus.EMPTY_GUARD_LENGTH.value: "길이 상한을 넘겨 비웠습니다",
    FieldStatus.EMPTY_CATEGORY_CONFLICT.value: "분류 판단이 갈려 직접 골라 주셔야 합니다",
    FieldStatus.EMPTY_MODEL_DECLINED.value: "확신이 없어 비워 두었습니다",
    FieldStatus.EMPTY_CALL_FAILED.value: "생성이 실패해 **확인하지 못했습니다** (다시 시도)",
    FieldStatus.EMPTY_SCHEMA_INVALID.value: "응답에 그 항목이 없었습니다",
}

# 셈이 0 이 아니면 **반드시 화면에 띄워야 하는** 사유들. 조용히 지나가면 초안이 거짓이 된다.
LOUD_STATUSES = (
    FieldStatus.EMPTY_CALL_FAILED.value,
    FieldStatus.EMPTY_GUARD_BASIS_MISMATCH.value,
    FieldStatus.EMPTY_CATEGORY_CONFLICT.value,
)

GUARD_KIND_LABELS = {
    "RULE": "0단 규칙(연락처·외부링크·봉투탈출)",
    "BANNED": "금지 성분 사전",
    "CLAIM": "효능 단정 사전 (0단이 못 잡는 축)",
    "OPTION_RULE": "옵션 등록 불변식",
    "BASIS_MISMATCH": "사진 근거 정합성",
    "LENGTH": "길이 상한",
    "CATEGORY_CONFLICT": "게이트와 모델의 분류 불일치",
    "UNKNOWN_LEAF": "열거 밖 리프",
}


class DraftExit(StrEnum):
    """초안이 끝난 자리. 응답 하나로 갈린다."""

    # 0원으로 끝난 셋. **서로 다른 라벨이어야 한다.**
    NAME_RULE_BLOCKED = "NAME_RULE_BLOCKED"
    ITEM_UNIDENTIFIED = "ITEM_UNIDENTIFIED"
    DRAFT_DISABLED = "DRAFT_DISABLED"
    LLM_DISABLED = "LLM_DISABLED"
    # 사진이 거부돼 **호출 전에** 끝났다. 호출 실패와 섞지 않는다.
    IMAGE_REJECTED = "IMAGE_REJECTED"
    # 모델을 불렀다.
    CALL_FAILED = "CALL_FAILED"
    CATEGORY_CONFLICT = "CATEGORY_CONFLICT"
    GUARD_EMPTIED = "GUARD_EMPTIED"
    FILLED = "FILLED"
    REUSED = "REUSED"


_EXIT_LABELS: dict[str, tuple[str, str]] = {
    DraftExit.NAME_RULE_BLOCKED.value: (
        "상품명 차단",
        "상품명이 0단 규칙에 걸려 **모델을 0회 불렀다. 0원이다.** 검수에 걸릴 이름으로 초안을 "
        "만들어 주는 것은 셀러를 함정에 빠뜨리는 것이다.",
    ),
    DraftExit.ITEM_UNIDENTIFIED.value: (
        "품목 미식별",
        "상품명도 카테고리도 품목을 집지 못해 **모델을 0회 불렀다. 0원이다.** 일반론을 쓰지 않고 "
        "비우고, 셀러에게 무엇을 더하면 되는지 적어 돌려준다.",
    ),
    DraftExit.DRAFT_DISABLED.value: (
        "기능 꺼짐",
        "enable_draft 가 꺼져 있다. 구현하고 측정하고 기본은 끈다 -- enable_triage 선례다. "
        "켜는 조건은 하네스 숫자가 정한다.",
    ),
    DraftExit.LLM_DISABLED.value: (
        "모델 없음",
        "모델 클라이언트가 없다(stub 모드). 빈 필드가 '제안할 것이 없었다'가 아니라 "
        "**확인하지 못했다**로 적힌다.",
    ),
    DraftExit.IMAGE_REJECTED.value: (
        "사진 거부",
        "토큰 예산을 넘겼거나 받지 않는 형식이다. **모델을 부르기 전에 멈췄고 0원이다.** "
        "AI 서버는 리사이즈하지 않으므로(의존성 금지) 셀러센터가 줄여야 하는 긴 변을 응답에 "
        "적어 돌려준다 -- 조용히 이미지만 빼면 셀러는 자기 사진이 쓰인 줄 안다.",
    ),
    DraftExit.CALL_FAILED.value: (
        "호출 실패",
        "모델 호출이 죽었다. 전 필드가 EMPTY_CALL_FAILED 다 -- **'제안할 것이 없었다'와 뜻이 "
        "정반대이고, 화면도 '다시 시도' 와 '직접 작성'을 다르게 보여 줘야 한다.**",
    ),
    DraftExit.CATEGORY_CONFLICT.value: (
        "분류 불일치",
        "0원 어휘 게이트와 모델이 **다른 리프를 지목했다.** 어느 쪽이 맞는지 모르므로 조용히 "
        "고르지 않고 비우고 셀러가 고르게 한다.",
    ),
    DraftExit.GUARD_EMPTIED.value: (
        "가드가 비움",
        "생성은 됐지만 가드가 채울 수 있는 필드를 전부 떨어뜨렸다. **재생성 0회** -- 비우고 "
        "사유를 적는 것이 두 번째 시도에서 겨우 통과한 문구보다 정직하다.",
    ),
    DraftExit.FILLED.value: (
        "초안 완료",
        "가드를 통과한 필드만 채웠다. 가격·재고·이미지 키는 설계상 채우지 않고, 그 사실이 "
        "'못 했다'와 다른 사유로 적힌다.",
    ),
    DraftExit.REUSED.value: (
        "재사용",
        "같은 입력의 초안을 프로세스 로컬 캐시에서 돌려줬다. **이 요청은 모델을 부르지 않았고 "
        "원장의 숫자는 처음 호출의 것이다** -- 재사용을 새 호출로 세면 예산이 부풀려진다.",
    ),
}

# 채울 수 있는 필드. 가격·재고·이미지 키는 설계상 빼므로 "가드가 전부 비웠다" 판정의
# 분모가 아니다. 넣으면 모든 응답이 GUARD_EMPTIED 로 그려진다.
FILLABLE_FIELDS = ("categoryId", "description", "optionGroups", "optionCombinations")


@dataclass(frozen=True, slots=True)
class StageRow:
    stage: str
    label: str
    status: str
    status_label: str
    note: str | None
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
    paid: bool


@dataclass(frozen=True, slots=True)
class FieldRow:
    name: str
    status: str
    sentence: str
    basis: str | None
    value: Any
    loud: bool


@dataclass(frozen=True, slots=True)
class LedgerRow:
    calls: list[dict[str, Any]]
    total_cost_micro_usd: int
    total_cost_krw: float
    total_latency_ms: int
    total_input_tokens: int
    total_cache_read_tokens: int
    total_output_tokens: int
    # Haiku 기본에서는 **0 이 정상이다.** 그 사실을 숫자로 남긴다.
    cache_reads: int


@dataclass(frozen=True, slots=True)
class DraftVariantView:
    """한 방식의 결과. (가)와 (나)가 같은 모양으로 나와야 나란히 놓을 수 있다."""

    label: str
    mode: str
    image_mode: str
    exit_point: str
    exit_label: str
    exit_note: str
    state: str
    draft_version: str
    model_called: bool
    reused: bool
    skip_reason: str | None
    item_grade: str
    item_resolved_by: str
    matched_terms: list[str]
    candidate_leaf_ids: list[int]
    image_usability: str
    image_detail: str | None
    image_tokens: int
    envelope_text: str | None
    image_attached: bool
    seller_notice: str | None
    description: str | None
    option_groups: list[dict[str, Any]]
    option_combinations: int
    category_id: int | None
    fields: list[FieldRow] = field(default_factory=list)
    guard_drops: list[dict[str, str]] = field(default_factory=list)
    stages: list[StageRow] = field(default_factory=list)
    ledger: LedgerRow | None = None


@dataclass(frozen=True, slots=True)
class CompareRow:
    """두 방식의 차이 한 줄. **사람이 이 표를 읽고 입력 방식을 고른다.**"""

    metric: str
    text_only: str
    with_image: str
    delta: str
    note: str


@dataclass(frozen=True, slots=True)
class DraftCompareView:
    mode: str
    source: str
    product_name: str
    category_given: int | None
    variants: list[DraftVariantView]
    compare: list[CompareRow]
    note: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_exit(response: SellerDraftResponse) -> DraftExit:
    """응답 하나로 어디서 끝났는지 가른다.

    순서가 중요하다. 재사용과 0원 분기를 먼저 보고, 그 다음에 호출 실패를 보고, 마지막에
    채움 여부를 본다 -- 거꾸로 보면 0원으로 끝난 건이 "초안 완료"로 그려진다.
    """
    if response.reusedFromCache:
        return DraftExit.REUSED
    if response.image.rejected:
        # **호출 전에 끝난 것**이다. 운영 경로에서는 거부 시 예외가 나므로 응답에 이 플래그가
        # 서는 길은 거부를 화면 모델로 옮길 때뿐이다.
        return DraftExit.IMAGE_REJECTED
    if response.skipReason is SkipReason.NAME_RULE_BLOCKED:
        return DraftExit.NAME_RULE_BLOCKED
    if response.skipReason is SkipReason.ITEM_UNIDENTIFIED:
        return DraftExit.ITEM_UNIDENTIFIED
    if response.skipReason is SkipReason.DRAFT_DISABLED:
        return DraftExit.DRAFT_DISABLED
    if response.skipReason is SkipReason.LLM_UNAVAILABLE:
        return DraftExit.LLM_DISABLED
    if response.state is DraftState.FAILED:
        return DraftExit.CALL_FAILED
    if response.fieldStatus.get("categoryId") is FieldStatus.EMPTY_CATEGORY_CONFLICT:
        return DraftExit.CATEGORY_CONFLICT
    if any(status is FieldStatus.FILLED for status in response.fieldStatus.values()):
        return DraftExit.FILLED
    return DraftExit.GUARD_EMPTIED


def build_variant(
    response: SellerDraftResponse,
    *,
    label: str,
    mode: str,
    trace: DraftTrace | None = None,
) -> DraftVariantView:
    exit_point = classify_exit(response)
    exit_label, exit_note = _EXIT_LABELS[exit_point.value]
    return DraftVariantView(
        label=label,
        mode=mode,
        image_mode=response.imageMode.value,
        exit_point=exit_point.value,
        exit_label=exit_label,
        exit_note=exit_note,
        state=response.state.value,
        draft_version=response.draftVersion,
        model_called=response.modelCalled,
        reused=response.reusedFromCache,
        skip_reason=response.skipReason.value if response.skipReason else None,
        item_grade=response.itemSignal.grade.value,
        item_resolved_by=response.itemSignal.resolvedBy.value,
        matched_terms=list(response.itemSignal.matchedTerms),
        candidate_leaf_ids=list(response.itemSignal.candidateLeafIds),
        image_usability=response.imageUsability.value,
        image_detail=response.image.detail,
        image_tokens=response.image.estimatedTokens,
        envelope_text=trace.envelope_text if trace else None,
        image_attached=bool(trace and trace.image_attached),
        seller_notice=response.sellerNotice,
        description=response.draft.description,
        option_groups=[
            {"name": group.name, "values": list(group.values)}
            for group in (response.draft.optionGroups or [])
        ],
        option_combinations=len(response.draft.optionCombinations or []),
        category_id=response.draft.categoryId,
        fields=_fields(response),
        guard_drops=[
            {
                "field": drop.field,
                "kind": drop.kind.value,
                "kind_label": GUARD_KIND_LABELS.get(drop.kind.value, drop.kind.value),
                "detail": drop.detail,
            }
            for drop in response.guardDrops
        ],
        stages=_stages(trace),
        ledger=_ledger(response),
    )


def _fields(response: SellerDraftResponse) -> list[FieldRow]:
    """**모든 초안 필드를 한 줄씩 그린다.** 사유 없이 빈 필드가 화면에 남지 않는다."""
    dumped = response.draft.model_dump(mode="json")
    rows: list[FieldRow] = []
    for name in DRAFT_FIELDS:
        status = response.fieldStatus.get(name, FieldStatus.EMPTY_SCHEMA_INVALID)
        basis = response.basis.get(name)
        rows.append(
            FieldRow(
                name=name,
                status=status.value,
                sentence=FIELD_STATUS_SENTENCES.get(status.value, status.value),
                basis=basis.value if basis else None,
                value=dumped.get(name),
                loud=status.value in LOUD_STATUSES,
            )
        )
    return rows


def _stages(trace: DraftTrace | None) -> list[StageRow]:
    """추적이 없으면 **거짓 단계를 그리지 않는다.**

    초안 응답에는 단계가 남지 않아서 되살릴 재료가 없고, 억지로 만들면 화면이 추측을
    사실처럼 그린다.
    """
    if trace is None:
        return []
    recorded = {stage_as_dict(record)["stage"]: stage_as_dict(record) for record in trace.stages}
    rows: list[StageRow] = []
    for stage, label in CANONICAL_DRAFT_STAGES:
        row = recorded.get(stage)
        if row is None:
            rows.append(
                StageRow(
                    stage=stage,
                    label=label,
                    status=NOT_REACHED,
                    status_label=_STATUS_LABELS[NOT_REACHED],
                    note=None,
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
            continue
        cost = row.get("cost_micro_usd")
        rows.append(
            StageRow(
                stage=stage,
                label=row.get("label") or label,
                status=row["status"],
                status_label=_STATUS_LABELS.get(row["status"], row["status"]),
                note=row.get("note"),
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
                paid=row.get("model_id") is not None,
            )
        )
    return rows


def _ledger(response: SellerDraftResponse) -> LedgerRow:
    calls = [call.model_dump(mode="json") | {"cost_krw": _krw(call.costMicroUsd)} for call in response.usage]
    return LedgerRow(
        calls=calls,
        total_cost_micro_usd=response.total_cost_micro_usd(),
        total_cost_krw=_krw(response.total_cost_micro_usd()),
        total_latency_ms=sum(call.latencyMs for call in response.usage),
        total_input_tokens=sum(call.inputTokens for call in response.usage),
        total_cache_read_tokens=sum(call.cacheReadInputTokens for call in response.usage),
        total_output_tokens=sum(call.outputTokens for call in response.usage),
        cache_reads=sum(1 for call in response.usage if call.cacheReadInputTokens > 0),
    )


def compare_rows(text: DraftVariantView, image: DraftVariantView | None) -> list[CompareRow]:
    """두 방식의 차이를 표로 만든다. **비용·지연·초안이 한 화면에 있어야 고를 수 있다.**

    이미지 변형이 없으면 그 사실을 적은 한 줄만 돌려준다 -- 빈 칸을 0 으로 그리면
    "이미지가 비용을 0 더한다"로 읽힌다.
    """
    if image is None:
        return [
            CompareRow(
                metric="이미지 변형",
                text_only="있음",
                with_image="없음",
                delta="-",
                note="이 시나리오에는 사진 카세트가 없다. 비교할 숫자가 없는 것을 0 으로 그리지 않는다",
            )
        ]

    text_cost = text.ledger.total_cost_micro_usd if text.ledger else 0
    image_cost = image.ledger.total_cost_micro_usd if image.ledger else 0
    text_latency = text.ledger.total_latency_ms if text.ledger else 0
    image_latency = image.ledger.total_latency_ms if image.ledger else 0
    text_filled = sum(1 for row in text.fields if row.status == FieldStatus.FILLED.value)
    image_filled = sum(1 for row in image.fields if row.status == FieldStatus.FILLED.value)

    ratio = (image_cost - text_cost) / text_cost if text_cost else 0.0
    return [
        CompareRow(
            metric="건당 비용",
            text_only=f"{text_cost} uUSD ({_krw(text_cost):.2f}원)",
            with_image=f"{image_cost} uUSD ({_krw(image_cost):.2f}원)",
            delta=f"{image_cost - text_cost:+d} uUSD ({ratio:+.0%})",
            note="이미지는 messages 에 들어가 캐시 지점 뒤다 -- 접두부 캐시를 깨지 않지만 할인도 못 받는다",
        ),
        CompareRow(
            metric="지연",
            text_only=f"{text_latency} ms",
            with_image=f"{image_latency} ms",
            delta=f"{image_latency - text_latency:+d} ms",
            note="지연의 지배 항은 이미지가 아니라 출력 토큰이다. 이미지는 prefill 만 늘린다",
        ),
        CompareRow(
            metric="고른 소분류",
            text_only=str(text.category_id) if text.category_id else "비움",
            with_image=str(image.category_id) if image.category_id else "비움",
            delta=("같다" if text.category_id == image.category_id else "**다르다**"),
            note="층별로 봐야 뜻이 생긴다. 한 건의 일치는 결론이 아니다 -- 하네스가 50 리프에서 센다",
        ),
        CompareRow(
            metric="채운 필드 수",
            text_only=str(text_filled),
            with_image=str(image_filled),
            delta=f"{image_filled - text_filled:+d}",
            note="빈 필드마다 사유가 다르다. 사유를 안 보고 개수만 보면 '못 봤다'가 '없다'로 읽힌다",
        ),
        CompareRow(
            metric="사진 자기보고",
            text_only=text.image_usability,
            with_image=image.image_usability,
            delta=(
                "**MISMATCHES_NAME — 사진이 상품명과 다르다고 모델이 적었다**"
                if image.image_usability == ImageUsability.MISMATCHES_NAME.value
                else image.image_usability
            ),
            note="오사진에서 이것을 못 내고 초안을 써 버리면 이미지는 정확도와 무관하게 켜지 않는다",
        ),
        CompareRow(
            metric="가드 탈락",
            text_only=str(len(text.guard_drops)),
            with_image=str(len(image.guard_drops)),
            delta=f"{len(image.guard_drops) - len(text.guard_drops):+d}",
            note="겹별로 가른다 -- claims 항목이 효능 사전을 새로 만들어야 했는지의 증거다",
        ),
    ]


def build_compare(
    *,
    product_name: str,
    category_given: int | None,
    mode: str,
    source: str,
    text: DraftVariantView,
    image: DraftVariantView | None,
) -> DraftCompareView:
    return DraftCompareView(
        mode=mode,
        source=source,
        product_name=product_name,
        category_given=category_given,
        variants=[v for v in (text, image) if v is not None],
        compare=compare_rows(text, image),
        note=(
            "두 방식은 별 코드 경로가 아니다 -- 같은 프롬프트·툴·모델·접두부를 쓰고 "
            "**이미지 블록 하나의 유무만 다르다.** 변수가 하나라서 이 비교가 공정하다. "
            "한 건의 차이는 결론이 아니고, 층별 한계 이득은 harness/run_draft.py 가 잰다."
        ),
    )


def _krw(micro_usd: int) -> float:
    return micro_usd * KRW_PER_USD / 1_000_000


__all__ = [
    "CANONICAL_DRAFT_STAGES",
    "FIELD_STATUS_SENTENCES",
    "FILLABLE_FIELDS",
    "GUARD_KIND_LABELS",
    "LOUD_STATUSES",
    "CompareRow",
    "DraftCompareView",
    "DraftExit",
    "DraftVariantView",
    "FieldRow",
    "LedgerRow",
    "StageRow",
    "build_compare",
    "build_variant",
    "classify_exit",
    "compare_rows",
]
