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

    세 값이 화면에서 서로 다른 문장이 된다. 뭉개면 "분석한 12건에 단점 언급이 없었다"와
    "요약하기에 리뷰가 아직 적다"가 같은 말로 읽힌다.
    """

    NO_NEGATIVE_MENTIONS_FOUND = "NO_NEGATIVE_MENTIONS_FOUND"
    BELOW_MENTION_THRESHOLD = "BELOW_MENTION_THRESHOLD"
    NOT_ENOUGH_REVIEWS = "NOT_ENOUGH_REVIEWS"


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
    newReviews: list[ReviewInput] = Field(default_factory=list)
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


class ScopeSummary(BaseModel):
    scope: SummaryScope
    segmentKey: str | None = None
    basisReviewCount: int = 0
    unextractedReviewCount: int = 0
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
    calls: list[SummaryCallUsage] = Field(default_factory=list)
    dropped: DroppedCounts = Field(default_factory=DroppedCounts)
    failureDetail: str | None = None
    generatedAt: datetime
    schemaVersion: int = SUMMARY_SCHEMA_VERSION

    def event_id(self) -> str:
        return f"review-summary-{self.requestId}"
