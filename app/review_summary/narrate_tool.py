"""서술 툴 스키마. **상수다.**

가장 중요한 설계는 **없는 필드**다.

- ``mentionCount``·``mentionShare`` 가 없다. 코드가 집계표에서 채운다. 모델이 셈을 적을 수
  있으면 "반복된다"의 판정이 다시 모델에게 넘어간다 -- 이 설계가 2단으로 가른 이유가
  그것이다.
- 반려 열거가 없다. 요약은 판정이 아니고 ``Verdict`` 를 내지 않는다.

출력 상한을 스키마로 못박는다(장 3 / 단 3 / 항목당 인용 3). 출력 토큰이 서술 비용의 지배
항이라 이 상한이 곧 비용 상한이다 -- 300토큰 7.42원 / 700토큰 10.22원 / 1,400토큰
15.12원(전부 추정).

``consAbsenceReason`` 에 빈 문자열을 열거에 넣은 것은 ``strict`` 모드에서 nullable 을 쓰지
않기 위한 것이다. 코드가 "단점이 비었으면 사유가 있어야 하고, 단점이 있으면 사유가 비어야
한다"를 사후 검사한다.
"""

from __future__ import annotations

from typing import Any

from app.review_summary.aspects import aspect_enum
from app.schemas.summary import ConsAbsenceReason

NARRATE_TOOL_NAME = "write_review_summary"

PROS_MAX = 3
CONS_MAX = 3
QUOTES_PER_ASPECT_MAX = 3

_QUOTE_ITEM: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reviewId", "text"],
    "properties": {
        "reviewId": {"type": "integer", "description": "집계표에 적힌 리뷰 번호."},
        "text": {"type": "string", "description": "집계표의 인용을 글자 그대로. 고치면 폐기된다."},
    },
}


def _aspect_list(description: str) -> dict[str, Any]:
    return {
        "type": "array",
        "description": description,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["aspectCode", "headline", "quotes"],
            "properties": {
                "aspectCode": {"type": "string", "enum": aspect_enum()},
                "headline": {
                    "type": "string",
                    "description": "보호자가 읽을 한 줄. 60자 이내. 효능·의료 단정 표현을 쓰지 않는다.",
                },
                "quotes": {"type": "array", "items": _QUOTE_ITEM},
            },
        },
    }


NARRATE_TOOL: dict[str, Any] = {
    "name": NARRATE_TOOL_NAME,
    "description": "집계표의 각 줄에 보호자가 읽을 한 줄을 붙인다. 건수는 적지 않는다.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["pros", "cons", "consAbsenceReason"],
        "properties": {
            "pros": _aspect_list(f"집계표의 PRO 줄에서 최대 {PROS_MAX}개."),
            "cons": _aspect_list(f"집계표의 CON 줄에서 최대 {CONS_MAX}개."),
            "consAbsenceReason": {
                "type": "string",
                "enum": [*(r.value for r in ConsAbsenceReason), ""],
                "description": "cons 가 빈 배열일 때만 값을 적는다. cons 가 있으면 빈 문자열.",
            },
        },
    },
}
