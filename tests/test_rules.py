"""0단 규칙 보강 셋을 고정한다 — 클릭 유도·거짓 당첨·구절 반복.

11/2 하네스 실측이 이 셋을 요구했다. Haiku 선별이 ``"무료 쿠폰 이벤트 당첨 지금 바로 클릭
지금 바로 클릭"`` 을 2/2 CLEAR로 통과시켰고, 선별의 CLEAR는 관점 호출을 **아예 없앤다** --
그러면 그 문구를 어느 계층도 읽지 않는다. 정규식 계층은 0원이고 주입으로 뚫리지 않아
그 사각을 닫는 자리로 맞다.

**양성보다 음성이 중요하다.** 셋 다 정상 문구와 글자 모양으로 완전히 갈리지 않으므로
비차단으로 두었고(차단이면 오탐의 값이 셀러의 판매 중단이다), 그래도 적중 자체가 선별을
건너뛰게 만들어 Sonnet 2회를 태운다. 어디까지 적중하고 어디서 멈추는지를 표로 박아 둔다.
"""

from __future__ import annotations

import pytest

from app.inspection import rules
from app.schemas.inspection import ViolationCategory

# 적중이 없어야 하는 정상 문구. 규칙을 넓히는 변경이 여기 걸린다.
CLEAN: tuple[str, ...] = (
    "쿠폰 이벤트 진행 중입니다",
    "이벤트 당첨자는 공지에서 확인해 주세요",
    "당첨자 발표를 축하합니다",
    "아래 옵션에서 사이즈를 선택해 주세요",
    "상세 정보는 https://shop.aechak.co.kr/p/1 에서 확인해 주세요",
    "글루코사민과 MSM이 들어간 영양제입니다. 관절 건강에 도움을 줄 수 있습니다.",
    "소형견용 60정 1개 무료배송\n중형견용 60정 1개 무료배송\n대형견용 60정 1개 무료배송",
    "처방식사료 신장 케어 건식사료입니다",
    "수의사 처방식 사료입니다",
)


def spam_quotes(text: str) -> list[str]:
    return [h.quote for h in rules.screen(text).hits if h.category is ViolationCategory.SPAM]


@pytest.mark.parametrize("text", CLEAN)
def test_정상_문구에는_적중이_없다(text: str) -> None:
    """오탐 하나가 Sonnet 2회를 공짜로 태운다. 평범한 상품 문구가 전부 여기 와야 한다."""
    assert rules.screen(text).hits == [], text


@pytest.mark.parametrize(
    "text",
    [
        "자세한 내용은 지금 바로 클릭",
        "구매는 클릭하세요",
        "여기를 클릭해 주세요",
        "링크를 클릭하면 할인됩니다",
        "아래 버튼을 눌러 주세요",
    ],
)
def test_클릭_유도_문구를_SPAM으로_적는다(text: str) -> None:
    hits = [h for h in rules.screen(text).hits if h.reason == "클릭·이동을 유도하는 문구"]

    assert hits, text
    assert {h.category for h in hits} == {ViolationCategory.SPAM}


@pytest.mark.parametrize(
    "text",
    [
        "축하합니다 고객님 1등에 당첨되셨습니다",
        "이벤트에 당첨되었습니다",
        "당첨자로 선정되셨으니 연락 주세요",
    ],
)
def test_거짓_당첨_안내를_SPAM으로_적는다(text: str) -> None:
    hits = [h for h in rules.screen(text).hits if h.reason == "거짓 당첨 안내로 읽히는 문구"]

    assert hits, text
    assert {h.category for h in hits} == {ViolationCategory.SPAM}


@pytest.mark.parametrize(
    "text",
    (
        "아목시실린 50정 처방전 없이 바로 발송",
        "Amoxicillin 250mg 소분 판매",
        "아포퀠 16mg 남은 거 팝니다",
    ),
)
def test_처방약_이름은_PRESCRIPTION_DRUG로_적고_선별을_건너뛰게_한다(text: str) -> None:
    """차단하지 않는다. 복약 주의문처럼 약 이름을 언급만 한 정상 문구가 있어 판단은 관점에 맡기고,
    선별이 CLEAR 로 흘려보내지 못하게 승급만 시킨다.

    처방이라는 단어만 있는 정상 문구는 위 CLEAN 이 고정한다.
    """
    result = rules.screen(text)
    assert [h.category for h in result.hits] == [ViolationCategory.PRESCRIPTION_DRUG], text
    assert result.blocking == [], text
    assert [h.category for h in result.triage_trumping] == [ViolationCategory.PRESCRIPTION_DRUG], text


def test_같은_구절을_세_번_반복하면_적중이다() -> None:
    text = "강아지 간식 아주 맛있어요 " * 3

    hits = [h for h in rules.screen(text).hits if h.reason.startswith("같은 구절이")]

    assert len(hits) == 1, "창이 겹쳐도 같은 사실은 한 줄이다. 여러 줄이면 어드민 큐가 같은 문구로 찬다"
    assert hits[0].category is ViolationCategory.SPAM
    assert hits[0].quote == "강아지 간식 아주 맛있어요"
    assert hits[0].reason == "같은 구절이 3번 반복된다"
    assert (hits[0].start, hits[0].end) == (None, None), (
        "세 번 나온 구절에서 한 번만 가리키는 구간은 거짓이다. 금지 성분 적중과 같은 모양이다"
    )


def test_두_번_반복은_적중이_아니다() -> None:
    """임계가 셋이다. 둘을 적중으로 두면 "A와 B. A와 B" 꼴의 평범한 설명이 전부 걸린다."""
    text = "강아지 간식 아주 맛있어요 " * 2

    assert [h for h in rules.screen(text).hits if h.reason.startswith("같은 구절이")] == []


def test_어절_셋짜리_구절의_반복은_적중이_아니다() -> None:
    """창이 어절 넷이다. 짧은 구절은 정상 문구에서도 세 번 나온다."""
    text = "최고의 간식 최고의 간식 최고의 간식"

    assert [h for h in rules.screen(text).hits if h.reason.startswith("같은 구절이")] == []


def test_반복이_꽉_찬_짧은_본문도_길이_검사를_빠져나가지_않는다() -> None:
    """창이 **겹칠 수 있으므로** 최소 길이가 창 길이 x 반복 횟수가 아니다.

    곱(12어절)으로 적었더니 반복이 꽉 찬 10어절 본문이 검사에서 그대로 빠져나갔다 --
    실측 케이스가 정확히 그 길이였다.
    """
    text = "지금 바로 클릭 " * 3 + "지금"

    assert [h for h in rules.screen(text).hits if h.reason.startswith("같은 구절이")]


def test_11월_2일_실측_케이스가_규칙에서_걸린다() -> None:
    """선별이 2/2 CLEAR로 통과시킨 본문이다. 이제 규칙이 먼저 잡는다."""
    text = "무료 쿠폰 이벤트 당첨 지금 바로 클릭 지금 바로 클릭"

    result = rules.screen(text)

    assert spam_quotes(text), "적중이 0이면 선별 CLEAR 한 번으로 아무도 이 본문을 읽지 않는다"
    assert result.blocking == [], "정상 안내와 글자 모양으로 갈리지 않으므로 차단이 아니다"
    assert result.triage_trumping, "비차단이어도 선별은 건너뛰고 관점 둘이 봐야 한다"


def test_보강_셋은_모델에_보낼_본문에서_가리지_않는다() -> None:
    """지시형 문장과 다르다. 이 셋은 **판단 대상**이라 모델이 글자를 봐야 한다.

    가리면 관점은 "[가림]" 만 받고, 인용 검증은 가린 본문 기준이라 모델이 낼 수 있는 인용이
    사라진다 -- 승급시켜 놓고 하류가 아무것도 말할 수 없는 상태가 된다.
    """
    text = "당첨되셨습니다. 지금 바로 클릭"

    redacted = rules.screen(text).redacted

    assert redacted == text


def contact_quotes(text: str) -> list[str]:
    return [h.quote for h in rules.screen(text).hits if h.category is ViolationCategory.CONTACT_INFO]


@pytest.mark.parametrize(
    "text",
    [
        "E2E-ADM 순면 손수건 C",
        "E2E-ADM-MON 산책 물병 D",
        "admin 전용 샘플",
        "HDMI 케이블 정리 파우치",
        "instant 사료 보관통",
        "randomly 섞인 간식 세트",
    ],
)
def test_영문_단어_안의_dm_insta는_연락처가_아니다(text: str) -> None:
    """11/7 데모 점검에서 상품명 "E2E-ADM" 의 DM 이 차단으로 걸려 실제 위반을 모델이 못 봤다."""
    assert contact_quotes(text) == [], text


@pytest.mark.parametrize(
    "text",
    [
        "DM 주세요",
        "문의는 dm으로",
        "인스타DM 환영",
        "DMs open",
        "insta 아이디로 연락",
        "instagram 문의",
        "디엠 주세요",
    ],
)
def test_메신저_약어는_한글_기호와_붙어도_잡는다(text: str) -> None:
    assert contact_quotes(text), text
