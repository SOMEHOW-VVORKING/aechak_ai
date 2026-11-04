"""관점 정의와 툴 스키마.

분류 11종을 한 프롬프트에 몰아넣지 않는다. 근거는 둘이다.

첫째, 긴 지시 목록에서 개별 규칙의 준수율이 떨어진다. 지시 하나당 95%를 지켜도
열 개를 동시에 지킬 확률은 그보다 훨씬 낮아진다.

둘째가 더 중요하다. **관점 단위로 부분 실패를 추적할 수 있다.** 한 관점이 레이트 리밋으로
돌지 못했을 때 그것을 "문제 없음"이 아니라 "미검사"로 기록해야 하고, 단일 호출에서는
그 구분이 불가능하다.

**관점을 분류 개수로 쪼개지 않고 참조 자료와 판단 양식으로 쪼갠다.** 라벨 이름만 다른
근친 복제 프롬프트를 네 개 두면 실패 지점과 꼬리 지연만 늘어난다. 표시·광고는 정책 전문을
읽어 의미를 판단하고, 행위·거래는 구조와 행위를 본다.

성분 안전은 **표시·광고 관점이 함께 본다.** 0단 사전이 네거티브 리스트로 대부분을 잡지만
그 사전이 스스로 적어 둔 사각이 있다 -- 자모 분리, 한영 혼용, 의미를 비틀어 지칭하는 우회가
정규화로 모이지 않는다. 그 사각의 수신자가 모델 단계라고 ``banned.py``가 약속하는데, 관점
열거에서 ``BANNED_INGREDIENT``를 빼면 모델이 그것을 **보고할 수단 자체가 없어** 사전이 놓친
금지 성분이 어느 계층도 거치지 않고 통과한다. 중복 보고는 질문이 막는다.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.inspection import Perspective, ViolationCategory


@dataclass(frozen=True, slots=True)
class PerspectiveSpec:
    perspective: Perspective
    categories: tuple[ViolationCategory, ...]
    question: str


# 상품과 리뷰가 같이 쓴다. 리뷰도 효능을 주장할 수 있어서
# ("이거 먹고 우리 아이 관절염 나았어요") 리뷰에서 이 관점을 빼지 않는다.
_DISPLAY_ADS = PerspectiveSpec(
    perspective=Perspective.DISPLAY_ADS,
    categories=(
        ViolationCategory.MEDICAL_CLAIM,
        ViolationCategory.EFFICACY_OVERCLAIM,
        ViolationCategory.FALSE_CLAIM,
        ViolationCategory.DISPARAGEMENT,
        ViolationCategory.BANNED_INGREDIENT,
    ),
    question=(
        "당신의 관점은 **표시와 광고**입니다. 위 정책의 MEDICAL_CLAIM, EFFICACY_OVERCLAIM, "
        "FALSE_CLAIM, DISPARAGEMENT, BANNED_INGREDIENT 다섯 분류만 보십시오. 효능을 어디까지 "
        "말할 수 있는지가 판단의 핵심이고, 도움을 줄 수 있다는 표현과 치료한다는 단정을 가르는 "
        "것이 당신의 일입니다.\n"
        "BANNED_INGREDIENT 는 앞 단계의 사전이 이미 대부분을 차단했습니다. **사전이 차단한 것을 "
        "중복 보고하지 마십시오.** 당신이 볼 것은 사전이 글자 모양으로 잡지 못하는 것 하나입니다 -- "
        "성분명을 비틀어 지칭하거나 한글과 영문을 섞어 적은 표기, 그리고 섭취용 제품인지 "
        "위생용품인지를 카테고리와 함께 봐야 가려지는 경우입니다."
    ),
)

_CONDUCT_TRADE = PerspectiveSpec(
    perspective=Perspective.CONDUCT_TRADE,
    categories=(
        ViolationCategory.CONTACT_INFO,
        ViolationCategory.OFF_PLATFORM,
        ViolationCategory.SPAM,
        ViolationCategory.ABUSE,
        ViolationCategory.PII,
        ViolationCategory.IRRELEVANT,
    ),
    question=(
        "당신의 관점은 **행위와 거래**입니다. 위 정책의 CONTACT_INFO, OFF_PLATFORM, SPAM, "
        "ABUSE, PII, IRRELEVANT 여섯 분류만 보십시오. 효능 표현은 보지 마십시오. "
        "명백한 연락처는 이미 규칙이 가렸으므로, 당신은 우회 표기와 맥락상의 유도를 찾으십시오."
    ),
)

_SINGLE = PerspectiveSpec(
    perspective=Perspective.ALL,
    categories=tuple(ViolationCategory),
    question=(
        "분류 전체를 보십시오. 세 축을 모두 봐야 합니다.\n"
        "**표시와 광고**: 효능을 어디까지 말할 수 있는지가 판단의 핵심이고, 도움을 줄 수 "
        "있다는 표현과 치료한다는 단정을 가르는 것이 당신의 일입니다.\n"
        "**행위와 거래**: 명백한 연락처는 이미 규칙이 가렸으므로, 당신은 우회 표기와 "
        "맥락상의 유도를 찾으십시오. 다른 경로로 문의하거나 거래하라는 안내가 그것입니다.\n"
        "**금지 성분**: 앞 단계의 사전이 이미 대부분을 차단했으므로 사전이 차단한 것을 "
        "중복 보고하지 말고, 사전이 글자 모양으로 잡지 못하는 표기와 카테고리를 함께 봐야 "
        "가려지는 경우만 보십시오."
    ),
)

_FANOUT: tuple[PerspectiveSpec, ...] = (_DISPLAY_ADS, _CONDUCT_TRADE)


def specs_for(*, fanout: bool) -> tuple[PerspectiveSpec, ...]:
    """팬아웃을 끄면 단일 판정으로 되돌린다. 기준선과 비교하기 위한 스위치다.

    대상 종류로 갈라지지 않는다. 상품과 리뷰가 같은 두 관점을 쓰고, 종류별 표를 두면
    한쪽만 고쳐도 테스트가 그대로 녹색인 자리가 생긴다. ``TargetType`` 은 요청 계약과
    프롬프트의 ``<대상>`` 줄이 쓰는 값으로 남는다.
    """
    if not fanout:
        return (_SINGLE,)
    return _FANOUT


def findings_tool(spec: PerspectiveSpec) -> dict[str, object]:
    """``strict`` 툴로 구조화 출력을 강제한다. 본문 텍스트를 파싱하지 않는다.

    열거에 반려가 없다. 모델이 낼 수 있는 최대치는 위반 보고이고, 상태를 바꾸는 것은 코드다.
    """
    return {
        "name": "report_findings",
        "description": "검수 결과를 보고한다. 위반이 없으면 findings를 빈 배열로 둔다.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["findings"],
            "properties": {
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["category", "quote", "reason", "suggestion"],
                        "properties": {
                            "category": {
                                "type": "string",
                                "enum": [c.value for c in spec.categories],
                            },
                            "quote": {
                                "type": "string",
                                "description": "검수 대상 텍스트에 실제로 있는 연속된 문자열",
                            },
                            "reason": {"type": "string"},
                            "suggestion": {
                                "type": ["string", "null"],
                                "description": "상품이면 정책에 맞게 바꾼 문구. 리뷰는 null",
                            },
                        },
                    },
                },
            },
        },
    }


REWRITE_TOOL: dict[str, object] = {
    "name": "propose_rewrite",
    "description": "정책을 지키면서 같은 뜻을 전하는 문구를 제안한다.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["sellerNotice", "rewrites"],
        "properties": {
            "sellerNotice": {
                "type": "string",
                "description": "셀러에게 보여 줄 한두 문장. 무엇을 왜 고쳐야 하는지",
            },
            "rewrites": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["original", "suggestion"],
                    "properties": {
                        "original": {"type": "string"},
                        "suggestion": {"type": "string"},
                    },
                },
            },
        },
    },
}
