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
_EXTERNAL_URL = re.compile(r"https?://(?!(?:[a-z0-9-]+\.)*aechak\.co\.kr)[^\s]+", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# 판정자를 향한 지시형 문장. 강도를 둘로 가른다.
#
# 강한 신호는 정상적인 상품 설명에 나올 이유가 없다. 앞선 지시를 무시하라거나 시스템
# 프롬프트를 거론하는 문장은 판정기를 겨냥한 것이고, 그 자체가 정책 위반이라 즉시 차단한다.
_INJECTION_STRONG = re.compile(
    r"(이전\s*지시|앞의?\s*지시|시스템\s*프롬프트|너는\s*이제"
    r"|ignore\s+(all\s+)?previous|disregard\s+(all\s+)?(previous|above)"
    r"|you\s+are\s+now|new\s+instructions?)",
    re.I,
)
# 약한 신호는 다른 맥락에서도 나온다. 기록해 모델에게 넘기고 차단은 하지 않는다.
# 다만 모델에 보내는 텍스트에서는 가린다 — 찾아 놓고 그대로 보여주면 방어 한 겹을 버린다.
_INJECTION_WEAK = re.compile(
    r"(무시하\w*\s*(고|라|세요)|승인\s*(해|하세요|해주세요)|통과\s*(시켜|시키세요))",
    re.I,
)

_REDACTION = "[가림]"


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


def normalize(text: str) -> str:
    """판정 전용 정규화. 호환 분해로 동형문자를 모으고 반복을 줄인다."""
    folded = unicodedata.normalize("NFKC", text)
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
    """적중 문자열을 원문에서 다시 찾아 구간을 맞춘다. 못 찾으면 그대로 둔다."""
    for hit in hits:
        if hit.start is None:
            continue
        found = original.find(hit.quote)
        if found >= 0:
            hit.start = found
            hit.end = found + len(hit.quote)


def _redact(text: str) -> str:
    """모델과 카프카에 평문으로 남기지 않을 것들을 가린다."""
    out = text
    # 지시형 문장을 함께 가린다. 규칙이 찾아 놓고도 그대로 보여주면 방어 한 겹을 버린다.
    for pattern in (_PHONE, _EMAIL, _ACCOUNT, _INJECTION_STRONG, _INJECTION_WEAK):
        out = pattern.sub(_REDACTION, out)
    return out
