"""검수 계약의 파이썬 미러. 백엔드 ``com.aechak.message.inspection`` 패키지와 1:1이다."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1

INSPECTION_AGGREGATE_TYPE = "inspection"
VERDICT_AGGREGATE_TYPE = "verdict"


class TargetType(StrEnum):
    PRODUCT = "PRODUCT"
    REVIEW = "REVIEW"


class ViolationCategory(StrEnum):
    CONTACT_INFO = "CONTACT_INFO"
    OFF_PLATFORM = "OFF_PLATFORM"
    MEDICAL_CLAIM = "MEDICAL_CLAIM"
    EFFICACY_OVERCLAIM = "EFFICACY_OVERCLAIM"
    BANNED_INGREDIENT = "BANNED_INGREDIENT"
    FALSE_CLAIM = "FALSE_CLAIM"
    DISPARAGEMENT = "DISPARAGEMENT"
    ABUSE = "ABUSE"
    SPAM = "SPAM"
    PII = "PII"
    IRRELEVANT = "IRRELEVANT"


class Perspective(StrEnum):
    """관점. 분류 개수가 아니라 참조하는 자료와 판단 양식으로 쪼갠다."""

    ALL = "ALL"
    DISPLAY_ADS = "DISPLAY_ADS"
    CONDUCT_TRADE = "CONDUCT_TRADE"
    # 결정론적 규칙 단계의 결과도 같은 테이블에 적는다
    RULE = "RULE"
    # 선별 단계. 승급 여부만 정하고 판정하지 않으므로 집계에서 세지 않는다
    TRIAGE = "TRIAGE"


class PerspectiveOutcome(StrEnum):
    """통과와 미검사를 반드시 구분한다. 뭉개면 못 본 것이 문제 없음으로 기록된다."""

    VIOLATION = "VIOLATION"
    CLEAR = "CLEAR"
    UNCHECKED = "UNCHECKED"


class Verdict(StrEnum):
    PASS = "PASS"
    FLAG = "FLAG"
    FAILED = "FAILED"


class CallOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    TIMEOUT = "TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    REFUSED = "REFUSED"
    SCHEMA_INVALID = "SCHEMA_INVALID"
    ERROR = "ERROR"


class ProductInspectionRequest(BaseModel):
    """백엔드 ``ProductInspectionRequestedMessage``의 payload."""

    model_config = ConfigDict(extra="ignore")

    inspectionId: int
    productId: int
    productVersionNo: int
    contentHash: str
    productName: str
    description: str | None = None
    optionNames: list[str] = Field(default_factory=list)
    categoryPath: list[str] = Field(default_factory=list)
    imageUrls: list[str] = Field(default_factory=list)
    truncated: bool = False
    schemaVersion: int = SCHEMA_VERSION

    @property
    def target_type(self) -> TargetType:
        return TargetType.PRODUCT

    @property
    def target_ref(self) -> int:
        return self.productId

    def inspect_text(self) -> str:
        parts = [self.productName]
        if self.description:
            parts.append(self.description)
        parts.extend(self.optionNames)
        return "\n".join(parts)


class ReviewInspectionRequest(BaseModel):
    """백엔드 ``ReviewInspectionRequestedMessage``의 payload."""

    model_config = ConfigDict(extra="ignore")

    inspectionId: int
    reviewId: int
    productId: int
    contentHash: str
    content: str
    rating: int
    categoryPath: list[str] = Field(default_factory=list)
    truncated: bool = False
    schemaVersion: int = SCHEMA_VERSION

    @property
    def target_type(self) -> TargetType:
        return TargetType.REVIEW

    @property
    def target_ref(self) -> int:
        return self.reviewId

    def inspect_text(self) -> str:
        return self.content


class PerspectiveResult(BaseModel):
    perspective: Perspective
    outcome: PerspectiveOutcome
    skipReason: str | None = None


class Violation(BaseModel):
    category: ViolationCategory
    perspective: Perspective
    spanStart: int | None = None
    spanEnd: int | None = None
    quote: str | None = None
    suggestion: str | None = None
    confidence: float | None = None


class CallUsage(BaseModel):
    requestId: str
    callId: str
    perspective: Perspective
    modelId: str
    inputTokens: int = 0
    cacheReadInputTokens: int = 0
    cacheCreationInputTokens: int = 0
    outputTokens: int = 0
    costMicroUsd: int = 0
    latencyMs: int = 0
    outcome: CallOutcome = CallOutcome.SUCCESS


class ContentVerdict(BaseModel):
    """백엔드 ``ContentVerdictMessage``의 payload."""

    inspectionId: int
    targetType: TargetType
    targetRef: int
    contentHash: str
    verdict: Verdict
    perspectives: list[PerspectiveResult]
    violations: list[Violation]
    sellerNotice: str | None = None
    calls: list[CallUsage]
    promptVersion: str
    schemaVersion: int = SCHEMA_VERSION
    decidedAt: datetime

    def event_id(self) -> str:
        return f"content-verdict-{self.inspectionId}"
