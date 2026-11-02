"""반려동물 금지 성분 사전과 우회 표기 탐지.

사전의 근거는 ASPCA 중독관리센터의 "People Foods to Avoid Feeding Your Pets",
FDA의 "Paws Off! Xylitol is Toxic to Dogs", AKC의 자일리톨 해설이다.

여기서 중요한 판단이 둘 있다.

**차단 목록은 "먹이면 독성이 있다"만으로 정하지 않는다.** 반려동물 상품에 정당하게
쓰이는 성분은 차단하지 않고 기록만 해서 모델에게 넘긴다. 모델은 카테고리를 함께 보기
때문에 위생용품의 에탄올과 간식의 알코올을 가를 수 있다. 코코넛오일, 유제품, 감귤,
견과류, 소금, 알코올이 그렇다. 이것들을 차단 목록에 넣으면 정상 상품이 대량으로 막힌다.

**우회 표기는 정규화로 잡는다.** 모델도 임베딩도 쓰지 않는다. 띄어쓰기·기호 삽입·글자
반복·대소문자·전각·동형문자는 전부 글자 모양 문제라서, 입력과 사전에 같은 정규화를
적용한 뒤 부분 문자열로 찾으면 걸린다. 결정론적이라 프롬프트 주입으로 뚫리지 않는다.

잡지 못하는 것을 적어 둔다. 자모를 분리한 표기(``ㅈ일리톨``), 초성만 쓴 표기(``ㅈㅇㄹㅌ``),
한글과 영문을 섞은 표기(``자일리tol``), 원래 없던 글자를 끼워 넣은 표기(``자일리이이톨``)는
정규화로 모이지 않는다. 자모 편집 거리를 쓰면 잡히지만 거리를 2까지 열면 전혀 다른 말이
걸려 과탐이 커진다. 그리고 우회의 목적은 "검수는 피하면서 구매자는 읽게 하는 것"이라,
구매자도 읽기 어려운 표기는 공격자에게도 쓸모가 적다. 의미를 비틀어 지칭하는
우회(``그 달달한 갈색 간식``)는 애초에 글자 문제가 아니라서 모델 단계가 본다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# 숫자를 닮은 글자로 바꾼 표기만 되돌린다. 숫자는 성분명에 들어갈 일이 없어 양방향
# 모두 안전하다.
#
# 글자끼리 바꿔 쓰는 것(대문자 I와 소문자 l)은 **일부러 다루지 않는다.** 둘을 같은
# 글자로 모으면 사전과 입력에 같이 적용해야 하는데, 그러면 ``week``가 ``leek``(대파)와
# 만나는 식으로 정상 영문 단어가 성분으로 걸린다. 한쪽에만 적용하면 ``BIRCH``가
# ``blrch``가 되어 사전과 어긋난다. 한국 셀러가 자일리톨을 ``xyIitol``로 적어 우회할
# 상황은 현실성이 낮아, 이 변형은 잡지 못하는 것으로 남긴다.
_HOMOGLYPHS = str.maketrans({"0": "o", "1": "l"})
# 정규화 단계에서 지우는 것들. 띄어쓰기와 기호 삽입 우회가 여기서 무력화된다.
_SEPARATORS = re.compile(r"[\s\-_.·•~*/\\|,()\[\]{}'\"`+=:;]")
_REPEATS = re.compile(r"(.)\1+")


def fold(text: str) -> str:
    """판정 전용 정규화. 사전과 입력에 **같은 함수**를 적용해야 의미가 있다.

    호환 분해로 전각·동형문자를 모으고, 소문자로 내리고, 구분 기호를 지우고,
    같은 글자 반복을 한 번으로 줄인다. 반복 축약을 사전에도 적용하므로
    ``coffee``가 ``cofe``가 되어도 양쪽이 같이 변해 매칭이 유지된다.

    **불가시 문자는 지우지 않는다.** 지우는 함수(``rules.strip_invisible``)가 이 모듈을
    import 하는 쪽에 있어 여기서 부르면 순환 import 다(양방향 모두 ``ImportError``).
    그래서 **사람이 쓴 텍스트를 판정에 넘기는 호출자가 먼저 지운다** -- ``rules.screen``과
    ``seller_draft.claims`` 의 두 입구다. 품목 신호(``seller_draft.catalog``)는 지우지
    않는다. 거기서 적중이 줄면 품목 미확정이 되어 초안을 **건너뛰므로** 안전한 방향이다.
    """
    folded = unicodedata.normalize("NFKC", text).lower().translate(_HOMOGLYPHS)
    return _REPEATS.sub(r"\1", _SEPARATORS.sub("", folded))


@dataclass(frozen=True, slots=True)
class BannedTerm:
    """금지 성분 한 종류.

    ``deny_suffix``는 그 표기 뒤에 붙으면 성분이 아니게 되는 말이다. 없으면 정상 단어가
    대량으로 걸린다 — ``포도``는 ``포도당``(사료의 흔한 성분)과 ``포도씨유``를 품고,
    ``카카오``는 메신저 이름을 품는다.
    """

    canonical: str
    aliases: tuple[str, ...]
    deny_suffix: tuple[str, ...] = ()
    blocking: bool = True
    note: str = ""

    def folded_forms(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(fold(a) for a in (self.canonical, *self.aliases) if a))

    def folded_deny(self) -> tuple[str, ...]:
        return tuple(fold(s) for s in self.deny_suffix if s)


# ── 차단: 먹이면 독성이 있고 반려동물 상품에 정당한 용도가 없는 것 ──────────────
#
# 자일리톨의 다른 이름이 많은 것은 FDA·AKC가 명시한 사실이다. "자작나무 설탕"과
# "birch sugar"는 같은 물질이고, 사람 식품 라벨에서 실제로 그렇게 쓰인다.
BLOCKING_TERMS: tuple[BannedTerm, ...] = (
    BannedTerm(
        "자일리톨",
        (
            "xylitol",
            "자작나무설탕",
            "birch sugar",
            "birch sap",
            "birch bark extract",
            "wood sugar",
            "목당",
        ),
        note="개는 소량으로도 저혈당과 간부전에 이른다. 사람 식품 라벨에서 자작나무 설탕으로 표기된다",
    ),
    BannedTerm(
        "초콜릿",
        ("초콜렛", "쵸콜릿", "chocolate", "코코아", "cocoa", "다크초콜릿", "베이킹초콜릿"),
        note="메틸잔틴. 다크일수록 위험하다",
    ),
    BannedTerm(
        "카카오",
        ("cacao",),
        # 메신저·금융 서비스 이름과 글자가 겹친다. 거르지 않으면 "카카오톡으로 문의"가
        # 금지 성분으로 기록된다 — 어차피 연락처 규칙이 차단하지만 분류가 틀린다.
        #
        # ``T`` 는 뺐다. ``fold`` 가 소문자로 내리므로 **tail 이 t 로 시작하는 표기가
        # 전부 제외됐고**, ``cacao tea``·``cacao tree`` 가 적중 0이 됐다. 대가로
        # ``카카오T`` 가 성분으로 걸릴 수 있다 -- 반려동물 상품·리뷰에서 그 언급보다
        # 영문 표기의 거짓 음성이 비싸다고 보아 이쪽을 택했다.
        deny_suffix=("톡", "페이", "뱅크", "맵", "모빌리티", "엔터"),
        note="초콜릿과 같은 메틸잔틴 원료",
    ),
    BannedTerm(
        "포도",
        ("건포도", "grape", "raisin", "sultana", "설타나"),
        deny_suffix=("당", "씨유", "씨오일", "씨 오일", "씨추출물"),
        note="타르타르산이 신장을 망친다. 포도당과 포도씨유는 전혀 다른 것이라 제외한다",
    ),
    BannedTerm("마카다미아", ("macadamia",), note="개에게 쇠약·떨림·고열"),
    BannedTerm(
        "양파",
        ("onion", "양파분말", "양파가루"),
        note="알리움. 적혈구를 파괴해 빈혈을 만든다. 고양이가 더 민감하다",
    ),
    BannedTerm("마늘", ("garlic", "마늘분말", "마늘가루"), note="알리움"),
    BannedTerm("부추", ("chive", "chives"), note="알리움"),
    BannedTerm(
        "대파", ("쪽파", "실파", "scallion", "green onion", "leek", "리크", "샬롯", "shallot"), note="알리움"
    ),
    BannedTerm("카페인", ("caffeine",), note="메틸잔틴"),
    BannedTerm(
        "커피",
        ("coffee", "에스프레소", "espresso"),
        deny_suffix=("색",),
        note="메틸잔틴. 커피색은 색 표현이라 제외한다",
    ),
)

# ── 기록만: 먹이면 문제가 될 수 있지만 반려동물 상품에 정당하게 쓰이는 것 ──────
#
# 차단하지 않고 모델에게 넘긴다. 모델은 카테고리를 함께 보므로 위생용품의 에탄올과
# 간식의 알코올을 가를 수 있다. 여기 있는 말을 차단하면 정상 상품이 대량으로 막힌다.
ADVISORY_TERMS: tuple[BannedTerm, ...] = (
    BannedTerm(
        "알코올", ("alcohol", "에탄올", "ethanol", "주정"), blocking=False, note="위생용품에 정당하게 쓰인다"
    ),
    BannedTerm("아보카도", ("avocado",), blocking=False, note="개·고양이에게는 ASPCA가 명시하지 않는다"),
    BannedTerm("코코넛오일", ("coconut oil",), blocking=False, note="반려동물 샴푸·간식에 흔하다"),
    BannedTerm(
        "우유",
        ("milk", "유제품"),
        deny_suffix=("단백",),
        blocking=False,
        note="락토프리 반려동물 우유 제품이 있다",
    ),
    BannedTerm(
        "호두",
        ("walnut", "피칸", "pecan", "아몬드", "almond"),
        blocking=False,
        note="지방이 많아 췌장염 위험",
    ),
)


# 성분 앞뒤에 이 말이 붙으면 "쓰지 않았다"는 뜻이다.
#
# 키워드를 그대로 정규식에 박지 않고 fold를 거쳐 만든다. 정규화가 공백을 지우고 반복을
# 줄이기 때문에, 날것으로 적으면 ``free``가 접힌 ``fre``와 만나지 못해 "xylitol free"가
# 성분으로 걸린다. 사전과 입력에 같은 정규화를 적용하는 원칙을 여기에도 적용한다.
_NEG_BEFORE_WORDS = (
    "무",
    "없는",
    "없이",
    "불포함",
    "미포함",
    "제외",
    "프리",
    "free",
    "non",
    "무첨가",
    "제로",
    "zero",
)
_NEG_AFTER_WORDS = (
    "없",
    "불포함",
    "미포함",
    "제외",
    "프리",
    "free",
    "무첨가",
    "불검출",
    "제로",
    "zero",
    "안들어",
)


def _alternation(words: tuple[str, ...]) -> str:
    return "|".join(sorted({re.escape(fold(w)) for w in words}, key=len, reverse=True))


@dataclass(frozen=True, slots=True)
class TermHit:
    """금지 성분 적중.

    위치를 담지 않는다. 정규화가 공백과 기호를 지워 원문과 길이가 달라지므로, 되찾은
    위치를 넘기면 엉뚱한 구간을 가리킨다. 셀러에게 필요한 것은 "어느 성분이 걸렸는가"이고
    그건 대표 표기로 충분하다.
    """

    term: BannedTerm


def find_terms(text: str) -> list[TermHit]:
    """금지 성분을 찾는다. 성분 하나당 한 번만 보고한다.

    같은 성분이 여러 번 나와도 적중을 늘리지 않는다. 셀러에게 전할 말이 "자일리톨이
    세 번 나왔다"가 아니라 "자일리톨을 빼라"이기 때문이다.
    """
    folded = fold(text)
    if not folded:
        return []

    hits: list[TermHit] = []
    for term in (*BLOCKING_TERMS, *ADVISORY_TERMS):
        deny = term.folded_deny()
        for form in term.folded_forms():
            if _matches(folded, form, deny):
                hits.append(TermHit(term))
                break
    return hits


_NEGATION_BEFORE = re.compile(f"({_alternation(_NEG_BEFORE_WORDS)})$", re.I)
_NEGATION_AFTER = re.compile(f"^({_alternation(_NEG_AFTER_WORDS)})", re.I)


def _matches(folded: str, form: str, deny: tuple[str, ...]) -> bool:
    """정규화 문자열 안에서 성분 표기를 찾되, 제외 조건에 걸리는 자리는 넘긴다."""
    at = folded.find(form)
    while at >= 0:
        tail = folded[at + len(form) :]
        head = folded[:at]
        blocked_by_suffix = any(tail.startswith(d) for d in deny)
        negated = bool(_NEGATION_BEFORE.search(head)) or bool(_NEGATION_AFTER.match(tail))
        if not blocked_by_suffix and not negated:
            return True
        at = folded.find(form, at + 1)
    return False
