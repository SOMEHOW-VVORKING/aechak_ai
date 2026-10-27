"""추출 툴 스키마. **상수다 — 요청 데이터로 바뀌지 않는다.**

렌더 순서가 ``tools -> system -> messages`` 이고 ``cache_control`` 이 마지막 system 블록에
붙으므로 **tools 가 접두부에 함께 캐시된다.** ``findings_tool(spec)`` 이 관점마다 다른
분류 enum 을 실어 캐시 엔트리가 관점별로 갈라져 있는 것이 그 실례다. 그래서 항목 코드
열거를 카테고리와 무관한 고정 18종으로 두었다 -- 카테고리마다 enum 이 달라지면 캐시 쓰기가
카테고리 수만큼 난다.

``strict`` 모드의 제약 둘을 코드로 우회한다.

1. **문자열 길이 제약(``minLength``/``maxLength``)을 받지 않는다.** ``TRIAGE_TOOL`` 주석이
   같은 사실을 적어 뒀다. 인용 80자 상한은 프롬프트로 누르고 ``evidence.py`` 가 사후
   검사하며 ``max_tokens`` 가 백스톱이다.
2. **nullable 을 쓰지 않는다.** ``{"type": ["object", "null"]}`` 가 받아들여지는지 확신할 수
   없고 전건 400 이 ``SCHEMA_INVALID`` 로 숨는다. 그래서 펫 언급을 ``present: boolean`` 을
   든 객체로 받고, 미기재 수치는 0 으로 싣는다. ``evidence._verify_pet`` 이 0 을 "0kg"으로
   읽지 않고 미기재로 접는다 -- 그 변환이 한 자리에 있는 것이 중요하다.
"""

from __future__ import annotations

from typing import Any

from app.review_summary.aspects import aspect_enum
from app.schemas.summary import Polarity

EXTRACT_TOOL_NAME = "report_review_aspects"

EXTRACT_TOOL: dict[str, Any] = {
    "name": EXTRACT_TOOL_NAME,
    "description": "리뷰마다 언급한 항목과 극성, 그 근거 인용을 돌려준다. 요약 문장을 쓰지 않는다.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["reviews"],
        "properties": {
            "reviews": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["reviewId", "aspects", "petMention"],
                    "properties": {
                        "reviewId": {
                            "type": "integer",
                            "description": "머리줄에 적힌 리뷰 번호. 받지 않은 번호를 쓰면 그 줄은 버려진다.",
                        },
                        "aspects": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["aspectCode", "aspectLabel", "polarity", "quote"],
                                "properties": {
                                    "aspectCode": {"type": "string", "enum": aspect_enum()},
                                    "aspectLabel": {
                                        "type": "string",
                                        "description": "무엇을 말했는지 한 구절로. 예: 연어 냄새가 강하다",
                                    },
                                    "polarity": {
                                        "type": "string",
                                        "enum": [p.value for p in Polarity],
                                    },
                                    "quote": {
                                        "type": "string",
                                        "description": "그 리뷰 본문에 있는 연속 문자열. 80자 이내.",
                                    },
                                },
                            },
                        },
                        "petMention": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": [
                                "present",
                                "species",
                                "breedLabel",
                                "weightKg",
                                "ageYears",
                                "quote",
                            ],
                            "properties": {
                                "present": {
                                    "type": "boolean",
                                    "description": "본문에 반려동물 정보가 적혀 있는가. 없으면 false.",
                                },
                                "species": {"type": "string", "enum": ["DOG", "CAT", ""]},
                                "breedLabel": {
                                    "type": "string",
                                    "description": "본문에 적힌 품종 표기. 없으면 빈 문자열.",
                                },
                                "weightKg": {
                                    "type": "number",
                                    "description": "본문에 적힌 체중. 적혀 있지 않으면 0. 추측하지 않는다.",
                                },
                                "ageYears": {
                                    "type": "integer",
                                    "description": "본문에 적힌 나이(년). 적혀 있지 않으면 0.",
                                },
                                "quote": {
                                    "type": "string",
                                    "description": "그 정보가 적힌 본문 구간. present 가 true 면 필수.",
                                },
                            },
                        },
                    },
                },
            }
        },
    },
}
