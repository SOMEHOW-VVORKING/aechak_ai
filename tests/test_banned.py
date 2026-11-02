"""금지 성분 사전과 우회 표기 탐지를 고정한다.

여기서 지키는 것이 둘이다. **우회 표기를 잡는 것**과 **정상 성분을 잡지 않는 것**이다.
후자가 더 중요하다. 반려동물 사료에 흔한 포도당이나 파프리카가 금지 성분으로 걸리면
정상 상품이 대량으로 막히고, 그러면 기능 자체를 끌 수밖에 없다.
"""

from __future__ import annotations

import pytest

from app.inspection import rules
from app.inspection.banned import find_terms, fold


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


def test_카카오_제외_접미가_영문_표기를_눌러_버리지_않는다() -> None:
    """``fold`` 가 ``T`` 를 ``t`` 로 내리므로 **tail 이 t 로 시작하는 표기가 전부 제외됐다.**

    ``cacao tea`` 는 공백이 지워져 ``cacaotea`` 가 되고, ``cacao tree`` 는 반복 축약까지
    겹쳐 ``cacaotre`` 가 된다. 둘 다 카카오가 들어간 제품 표기인데 적중이 0이었다.
    메신저·금융 이름 제외는 그대로 일해야 한다.
    """
    assert "카카오" in blocking("cacao tea 분말 함유"), "거짓 음성이다"
    assert "카카오" in blocking("cacao tree 추출물"), "거짓 음성이다"
    assert "카카오" not in canonicals("카카오맵으로 찾아오세요"), "지명 서비스 제외는 유지한다"
    assert "카카오" not in canonicals("카카오페이로 결제"), "결제 서비스 제외는 유지한다"


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


# --- 불가시 문자: 사전은 지우지 않고, 사전을 부르는 쪽이 지운다 -----------------------------

# 폭 0 공백 / 한글 필러 / 이형자 선택자. 전 범위를 훑는 단언은 ``test_summary_envelope`` 에
# 있고(``strip_invisible`` 이 분류로 긋는 것을 거기서 고정한다), 여기서는 **이 호출 경로가
# 그 함수를 지나는가**만 본다.
_INVISIBLE = ("​", "ㅤ", "️")


@pytest.mark.parametrize("char", _INVISIBLE)
def test_불가시_문자를_끼운_성분_표기가_0단_검사에서_잡힌다(char: str) -> None:
    """**``fold()`` 는 불가시 문자를 지우지 않는다.** 지우는 함수가 ``rules`` 에 있고 그
    모듈이 이 모듈을 import 하므로, 여기서 부르면 순환 import 다(실행해 확인했다 --
    양방향 모두 ``ImportError``). 그래서 **사전을 부르는 쪽**이 먼저 지운다.

    머리글이 "우회 표기는 정규화로 잡는다"고 선언하는데 글자 사이에 폭 0 문자를 끼운
    표기는 적중이 0이었다. 글자 모양이 같으므로 구매자는 ``자일리톨``을 그대로 읽는다.
    """
    text = f"자일리{char}톨 함유 수제 간식"

    assert "자일리톨" in {h.quote for h in rules.screen(text).blocking}, (
        f"U+{ord(char):04X} 한 글자로 금지 성분 차단이 뚫린다: fold -> {fold(text)!r}"
    )


def test_불가시_문자를_끼운_비차단_성분도_권고로_남는다() -> None:
    """차단이 아니라 **기록**이 사라지는 쪽도 손실이다 -- 모델이 볼 성분 신호가 조용히 빈다."""
    hits = {h.quote for h in rules.screen("코코​넛오일 샴푸").hits}

    assert "코코넛오일" in hits, "비차단 성분 권고가 한 글자로 사라진다"
