"""백엔드가 소유한 표본으로 양방향 계약을 고정한다.

표본은 백엔드의 ``messagingObjectMapper`` 빈이 뽑은 바이트다. 다른 매퍼로 만든 표본을
쓰면 두 레포가 서로 다른 것에 대해 녹색이 된다.
"""

from __future__ import annotations

import json
from typing import Any

from conftest import backend_model_verified

from app.inspection import triage
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
from app.schemas.summary import (
    SUMMARY_AGGREGATE_TYPE,
    SUMMARY_SCHEMA_VERSION,
    AspectCode,
    ConsAbsenceReason,
    ExtractStatus,
    Polarity,
    ReviewSummaryMessage,
    ReviewSummaryRequest,
    SummaryScope,
    SummaryState,
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


def test_뒤집힘_신호를_백엔드_표본에서_읽고_같은_키로_되돌린다(contract: Any) -> None:
    raw = contract("content-verdict-triage-promoted.json")

    verdict = ContentVerdict.model_validate(raw)

    assert verdict.criticOverturned is True, (
        "이 필드가 없으면 백엔드의 어드민 '비평 뒤집음' 경로가 처음부터 발화하지 않는다"
    )
    emitted = json.loads(verdict.model_dump_json())
    assert set(emitted) == set(raw), "키 집합이 다르면 백엔드가 에러 없이 일부를 떨어뜨린다"


def test_뒤집히지_않은_표본은_신호가_꺼져_있다(contract: Any) -> None:
    for name in ("content-verdict.json", "content-verdict-triage.json"):
        verdict = ContentVerdict.model_validate(contract(name))

        assert verdict.criticOverturned is False, f"{name}"


def test_승급_표본은_선별_관점을_미검사로_남긴다(contract: Any) -> None:
    """승급 경로의 TRIAGE 행이 백엔드 계약의 일부다.

    승급은 판정을 내린 것이 아니라 더 보라고 넘긴 것이라 CLEAR로 적으면 거짓이 된다.
    """
    verdict = ContentVerdict.model_validate(contract("content-verdict-triage-promoted.json"))

    rows = {p.perspective: p for p in verdict.perspectives}

    assert rows[Perspective.TRIAGE].outcome is PerspectiveOutcome.UNCHECKED
    assert rows[Perspective.TRIAGE].skipReason == "PROMOTED"


def test_건너뛴_표본은_선별_관점이_통과이고_게이트를_넘는다(contract: Any) -> None:
    """선별이 건너뛴 통과가 사람 손을 거치지 않고 받아들여지는 모양을 고정한다.

    건너뛴 관점에 행을 남기는 것은 호출을 쏜 증거가 아니라 왜 안 쐈는지를 적는 자리이고,
    백엔드 게이트가 통과를 받아들이는 조건이 바로 그 행의 존재다. 그 행이 빠지면 백엔드는
    선별이 건너뛴 건을 전건 관리자 검토 큐로 보내고, 그러면 선별은 Sonnet 호출만 아끼고
    사람 손은 그만큼 더 쓰는 장치가 된다.
    """
    verdict = ContentVerdict.model_validate(contract("content-verdict-triage.json"))

    rows = {p.perspective: p for p in verdict.perspectives}

    assert rows[Perspective.TRIAGE].outcome is PerspectiveOutcome.CLEAR
    for perspective in (Perspective.DISPLAY_ADS, Perspective.CONDUCT_TRADE):
        assert rows[perspective].outcome is PerspectiveOutcome.UNCHECKED
        assert rows[perspective].skipReason == triage.TRIAGE_SKIP_REASON
    assert backend_model_verified(verdict)


# --- 리뷰 요약 ----------------------------------------------------------------------

#: 백엔드가 보내지만 요약이 **한 번도 읽지 않는** payload 키.
#:
#: 앞 넷은 봉투 자리다 -- 러너는 ``aggregateType``·``eventId``·``occurredAt``·``orderingKey``
#: 를 payload 가 아니라 ``Envelope`` 에서 읽는다. 뒤 둘은 요청 계약에서 **일부러 뺀 것**이고
#: 왜 안 받는지는 ``ReviewSummaryRequest`` 독스트링에 있다(분모는 ``basisReviewCount`` 하나다).
_SUMMARY_REQUEST_IGNORED = frozenset(
    {
        "aggregateType",
        "eventId",
        "occurredAt",
        "orderingKey",
        "totalVisibleReviews",
        "ratingBuckets",
    }
)

#: ``newReviews`` 항목에서 같은 이유로 읽지 않는 키(``ReviewInput`` 독스트링).
_SUMMARY_REVIEW_IGNORED = frozenset({"hasPhoto", "createdAt"})


def _key_paths(node: Any, path: str = "") -> dict[str, set[str]]:
    """객체 트리의 경로마다 키 집합을 모은다. 리스트 항목은 같은 경로의 합집합으로 본다."""
    found: dict[str, set[str]] = {}
    if isinstance(node, dict):
        found[path] = set(node)
        for key, value in node.items():
            for sub, keys in _key_paths(value, f"{path}.{key}" if path else key).items():
                found.setdefault(sub, set()).update(keys)
    elif isinstance(node, list):
        for item in node:
            for sub, keys in _key_paths(item, f"{path}[]").items():
                found.setdefault(sub, set()).update(keys)
    return found


def test_요약_요청_표본에서_우리가_요구하는_것이_빠짐없이_온다(contract: Any) -> None:
    """**이 표본이 백엔드가 실제로 보내는 바이트다.**

    요청 계약은 한 방향으로만 안전하다. 우리가 읽지 않는 키를 백엔드가 더 보내는 것은 모든
    요약 모델이 ``extra="ignore"`` 라서 조용히 버려지지만, 우리가 요구하는 키를 백엔드가
    빼면 ``ValidationError`` 이고 러너는 그것을 경고 후 커밋해 **영구히 버린다.** 그래서
    빠진 키는 여기서 잡아야 한다 -- 런타임에는 아무 소리도 나지 않는다.
    """
    raw = contract("review-summary-requested.json")

    request = ReviewSummaryRequest.model_validate(raw)

    assert request.requestId == "0b6c1f0e-3f7e-4d8a-9a51-6f2f6c7d2a10"
    assert request.productId == 42
    assert request.categoryPath == ["강아지", "사료", "건식사료"]
    assert request.schemaVersion == SUMMARY_SCHEMA_VERSION
    review = request.newReviews[0]
    assert (review.reviewId, review.rating, review.optionName) == (101, 5, "2kg")
    assert review.content == "허겁지겁 먹어요. 알이 좀 커요"
    scope = request.requestedScopes[0]
    assert scope.scope is SummaryScope.PRODUCT
    assert scope.segmentKey is None
    assert (scope.basisReviewCount, scope.unextractedReviewCount) == (12, 1)
    row = scope.tally[0]
    assert (row.aspectCode, row.polarity, row.mentionCount) == (
        AspectCode.PALATABILITY,
        Polarity.PRO,
        6,
    )
    assert row.quoteSamples[0].reviewId == 99, "인용 귀속이 없으면 검증을 다시 돌릴 수 없다"

    emitted = json.loads(request.model_dump_json())
    assert not set(emitted) - set(raw), "우리가 요구하는 키를 백엔드가 빼면 전건이 조용히 버려진다"
    assert not set(emitted["newReviews"][0]) - set(raw["newReviews"][0])
    assert not set(emitted["requestedScopes"][0]) - set(raw["requestedScopes"][0])
    assert not set(emitted["requestedScopes"][0]["tally"][0]) - set(raw["requestedScopes"][0]["tally"][0])


def test_요약_요청에서_우리가_버리는_키는_알려진_것뿐이다(contract: Any) -> None:
    """백엔드가 새 키를 더하면 **읽을지 말지를 사람이 정해야 한다.**

    ``extra="ignore"`` 는 두 레포의 배포 순서를 지켜 주는 장치이고 새 입력을 승인하는
    장치가 아니다. 집합을 열어 두면 백엔드가 보낸 새 필드가 녹색 테스트 밑에서 영구히
    무시된다 -- 지금 이 넷(``totalVisibleReviews``·``ratingBuckets``·``hasPhoto``·
    ``createdAt``)이 **계약에서 빠진 뒤에도 선을 타고 흐르는** 모양이 그 증거다.
    """
    raw = contract("review-summary-requested.json")

    emitted = json.loads(ReviewSummaryRequest.model_validate(raw).model_dump_json())

    assert set(raw) - set(emitted) <= set(_SUMMARY_REQUEST_IGNORED)
    assert set(raw["newReviews"][0]) - set(emitted["newReviews"][0]) <= set(_SUMMARY_REVIEW_IGNORED)


def test_요약_요청_표본이_상품별_파티션_키를_들고_온다(contract: Any) -> None:
    """``orderingKey`` 가 상품 식별자여야 한다 -- 요청 토픽은 파티션이 하나가 아니다.

    카프카는 파티션 안에서만 키 단위 묶음을 보장한다. 키가 상품별이 아니면 한 상품의 두
    요청이 다른 파티션으로 흩어지고, 우리는 결과 봉투에 **받은 키를 그대로 승계**하므로
    (``app/kafka/summary_runner.py``) 그 흩어짐이 결과 토픽까지 따라간다. 우리 쪽에서
    고칠 수 없는 어긋남이라 표본이 그 사실을 들고 있어야 한다.
    """
    raw = contract("review-summary-requested.json")

    assert raw["orderingKey"] == str(raw["productId"])
    # 같은 자리에서 집합체 이름도 본다. 백엔드 ``Topics.of()`` 가 **이 문자열 하나를
    # 특별 취급해** 요청을 ``aechak.summary.requests`` 로 보낸다(아니면 규칙대로
    # ``aechak.<집합체>.events`` 가 된다). 어긋나면 요청이 우리가 듣지 않는 토픽으로 가고
    # 러너에는 아무 소리도 나지 않는다 -- 요약이 전건 조용히 멈춘 것처럼 보인다.
    assert raw["aggregateType"] == SUMMARY_AGGREGATE_TYPE


def test_요약_결과_표본의_키_집합이_우리_출력과_같다(contract: Any) -> None:
    """백엔드가 이 표본을 "AI 가 직렬화한 모양"으로 들고 테스트를 박았다.

    백엔드 매퍼는 모르는 필드를 무시하므로 **키 이름이 어긋나면 에러 없이 일부가 사라진다.**
    판정 테이블은 전부 널 불가라 사라진 자리는 기본값으로 저장되고, 그것이 화면까지 간다.
    경로마다 키 집합을 맞춰 두면 한쪽이 필드를 더하거나 이름을 바꿀 때 여기서 터진다.
    """
    raw = contract("review-summary-result.json")

    message = ReviewSummaryMessage.model_validate(raw)

    emitted = json.loads(message.model_dump_json())
    assert _key_paths(emitted) == _key_paths(raw)


def test_요약_결과_표본이_미추출_세_모양을_구분해_들고_있다(contract: Any) -> None:
    """**사유 있는 미추출과 사유 없는 미추출은 다른 것이다.**

    사유 있는 쪽(``INJECTION``)은 다시 보내도 또 빠지므로 백엔드가 재요청 대상에서
    빼야 하고, 사유 없는 쪽(호출 실패)은 다음 회차에 다시 보내야 한다. 둘을 섞으면
    읽지 못한 부정 리뷰가 "단점 없음"으로 굳는다.
    """
    message = ReviewSummaryMessage.model_validate(contract("review-summary-result.json"))

    assert message.state is SummaryState.OK
    rows = {row.reviewId: row for row in message.extracted}
    assert rows[101].extracted is ExtractStatus.EXTRACTED
    assert rows[102].excludeReason is not None, "사유 있는 미추출은 다시 보낼 대상이 아니다"
    assert rows[103].extracted is ExtractStatus.UNEXTRACTED
    assert rows[103].excludeReason is None, "사유 없는 미추출은 다음 회차에 다시 보내야 한다"
    product, segment = message.summaries
    assert product.analyzedReviewCount == 10
    assert segment.consAbsenceReason is ConsAbsenceReason.NOT_ENOUGH_REVIEWS
