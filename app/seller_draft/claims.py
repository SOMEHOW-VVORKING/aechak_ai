"""효능 단정 어휘 사전. **생성측 전용이고 판정을 내지 않는다.**

## 이 모듈이 왜 필요한가 — 실행해 확인한 사실

``rules.screen()`` 은 효능·의료 주장을 **하나도** 잡지 못한다.

    rules.screen("관절염 치료에 효과가 있습니다. 2주 만에 완치됩니다. 100% 효과 보장")
        -> hits == []
    rules.screen("식약처 인증 완료 제품입니다")   -> hits == []
    rules.screen("수의사 처방 의약품입니다")      -> hits == []

0단 정규식은 연락처·이메일·외부URL·계좌·봉투탈출과 ``banned.find_terms`` 뿐이다.
``MEDICAL_CLAIM`` 과 ``EFFICACY_OVERCLAIM`` 은 ``perspectives.py`` 의 표시·광고 관점이
소유하는 **모델 전용 분류**다. 그러므로 "생성물을 기존 0단 룰에 태우면 검수를 통과한다"는
설계는 틀렸다 -- 0단은 연락처·외부URL·봉투탈출·금지성분까지만 공짜로 막는다.

## 왜 ``rules.py`` 에 넣지 않는가

넣으면 ``screen()`` 에 적중이 새로 생겨 **지금 통과하는 상품이 조용히 차단되고 기존 판정이
바뀐다.** 이 사전은 "셀러에게 이 문구를 권하지 않는다"만 결정하고, 위반 여부는 등록 시점의
기존 검수 파이프라인이 본다.

## 왜 ``demo/mock.py`` 의 ``KEYWORD_TABLE`` 을 import 하지 않는가

후보 표가 거기 이미 있지만 ``tests/test_demo.py`` 가 ``app/`` 어느 파일도 ``demo`` 를
import 하지 않음을 단언하고 Dockerfile 이 ``demo`` 를 COPY 하지 않는다 -- import 하면
**운영에서 ImportError** 다. 표를 여기 새로 쓰고, 테스트가 두 표의 교집합을 고정해 한쪽만
늘어나는 것을 잡는다.

## 과탐 쪽으로 기울였다

제외 목록이 불완전한 것이 이 계층의 알려진 한계다(``banned.py`` 도 같은 한계를 머리글에
적어 뒀다). 그래서 **과탐 방향**으로 기울인다 -- 과탐은 필드를 비우게 하고(셀러가 직접
쓴다) 과소탐은 셀러 상품이 차단되게 한다. 비용이 비대칭이므로 방향이 정해진다.

## 막지 못하는 것

사전에 없는 새 효능 표현("3일이면 달라집니다"), 의미로 효능을 암시하는 문장. 그것은 등록
시점의 기존 검수 파이프라인이 본다. **여기서 못 막는 것을 "통과"로 적지 않는다** --
이 모듈이 돌려주는 것은 "적어 둔 표현에 걸리지 않았다"이고 "깨끗하다"가 아니다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.inspection.banned import fold

# ``POLICY_PREFIX`` 가 위반 예시로 든 문구 전건. **여기 적어 두고 테스트가 드리프트를 잡는다.**
#
# 정책 문서를 고쳐 예시가 늘면 테스트가 깨진다. 깨지는 쪽이 조용히 어긋나는 쪽보다 낫다 --
# 초안 생성기가 자기 플랫폼의 검수에 걸리는 문구를 셀러에게 권하면 셀러가 그대로 저장했을 때
# 자기 상품이 차단된다.
POLICY_CLAIM_EXAMPLES: tuple[str, ...] = (
    "관절염 치료에 효과",
    "슬개골 탈구가 낫습니다",
    "아토피를 완치시켜요",
    "100% 효과 보장",
    "2주 만에 털빠짐 완전 해결",
    "모든 강아지에게 효과",
    "식약처 인증 완료",
    "수의사 처방 의약품",
    "국내산(실제 수입품)",
)

# 위 예시 가운데 **이 사전이 잡지 못하는 것.** 적어 두지 않으면 커버리지 테스트가 거짓이 된다.
#
# 원산지 허위는 어휘 문제가 아니다 -- "국내산"이라고 적힌 것이 거짓인지는 상품이 실제로
# 어디서 왔는지를 알아야 판단되고, 그것은 문자열 계층에 없는 정보다. 초안이 원산지를
# 쓰지 않게 하는 것이 맞는 방어이고, 그래서 사실 슬롯에 원산지 자리를 두지 않았다.
UNCOVERED_POLICY_EXAMPLES: tuple[str, ...] = ("국내산(실제 수입품)",)

# **보류 집합. 사전을 쓸 때 쓰지 않았고, 앞으로도 이 목록을 보고 사전을 고치지 않는다.**
#
# 위 ``POLICY_CLAIM_EXAMPLES`` 를 분모로 커버리지를 재면 그 숫자는 **순환이다** -- 사전이
# 그 목록에서 나왔고 같은 목록이 프롬프트에도 들어간다(``forbidden_examples_block``).
# 8/8 은 미지의 표현에 대해 아무것도 말하지 않는데 리포트 한 줄로는 커버리지처럼 읽힌다.
#
# 그래서 사전을 만들 때 참조하지 않은 표현을 따로 둔다. 셀러가 실제로 쓸 만하고 등록 시점
# 검수의 MEDICAL_CLAIM·EFFICACY_OVERCLAIM 관점이 잡을 문구들이다. **이 목록에 맞춰 사전을
# 늘리면 그 순간 이것도 순환이 되어 측정 도구가 하나 사라진다** -- 늘리고 싶으면 새 보류
# 집합을 먼저 만들어라.
#
# 네거티브 리스트의 성질상 이 숫자는 낮게 나온다. 낮은 것이 사실이고, 숨기는 것보다 낫다.
HOLDOUT_CLAIM_EXAMPLES: tuple[str, ...] = (
    "염증을 가라앉혀 줍니다",
    "피부병이 사라집니다",
    "면역력을 높여 질병을 예방합니다",
    "사용 2주 후 통증이 줄어듭니다",
    "눈에 띄게 좋아졌다는 후기가 많습니다",
    "3일이면 달라집니다",
    "장 건강을 되찾아 줍니다",
    "알러지를 잡아 줍니다",
)


@dataclass(frozen=True, slots=True)
class ClaimTerm:
    """효능 단정 표현 하나.

    ``deny_prefix``/``deny_suffix`` 는 그 앞뒤에 붙으면 단정이 아니게 되는 말이다.
    ``banned.py`` 의 ``deny_suffix`` 설계를 그대로 빌려오고 앞쪽을 하나 더 뒀다 --
    ``인증`` 은 ``자체 인증 기준`` 에서 빠져야 하고 그것은 **앞**에 붙는 말이다.
    """

    canonical: str
    aliases: tuple[str, ...] = ()
    deny_prefix: tuple[str, ...] = ()
    deny_suffix: tuple[str, ...] = ()
    note: str = ""

    def folded_forms(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(fold(a) for a in (self.canonical, *self.aliases) if fold(a)))

    def folded_deny_prefix(self) -> tuple[str, ...]:
        return tuple(fold(s) for s in self.deny_prefix if fold(s))

    def folded_deny_suffix(self) -> tuple[str, ...]:
        return tuple(fold(s) for s in self.deny_suffix if fold(s))


# 질병을 치료·완치한다고 단정하는 표현.
_MEDICAL: tuple[ClaimTerm, ...] = (
    ClaimTerm(
        "치료",
        aliases=("치유", "치료제"),
        # "치료 보조"는 단정이 아니고, "처방식사료"의 설명에 자연스럽게 나온다.
        deny_suffix=("보조", "목적이아닙", "가아닙", "제가아닙"),
        note="질병 치료를 단정한다",
    ),
    ClaimTerm("완치", aliases=("완전히낫", "완전치료"), note="완치를 단정한다"),
    ClaimTerm(
        "낫습니",
        aliases=("나았", "낫게해", "낫는다", "호전됩니다"),
        note="질병이 낫는다고 단정한다",
    ),
    ClaimTerm(
        "의약품",
        aliases=("동물용의약품",),
        deny_suffix=("이아닙", "가아닙", "은아닙"),
        note="의약품으로 오인될 수 있다",
    ),
    ClaimTerm(
        "처방",
        # 리프 라벨이 ``처방식사료``(114·213)다. 그 상품의 설명이 "처방식"을 쓰는 것은
        # 정상이고, 막으면 그 리프의 초안이 영구히 빈다.
        deny_suffix=("식", "전용사료"),
        aliases=("수의사처방",),
        note="의약품 처방으로 읽힌다",
    ),
)

# 수치나 단정으로 효과를 부풀리는 표현.
_EFFICACY: tuple[ClaimTerm, ...] = (
    # fold() 가 반복 글자를 줄이므로 "100%"는 "10%"로 접힌다. 사전과 입력에 같은 함수를
    # 적용하므로 양쪽이 같이 접혀 매칭이 유지된다 -- banned.py 와 같은 원칙이다.
    ClaimTerm("100%", aliases=("100퍼센트", "백퍼"), note="효과를 수치로 단정한다"),
    ClaimTerm(
        "보장",
        deny_suffix=("기간", "기한", "서", "범위"),
        note="효과를 보장한다",
    ),
    ClaimTerm(
        "즉시",
        aliases=("즉각",),
        # "즉시 발송"은 배송 문구다. 사실 슬롯에 배송 자리가 없지만 들어올 여지를 막아 둔다.
        deny_suffix=("배송", "발송", "출고", "환불"),
        note="즉각적인 효과를 단정한다",
    ),
    ClaimTerm(
        "완전해결",
        aliases=("완전해소", "말끔히해결", "싹사라집"),
        note="효과를 단정한다",
    ),
    ClaimTerm(
        "모든반려동물에게효과",
        aliases=(
            "모든강아지에게효과",
            "모든고양이에게효과",
            "모든아이에게효과",
            "누구에게나효과",
        ),
        note="수치 없이 효과를 단정한다",
    ),
    ClaimTerm(
        "부작용이전혀없",
        aliases=("부작용은전혀없", "부작용없음", "부작용이없습니다"),
        note="부작용이 없다고 단정한다",
    ),
    ClaimTerm("특효", aliases=("만병통치",), note="효과를 단정한다"),
)

# 없는 인증·자격을 주장하는 표현. 원산지 허위는 어휘 문제가 아니라 뺐다.
_CERTIFICATION: tuple[ClaimTerm, ...] = (
    ClaimTerm(
        "인증",
        # "자체 인증 기준"은 정책이 통과로 든 표현이다.
        deny_prefix=("자체",),
        deny_suffix=("받지않", "없음"),
        aliases=("식약처인증", "인증완료"),
        note="확인되지 않은 인증을 주장한다",
    ),
    ClaimTerm("허가", aliases=("정부허가", "식약처허가"), note="확인되지 않은 허가를 주장한다"),
)

CLAIM_TERMS: tuple[ClaimTerm, ...] = (*_MEDICAL, *_EFFICACY, *_CERTIFICATION)

# 표현 앞뒤에 이 말이 붙으면 단정이 아니다. ``banned.py`` 와 같은 수법이고, 키워드를
# 정규식에 날것으로 박지 않고 ``fold`` 를 거쳐 만든다 -- 정규화가 공백을 지우기 때문이다.
_NEG_BEFORE_WORDS = ("무", "없는", "없이", "비", "아닌", "불")
_NEG_AFTER_WORDS = (
    "없",
    "아니",
    "불가",
    "대신하지",
    "대체하지",
    "하지않",
    "되지않",
    "할수없",
)


def _alternation(words: tuple[str, ...]) -> str:
    return "|".join(sorted({re.escape(fold(w)) for w in words}, key=len, reverse=True))


_NEGATION_BEFORE = re.compile(f"({_alternation(_NEG_BEFORE_WORDS)})$")
_NEGATION_AFTER = re.compile(f"^({_alternation(_NEG_AFTER_WORDS)})")


@dataclass(frozen=True, slots=True)
class ClaimHit:
    """효능 단정 적중.

    위치를 담지 않는다. 정규화가 공백과 기호를 지워 원문과 길이가 달라지므로, 되찾은 위치를
    넘기면 엉뚱한 구간을 가리킨다 -- ``banned.TermHit`` 가 같은 이유로 같은 선택을 했다.
    """

    term: ClaimTerm
    matched_form: str

    @property
    def canonical(self) -> str:
        return self.term.canonical

    @property
    def reason(self) -> str:
        return self.term.note or "효능을 단정하는 표현"


def _matches(folded: str, form: str, prefixes: tuple[str, ...], suffixes: tuple[str, ...]) -> bool:
    at = folded.find(form)
    while at >= 0:
        head = folded[:at]
        tail = folded[at + len(form) :]
        denied = any(head.endswith(p) for p in prefixes) or any(tail.startswith(s) for s in suffixes)
        negated = bool(_NEGATION_BEFORE.search(head)) or bool(_NEGATION_AFTER.match(tail))
        if not denied and not negated:
            return True
        at = folded.find(form, at + 1)
    return False


def screen(text: str) -> list[ClaimHit]:
    """효능 단정 표현을 찾는다. **표현 하나당 한 번만 보고한다. 0원.**

    같은 표현이 여러 번 나와도 적중을 늘리지 않는다 -- 셀러에게 전할 말이 "치료가 세 번
    나왔다"가 아니라 "그 필드를 비웠다"이기 때문이다.
    """
    folded = fold(text or "")
    if not folded:
        return []
    hits: list[ClaimHit] = []
    for term in CLAIM_TERMS:
        prefixes = term.folded_deny_prefix()
        suffixes = term.folded_deny_suffix()
        for form in term.folded_forms():
            if _matches(folded, form, prefixes, suffixes):
                hits.append(ClaimHit(term, form))
                break
    return hits


def forbidden_examples_block() -> str:
    """프롬프트에 넣을 금지 예시 블록.

    ``POLICY_PREFIX`` **전문을 재사용하지 않는다.** 2,617자가 접두부에 더해지면 모델 선택의
    비용 교차점이 밀리고, 판정 정책과 생성 지침이 한 문자열에 섞이면 ``POLICY_VERSION`` 을
    올릴 때 초안 재사용 키까지 끊긴다. 금지 예시만 발췌하고 테스트가 드리프트를 잡는다.
    """
    covered = [ex for ex in POLICY_CLAIM_EXAMPLES if ex not in UNCOVERED_POLICY_EXAMPLES]
    return "\n".join(f'- "{example}"' for example in covered)


__all__ = [
    "CLAIM_TERMS",
    "HOLDOUT_CLAIM_EXAMPLES",
    "POLICY_CLAIM_EXAMPLES",
    "UNCOVERED_POLICY_EXAMPLES",
    "ClaimHit",
    "ClaimTerm",
    "forbidden_examples_block",
    "screen",
]
