"""초안 시나리오와 손으로 쓴 카세트. **모델 없이 돌고 비용이 0이다.**

``demo/cassettes.py`` 와 ``demo/summary_cassettes.py`` 의 규약을 지킨다.

- **시나리오가 파이프라인 모양 전부를 든다.** 환경변수를 한 칸도 읽지 않는다 -- 임계나
  이미지 모드가 앰비언트 환경에서 들어오면 같은 시나리오가 셸에 따라 다른 경로를 그린다.
- **캐시 읽기를 0으로 둔다.** 기본 모델 Haiku 4.5 의 최소 캐시 접두부가 4,096토큰이라 초안
  접두부는 캐시되지 않는다. 원장에 캐시 적중을 꾸며 넣으면 콘솔이 근거를 거짓으로 보여 준다.
- **적어 둔 경로로 돌지 않으면 그림을 내보내지 않는다.** ``expected_exit`` 가 그 장치다.
- **재사용 캐시를 끈다**(``draft_cache_entries=0``). 켜 두면 같은 시나리오를 두 번 돌릴 때
  두 번째가 재사용으로 그려져 발표 중에 단계가 사라진다.

## 사진을 어떻게 넣는가 — 합성 헤더

``imagemeta`` 는 **헤더만** 읽고 픽셀을 디코드하지 않는다(그래서 Pillow 가 필요 없다).
그 덕에 재생 시나리오는 **서명 + IHDR + 패딩**으로 만든 PNG 로 실제 헤더 파싱 경로를
그대로 태울 수 있다. 패딩을 두는 이유가 있다 -- 패딩이 없으면 ``bytes/pixel`` 이 극단적으로
낮아 전건이 ``IMAGE_LOW_FIDELITY`` 힌트를 달고, 콘솔이 "사진이 전부 흐리다"고 그린다.

합성 헤더로 **보여 줄 수 없는 것**을 적어 둔다: 모델이 실제 사진을 읽는 능력. 그것은 키가
들어온 뒤 하네스의 실제 픽스처로만 측정된다. 여기서 보여 주는 것은 **분기와 비용 구조**다.
"""

from __future__ import annotations

import base64
import struct
import zlib
from dataclasses import dataclass, field
from typing import Any

from app.schemas.draft import (
    DraftImage,
    ImageMode,
    LockedFields,
    OptionGroupDraft,
    SellerDraftRequest,
)
from app.schemas.inspection import CallOutcome
from app.seller_draft.tool import DRAFT_TOOL_NAME, LEAF_DECLINED
from app.settings import Settings
from demo.draft_view import DraftExit
from demo.replay import ScriptedCall, SequencedCallKey

# 합성 PNG 의 바이트/픽셀. 실제 JPEG 사진의 흔한 값 근처에 둔다 -- 너무 낮으면 전건이
# 저품질 힌트를 달고, 너무 높으면 요청 본문이 쓸데없이 커진다.
_SYNTHETIC_BYTES_PER_PIXEL = 0.12


def synthetic_png(width: int, height: int) -> bytes:
    """구조적으로 유효한 PNG 헤더 + 패딩. **픽셀은 의미가 없다.**

    ``imagemeta`` 가 서명과 IHDR 만 읽으므로 이것으로 치수·토큰 예산·저품질 판정 경로가
    전부 실제로 돈다. 패딩은 ``tEXt`` 청크에 담는다 -- 표준 청크라 다른 도구로 열어도
    깨지지 않고, 길이만 늘린다.
    """

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    pad_len = max(0, int(width * height * _SYNTHETIC_BYTES_PER_PIXEL) - 128)
    padding = chunk(b"tEXt", b"Comment\x00" + b"d" * pad_len)
    idat = chunk(b"IDAT", zlib.compress(b"\x00" * (width * 3 + 1)))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + padding + idat + chunk(b"IEND", b"")


def draft_image(width: int = 896, height: int = 896) -> DraftImage:
    data = synthetic_png(width, height)
    return DraftImage(
        mediaType="image/png",
        base64=base64.b64encode(data).decode("ascii"),
        declaredWidthPx=width,
        declaredHeightPx=height,
    )


def _slots(item: str = "", spec: str = "", usage: str = "", care: str = "") -> dict[str, str]:
    return {
        "itemSummary": item,
        "materialOrSpec": spec,
        "usage": usage,
        "careAndStorage": care,
    }


def _basis(category: str = "NAME", description: str = "NAME", options: str = "NAME") -> dict[str, str]:
    return {"categoryId": category, "description": description, "optionGroups": options}


def _tool_input(
    *,
    leaf: int,
    slots: dict[str, str],
    groups: list[dict[str, Any]] | None = None,
    basis: dict[str, str] | None = None,
    usability: str = "NO_IMAGE_SENT",
    notice: str = "",
) -> dict[str, Any]:
    return {
        "leafCategoryId": leaf,
        "descriptionParts": slots,
        "optionGroups": groups or [],
        "basis": basis or _basis(),
        "imageUsability": usability,
        "sellerNotice": notice,
    }


def _call(tool_input: dict[str, Any] | None, **overrides: Any) -> ScriptedCall:
    """토큰 수를 들고 있어야 원장의 비용이 **운영 단가 함수**로 계산된다."""
    base: dict[str, Any] = {
        "tool_input": tool_input,
        "input_tokens": 2_340,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 450,
        "latency_ms": 1_900,
    }
    base.update(overrides)
    return ScriptedCall(**base)


_KEY: SequencedCallKey = (DRAFT_TOOL_NAME, 0)


@dataclass(frozen=True, slots=True)
class DraftScenario:
    id: str
    title: str
    note: str
    product_name: str
    expected_exit: str
    category_id: int | None = None
    locked: LockedFields | None = None
    llm_disabled: bool = False
    # 텍스트 변형(가)의 카세트.
    entries: dict[SequencedCallKey, ScriptedCall] = field(default_factory=dict)
    # 이미지 변형(나)의 카세트. 없으면 그 열을 "없음"으로 그린다.
    image_entries: dict[SequencedCallKey, ScriptedCall] | None = None
    image_px: int = 896
    expected_image_exit: str | None = None

    def settings(self, *, with_image: bool) -> Settings:
        """**리터럴에서 조립한다.** 환경변수를 한 칸도 읽지 않는다.

        키도 비운다 -- 재생은 네트워크를 타지 않으므로 키가 필요 없고, 있으면 실수로
        유료 경로를 밟을 여지가 생긴다.
        """
        return Settings(
            anthropic_api_key="",
            enable_draft=True,
            draft_model="claude-haiku-4-5",
            draft_effort=None,
            max_tokens_draft=2048,
            draft_timeout_seconds=25.0,
            draft_description_chars=400,
            draft_image_mode=(ImageMode.ALWAYS if with_image else ImageMode.OFF).value,
            draft_image_max_tokens=1400,
            draft_cache_entries=0,
        )

    def to_request(self, *, with_image: bool) -> SellerDraftRequest:
        return SellerDraftRequest(
            requestId=f"demo-{self.id}" + ("-image" if with_image else "-text"),
            productName=self.product_name,
            categoryId=self.category_id,
            lockedFields=self.locked,
            image=draft_image(self.image_px, self.image_px) if with_image else None,
        )


DRAFT_SCENARIOS: tuple[DraftScenario, ...] = (
    DraftScenario(
        id="draft-pinned-text",
        title="품목 확정 — 텍스트만",
        note="상품명에 '목줄' 이 드러나 0원 게이트가 리프를 확정한다. 모델은 설명과 옵션만 채운다",
        product_name="강아지 목줄 M 네이비",
        expected_exit=DraftExit.FILLED.value,
        entries={
            _KEY: _call(
                _tool_input(
                    leaf=152,
                    slots=_slots(
                        item="중소형견용 목줄입니다. 가슴줄이 아닌 목에 채우는 형태입니다.",
                        spec="나일론 웨빙에 플라스틱 버클, 목둘레 30~45cm 조절 가능.",
                        usage="목둘레에 손가락 두 개가 들어갈 정도로 조절해 채워 주세요.",
                        care="오염 시 미온수에 손세탁 후 그늘에서 말려 주세요.",
                    ),
                    groups=[
                        {"name": "사이즈", "values": ["S", "M", "L"]},
                        {"name": "색상", "values": ["네이비", "블랙", "레드"]},
                    ],
                )
            )
        },
        image_entries={
            _KEY: _call(
                _tool_input(
                    leaf=152,
                    slots=_slots(
                        item="중소형견용 목줄입니다. 사진에서 버클형 잠금이 보입니다.",
                        spec="나일론 웨빙에 플라스틱 사이드릴리즈 버클, 폭 약 2cm.",
                        usage="목둘레에 손가락 두 개가 들어갈 정도로 조절해 채워 주세요.",
                        care="오염 시 미온수에 손세탁 후 그늘에서 말려 주세요.",
                    ),
                    groups=[
                        {"name": "사이즈", "values": ["S", "M", "L"]},
                        {"name": "색상", "values": ["네이비", "블랙", "레드"]},
                    ],
                    basis=_basis(category="NAME", description="IMAGE", options="IMAGE"),
                    usability="USED",
                ),
                input_tokens=3_364,
                output_tokens=470,
                latency_ms=2_400,
            )
        },
        expected_image_exit=DraftExit.FILLED.value,
    ),
    DraftScenario(
        id="draft-opaque-no-call",
        title="품목 미식별 — 0원",
        note="상품명도 카테고리도 품목을 집지 못한다. **모델을 0회 부르고 일반론을 쓰지 않는다**",
        product_name="모모의 하루",
        expected_exit=DraftExit.ITEM_UNIDENTIFIED.value,
        entries={},
        image_entries={},
        expected_image_exit=DraftExit.ITEM_UNIDENTIFIED.value,
    ),
    DraftScenario(
        id="draft-name-rule-blocked",
        title="상품명 차단 — 0원",
        note="상품명에 외부 메신저 유도가 있다. 검수에 걸릴 이름으로 초안을 만들어 주지 않는다",
        product_name="구매문의 카톡 petshop99 강아지 목줄",
        expected_exit=DraftExit.NAME_RULE_BLOCKED.value,
        entries={},
    ),
    DraftScenario(
        id="draft-name-claim-blocked",
        title="상품명 효능 단정 — 0원",
        note="0단 룰은 효능을 **하나도** 잡지 않는다. 그 겹이 없으면 이 이름에 완성 초안을 "
        "만들어 주고 셀러가 저장하면 등록 검수가 상품명으로 막는다",
        product_name="관절염 치료 목줄",
        expected_exit=DraftExit.NAME_CLAIM_BLOCKED.value,
        entries={},
    ),
    DraftScenario(
        id="draft-claim-dropped",
        title="효능 단정 — 그 슬롯만 비운다",
        note="0단 룰이 **하나도 잡지 못하는** 축이다. 전용 효능 사전이 itemSummary 만 떨어뜨리고 "
        "나머지 슬롯을 살린다",
        product_name="강아지 관절 영양제 글루코사민",
        expected_exit=DraftExit.FILLED.value,
        entries={
            _KEY: _call(
                _tool_input(
                    leaf=132,
                    slots=_slots(
                        item="관절염 치료에 효과가 있는 영양제입니다. 2주 만에 완치됩니다.",
                        spec="글루코사민 500mg, MSM 200mg 함유. 60정 1통.",
                        usage="체중 10kg 기준 1일 1정을 사료에 섞어 급여하세요.",
                        care="직사광선을 피해 실온 보관하고 개봉 후 3개월 내 급여하세요.",
                    ),
                    groups=[{"name": "중량", "values": ["60정", "120정"]}],
                )
            )
        },
    ),
    DraftScenario(
        id="draft-banned-dropped",
        title="금지 성분 — 그 슬롯만 비운다",
        note="모델이 자일리톨을 설명에 써 넣었다. 셀러가 그대로 저장하면 상품이 차단된다",
        product_name="강아지 수제간식 고구마 말랭이",
        expected_exit=DraftExit.FILLED.value,
        entries={
            _KEY: _call(
                _tool_input(
                    leaf=122,
                    slots=_slots(
                        item="국내산 고구마를 저온 건조한 수제 간식입니다.",
                        spec="고구마 98%, 자일리톨 2% 함유. 100g 포장.",
                        usage="하루 2~3개씩 간식으로 급여하세요.",
                        care="개봉 후 냉장 보관하고 2주 내 급여하세요.",
                    ),
                    groups=[{"name": "중량", "values": ["100g", "300g"]}],
                )
            )
        },
    ),
    DraftScenario(
        id="draft-option-rule",
        title="옵션 불변식 위반 — 전체를 비운다",
        note="색상의 '블랙' 과 패턴의 '블랙' 이 겹친다. **모델이 자연스럽게 위반하는 규칙**이고 "
        "그대로 저장하면 등록이 거부된다",
        product_name="강아지 아우터 패딩 조끼 M",
        expected_exit=DraftExit.FILLED.value,
        entries={
            _KEY: _call(
                _tool_input(
                    leaf=161,
                    slots=_slots(
                        item="소형견용 패딩 조끼입니다. 등판에 리드줄 고리가 있습니다.",
                        spec="겉감 폴리에스터, 충전재 폴리 솜. 등길이 25~35cm.",
                        care="30도 이하 물에 손세탁하고 건조기를 쓰지 마세요.",
                    ),
                    groups=[
                        {"name": "색상", "values": ["블랙", "베이지"]},
                        {"name": "패턴", "values": ["블랙", "체크"]},
                    ],
                )
            )
        },
    ),
    DraftScenario(
        id="draft-basis-mismatch",
        title="사진을 못 봤다면서 사진 근거를 적었다",
        note="**코드가 그 필드를 폐기한다.** '못 봤는데 봤다고 적은' 상태가 응답에 남지 않는다",
        product_name="고양이 장난감 낚싯대",
        category_id=2,
        expected_exit=DraftExit.FILLED.value,
        entries={
            _KEY: _call(
                _tool_input(
                    leaf=241,
                    slots=_slots(
                        item="고양이용 낚싯대 장난감입니다.",
                        spec="플라스틱 손잡이에 깃털 토이가 달린 형태.",
                    ),
                    groups=[{"name": "색상", "values": ["옐로우", "핑크"]}],
                )
            )
        },
        image_entries={
            _KEY: _call(
                _tool_input(
                    leaf=241,
                    slots=_slots(
                        item="사진에서 깃털 토이가 보입니다.",
                        spec="플라스틱 손잡이에 깃털, 길이 약 45cm.",
                    ),
                    groups=[{"name": "색상", "values": ["옐로우", "핑크"]}],
                    basis=_basis(category="NAME", description="IMAGE", options="IMAGE"),
                    # 사진이 흐려 못 봤다면서 설명·옵션을 사진 근거로 적었다.
                    usability="TOO_DARK_OR_BLURRY",
                ),
                input_tokens=3_364,
            )
        },
        expected_image_exit=DraftExit.FILLED.value,
    ),
    DraftScenario(
        id="draft-category-conflict",
        title="게이트와 모델이 다른 리프를 지목했다",
        note="어느 쪽이 맞는지 모르므로 조용히 고르지 않고 **비우고 셀러가 고르게 한다**",
        product_name="리드줄 2m 자동 리트랙터블",
        expected_exit=DraftExit.CATEGORY_CONFLICT.value,
        entries={
            _KEY: _call(
                _tool_input(
                    # 게이트는 152(목줄/리드줄), 모델은 151(하네스/가슴줄).
                    leaf=151,
                    slots=_slots(item="자동 리드줄입니다.", spec="최대 길이 2m, 중소형견용."),
                    groups=[{"name": "색상", "values": ["블랙", "레드"]}],
                )
            )
        },
    ),
    DraftScenario(
        id="draft-model-declined",
        title="모델이 고르지 못했다고 답했다",
        note="센티넬 0 이다. **'필드를 아예 내지 않은 것'과 뜻이 정반대**이고 설명·옵션도 함께 "
        "비운다 -- 품목을 모르는 채로 쓴 설명은 어느 상품에도 맞지 않는다",
        product_name="프리미엄 장난감 3종",
        expected_exit=DraftExit.GUARD_EMPTIED.value,
        entries={
            _KEY: _call(
                _tool_input(
                    leaf=LEAF_DECLINED,
                    slots=_slots(item="반려동물용 장난감 세트입니다."),
                    groups=[],
                    basis=_basis(category="NONE", description="NONE", options="NONE"),
                    notice="상품명에 어떤 동물용인지 적어 주시면 분류를 채울 수 있습니다.",
                ),
                output_tokens=160,
            )
        },
    ),
    DraftScenario(
        id="draft-call-failed",
        title="호출 실패 — '제안할 것이 없었다'가 아니다",
        note="전 필드가 EMPTY_CALL_FAILED 다. 화면이 '다시 시도' 와 '직접 작성'을 다르게 보여 줘야 한다",
        product_name="강아지 하네스 가슴줄 M",
        expected_exit=DraftExit.CALL_FAILED.value,
        entries={
            _KEY: _call(None, outcome=CallOutcome.RATE_LIMITED, detail="rate limited (unknown)"),
            # 서비스가 조건부로 한 번 더 쏜다. 두 번째도 실패하는 카세트를 적어 둔다.
            (DRAFT_TOOL_NAME, 1): _call(
                None, outcome=CallOutcome.RATE_LIMITED, detail="rate limited (unknown)"
            ),
        },
    ),
    DraftScenario(
        id="draft-wrong-photo",
        title="오사진 — 사진 쪽 초안을 쓰지 않는다",
        note="**이미지 입장의 최악 실패 모양**이 사진 쪽 상품의 초안을 조용히 써 주는 것이다. "
        "MISMATCHES_NAME 을 열거값으로 받아 걸러낸다",
        product_name="코지 블랭킷 라지",
        category_id=1,
        expected_exit=DraftExit.FILLED.value,
        entries={
            _KEY: _call(
                _tool_input(
                    leaf=142,
                    slots=_slots(
                        item="소형견용 담요입니다.",
                        spec="극세사 기모, 100x70cm.",
                        care="찬물에 세탁하고 저온 건조하세요.",
                    ),
                    groups=[{"name": "사이즈", "values": ["M", "L"]}],
                )
            )
        },
        image_entries={
            _KEY: _call(
                _tool_input(
                    leaf=142,
                    slots=_slots(
                        item="소형견용 담요입니다.",
                        spec="극세사 기모, 100x70cm.",
                        care="찬물에 세탁하고 저온 건조하세요.",
                    ),
                    groups=[{"name": "사이즈", "values": ["M", "L"]}],
                    basis=_basis(category="NAME", description="NAME", options="NAME"),
                    usability="MISMATCHES_NAME",
                    notice="사진 속 상품이 상품명과 달라 보입니다. 사진을 확인해 주세요.",
                ),
                input_tokens=3_364,
            )
        },
        expected_image_exit=DraftExit.FILLED.value,
    ),
    DraftScenario(
        id="draft-locked-fields",
        title="셀러가 고친 필드를 잠근다",
        note="고친 값은 그대로 두고 나머지만 채운다. **신뢰할 수 없는 입력이라 봉투 안에 "
        "중성화를 지나 들어간다**",
        product_name="고양이모래 벤토나이트 10L",
        locked=LockedFields(
            description="저희 매장에서 직접 소분합니다. 응고력이 좋아 재구매율이 높습니다.",
            optionGroups=[OptionGroupDraft(name="용량", values=["5L", "10L"])],
        ),
        expected_exit=DraftExit.FILLED.value,
        entries={
            _KEY: _call(
                _tool_input(
                    leaf=251,
                    slots=_slots(item="응고형 벤토나이트 고양이 모래입니다."),
                    groups=[{"name": "향", "values": ["무향", "베이비파우더"]}],
                    notice="용량 옵션을 이미 적어 주셨으므로 향 옵션만 제안했습니다.",
                )
            )
        },
    ),
    DraftScenario(
        id="draft-image-too-large",
        title="사진이 토큰 예산을 넘겼다 — 거부하고 필요 치수를 적어 준다",
        note="**AI 서버는 리사이즈하지 않는다**(의존성 금지). 헤더에서 치수를 읽어 거부하면서 "
        "셀러센터가 줄여야 하는 긴 변을 응답에 적어 돌려준다",
        product_name="강아지 하네스 가슴줄 M",
        expected_exit=DraftExit.FILLED.value,
        entries={
            _KEY: _call(
                _tool_input(
                    leaf=151,
                    slots=_slots(item="중소형견용 가슴줄 하네스입니다.", spec="가슴둘레 35~50cm 조절."),
                    groups=[{"name": "사이즈", "values": ["S", "M", "L"]}],
                )
            )
        },
        image_entries={},
        image_px=1500,
        expected_image_exit=DraftExit.IMAGE_REJECTED.value,
    ),
)

DRAFT_BY_ID: dict[str, DraftScenario] = {scenario.id: scenario for scenario in DRAFT_SCENARIOS}


__all__ = [
    "DRAFT_BY_ID",
    "DRAFT_SCENARIOS",
    "DraftScenario",
    "draft_image",
    "synthetic_png",
]
