"""초안 툴 스키마. **상수다 — 요청 데이터로 바뀌지 않는다.**

## 왜 요청마다 좁히지 않는가

렌더 순서가 ``tools -> system -> messages`` 이고 ``cache_control`` 이 마지막 system 블록에
붙으므로 **tools 가 접두부에 함께 캐시된다.** ``findings_tool(spec)`` 이 관점마다 다른 분류
enum 을 실어 캐시 엔트리가 관점별로 갈라져 있는 것이 그 실례다. 0단이 좁힌 후보 3~5개로
enum 을 요청마다 줄이면 (ㄱ) 중분류 14개만큼 엔트리가 갈라지고 (ㄴ) 더 나쁘게는 **0단이
틀렸을 때 모델이 정답을 고를 수단을 잃어 0단의 구멍이 그대로 최종 오답이 된다.**
좁히는 것은 user content 의 힌트로 한다.

## 왜 라벨이 아니라 정수 id 인가

리프 라벨 6개가 강아지/고양이에 중복이고 50개 중 14개는 자기만의 고유 표면형이 없다(실측).
모델에게 라벨을 내게 하고 서버가 id 로 맵핑하면 **거기서 조용히 틀린다.** 닫힌 정수 열거 +
``strict: true`` 가 열거 밖 값을 API 단에서 막고, 서버는 ``LEAVES`` 로 대조만 한다.

## ``strict`` 모드의 제약 둘을 코드로 우회한다

1. **길이·개수 제약을 받지 않는다**(``minLength``/``maxLength``/``maxItems``).
   ``TRIAGE_TOOL`` 주석이 같은 사실을 적어 뒀다. 길이는 프롬프트로 누르고 ``max_tokens`` 가
   백스톱이며 ``guard.py`` 가 사후 검사한다.
2. **nullable 을 쓰지 않는다.** ``{"type": ["integer","null"]}`` 가 받아들여지는지 확신할 수
   없고 전건 400 이 ``SCHEMA_INVALID`` 로 숨는다 -- ``EXTRACT_TOOL`` 이 같은 이유로 펫 언급을
   ``present: boolean`` + 0 센티넬로 받았다. 그래서 "고르지 못했다"를 ``leafCategoryId: 0``
   으로, 미기재 문자열을 빈 문자열로 받고 **그 변환을 ``guard.py`` 한 자리에서** 한다.
   센티넬 0 은 ``FieldStatus.EMPTY_MODEL_DECLINED`` 로 옮겨진다 -- 모델이 정직하게 답한
   것이고 스키마 고장이 아니다.

## 없는 필드가 설계다

``productName`` 이 **툴 스키마에 없다.** 검수 내용 해시의 입력이라 셀러가 그대로 저장하면
새 검수가 큐에 들어가고(= 과금), 무엇보다 이 요청의 입력이다 -- 입력을 고쳐 돌려주는 것은
월권이다. 안내는 ``sellerNotice`` 가 하고 그것은 폼 필드가 아니라 해시에 닿지 않는다.

``descriptionParts`` 에 **"효능·효과" 슬롯이 없다.** 모델이 효능을 쓸 자리를 아예 주지 않는
것이 프롬프트로 금지하는 것보다 강하다 -- ``perspectives.py`` 의 "열거에 반려가 없다"와
같은 수법이다. ``optionCombinations`` 도 없다. 그룹과 값 이름만 받고 조합은 서버가 만든다.
"""

from __future__ import annotations

from typing import Any

from app.schemas.draft import Basis, ImageUsability
from app.seller_draft.catalog import LEAF_IDS

DRAFT_TOOL_NAME = "draft_product_form"

# "고르지 못했다"의 센티넬. nullable 을 쓰지 않기 위한 값이고 ``guard.py`` 가 None 으로 접는다.
LEAF_DECLINED = 0

# **슬롯 설명이 확인 출처를 열거하지 않는다.** 출처가 모드마다 다르기 때문이다 --
# ``FILL`` 은 상품명·사진이고 ``REFINE`` 은 거기에 셀러 원문이 더해진다. 여기 "사진이나
# 상품명으로 확인되지 않으면 비워라" 를 적어 두면 **툴 스키마가 모드 지시를 이겨** 다시 쓰기
# 요청에서 슬롯이 전멸하고, 서버는 원문으로 되돌려 유료 호출 한 번이 아무 일도 하지 않는다.
# 출처를 말하는 자리는 ``prompt.DRAFT_PREFIX`` 의 지시 1 하나다.
_SLOT_KEYS: tuple[tuple[str, str], ...] = (
    ("itemSummary", "이 상품이 무엇인가. 품목·용도·대상 반려동물 크기대. 모르면 빈 문자열."),
    ("materialOrSpec", "소재·규격·구성. 확인되지 않으면 빈 문자열."),
    ("usage", "사용법 또는 급여법. 확인되지 않으면 빈 문자열."),
    ("careAndStorage", "보관·세탁·주의. 확인되지 않으면 빈 문자열."),
)

_BASIS_ENUM = [b.value for b in Basis]


def _basis_field(what: str) -> dict[str, Any]:
    return {
        "type": "string",
        "enum": _BASIS_ENUM,
        "description": f"{what} 를 무슨 근거로 채웠는가. 채우지 않았으면 NONE.",
    }


DRAFT_TOOL: dict[str, Any] = {
    "name": DRAFT_TOOL_NAME,
    "description": (
        "셀러가 입력한 상품명과 (있으면) 사진·셀러가 쓴 설명으로 상품 등록 폼의 초안을 채운다. "
        "확인할 수 없는 것은 빈 문자열로 두고 근거를 NONE 으로 적는다. 상품명은 고치지 않는다."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "leafCategoryId",
            "descriptionParts",
            "optionGroups",
            "basis",
            "imageUsability",
            "sellerNotice",
        ],
        "properties": {
            "leafCategoryId": {
                "type": "integer",
                "enum": [LEAF_DECLINED, *LEAF_IDS],
                "description": (
                    "소분류 id 하나. 지시문에 적힌 50개 가운데 고른다. "
                    f"어느 것인지 모르면 추측하지 말고 {LEAF_DECLINED} 을 적는다."
                ),
            },
            "descriptionParts": {
                "type": "object",
                "additionalProperties": False,
                "required": [key for key, _desc in _SLOT_KEYS],
                "properties": {key: {"type": "string", "description": desc} for key, desc in _SLOT_KEYS},
            },
            "optionGroups": {
                "type": "array",
                "description": (
                    "제안할 옵션 그룹. 최대 2개, 그룹당 값 최대 6개. "
                    "값 이름은 그룹이 달라도 전체에서 겹치면 안 된다. 제안할 것이 없으면 빈 배열."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["name", "values"],
                    "properties": {
                        "name": {"type": "string", "description": "그룹 이름. 예: 사이즈, 색상, 중량"},
                        "values": {
                            "type": "array",
                            "description": "그 그룹의 값 이름들. 예: S, M, L",
                            "items": {"type": "string"},
                        },
                    },
                },
            },
            "basis": {
                "type": "object",
                "additionalProperties": False,
                "required": ["categoryId", "description", "optionGroups"],
                "properties": {
                    "categoryId": _basis_field("소분류"),
                    "description": _basis_field("설명"),
                    "optionGroups": _basis_field("옵션"),
                },
            },
            "imageUsability": {
                "type": "string",
                "enum": [u.value for u in ImageUsability],
                "description": (
                    "사진이 쓸 만했는가. 사진을 받지 않았으면 NO_IMAGE_SENT. "
                    "사진 속 상품이 상품명과 다르면 MISMATCHES_NAME -- 그때는 사진을 근거로 "
                    "아무 필드도 채우지 마십시오."
                ),
            },
            "sellerNotice": {
                "type": "string",
                "description": (
                    "셀러에게 전할 한두 문장. 품목을 알 수 없었다면 상품명에 어떤 말을 넣으면 "
                    "좋은지 적는다. 전할 말이 없으면 빈 문자열."
                ),
            },
        },
    },
}


__all__ = ["DRAFT_TOOL", "DRAFT_TOOL_NAME", "LEAF_DECLINED"]
