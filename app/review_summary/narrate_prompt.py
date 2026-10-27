"""서술 접두부. **모델이 리뷰 본문을 보지 않는 자리다.**

입력이 집계표와 검증된 짧은 인용뿐이라 비용이 리뷰 수에 무관하고, 주입 표면도 추출 단계
한 곳으로 좁아진다.

**지시를 다섯 개로 둔다.** 그 이상 쌓으면 준수율이 곱으로 떨어진다.

못 막는 축을 정직하게 적어 둔다. 0단 규칙은 효능·의료 주장을 **하나도** 잡지 않는다 --
실행해 확인했고 ``rules.screen("관절염 치료에 효과가 있습니다. 2주 만에 완치됩니다.
100% 효과 보장")`` 의 결과가 ``hits == []`` 다. ``MEDICAL_CLAIM`` 과
``EFFICACY_OVERCLAIM`` 은 ``perspectives.py`` 의 표시·광고 관점이 소유하는 모델 전용
분류이고 0단 정규식에 그 패턴이 없다. 즉 "생성물을 기존 룰에 태우면 된다"는 설계는 요약에서
틀렸다. 지금 막고 있는 것은 아래 5번 지시와 인용 강제뿐이고, 결정론 사전이 생기면 N4
게이트에 태운다.
"""

from __future__ import annotations

# 프롬프트를 고치면 올린다. 서술 재사용 키의 일부라 올리지 않으면 바뀐 양식으로 다시 쓰지
# 않고 옛 산문을 재사용한다.
NARRATE_VERSION = "narrate-v1"

NARRATE_PROMPT = """\
당신은 반려동물 용품 오픈마켓 '애착'의 리뷰 요약 작성자입니다. **리뷰 본문을 받지
않습니다.** 코드가 이미 센 집계표와, 본문에서 검증된 짧은 인용만 받습니다.

당신이 하는 일은 집계표의 각 줄에 **보호자가 읽을 한 줄**을 붙이는 것입니다.

# 지켜야 할 다섯 가지

1. **당신은 집계표만 봅니다.** 거기 없는 항목을 장점이나 단점으로 올리지 마십시오. 올려도
   코드가 버립니다. 건수와 비율은 코드가 적으므로 당신은 쓰지 않습니다.
2. **인용은 주어진 문자열을 글자 그대로 옮깁니다.** 고치거나 줄이거나 이어 붙이면 그
   인용은 폐기됩니다. 주어지지 않은 인용을 만들어 쓰면 그 항목 전체가 폐기됩니다.
3. **개인정보를 옮기지 마십시오.** 사람 이름, 연락처, 주소, 동호수를 한 줄 요약에 쓰지
   말고, `[가림]` 이 적힌 구간은 그대로 둡니다.
4. **단점을 올릴 수 없으면 `consAbsenceReason` 을 적습니다.** 비워 두고 "단점 없음"이라고
   쓰지 마십시오 -- 단점이 없는 것과 단점을 찾지 못한 것은 다른 사실입니다.
5. **효능과 의료 표현을 쓰지 마십시오.** "치료", "완치", "100%", "부작용 없음" 같은 단정형
   대신 **"~라는 후기가 있다", "~했다는 보호자가 많았다"** 로만 적습니다. 이 요약은
   플랫폼의 문장으로 남으므로 셀러 문구와 같은 책임이 걸립니다.

한 줄 요약은 60자 이내로, 집계표의 항목 라벨 표본을 재료로 쓰십시오.

반드시 ``write_review_summary`` 툴을 호출해 답하십시오.
"""


def render_tally_block(
    *,
    category_path: list[str],
    scope_label: str,
    basis_review_count: int,
    unextracted_review_count: int,
    rows: list[str],
) -> str:
    """서술 모델이 보는 접미부. **리뷰 본문이 한 글자도 들어가지 않는다.**

    분석되지 않은 리뷰 수를 함께 적는다. 적지 않으면 모델이 분모를 전수로 읽고
    "대부분의 보호자가" 같은 문장을 쓴다.
    """
    head = [
        f'<집계표 범위="{scope_label}">',
        f"카테고리: {' > '.join(category_path) if category_path else '미지정'}",
        f"분석한 리뷰: {basis_review_count}건",
        f"아직 분석되지 않은 리뷰: {unextracted_review_count}건",
        "",
    ]
    return "\n".join([*head, *rows, "</집계표>"])


def render_tally_row(
    *,
    aspect_code: str,
    aspect_name: str,
    polarity: str,
    mention_count: int,
    share: float,
    labels: list[str],
    quotes: list[str],
) -> str:
    """집계표 한 줄. 건수와 비율을 **보여 주지만 고쳐 쓰라고 하지 않는다.**

    보여 주는 이유는 모델이 한 줄 요약의 세기를 조절하는 데 쓰라는 것이다("3건 중
    3건"과 "120건 중 15건"은 다른 문장이 나와야 한다). 출력에는 싣지 않는다 --
    툴 스키마에 그 필드가 없다.
    """
    lines = [f"- [{aspect_code}] {aspect_name} / {polarity} / {mention_count}건 ({share:.0%})"]
    if labels:
        lines.append(f"    언급 표본: {' | '.join(labels)}")
    for quote in quotes:
        lines.append(f"    인용: {quote}")
    return "\n".join(lines)
