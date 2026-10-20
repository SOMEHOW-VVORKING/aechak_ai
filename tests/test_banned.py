"""금지 성분 사전과 우회 표기 탐지를 고정한다.

여기서 지키는 것이 둘이다. **우회 표기를 잡는 것**과 **정상 성분을 잡지 않는 것**이다.
후자가 더 중요하다. 반려동물 사료에 흔한 포도당이나 파프리카가 금지 성분으로 걸리면
정상 상품이 대량으로 막히고, 그러면 기능 자체를 끌 수밖에 없다.
"""

from __future__ import annotations

import pytest

from app.inspection.banned import BLOCKING_TERMS, find_terms, fold


def canonicals(text: str) -> set[str]:
    return {h.term.canonical for h in find_terms(text)}


def blocking(text: str) -> set[str]:
    return {h.term.canonical for h in find_terms(text) if h.term.blocking}


@pytest.mark.parametrize(
    "text",
    [
        "자일리톨 함유 치약",
        "자 일 리 톨 들어있어요",
        "자-일-리-톨 함유",
        "자일리리톨 함유",
        "ＸＹＬＩＴＯＬ 함유",  # noqa: RUF001  전각 우회를 일부러 쓴다
        "xy1itol 함유",
        "XyLiToL 포함",
    ],
)
def test_자일리톨_우회_표기를_잡는다(text: str) -> None:
    assert "자일리톨" in blocking(text), f"우회 표기가 통과했다: {text!r} -> {fold(text)!r}"


@pytest.mark.parametrize(
    "text",
    ["자작나무 설탕으로 단맛을 냈습니다", "sweetened with BIRCH SUGAR", "wood sugar 함유", "birch sap 추출"],
)
def test_자일리톨의_다른_이름을_잡는다(text: str) -> None:
    # 사람 식품 라벨에서 실제로 쓰이는 표기다. FDA와 AKC가 같은 물질이라고 명시한다.
    assert "자일리톨" in blocking(text), f"다른 이름이 통과했다: {text!r}"


@pytest.mark.parametrize("text", ["무자일리톨 치약", "자일리톨 무첨가", "자일리톨 불포함", "xylitol free"])
def test_쓰지_않았다는_표현은_성분으로_보지_않는다(text: str) -> None:
    assert "자일리톨" not in canonicals(text)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("포도당 시럽 포함", "포도당은 사료에 흔한 당이고 포도와 다른 물질이다"),
        ("포도씨유 베이스", "포도씨유에는 타르타르산이 없다"),
        ("파프리카 분말 함유", "짧은 성분명이 긴 정상 단어에 묻히면 안 된다"),
        ("파슬리 가루", "같은 이유"),
        ("커피색 하네스", "커피색은 색 표현이다"),
        ("연어와 고구마 건사료 2kg", "정상 상품"),
    ],
)
def test_정상_성분을_잡지_않는다(text: str, reason: str) -> None:
    assert canonicals(text) == set(), f"{reason}: {text!r}"


def test_카카오톡은_성분이_아니다() -> None:
    # 메신저 이름과 글자가 겹친다. 연락처 규칙이 따로 차단하므로 분류만 틀리면 안 된다.
    assert "카카오" not in canonicals("카카오톡으로 문의주세요")
    assert "카카오" in blocking("카카오 함유 쿠키")


def test_반려동물_상품에_정당한_성분은_차단하지_않고_기록만_한다() -> None:
    # 모델은 카테고리를 함께 보므로 위생용품의 에탄올과 간식의 알코올을 가를 수 있다.
    hits = find_terms("에탄올 함유 물티슈")
    assert {h.term.canonical for h in hits} == {"알코올"}
    assert all(not h.term.blocking for h in hits)
    assert blocking("코코넛오일 샴푸") == set()


def test_알리움류를_모두_잡는다() -> None:
    for text, name in [
        ("양파분말 소량", "양파"),
        ("마늘가루 첨가", "마늘"),
        ("부추 들어감", "부추"),
        ("대파와 닭가슴살", "대파"),
        ("green onion 포함", "대파"),
    ]:
        assert name in blocking(text), f"알리움을 놓쳤다: {text!r}"


def test_같은_성분이_여러_번_나와도_한_번만_보고한다() -> None:
    # 셀러에게 전할 말은 "세 번 나왔다"가 아니라 "빼라"다.
    hits = find_terms("자일리톨 자일리톨 자일리톨")
    assert len(hits) == 1


def test_사전_항목에_근거를_남긴다() -> None:
    # 왜 금지인지가 코드에 없으면 다음 사람이 함부로 지운다.
    without_note = [t.canonical for t in BLOCKING_TERMS if not t.note]
    assert without_note == [], f"근거 없는 항목: {without_note}"
