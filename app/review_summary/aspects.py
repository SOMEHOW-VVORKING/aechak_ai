"""항목 어휘. 열거의 정본은 계약(``app/schemas/summary.py``)이 소유하고 여기는 라벨만 든다.

방향을 그렇게 잡은 이유: ``app/inspection/rules.py``가 ``ViolationCategory``를
``app/schemas/inspection.py``에서 import하는 것과 같은 모양이다. 열거가 백엔드 저장 칼럼
(``review_aspects.aspect_code``)으로 나가는 값이므로 계약 쪽이 주인이고, 도메인 모듈은
그것을 읽어 프롬프트 문장을 만든다. 두 자리에 열거를 적으면 언젠가 갈라진다.

라벨을 따로 두는 이유는 **프롬프트가 사람 말로 쓰여야** 하고 그 문장이 열거 값과 1:1로
묶여 있어야 하기 때문이다. 테스트가 "열거 전건에 라벨이 있다"를 단언해 열거를 늘릴 때
프롬프트가 조용히 뒤처지는 것을 막는다.
"""

from __future__ import annotations

from app.schemas.summary import AspectCode, Polarity

# 코드 -> (화면 이름, 모델에게 주는 판정 기준 한 줄).
#
# 기준 줄이 있어야 "냄새"가 사료의 비린내인지 배변패드의 탈취 실패인지 모델이 가르지 않고
# 같은 코드로 모은다 -- 세는 축이 흔들리면 집계가 "반복된다"를 틀리게 센다.
ASPECT_LABELS: dict[AspectCode, tuple[str, str]] = {
    AspectCode.PALATABILITY: ("기호성", "아이가 잘 먹는지, 거부하는지"),
    AspectCode.PRICE: ("가격", "값이 싸다·비싸다, 가성비"),
    AspectCode.DELIVERY: ("배송", "도착 속도, 배송 사고"),
    AspectCode.PACKAGING: ("포장", "포장 상태, 파손, 밀봉"),
    AspectCode.SMELL: ("냄새", "제품 자체의 냄새"),
    AspectCode.DURABILITY: ("내구성", "오래 쓰는지, 금방 망가지는지"),
    AspectCode.SIZE_FIT: ("크기·핏", "표기 대비 크기, 몸에 맞는지"),
    AspectCode.EASE_OF_USE: ("사용 편의", "쓰기·조립·급여가 편한지"),
    AspectCode.REPURCHASE: ("재구매 의사", "다시 살지, 추천하는지"),
    AspectCode.PERCEIVED_EFFECT: ("효과 체감", "보호자가 변화를 느꼈다는 서술"),
    AspectCode.ADVERSE_REACTION: ("이상 반응", "설사·구토·가려움 같은 몸의 반응"),
    AspectCode.NOISE: ("소음", "작동 소리"),
    AspectCode.CLEANING: ("세척", "닦고 빨고 비우는 수고"),
    AspectCode.DESIGN: ("디자인", "모양, 색, 인테리어와의 어울림"),
    AspectCode.LEAKAGE: ("흘림·누출", "새거나 흘리거나 가루가 날리는 것"),
    AspectCode.SELLER_SUPPORT: ("고객 응대", "문의 답변, 교환·반품 처리"),
    AspectCode.QUANTITY: ("양·용량", "표기 대비 양, 얼마나 오래 쓰는지"),
    AspectCode.OTHER: ("기타", "위 어디에도 맞지 않는 반복 가능한 속성"),
}

# 극성 정의. 모델에게 주는 문장이 코드의 교차검사와 같은 뜻이어야 한다.
POLARITY_LABELS: dict[Polarity, str] = {
    Polarity.PRO: "장점. 그 항목을 긍정적으로 말한 것",
    Polarity.CON: "단점. 그 항목을 부정적으로 말한 것",
}


def aspect_enum() -> list[str]:
    """툴 스키마에 싣는 열거. **요청 데이터로 바뀌지 않는 상수다.**

    카테고리마다 다른 enum을 실으면 캐시 엔트리가 카테고리 수만큼 갈라진다 --
    ``findings_tool(spec)``이 관점마다 다른 enum을 실어 이미 그 상태인 것이 실례다.
    """
    return [code.value for code in AspectCode]


def render_aspect_guide() -> str:
    """프롬프트에 들어갈 항목 정의 블록. 열거 순서를 그대로 따른다."""
    lines = []
    for code in AspectCode:
        name, rule = ASPECT_LABELS[code]
        lines.append(f"- {code.value} ({name}): {rule}")
    return "\n".join(lines)


def parse_aspect_code(raw: object) -> AspectCode | None:
    """모델이 돌려준 코드를 열거로 접는다. 모르는 값은 **버린다.**

    ``OTHER``로 접지 않는다. 열거 밖의 값이 왔다는 것은 툴 enum이 지켜지지 않았다는
    뜻이고, 그걸 ``OTHER``로 구제하면 스키마가 깨진 사실이 집계에 섞여 보이지 않는다.
    호출부가 ``unknownAspectCode``로 센다.
    """
    if not isinstance(raw, str):
        return None
    try:
        return AspectCode(raw)
    except ValueError:
        return None


def parse_polarity(raw: object) -> Polarity | None:
    if not isinstance(raw, str):
        return None
    try:
        return Polarity(raw)
    except ValueError:
        return None
