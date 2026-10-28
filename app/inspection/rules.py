"""0단 결정론 규칙.

모델에 맡기면 안 되는 판정이 여기 있다. 연락처·계좌·외부 URL은 정규식이 100% 잡고,
금지 성분은 네거티브 리스트 매칭이라 의미 판단이 필요 없다. 둘 다 확률적 판정으로
돌리면 프롬프트 인젝션으로 뚫릴 수 있는데, 정규식은 뚫리지 않는다.

이 단계가 하는 일이 둘이다.
  1. 결정론적 위반을 찾아 모델 호출 없이 판정한다.
  2. 모델에 보낼 텍스트에서 연락처·계좌를 가린다(레닥션). 개인정보 국외 이전 범위를 줄인다.

주의: 애착 백엔드의 기존 리뷰 마스킹(아호코라식)은 건드리지 않는다. 그쪽 ``matchedRatio``의
분모를 바꾸면 기존 리뷰의 차단 비율이 조용히 변한다. 이것은 판정 전용 별 경로다.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

from app.inspection.banned import find_terms
from app.schemas.inspection import ViolationCategory

# 전화번호. 한글 숫자 표기와 구분자 삽입을 함께 본다.
# 평범한 숫자 표기를 먼저 보고, 그 다음에 한글 음차 표기를 본다.
# 순서를 뒤집으면 "010-1234-5678"에서 앞 세 글자만 잡혀 뒤가 가려지지 않는다.
_PHONE = re.compile(
    r"0\d{1,2}[\-.\s]?\d{3,4}[\-.\s]?\d{4}"
    r"|(?:공|영)\s*[\-.\s]?(?:일|1)\s*[\-.\s]?(?:공|영)"
    r"(?:[\s\-.]*[0-9일이삼사오육칠팔구공영]){6,}"
)
# 계좌번호로 보이는 숫자 뭉치. 은행명이 함께 나오면 강한 신호다.
_ACCOUNT = re.compile(r"\d{2,3}[\-\s]\d{2,6}[\-\s]\d{2,8}")
_BANK = re.compile(r"(국민|신한|우리|하나|농협|기업|카카오\s*뱅크|토스\s*뱅크|새마을|신협|우체국)\s*(은행)?")
# 외부 메신저·마켓 유도
_MESSENGER = re.compile(
    r"(카톡|카카오\s*톡|오픈\s*채팅|open\.kakao|텔레\s*그램|telegram|라인\s*아이디|insta|디엠|dm)",
    re.I,
)
# 부정 전방탐색 끝에 **호스트 경계**를 둔다. 경계가 없으면 자사 도메인이 접두사로 쓰여
# ``aechak.co.kr.evil.example`` 의 실제 호스트(``evil.example``)가 자사로 읽혔다.
# 경계는 호스트가 끝나는 자리에 올 수 있는 글자뿐이고, 그래서 서브도메인(``shop.``)과
# 경로(``/p/1``) 허용은 그대로 남는다.
_EXTERNAL_URL = re.compile(r"https?://(?!(?:[a-z0-9-]+\.)*aechak\.co\.kr(?:[:/?#]|\s|$))[^\s]+", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# 판정자를 향한 지시형 문장. 강도를 둘로 가른다.
#
# 강한 신호는 정상적인 상품 설명에 나올 이유가 없다. 앞선 지시를 무시하라거나 시스템
# 프롬프트를 거론하는 문장은 판정기를 겨냥한 것이고, 그 자체가 정책 위반이라 즉시 차단한다.
# 데이터 봉투의 구분자를 흉내 내는 것도 강한 신호다. 상품 설명과 리뷰가 모델에 나갈 때
# 아래 ``ENVELOPE_TOKENS``로 감싸이므로, 본문 안에 같은 토큰이 있으면 봉투를 닫고 그 밖에서
# 운영자 지시처럼 보이는 문장을 끼울 수 있다. 반려동물 용품 문구에 이 토큰이 정당하게 들어갈
# 이유가 없어서 차단으로 둔다. 대괄호를 두른 시스템 머리줄도 같은 모양의 공격이다.
_INJECTION_STRONG = re.compile(
    r"(이전\s*지시|앞의?\s*지시|시스템\s*프롬프트|너는\s*이제"
    r"|ignore\s+(all\s+)?previous|disregard\s+(all\s+)?(previous|above)"
    r"|you\s+are\s+now|new\s+instructions?"
    r"|</?본문>|</대상>|<대상|\[시스템\]|\[system\])",
    re.I,
)
# 약한 신호는 다른 맥락에서도 나온다. 기록해 모델에게 넘기고 차단은 하지 않는다.
# 다만 모델에 보내는 텍스트에서는 가린다 — 찾아 놓고 그대로 보여주면 방어 한 겹을 버린다.
_INJECTION_WEAK = re.compile(
    r"(무시하\w*\s*(고|라|세요)|승인\s*(해|하세요|해주세요)|통과\s*(시켜|시키세요))",
    re.I,
)

_REDACTION = "[가림]"

# 모델에 보내는 본문을 감싸는 구분자. ``triage.py``와 ``pipeline.py``가 같은 토큰을 쓴다.
#
# 한 자리에 모아 두는 이유가 있다. 본문 안에 같은 토큰이 있으면 셀러 설명이 봉투를 **닫고**
# 그 밖에서 새 봉투를 열 수 있고, 그러면 주입된 문장이 모델 입장에서 운영자가 끼운 지시와
# 구별되지 않는다. 선별이 켜지면 그 한 번의 CLEAR가 Sonnet 두 관점과 백엔드 게이트를 모두
# 건너뛰게 하므로, 성공한 인젝션이 곧 무검수 자동 승인이 된다.
ENVELOPE_TOKENS: tuple[str, ...] = ("</본문>", "<본문>", "</대상>", "<대상")
_NEUTRALIZED = "[태그]"

# 폭이 0이거나 방향만 바꾸는 서식 문자. **글자 모양에 나타나지 않으면서 문자열 비교만 깬다.**
#
# 전각 부호로 쓴 닫는 태그는 NFKC가 모아 주므로 차단으로 잡혔는데, 글자 사이에 폭 0 문자를
# 끼운 표기는 적중이 0건이었다. 소비자가 파서가 아니라 모델이라는 점이 그 비대칭을 위험하게
# 만든다 -- 모델은 폭 0 문자를 사이에 둔 토큰을 닫는 태그로 읽을 수 있고, 그러면 본문이
# 봉투를 닫는다.
#
# **코드포인트를 손으로 적지 않는다.** 손으로 적은 열거가 바로 옆 글자를 전부 열어 둔 것이
# 이 자리의 세 번째 결함이었다.
#
# 그래서 근거를 **유니코드 분류**로 옮긴다. ``Cf``(format) 전부 + 분류가 ``Cf`` 가 아니면서
# 폭이 0인 글자 집합이고, 후자만 열거한다. 테스트가 유니코드 전 범위를 훑어 이 둘의 합집합이
# 전건 차단되는 것을 단언하므로 열거를 늘려도 측정이 함께 따라온다.
#
# 대가를 적어 둔다. ``U+200D``(ZWJ)와 이형자 선택자를 지우므로 이모지 결합 문자열이
# 낱글자로 풀린다. 모델이 보는 문자열과 인용 검증 기준이 같은 함수를 지나므로 검증이
# 어긋나지는 않고, 화면에 나가는 인용의 이모지 모양만 달라진다. 봉투가 닫히는 쪽이 훨씬
# 비싼 실패다. **한글 자모와 결합 악센트는 지우지 않는다** -- ``Mn`` 전체를 지우면
# 베트남어·태국어 본문이 망가지므로 분류로 긋지 않고 폭 0 인 것만 열거한다.
ZERO_WIDTH_MARKS: frozenset[str] = frozenset(
    "\u034f"  # COMBINING GRAPHEME JOINER
    "\u115f\u1160"  # 한글 초성·중성 필러
    "\u17b4\u17b5"  # 크메르 고유 모음 (폭 0)
    "\u180b\u180c\u180d\u180f"  # 몽골 자유 이형자 선택자
    "\u3164"  # 한글 필러
    "\uffa0"  # 반각 한글 필러
    + "".join(chr(cp) for cp in range(0xFE00, 0xFE10))  # 이형자 선택자 1-16
    + "".join(chr(cp) for cp in range(0xE0100, 0xE01F0))  # 이형자 선택자 보충
)


@lru_cache(maxsize=8192)
def _is_invisible(char: str) -> bool:
    """그 한 글자가 불가시인가. **글자마다 보고 외운다.**

    유니코드 전 범위를 미리 훑어 표를 만들면 import 가 느려지고(``Cf`` 는 ``U+E0001`` 까지
    흩어져 있다) 범위를 하드코딩하면 파이썬의 유니코드 판이 올라갈 때 조용히 어긋난다.
    입력에 실제로 나타난 글자만 분류를 묻고 그 답을 외우면 둘 다 피한다.
    """
    return unicodedata.category(char) == "Cf" or char in ZERO_WIDTH_MARKS


def strip_invisible(text: str) -> str:
    """불가시 문자를 지운다. 판정과 중성화가 같은 글자 집합을 보게 만드는 함수다."""
    if text.isascii():
        # ASCII 에는 ``Cf`` 가 없다. 리뷰 본문 대부분이 아니지만 상품명·옵션명에서 흔하다.
        return text
    return "".join(char for char in text if not _is_invisible(char))


def neutralize_envelope(text: str) -> str:
    """본문에 들어 있는 봉투 구분자를 중성화한다. 프롬프트에 끼우기 **전에** 부른다.

    정규식 계층이 이 토큰을 강한 신호로 이미 차단하지만, 방어를 그 한 줄에 걸지 않는다 --
    정규식을 좁히거나 차단을 비차단으로 내리는 변경이 조용히 봉투를 다시 열기 때문이다.
    긴 토큰부터 치환해야 ``</본문>``이 ``<본문>`` 규칙에 먼저 쪼개지지 않는다.

    **폭 0 문자를 먼저 지운다.** 지우지 않으면 글자 사이에 그 문자를 끼운 토큰이 치환을
    통째로 빠져나가고, 조립된 봉투의 토큰 수 검사(``envelope_wellformed``)는 정확한 토큰만
    세므로 그 탈출이 검사에 보이지 않는다 -- 통과로 찍히면서 모델 입장에서는 봉투가 닫혀 있었다.
    """
    out = strip_invisible(text)
    for token in ENVELOPE_TOKENS:
        out = out.replace(token, _NEUTRALIZED)
    return out


@dataclass(slots=True)
class RuleHit:
    category: ViolationCategory
    quote: str
    # 금지 성분 적중은 위치를 담지 않는다. 정규화가 공백과 기호를 지워 원문과 길이가
    # 달라지므로, 되찾은 위치를 넘기면 엉뚱한 구간을 가리킨다.
    start: int | None
    end: int | None
    reason: str
    # 차단 여부를 분류가 아니라 적중마다 둔다. 같은 분류 안에서 강도가 갈리는 경우가 있다
    # (지시형 문장이 그렇다). 분류로 끌어내면 그 구분을 표현할 수 없다.
    blocking: bool


@dataclass(slots=True)
class RuleResult:
    """0단 결과.

    ``blocking``이 있으면 모델을 부르지 않고 바로 판정한다.
    ``redacted``는 모델에 보낼 텍스트이고, 오프셋이 원문과 달라질 수 있으므로
    모델이 돌려준 인용은 이 텍스트 기준으로 검증한다.
    """

    hits: list[RuleHit] = field(default_factory=list)
    redacted: str = ""

    @property
    def blocking(self) -> list[RuleHit]:
        """즉시 반려까지 갈 수 있는 위반. 결정론적으로 확정된 것만이다."""
        return [h for h in self.hits if h.blocking]

    @property
    def triage_trumping(self) -> list[RuleHit]:
        """선별에 묻지 않고 바로 승급시킬 적중. **적중 전부가 아니다.**

        지시형 문장(비차단 인젝션)은 선별에 물으면 안 된다. 물으면 Haiku 1회로 CLEAR가 나올
        수 있고, 그러면 결정론 계층이 의심한 건이 파이프라인에서 가장 약한 심사를 받는다.

        성분 적중은 다르다. ``ADVISORY_TERMS``의 적중은 "위험하다"가 아니라 "카테고리를
        함께 봐야 안다"는 뜻이고(``banned.py`` 머리 참조), 선별은 성분을 **판정하는** 것이
        아니라 더 볼지만 정한다. 그래서 물어도 안전하다. 전부 승급시키면 코코넛오일·우유처럼
        반려동물 상품에 흔한 성분명 한 단어로 셀러가 선별 절감을 0으로 만들고 Sonnet 2회를
        강제할 수 있다.
        """
        return [h for h in self.hits if h.category is not ViolationCategory.BANNED_INGREDIENT]


def normalize(text: str) -> str:
    """판정 전용 정규화. 호환 분해로 동형문자를 모으고 반복을 줄인다.

    폭 0 문자를 먼저 지운다. NFKC 는 그것들을 지우지 않으므로, 지우지 않으면 전화번호나
    봉투 토큰의 글자 사이에 그 문자를 끼운 표기가 **글자 모양이 같은데 전 규칙을
    빠져나간다.** 전각 표기는 모으면서 폭 0 표기는 못 모으는 비대칭을 닫는다.
    """
    folded = unicodedata.normalize("NFKC", strip_invisible(text))
    # 같은 글자를 세 번 이상 늘린 우회 표기를 둘로 줄인다
    return re.sub(r"(.)\1{2,}", r"\1\1", folded)


def screen(text: str) -> RuleResult:
    if not text:
        return RuleResult(redacted="")

    result = RuleResult()
    probe = normalize(text)

    for pattern, category, reason, blocking in (
        (_PHONE, ViolationCategory.CONTACT_INFO, "전화번호로 보이는 문자열", True),
        (_EMAIL, ViolationCategory.CONTACT_INFO, "이메일 주소", True),
        (_MESSENGER, ViolationCategory.CONTACT_INFO, "외부 메신저 유도", True),
        # 외부 링크도 차단한다. 자사 도메인은 정규식이 이미 제외하고, 상품 설명에 외부 링크를
        # 넣을 정당한 이유가 사실상 없다. 플랫폼 이탈 유도가 이 기능을 만든 이유 중 하나다.
        (_EXTERNAL_URL, ViolationCategory.OFF_PLATFORM, "외부 링크", True),
        (_INJECTION_STRONG, ViolationCategory.SPAM, "판정기를 겨냥한 지시형 문장", True),
        (_INJECTION_WEAK, ViolationCategory.SPAM, "지시형으로 읽힐 수 있는 문장", False),
    ):
        for m in pattern.finditer(probe):
            result.hits.append(RuleHit(category, m.group(0), m.start(), m.end(), reason, blocking))

    for m in _ACCOUNT.finditer(probe):
        window = probe[max(0, m.start() - 12) : m.end() + 12]
        if _BANK.search(window):
            result.hits.append(
                RuleHit(
                    ViolationCategory.CONTACT_INFO,
                    m.group(0),
                    m.start(),
                    m.end(),
                    "은행명과 함께 나온 계좌번호",
                    True,
                )
            )

    # 금지 성분은 전용 사전이 본다. 정규화로 띄어쓰기·기호·반복·전각 우회를 무력화하고,
    # 부정 표현과 제외 접미("포도당", "카카오톡")를 거른 뒤 적중을 돌려준다.
    for found in find_terms(text):
        result.hits.append(
            RuleHit(
                ViolationCategory.BANNED_INGREDIENT,
                found.term.canonical,
                None,
                None,
                found.term.note or "반려동물에게 위험한 성분",
                found.term.blocking,
            )
        )

    # 적중 구간은 정규화한 텍스트 기준이라 원문과 어긋날 수 있다(호환 분해와 반복 축약이
    # 길이를 바꾼다). 백엔드에 넘기는 구간은 원문 기준이어야 하므로 되찾는다.
    _realign(result.hits, text)
    result.redacted = _redact(text)
    return result


def _realign(hits: list[RuleHit], original: str) -> None:
    """적중 문자열을 원문에서 다시 찾아 구간을 맞춘다. 못 찾으면 그대로 둔다.

    **인용별 탐색 위치를 들고 간다.** 같은 문자열이 두 번 나오면 둘째 적중도 첫 구간을
    가리켜, 계약의 ``spanStart``/``spanEnd`` 와 콘솔 하이라이트가 둘 다 첫 번째를 가리킨다.
    못 찾은 적중은 위치를 올리지 않는다 -- 정규화로 길이가 달라진 경우라서 그 뒤 적중의
    탐색 범위를 좁힐 근거가 없다.
    """
    cursor: dict[str, int] = {}
    for hit in hits:
        if hit.start is None:
            continue
        found = original.find(hit.quote, cursor.get(hit.quote, 0))
        if found >= 0:
            hit.start = found
            hit.end = found + len(hit.quote)
            cursor[hit.quote] = hit.end


def _redact(text: str) -> str:
    """모델과 카프카에 평문으로 남기지 않을 것들을 가린다."""
    out = text
    # 지시형 문장을 함께 가린다. 규칙이 찾아 놓고도 그대로 보여주면 방어 한 겹을 버린다.
    for pattern in (_PHONE, _EMAIL, _ACCOUNT, _INJECTION_STRONG, _INJECTION_WEAK):
        out = pattern.sub(_REDACTION, out)
    return out
