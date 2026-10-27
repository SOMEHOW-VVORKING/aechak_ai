"""셀러 폼 초안 계약. **검수 계약(``app/schemas/inspection.py``)과 별 스키마다.**

재사용하지 않는 이유가 둘이고 둘 다 코드에 근거가 있다.

1. ``CallUsage.perspective`` 는 백엔드 ``Perspective`` 열거에 있는 값만 실을 수 있다.
   초안 단계 이름을 거기 더하면 백엔드 ``com.aechak.message.inspection`` 역직렬화가 깨진다.
   그래서 ``DraftCallUsage`` 는 ``perspective`` 대신 ``stage`` 를 든다.
2. 검수의 합성 ``promptVersion`` 은 백엔드 ``VARCHAR(40)`` 에 묶여 있고 현재 33자다 --
   여유 7자. 초안 버전을 그 문자열에 섞으면 즉시 넘친다. 초안은 ``draftVersion`` 을
   자기 필드로 든다.

## 응답이 ``ProductDraftSaveRequest`` 모양인 이유

백엔드 ``product_drafts`` 의 임시저장 DTO 와 1:1 이다. 그래서 **백엔드 스키마 변경이 0이고**
다음 티켓은 "AI 응답 -> 초안 저장" 한 줄이 된다. 그 DTO 의 전 필드가 널 허용이라
**모델이 자신 없는 필드를 비워 두는 것이 계약에 자연스럽게 표현된다** -- 추측으로 채워
통과처럼 보이게 만들 필요가 없다.

## 이 계약의 가장 중요한 불변식

**``fieldStatus`` 와 ``basis`` 는 서로를 대체하지 않는다.** ``basis`` 는 "무슨 근거로
채웠는가", ``fieldStatus`` 는 "왜 비었는가"다. 그리고 빈 필드에 사유 없이 남는 자리가
없어야 한다 -- 특히 ``EMPTY_CALL_FAILED``(호출이 죽어서 못 봤다)를 "제안할 것이 없었다"로
표시하면 거짓이다. 셀러 화면도 둘을 다르게 보여 줘야 한다("다시 시도" 대 "직접 작성").

## 알려진 위험

이 스키마는 **백엔드에 거울 상대가 없는 상태에서 선언하는 계약이다.** 검수 메시지 계약조차
``origin/develop`` 에 없고 미머지 브랜치에 있다. 백엔드 티켓이 나중에 다른 모양을 고르면
이 스키마는 버려지거나 변환 계층이 생긴다.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.seller_draft.catalog import ItemGrade, ResolvedBy

DRAFT_SCHEMA_VERSION = 1


class Basis(StrEnum):
    """그 필드를 **무슨 근거로** 채웠는가. 모델이 필드마다 필수로 적는다."""

    NAME = "NAME"
    IMAGE = "IMAGE"
    SELLER_CATEGORY = "SELLER_CATEGORY"
    NONE = "NONE"


class ImageUsability(StrEnum):
    """사진이 쓸 만했는가. 모델이 스스로 적고 코드가 정합성을 강제한다.

    ``MISMATCHES_NAME`` 이 가장 값진 안전 출력이다. 셀러가 엉뚱한 사진을 올렸을 때 모델이
    **사진 쪽 상품의 초안을 조용히 써 주는 것**이 이미지 입장의 최악 실패 모양이고, 그것을
    열거값으로 받아야 거를 수 있다.
    """

    USED = "USED"
    NO_IMAGE_SENT = "NO_IMAGE_SENT"
    TOO_DARK_OR_BLURRY = "TOO_DARK_OR_BLURRY"
    TOO_SMALL = "TOO_SMALL"
    MULTIPLE_PRODUCTS = "MULTIPLE_PRODUCTS"
    MISMATCHES_NAME = "MISMATCHES_NAME"
    UNREADABLE = "UNREADABLE"


class FieldStatus(StrEnum):
    """필드마다 **반드시 하나.** 사유 없이 빈 필드는 응답에 남지 않는다.

    설계가 적어 둔 12 갈래에 셋을 더했다. 각각 섞으면 거짓이 되는 구분이다.

    - ``KEPT_LOCKED``: 셀러가 고쳐서 잠근 값이다. ``EMPTY_NOT_ATTEMPTED`` 로 접으면
      "설계상 채우지 않는 필드"와 같은 말이 되고, 화면이 셀러가 쓴 글을 안 보여 준다.
    - ``EMPTY_MODEL_DECLINED``: 모델이 "고르지 못했다"고 **답한** 것이다.
      ``EMPTY_SCHEMA_INVALID``(필드를 아예 내지 않았다)와 뜻이 정반대다 -- 전자는 정직한
      답이고 후자는 고장이다. 툴 스키마가 nullable 대신 ``0`` 센티넬을 쓰기 때문에 이
      구분이 필요해졌다.
    - ``EMPTY_NAME_RULE_BLOCKED``: **입력 상품명**이 0단 규칙에 걸려 아무것도 시도하지
      않았다. ``EMPTY_GUARD_RULE``(생성물이 걸려 버렸다)과 방향이 반대다.
    """

    FILLED = "FILLED"
    KEPT_LOCKED = "KEPT_LOCKED"
    EMPTY_NOT_ATTEMPTED = "EMPTY_NOT_ATTEMPTED"
    EMPTY_ITEM_UNIDENTIFIED = "EMPTY_ITEM_UNIDENTIFIED"
    EMPTY_NAME_RULE_BLOCKED = "EMPTY_NAME_RULE_BLOCKED"
    EMPTY_GUARD_RULE = "EMPTY_GUARD_RULE"
    EMPTY_GUARD_BANNED = "EMPTY_GUARD_BANNED"
    EMPTY_GUARD_CLAIM = "EMPTY_GUARD_CLAIM"
    EMPTY_GUARD_OPTION_RULE = "EMPTY_GUARD_OPTION_RULE"
    EMPTY_GUARD_BASIS_MISMATCH = "EMPTY_GUARD_BASIS_MISMATCH"
    EMPTY_GUARD_LENGTH = "EMPTY_GUARD_LENGTH"
    EMPTY_CATEGORY_CONFLICT = "EMPTY_CATEGORY_CONFLICT"
    EMPTY_MODEL_DECLINED = "EMPTY_MODEL_DECLINED"
    EMPTY_CALL_FAILED = "EMPTY_CALL_FAILED"
    EMPTY_SCHEMA_INVALID = "EMPTY_SCHEMA_INVALID"


class GuardKind(StrEnum):
    """어느 겹이 떨어뜨렸는가. **하네스가 가드 탈락률을 겹별로 가르는 축이다.**

    ``CLAIM`` 탈락률이 효능 사전을 새로 만들어야 했는지의 증거이고, ``OPTION_RULE``
    탈락률이 "값 이름 전역 유일" 전부-또는-전무 규칙의 위험을 측정한다.
    """

    RULE = "RULE"
    BANNED = "BANNED"
    CLAIM = "CLAIM"
    OPTION_RULE = "OPTION_RULE"
    BASIS_MISMATCH = "BASIS_MISMATCH"
    LENGTH = "LENGTH"
    CATEGORY_CONFLICT = "CATEGORY_CONFLICT"
    UNKNOWN_LEAF = "UNKNOWN_LEAF"


class SkipReason(StrEnum):
    """모델을 **부르지 않은** 사유. 전부 0원 분기다."""

    NAME_RULE_BLOCKED = "NAME_RULE_BLOCKED"
    ITEM_UNIDENTIFIED = "ITEM_UNIDENTIFIED"
    DRAFT_DISABLED = "DRAFT_DISABLED"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    REUSED_FROM_CACHE = "REUSED_FROM_CACHE"


class DraftState(StrEnum):
    OK = "OK"
    FAILED = "FAILED"


class ImageMode(StrEnum):
    """이미지를 쓰는 방식. 기본은 ``OFF`` 이고 하네스 숫자가 켠다.

    ``enable_triage: bool = False`` 선례와 같다 -- **구현하고 측정하고 기본은 끈다.**
    ``GATED`` 가 "비싼 호출 앞에 싼 게이트" 패턴이고, 다만 그 게이트는 모델이 아니라
    0원 어휘 사전이다.
    """

    OFF = "OFF"
    GATED = "GATED"
    ALWAYS = "ALWAYS"


# --- 요청 ---------------------------------------------------------------------------


class DraftImage(BaseModel):
    """셀러센터가 **축소해서** base64 로 보낸 사진 한 장.

    URL 로 받지 않는다. 등록 전 초안의 썸네일은 승격 전 tmp 키라 공개 URL 이 아니고,
    ``{"type":"url"}`` 은 Anthropic 이 직접 가져가야 하므로 외부에서 닿는 주소가 필요하다.
    base64 는 AI 서버가 네트워크를 타지 않아 의존성·권한·네트워크 면이 모두 깨끗하다 --
    대가는 요청 본문 33% 팽창뿐이다.

    선언 치수는 **비교용으로만** 받는다. 실측과 어긋나면 실측값으로 예산을 다시 계산한다.
    """

    model_config = ConfigDict(extra="ignore")

    mediaType: str = "image/jpeg"
    base64: str = ""
    declaredWidthPx: int = 0
    declaredHeightPx: int = 0


class OptionGroupDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    values: list[str] = Field(default_factory=list)


class OptionCombinationDraft(BaseModel):
    """조합은 **서버가 만든다.** 모델에게는 그룹명과 값 이름만 받는다.

    ``ProductOptions`` 의 불변식(조합 1개 이상 / 그룹마다 정확히 하나 지목 / 값 이름 전역
    유일 / 중복 집합 금지)을 모델이 반드시 하나는 깨고, 셀러가 그대로 저장하면 거부당한다.
    ``additionalPrice`` 는 0, ``stockQuantity`` 는 비운다 -- 돈과 재고는 채우지 않는다.
    조합 이름도 보내지 않는다. 백엔드 ``ProductService`` 가 ``" / "`` 연결로 만든다.
    """

    model_config = ConfigDict(extra="ignore")

    optionValues: list[str] = Field(default_factory=list)
    additionalPrice: int = 0
    stockQuantity: int | None = None


class LockedFields(BaseModel):
    """셀러가 고쳐서 잠근 값. **신뢰할 수 없는 입력이다.**

    상품 설명과 정확히 같은 성질이므로 봉투 안에 ``neutralize_envelope`` 를 지나 들어간다.
    """

    model_config = ConfigDict(extra="ignore")

    description: str | None = None
    optionGroups: list[OptionGroupDraft] | None = None


class SellerDraftRequest(BaseModel):
    """셀러센터 BE -> AI.

    **받지 않는 것을 명시한다**(의도): 가격·재고·이미지 키·셀러가 이미 쓴 설명·상품 식별자.
    초안이 채우지 않는 값이고 보내면 주입 표면만 넓어진다. 등록 전이라 ``publicId`` 도 없다.

    ``sellerId`` 는 로깅·레이트리밋용이고 **프롬프트에 넣지 않는다.**
    """

    model_config = ConfigDict(extra="ignore")

    requestId: str
    productName: str = Field(default="", max_length=255)
    categoryId: int | None = None
    sellerId: int | None = None
    lockedFields: LockedFields | None = None
    image: DraftImage | None = None
    schemaVersion: int = DRAFT_SCHEMA_VERSION


# --- 응답 ---------------------------------------------------------------------------


class ProductDraftSaveRequest(BaseModel):
    """백엔드 임시저장 DTO 와 1:1. **전 필드 널 허용이 계약의 요점이다.**"""

    categoryId: int | None = None
    # 설계상 **제안하지 않는다.** 검수 내용 해시의 입력이고, 셀러의 브랜드 결정이며
    # 무엇보다 이 요청의 **입력**이다 -- 입력을 고쳐 돌려주는 것은 월권이다.
    productName: str | None = None
    description: str | None = None
    regularPrice: int | None = None
    discountPrice: int | None = None
    discountStartAt: datetime | None = None
    discountEndAt: datetime | None = None
    thumbnailImageKey: str | None = None
    additionalImageKeys: list[str] | None = None
    detailImageKeys: list[str] | None = None
    optionGroups: list[OptionGroupDraft] | None = None
    optionCombinations: list[OptionCombinationDraft] | None = None


# 초안이 다루는 필드 전부. ``fieldStatus`` 가 **이 집합을 빠짐없이** 덮어야 한다.
DRAFT_FIELDS: tuple[str, ...] = tuple(ProductDraftSaveRequest.model_fields)


class ItemSignalOut(BaseModel):
    """어휘 게이트의 산출물. 모델을 0회 부른 0원 단계의 결과다."""

    grade: ItemGrade
    resolvedBy: ResolvedBy
    matchedTerms: list[str] = Field(default_factory=list)
    candidateLeafIds: list[int] = Field(default_factory=list)
    species: str | None = None


class ImageReport(BaseModel):
    """헤더 선검사 결과. 셀러센터가 사진을 고쳐 보낼 근거가 여기 있다."""

    usability: ImageUsability = ImageUsability.NO_IMAGE_SENT
    mediaType: str | None = None
    widthPx: int = 0
    heightPx: int = 0
    bytes: int = 0
    estimatedTokens: int = 0
    declaredMismatch: bool = False
    lowFidelity: bool = False
    rejected: bool = False
    problem: str | None = None
    detail: str | None = None
    maxLongEdgePx: int | None = None
    # 모드가 ``OFF`` 거나 ``GATED`` 에서 게이트가 확정한 경우. 사진이 나쁜 것이 아니다.
    skippedByMode: bool = False


class GuardDrop(BaseModel):
    """가드가 떨어뜨린 것 하나. **세지 않으면 초안이 조용히 얇아진다.**"""

    field: str
    kind: GuardKind
    detail: str


class DraftCallUsage(BaseModel):
    """초안 전용 원장. ``CallUsage`` 를 재사용하지 않는 이유는 모듈 머리에 적었다."""

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


class SellerDraftResponse(BaseModel):
    """AI -> 셀러센터 BE.

    ``draft`` 가 그대로 ``ProductDraftSaveRequest`` 로 넘어가고, 나머지는 **왜 그렇게
    채웠는지와 왜 비었는지**다. 그 둘을 빼면 셀러 화면이 빈 필드를 "제안할 것이 없었다"로
    읽는데, 호출이 죽은 것과 모델이 제안하지 않은 것은 뜻이 정반대다.
    """

    requestId: str
    draftVersion: str
    state: DraftState = DraftState.OK
    itemSignal: ItemSignalOut
    image: ImageReport = Field(default_factory=ImageReport)
    imageUsability: ImageUsability = ImageUsability.NO_IMAGE_SENT
    imageMode: ImageMode = ImageMode.OFF
    draft: ProductDraftSaveRequest = Field(default_factory=ProductDraftSaveRequest)
    basis: dict[str, Basis] = Field(default_factory=dict)
    fieldStatus: dict[str, FieldStatus] = Field(default_factory=dict)
    guardDrops: list[GuardDrop] = Field(default_factory=list)
    sellerNotice: str | None = None
    modelCalled: bool = False
    skipReason: SkipReason | None = None
    reusedFromCache: bool = False
    usage: list[DraftCallUsage] = Field(default_factory=list)
    failureDetail: str | None = None
    generatedAt: datetime
    schemaVersion: int = DRAFT_SCHEMA_VERSION

    @property
    def filled_fields(self) -> list[str]:
        return [name for name, status in self.fieldStatus.items() if status is FieldStatus.FILLED]

    def total_cost_micro_usd(self) -> int:
        return sum(call.costMicroUsd for call in self.usage)


__all__ = [
    "DRAFT_FIELDS",
    "DRAFT_SCHEMA_VERSION",
    "Basis",
    "DraftCallUsage",
    "DraftImage",
    "DraftState",
    "FieldStatus",
    "GuardDrop",
    "GuardKind",
    "ImageMode",
    "ImageReport",
    "ImageUsability",
    "ItemSignalOut",
    "LockedFields",
    "OptionCombinationDraft",
    "OptionGroupDraft",
    "ProductDraftSaveRequest",
    "SellerDraftRequest",
    "SellerDraftResponse",
    "SkipReason",
]
