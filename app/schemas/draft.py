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

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.seller_draft.catalog import ItemGrade, ResolvedBy

DRAFT_SCHEMA_VERSION = 1

# 잠근 옵션 상한의 **계약 사본**. 정본은 ``app/seller_draft/guard.py`` 의 같은 이름 상수다.
#
# 거기서 import 하지 않는 이유는 순환이다 -- ``guard`` 가 이 모듈의 ``OptionGroupDraft`` 를
# 쓴다. 사본을 두고 **표류를 테스트가 막는다**(``tests/test_draft_guard.py``).
# 가드 상한을 올리면서 계약을 빠뜨리면 ``build_combinations`` 가 다시 무제한 입력을 받는다.
#
# **제안 상한(그룹 2 / 값 6)이 아니라 등록 상한(그룹 5 / 값 50)이다.** 제안 상한을 걸면
# 셀러의 3그룹 옵션이 역직렬화에서 거절되는데 그것은 등록이 허용하는 값이다.
_LOCKED_MAX_OPTION_GROUPS = 5
_LOCKED_MAX_VALUES_PER_GROUP = 50
_OPTION_NAME_MAX = 100


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
    - ``EMPTY_NAME_CLAIM_BLOCKED``: **입력 상품명**에 효능 단정이 있어 아무것도 시도하지
      않았다. 위와 같은 방향이고 사유만 다르다 -- 0단 규칙은 연락처·봉투탈출·금지성분까지만
      보고 효능은 **하나도** 보지 않으므로, 둘을 한 값으로 접으면 셀러에게 "상품명의 무엇을
      고쳐야 하는지"를 말할 수 없다.
    """

    FILLED = "FILLED"
    KEPT_LOCKED = "KEPT_LOCKED"
    EMPTY_NOT_ATTEMPTED = "EMPTY_NOT_ATTEMPTED"
    EMPTY_ITEM_UNIDENTIFIED = "EMPTY_ITEM_UNIDENTIFIED"
    EMPTY_NAME_RULE_BLOCKED = "EMPTY_NAME_RULE_BLOCKED"
    EMPTY_NAME_CLAIM_BLOCKED = "EMPTY_NAME_CLAIM_BLOCKED"
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
    # **상품명에 효능 단정이 있는 건.** 0단 규칙(``rules.screen``)은 효능을 하나도 잡지
    # 않으므로 이 분기가 없으면 '관절염 치료 목줄' 에 완성 초안을 만들어 주고, 셀러가 그것을
    # 저장하면 등록 검수가 **상품명으로** 막는다 -- 초안이 셀러를 함정에 빠뜨리는 자리다.
    NAME_CLAIM_BLOCKED = "NAME_CLAIM_BLOCKED"
    ITEM_UNIDENTIFIED = "ITEM_UNIDENTIFIED"
    DRAFT_DISABLED = "DRAFT_DISABLED"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    REUSED_FROM_CACHE = "REUSED_FROM_CACHE"


class DraftState(StrEnum):
    """``OK`` 는 **"호출이 정상 종료했다"이고 "초안이 찼다"가 아니다.**

    건너뛴 건(``skipReason``)도 ``OK`` 로 나간다. 키가 없는 배포에서는 정상 상품명도
    ``skipReason=LLM_UNAVAILABLE`` + 12필드 전부 ``None`` + ``basis`` 전부 ``NONE`` 인
    응답이 ``OK`` 다(실측). ``FAILED`` 는 모델을 불러 놓고 결과를 쓸 수 없었던 경우뿐이다.

    **그래서 소비자가 ``state`` 만 보고 분기하면 빈 초안을 성공으로 읽는다.**
    ``skipReason``/``fieldStatus`` 를 함께 읽어야 "제안할 것이 없었다"와 "부를 수 없었다"가
    갈린다 -- 그 둘은 셀러 화면에서 뜻이 정반대다.
    """

    OK = "OK"
    FAILED = "FAILED"


class DraftMode(StrEnum):
    """초안을 **어떻게** 채우는가. 셀러가 쓴 값을 어떻게 다루는지만 가른다.

    - ``FILL``: 빈 칸만 채운다. 셀러가 쓴 설명은 입력으로만 주고 그대로 돌려준다.
    - ``REFINE``: 셀러 원문을 **다시 쓸 대상으로** 모델에 주고 다시 쓴 결과를 쓴다.

    생략하면 ``FILL`` 이다 -- 모드를 모르는 기존 호출자가 깨지지 않아야 한다.

    **``REFINE`` 이 가드를 우회하지 않는다.** 다시 쓴 문구도 0단 규칙·금지 성분·효능 사전을
    그대로 지나고, 걸리면 셀러 원문으로 되돌린다. 초안이 만든 문구가 등록 검수에서 막히면
    셀러가 두 번 일한다.
    """

    FILL = "FILL"
    REFINE = "REFINE"


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

    선언 치수는 **받기만 하고 읽지 않는다.** 예산은 매직 바이트로 읽은 실측 치수로만
    계산한다(``imagemeta.parse``). 셀러센터가 이미 보내는 필드라 계약에서 빼지 않았다.
    """

    model_config = ConfigDict(extra="ignore")

    mediaType: str = "image/jpeg"
    base64: str = ""
    declaredWidthPx: int = 0
    declaredHeightPx: int = 0


class OptionGroupDraft(BaseModel):
    """옵션 그룹 하나. **상한을 걸지 않는다.**

    이 타입은 모델 출력과 셀러의 잠근 값이 **함께 쓰는 모양**이다. 여기에 상한을 걸면
    모델이 값 7개를 돌려줄 때 역직렬화가 터지고, ``guard`` 가 사유와 함께 우아하게
    거부하도록 만든 설계가 무력해진다 -- 가드의 존재 이유가 "모델은 지시를 어긴다"다.
    상한은 신뢰할 수 없는 **요청 본문** 쪽, 즉 ``LockedFields`` 에 건다.
    """

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
    optionGroups: list[OptionGroupDraft] | None = Field(default=None, max_length=_LOCKED_MAX_OPTION_GROUPS)

    @field_validator("optionGroups")
    @classmethod
    def _bound_option_groups(cls, groups: list[OptionGroupDraft] | None) -> list[OptionGroupDraft] | None:
        """**값 수와 이름 길이를 역직렬화에서 막는다.**

        상한이 없으면 ``guard.build_combinations`` 가 무제한 입력을 받는다. 4그룹 x 20값이면
        160,000개 객체를 만들고, 백엔드 DTO 상한만큼(그룹 5 / 그룹당 값 50) 보내면
        50^5 = 312,500,000 조합이라 셀러가 기다리는 **동기 엔드포인트가 OOM 으로 죽는다.**
        가드가 곱을 세기 전에 거부하도록 고쳤지만 방어를 그 한 겹에 걸지 않는다.

        ``OptionGroupDraft`` 자체에 상한을 걸지 않는 이유는 그 타입이 **모델 출력과 함께
        쓰는 모양**이기 때문이다 -- 거기 걸면 모델이 값 7개를 돌려줄 때 역직렬화가 터지고,
        가드가 사유와 함께 우아하게 거부하도록 만든 설계가 무력해진다.

        수는 **백엔드 DTO 의 것**이다(``ProductDraftSaveRequest``). 초안의 제안 상한
        (그룹 2 / 값 6)을 쓰면 셀러의 3그룹 옵션이 역직렬화에서 거절되는데, 그것은 등록이
        허용하는 값이라 거절할 근거가 없다.
        """
        if groups is None:
            return None
        for group in groups:
            if len(group.values) > _LOCKED_MAX_VALUES_PER_GROUP:
                raise ValueError(f"잠근 옵션의 값은 그룹당 {_LOCKED_MAX_VALUES_PER_GROUP}개 이하여야 한다")
            if len(group.name) > _OPTION_NAME_MAX or any(
                len(value) > _OPTION_NAME_MAX for value in group.values
            ):
                raise ValueError(f"잠근 옵션의 이름은 {_OPTION_NAME_MAX}자 이하여야 한다")
        return groups


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
    # 셀러가 쓴 설명을 그대로 둘지(``FILL``) 다시 쓸지(``REFINE``). 생략하면 ``FILL`` 이다.
    mode: DraftMode = DraftMode.FILL
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
    "DraftMode",
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
