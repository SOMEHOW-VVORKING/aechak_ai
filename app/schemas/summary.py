"""리뷰 요약 계약. **검수 계약(``app/schemas/inspection.py``)과 별 스키마다.**

왜 재사용하지 않는지를 두 가지로 적어 둔다. 둘 다 코드에 근거가 있다.

1. ``CallUsage.perspective``는 백엔드 ``Perspective`` 열거에 있는 값만 실을 수 있다.
   요약 단계 이름을 거기 더하면 백엔드 ``com.aechak.message.inspection`` 역직렬화가 깨진다.
   그래서 ``SummaryCallUsage``는 ``perspective`` 대신 ``stage``를 든다 -- 자유 문자열이고
   백엔드 열거와 아무 관계가 없다.
2. 검수의 합성 ``promptVersion``은 백엔드 ``VARCHAR(40)``에 묶여 있고 현재 33자다.
   요약 버전을 그 문자열에 섞으면 즉시 넘친다. 요약은 ``extractVersion``과
   ``narrateVersion``을 자기 필드로 든다.

이 계약의 가장 중요한 불변식은 **통과와 미검사를 섞지 않는 것**이다. 세 자리에 나타난다.

- ``ExtractStatus``: 추출이 실패한 리뷰는 ``UNEXTRACTED``다. ``EXTRACTED`` + 빈 항목으로
  적지 않는다 -- 못 본 것과 항목이 없는 것은 다른 사실이다.
- ``ConsAbsenceReason``: "단점 없음" 한 가지로 접지 않고 세 갈래로 가른다.
- ``MatchTier.NO_PET_INFO``: 펫 정보가 없는 리뷰를 조건 불일치(``OTHER``)로 내리지 않는다.

그리고 요청의 ``content`` 필드는 **하나뿐이다.** 그 값은 백엔드
``Review.visibleContent(reviewStatus, content, displayContent)``의 결과여야 한다.
``content``와 ``displayContent``를 둘 다 받는 모양을 만들면 언젠가 틀린 쪽이 채워져
금칙어 마스킹이 요약에서 되돌려진다.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

SUMMARY_SCHEMA_VERSION = 1

# 메시지당 신규 리뷰 상한. 정본은 ``Settings.summary_max_new_reviews`` 다 -- 여기서
# import 하는 것은 ``app.settings`` 가 스키마를 import 하지 않아 순환이 생기지 않기 때문이고,
# 테스트가 두 값이 같은 것을 단언한다.
_MAX_NEW_REVIEWS = 40

SUMMARY_AGGREGATE_TYPE = "reviewSummary"
SUMMARY_REQUEST_EVENT_TYPE = "ReviewSummaryRequestedMessage"
SUMMARY_RESULT_EVENT_TYPE = "ReviewSummaryMessage"


class AspectCode(StrEnum):
    """항목 코드. **카테고리와 무관한 고정 열거 18종이다.**

    두 가지가 이 열거를 고정으로 만든 이유다.

    1. 툴 스키마가 요청 데이터로 바뀌면 캐시 엔트리가 그만큼 갈라진다. 렌더 순서가
       ``tools -> system -> messages``라서 툴이 접두부에 함께 캐시되고, 카테고리마다 다른
       enum을 실으면 카테고리 수만큼 캐시 쓰기가 난다.
    2. 열거를 나중에 쪼개면 저장된 ``review_aspects`` 전량이 무효가 되어 전 코퍼스를
       재추출해야 한다. ``OTHER``를 반드시 두는 것이 그 비용을 미루는 장치다 -- 새 현상이
       열거 변경을 강요하지 않고 거기 떨어지고, 자유 텍스트 ``aspectLabel``이 사람이 읽을
       수 있는 내용을 보존한다.

    세는 축은 이 고정 열거(코드가 센다)이고 읽는 축은 ``aspectLabel``(모델이 쓴다)이다.
    """

    PALATABILITY = "PALATABILITY"
    PRICE = "PRICE"
    DELIVERY = "DELIVERY"
    PACKAGING = "PACKAGING"
    SMELL = "SMELL"
    DURABILITY = "DURABILITY"
    SIZE_FIT = "SIZE_FIT"
    EASE_OF_USE = "EASE_OF_USE"
    REPURCHASE = "REPURCHASE"
    PERCEIVED_EFFECT = "PERCEIVED_EFFECT"
    ADVERSE_REACTION = "ADVERSE_REACTION"
    NOISE = "NOISE"
    CLEANING = "CLEANING"
    DESIGN = "DESIGN"
    LEAKAGE = "LEAKAGE"
    SELLER_SUPPORT = "SELLER_SUPPORT"
    QUANTITY = "QUANTITY"
    OTHER = "OTHER"


class Polarity(StrEnum):
    PRO = "PRO"
    CON = "CON"


class ExtractStatus(StrEnum):
    """추출 상태. 섞으면 못 본 리뷰가 "항목 없는 리뷰"로 적힌다."""

    EXTRACTED = "EXTRACTED"
    UNEXTRACTED = "UNEXTRACTED"


class PetSource(StrEnum):
    """펫 지식의 출처. 절대 합치지 않는다.

    ``SNAPSHOT``은 백엔드가 작성 시점에 기록한 권위 있는 값이고 세그먼트 분모에 들어간다.
    ``BODY_INFERRED``는 본문에서 추출해 인용으로 검증한 값이고 **정렬 가중치만** 쓴다 --
    세그먼트 분모에 넣으면 "비슷한 아이 보호자 n명"이라는 수치가 모델 추측으로 부풀려진다.
    """

    SNAPSHOT = "SNAPSHOT"
    BODY_INFERRED = "BODY_INFERRED"


class SummaryScope(StrEnum):
    PRODUCT = "PRODUCT"
    SEGMENT = "SEGMENT"


class ConsAbsenceReason(StrEnum):
    """단점이 비어 있는 사유. "없음" 한 가지로 접지 않는다.

    다섯 값이 화면에서 서로 다른 문장이 된다. 뭉개면 "분석한 12건에 단점 언급이 없었다"와
    "요약하기에 리뷰가 아직 적다"가 같은 말로 읽힌다.

    **``NO_NEGATIVE_MENTIONS_FOUND`` 가 올 수 있는 경로를 코드에서 좁혔다.** 그 값은
    "분석한 n건 전부를 읽었고 거기 단점 언급이 없었다"는 **강한 주장**이다. 못 읽은 리뷰가
    한 건이라도 있으면 그 주장을 할 수 없고, 반복으로 센 단점이 있는데 산문만 없을 때도
    할 수 없다. 그 두 경로에 전용 값을 둔다 -- 아래 둘이 그것이고, 없으면 거짓이 그 자리로
    흘러 들어간다.
    """

    NO_NEGATIVE_MENTIONS_FOUND = "NO_NEGATIVE_MENTIONS_FOUND"
    BELOW_MENTION_THRESHOLD = "BELOW_MENTION_THRESHOLD"
    NOT_ENOUGH_REVIEWS = "NOT_ENOUGH_REVIEWS"
    # 단점 언급이 0 이지만 **못 읽은 리뷰가 있다.** "없었다"로 적으면 거짓이다.
    PARTIALLY_UNEXTRACTED = "PARTIALLY_UNEXTRACTED"
    # 코드가 반복으로 센 단점이 있는데 **모델이 한 줄을 쓰지 않았다.** 건수는 남아 있다.
    NARRATION_MISSING = "NARRATION_MISSING"


class ExcludeReason(StrEnum):
    """추출 입력에서 뺀 사유. ``dropped`` 계수와 1:1이다.

    ``INJECTION``을 "항목 없음"에 섞지 않는 것이 요점이다 -- 판정기를 겨냥한 문장이 든
    리뷰는 못 본 것이고, 장단점이 없는 리뷰가 아니다.
    """

    INJECTION = "INJECTION"
    DUPLICATE = "DUPLICATE"
    EMPTY = "EMPTY"


class SummaryState(StrEnum):
    OK = "OK"
    FAILED = "FAILED"


# --- 요청 ---------------------------------------------------------------------------


class ReviewInput(BaseModel):
    """새로 추출할 리뷰 한 건.

    ``content`` 하나만 둔다. 백엔드가 ``Review.visibleContent(...)``의 결과를 넣어야 하고,
    ``ReviewItemResult.from``이 이미 그 선례다. ``authorUserId``는 **받지 않는다** --
    요약에 쓸 데가 없고 개인정보 표면만 넓힌다. ``reviewId``는 식별자가 아니라 인용 귀속 키다.
    """

    model_config = ConfigDict(extra="ignore")

    reviewId: int
    rating: int = Field(ge=1, le=5)
    content: str
    optionName: str = "기본"
    hasPhoto: bool = False
    createdAt: datetime | None = None


class QuoteSample(BaseModel):
    """검증된 인용 하나. 리뷰 귀속이 함께 있어야 검증을 다시 돌릴 수 있다."""

    model_config = ConfigDict(extra="ignore")

    reviewId: int
    text: str


class AspectTallyRow(BaseModel):
    """집계표 한 줄. 백엔드가 영구 보관하고 요청마다 그대로 돌려준다.

    이 줄이 이 설계의 영구 산출물이다. 요약 산문은 매번 새로 쓰이고 버려지지만 이 행은
    불변이고, 그래서 증분 요약의 드리프트 없이 비용을 N에 무관하게 만든다.
    """

    model_config = ConfigDict(extra="ignore")

    aspectCode: AspectCode
    polarity: Polarity
    mentionCount: int = 0
    labelSamples: list[str] = Field(default_factory=list)
    quoteSamples: list[QuoteSample] = Field(default_factory=list)


class RequestedScope(BaseModel):
    """서술을 요청한 스코프 하나.

    ``basisReviewCount``와 ``tally``를 **백엔드가 보낸다.** AI가 전수 조회를 하지 않는다 --
    DB에 붙지 않는 것이 이 서버의 첫 불변식이다. ``segmentKey``는 불투명 문자열로만
    다룬다(세그먼트 배정은 백엔드가 끝내 보낸다).
    """

    model_config = ConfigDict(extra="ignore")

    scope: SummaryScope
    segmentKey: str | None = None
    # **이 메시지의 ``newReviews`` 를 포함한 분모다.** 백엔드가 그렇게 세어 보내야 한다.
    #
    # 포함하지 않으면 AI 가 새 항목을 집계에 더하는 동안 분모는 그대로여서 언급 비율이
    # 1 을 넘을 수 있다. 세그먼트 스코프는 사정이 다르다 -- AI 는 리뷰가 어느 세그먼트에
    # 속하는지 모르므로(펫 스냅샷을 받지 않는다) 세그먼트 집계는 **보낸 그대로** 쓴다.
    basisReviewCount: int = 0
    # 백엔드가 "추출 결과를 아직 받지 못한" 리뷰로 센 수다. **AI 가 이번 회차에 못 읽은
    # 리뷰는 여기 들어 있지 않다** -- 그것은 AI 가 더한다(``ScopeSummary`` 주석 참조).
    unextractedReviewCount: int = 0
    tally: list[AspectTallyRow] = Field(default_factory=list)


class ReviewSummaryRequest(BaseModel):
    """백엔드 ``ReviewSummaryRequestedMessage``의 payload.

    ``petSnapshot``을 받지 않는다. 세그먼트 배정은 백엔드가 이미 끝내 보내므로 AI는
    ``segmentKey``만 알면 되고, 그렇게 두면 개인정보 표면이 줄고 유사도 함수 사본이
    두 레포에 생기지 않는다(정본은 ``app/review_summary/selection.py``이고 골든 벡터로 내보낸다).
    """

    model_config = ConfigDict(extra="ignore")

    requestId: str
    productId: int
    categoryPath: list[str] = Field(default_factory=list)
    totalVisibleReviews: int = 0
    ratingBuckets: list[dict[str, int]] = Field(default_factory=list)
    # **상한이 계약에 있다.** 없으면 최악 처리시간이 카프카 폴 간격을 넘겨 컨슈머 태스크가
    # 죽고, 죽으면 다시 띄우는 코드가 없고 ``/health`` 는 검수만 보므로 200 UP 이라 ECS 가
    # 교체하지 않는다. 정본은 ``Settings.summary_max_new_reviews`` 이고 그 값이
    # ``summary_worst_case_handle_seconds()`` 의 배치 항을 정한다 -- 두 수가 한 자리에서
    # 움직이게 묶어 두는 것이 이 레포가 밟은 함정의 재발 방지다.
    newReviews: list[ReviewInput] = Field(default_factory=list, max_length=_MAX_NEW_REVIEWS)
    requestedScopes: list[RequestedScope] = Field(default_factory=list)
    schemaVersion: int = SUMMARY_SCHEMA_VERSION


# --- 응답 ---------------------------------------------------------------------------


class ExtractedAspect(BaseModel):
    """검증을 통과한 항목 하나. 구간은 **모델이 본 텍스트** 기준이다.

    원문 기준으로 적으면 레닥션과 절단 때문에 어긋난다. 백엔드가 이 구간으로 원문을
    하이라이트하지 않도록 계약에 적어 둔다 -- 쓸 수 있는 것은 인용 문자열이다.
    """

    aspectCode: AspectCode
    aspectLabel: str
    polarity: Polarity
    quote: str
    quoteStart: int
    quoteEnd: int


class PetContext(BaseModel):
    """본문에서 추출한 펫 맥락. 인용으로 검증된 것만 나간다."""

    source: PetSource = PetSource.BODY_INFERRED
    species: str | None = None
    breedLabel: str | None = None
    weightKg: float | None = None
    ageYears: int | None = None
    quote: str


class ExtractedReview(BaseModel):
    extracted: ExtractStatus
    reviewId: int
    aspects: list[ExtractedAspect] = Field(default_factory=list)
    petContext: PetContext | None = None
    excludeReason: ExcludeReason | None = None
    truncated: bool = False
    note: str | None = None


class SummaryAspect(BaseModel):
    """요약에 실린 장점 또는 단점 하나.

    ``mentionCount``와 ``mentionShare``는 **코드가 채운다.** 툴 스키마에 그 필드가 없어서
    모델은 적을 수 없다 -- "반복된다"의 판정을 모델에게서 구조적으로 빼앗는 자리다.
    """

    aspectCode: AspectCode
    headline: str
    mentionCount: int
    mentionShare: float
    quotes: list[QuoteSample] = Field(default_factory=list)


class DroppedScope(BaseModel):
    """요청됐지만 처리하지 않은 스코프. **"요약 없음"과 구분되어야 한다.**

    메시지당 스코프 상한을 넘기면 뒤쪽을 버린다. 그것을 응답에 적지 않으면 백엔드 입장에서
    "그 세그먼트에 요약이 없다"와 "상한에 걸려 버렸다"가 같은 모양이고, 저장 로직이 전자로
    읽으면 그 세그먼트가 **영구히 빈 채로 캐시된다.** 이 레포가 다른 모든 자리에서 지키는
    "통과와 미검사를 섞지 않는다"의 예외를 만들지 않는다.
    """

    model_config = ConfigDict(extra="ignore")

    scope: SummaryScope
    segmentKey: str | None = None
    notProcessedReason: str


class ScopeSummary(BaseModel):
    scope: SummaryScope
    segmentKey: str | None = None
    basisReviewCount: int = 0
    # **요청값 + 이번 회차에 AI 가 못 읽은 건수**의 합이다. 요청값만 되읊으면 추출이
    # 실패한 리뷰가 "분석한 리뷰" 분모에 남아 못 읽은 부정 리뷰가 "단점 없음"이 된다.
    #
    # 비대칭이 하나 있고 그것이 의도다. 상품 스코프는 이번 회차 실패를 AI 가 세어 더하지만
    # **세그먼트 스코프는 보낸 값을 그대로 쓴다** -- AI 는 펫 스냅샷을 받지 않아 리뷰가 어느
    # 세그먼트에 속하는지 모르고, 추측해서 더하면 그것이 또 다른 거짓이다. 그래서 세그먼트
    # 분모는 백엔드가 책임진다(설계 문서의 백엔드 선결 목록에 적었다).
    unextractedReviewCount: int = 0
    # **실제로 분석된 건수.** 임계와 언급 비율의 분모가 이 수다.
    #
    # ``basisReviewCount`` 를 분모로 쓰면 20건 중 3건만 읽은 상태에서 최소 10건 게이트가
    # 통과하고 "리뷰 3건으로 반복되는 장점을 적는다"가 가능해진다. 실측으로 확인했다.
    analyzedReviewCount: int = 0
    pros: list[SummaryAspect] = Field(default_factory=list)
    cons: list[SummaryAspect] = Field(default_factory=list)
    consAbsenceReason: ConsAbsenceReason | None = None
    minorMentions: list[AspectTallyRow] = Field(default_factory=list)
    # 임계를 **넘었는데 모델이 한 줄을 쓰지 않은** 항목.
    #
    # ``minorMentions`` 와 절대 섞지 않는다. 저쪽은 "언급이 적어 반복이라 부를 수 없다"이고
    # 이쪽은 "반복이라고 셌는데 문장이 없다"다 -- 뭉개면 반복된 장단점이 조용히 사라지고
    # 화면은 그것을 "미달"로 적는다. 셈은 코드의 것이므로 모델이 안 써도 건수는 남아야 한다.
    unnarratedAspects: list[AspectTallyRow] = Field(default_factory=list)
    notEnoughReason: str | None = None
    # 서술 호출이 실패해 산문이 없는 경우. **빈 장단점의 세 번째 사유다.**
    #
    # ``cons: []`` 가 유효한 조건이 둘이 아니라 셋이다 -- ``consAbsenceReason`` 이 있거나,
    # ``notEnoughReason`` 이 있거나, 이 필드가 있을 때. 이것을 ``consAbsenceReason`` 에
    # 접으면 "호출이 죽어서 못 썼다"가 "단점 언급이 없었다"로 적힌다.
    narrateFailure: str | None = None
    tallyFingerprint: str = ""


class SummaryCallUsage(BaseModel):
    """요약 전용 원장. ``CallUsage``를 재사용하지 않는 이유는 모듈 머리에 적었다."""

    requestId: str
    callId: str
    stage: str
    modelId: str
    inputTokens: int = 0
    cacheReadInputTokens: int = 0
    cacheCreationInputTokens: int = 0
    outputTokens: int = 0
    costMicroUsd: int = 0
    latencyMs: int = 0
    outcome: str = "SUCCESS"


class DroppedCounts(BaseModel):
    """버린 것들의 셈. **0이 아니라 세는 것**이 이 모델의 요점이다.

    버린 것을 세지 않으면 요약이 조용히 얇아진다. 그리고 ``piiRedacted``와
    ``evidenceUnverified``는 하네스가 재야 하는 1순위 안전 지표다.
    """

    evidenceUnverified: int = 0
    # 평점과 극성이 어긋나 **버린** 항목. 1점 리뷰의 PRO 뿐이다.
    polarityConflict: int = 0
    # 어긋났지만 **남긴** 항목. 5점 리뷰의 CON. 극성 뒤집힘 탐지는 이 숫자가 한다 --
    # 버리면 5점 리뷰의 사소한 불만이 사라져 부정 리뷰 누락을 스스로 만든다
    # (``app/review_summary/evidence.py`` 의 경계 주석 참조).
    polarityConflictNoted: int = 0
    piiRedacted: int = 0
    injectionExcluded: int = 0
    duplicateFolded: int = 0
    maskHeavyQuote: int = 0
    truncated: int = 0
    unknownAspectCode: int = 0
    belowThresholdRejected: int = 0
    # **출력 게이트 탈락을 ``piiRedacted`` 에 섞지 않는다.** 섞으면 그 값이 올랐을 때
    # "입력에 연락처가 많았다"(정상)인지 "모델이 산문에 연락처를 썼다"(사고)인지 구분할 수
    # 없고, 하네스가 1순위 안전 지표로 읽는 숫자가 해석 불가가 된다.
    outputGateBlocked: int = 0
    # 자유 텍스트 게이트에 걸려 열거 기본값으로 되돌린 라벨 수.
    labelRejected: int = 0
    # **마스킹을 되돌린 라벨.** 0 이 정상이고 오르면 사고다.
    maskRestored: int = 0
    # ``mentionCount`` 가 분석된 건수를 넘은 집계 줄. 저장 집계 손상(재배달 중복 가산) 신호다.
    tallyInconsistent: int = 0
    # 임계를 넘었는데 모델이 한 줄을 쓰지 않은 항목 수. 0 이 정상이고, 오르면 서술
    # 접두부의 지시가 지켜지지 않는다는 신호다.
    narrationMissing: int = 0


class ReviewSummaryMessage(BaseModel):
    """AI -> 백엔드. ``aechak.summary.results``로 나간다."""

    requestId: str
    productId: int
    state: SummaryState = SummaryState.OK
    extractVersion: str
    narrateVersion: str
    extracted: list[ExtractedReview] = Field(default_factory=list)
    summaries: list[ScopeSummary] = Field(default_factory=list)
    # 상한에 걸려 처리하지 않은 스코프. 비어 있는 것이 정상이다.
    droppedScopes: list[DroppedScope] = Field(default_factory=list)
    calls: list[SummaryCallUsage] = Field(default_factory=list)
    dropped: DroppedCounts = Field(default_factory=DroppedCounts)
    failureDetail: str | None = None
    generatedAt: datetime
    schemaVersion: int = SUMMARY_SCHEMA_VERSION

    def event_id(self) -> str:
        """**백엔드는 이 값으로 멱등 소비해야 한다.**

        러너는 발행 뒤 커밋이라 발행 성공·커밋 실패 사이에 죽으면 같은 메시지가 재배달된다.
        백엔드가 멱등 처리하지 않으면 같은 리뷰의 항목이 저장 집계에 두 번 더해지고,
        그러면 ``mentionCount`` 가 분모를 넘는다. AI 쪽은 그 모순을 ``tallyInconsistent``
        로 세고 비율을 1.0 으로 자르지만, **고치는 것은 백엔드의 멱등성이다.**
        """
        return f"review-summary-{self.requestId}"
