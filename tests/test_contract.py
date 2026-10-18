"""백엔드가 소유한 표본으로 양방향 계약을 고정한다.

표본은 백엔드의 ``messagingObjectMapper`` 빈이 뽑은 바이트다. 다른 매퍼로 만든 표본을
쓰면 두 레포가 서로 다른 것에 대해 녹색이 된다.
"""

from __future__ import annotations

import json
from typing import Any

from app.schemas.envelope import Envelope
from app.schemas.inspection import (
    SCHEMA_VERSION,
    ContentVerdict,
    Perspective,
    PerspectiveOutcome,
    ProductInspectionRequest,
    ReviewInspectionRequest,
    TargetType,
    Verdict,
)


def test_상품_요청_표본을_빠짐없이_읽는다(contract: Any) -> None:
    raw = contract("product-inspection-requested.json")

    request = ProductInspectionRequest.model_validate(raw)

    assert request.inspectionId == 1001
    assert request.productId == 42
    assert request.productVersionNo == 3
    assert request.contentHash == "a" * 64
    assert request.productName == "관절 영양제"
    assert request.description == "글루코사민 함유"
    assert request.optionNames == ["소형견 / 60정"]
    assert request.categoryPath == ["강아지", "건강관리", "영양제"]
    assert request.imageUrls == ["https://media.aechak.co.kr/products/01J.png"]
    assert request.schemaVersion == SCHEMA_VERSION
    assert request.target_type is TargetType.PRODUCT
    assert request.target_ref == 42


def test_리뷰_요청_표본을_빠짐없이_읽는다(contract: Any) -> None:
    raw = contract("review-inspection-requested.json")

    request = ReviewInspectionRequest.model_validate(raw)

    assert request.inspectionId == 2002
    assert request.reviewId == 77
    assert request.productId == 42
    assert request.content == "우리 아이가 잘 먹어요"
    assert request.rating == 5
    assert request.target_type is TargetType.REVIEW
    assert request.target_ref == 77


def test_우리가_만드는_판정이_백엔드_표본과_같은_모양이다(contract: Any) -> None:
    raw = contract("content-verdict.json")

    verdict = ContentVerdict.model_validate(raw)

    assert verdict.verdict is Verdict.FLAG
    assert verdict.schemaVersion == SCHEMA_VERSION
    outcomes = {p.perspective: p.outcome for p in verdict.perspectives}
    assert outcomes[Perspective.DISPLAY_ADS] is PerspectiveOutcome.VIOLATION
    assert outcomes[Perspective.CONDUCT_TRADE] is PerspectiveOutcome.UNCHECKED

    # 우리가 다시 직렬화한 것이 표본의 키 집합과 같아야 한다.
    # 필드를 더하거나 이름을 바꾸면 백엔드가 조용히 버린다(모르는 필드를 무시하도록 설정됨).
    emitted = json.loads(verdict.model_dump_json())
    assert set(emitted) == set(raw), "키 집합이 다르면 백엔드가 에러 없이 일부를 떨어뜨린다"
    assert set(emitted["violations"][0]) == set(raw["violations"][0])
    assert set(emitted["calls"][0]) == set(raw["calls"][0])
    assert set(emitted["perspectives"][0]) == set(raw["perspectives"][0])


def test_입력_토큰을_셋으로_나눠_받는다(contract: Any) -> None:
    verdict = ContentVerdict.model_validate(contract("content-verdict.json"))

    call = verdict.calls[0]

    assert call.inputTokens + call.cacheReadInputTokens + call.cacheCreationInputTokens == 1200
    assert call.requestId != call.callId, (
        "논리 요청과 실제 호출을 같은 키로 적으면 재시도 비용이 한 행에 묻힌다"
    )


def test_엔벨로프_페이로드는_객체가_아니라_문자열이다(contract: Any) -> None:
    raw = contract("verdict-envelope.json")

    envelope = Envelope.model_validate(raw)

    assert isinstance(envelope.payload, str)
    assert envelope.eventType == "ContentVerdictMessage"
    assert envelope.producer == "aechak-ai"
    inner = ContentVerdict.model_validate(envelope.payload_obj())
    assert inner.inspectionId == 1001


def test_우리가_감싼_엔벨로프도_페이로드가_문자열이다(contract: Any) -> None:
    verdict = ContentVerdict.model_validate(contract("content-verdict.json"))

    envelope = Envelope.wrap(
        event_id=verdict.event_id(),
        event_type="ContentVerdictMessage",
        aggregate_type="verdict",
        ordering_key="42",
        payload_obj=verdict,
        trace_id="trace-1",
    )

    emitted = json.loads(envelope.to_json())
    assert isinstance(emitted["payload"], str), (
        "객체로 넣으면 백엔드의 Envelope.payload(String) 역직렬화가 깨진다"
    )
    assert set(emitted) == set(contract("verdict-envelope.json")), "엔벨로프 필드는 여덟 개다"
    assert emitted["traceId"] == "trace-1", "상관키는 새로 만들지 않고 승계한다"
