"""추출 입력 게이트와 봉투 조립. **모델 0회, 0원.**

## 왜 ``rules.py``를 고치지 않는가

리뷰 N건을 한 봉투에 넣으려면 구분자가 필요하다. 새 토큰(``<리뷰 번호="3">`` 같은 것)을
만들면 ``rules.ENVELOPE_TOKENS``에 등록해야 하고, 그 자리는 과거 "봉투 탈출" 치명 결함이
난 곳이다. 등록하지 않으면 중성화가 돌지 않아 리뷰 본문이 봉투를 닫을 수 있고, 등록하면서
``_INJECTION_STRONG``까지 넓히면 **기존 검수 판정에 새 차단 적중이 생겨 과거 리뷰의 차단
비율이 조용히 변한다.**

그래서 **새 토큰을 만들지 않고** 이미 등록된 ``<본문>``/``</본문>``을 리뷰마다 반복한다.
얻는 것이 셋이다.

1. ``neutralize_envelope()``가 중성화하고 **동시에** ``_INJECTION_STRONG``이 차단 신호로
   잡는다. 두 겹이 이미 걸려 있다.
2. 검수 경로의 판정 바이트가 1비트도 변하지 않는다.
3. ``rules.py``가 무수정이다.

## 봉투에 들어가는 **모든** 문자열이 게이트를 지난다

앞선 판단 하나가 틀렸다. "머리줄은 봉투 토큰이 아니라 평문이라 안전하다"로 적었는데,
머리줄에 들어가는 ``optionName``은 **셀러가 타이핑한 자유 텍스트**다 -- 백엔드가 주는
값이지만 백엔드가 만든 값이 아니고, 리뷰 작성 시점에 복사된 ``optionNameSnapshot``이다.
원문을 그대로 박으면 그 한 칸으로 봉투가 닫히고 진짜 본문보다 **앞에** 운영자 지시처럼
보이는 위조 블록이 열린다. 실제로 재현했다.

그래서 ``header_safe()``가 머리줄·메타줄의 모든 조각을 레닥션 -> 중성화 -> 줄바꿈 제거 ->
길이 상한으로 통과시킨다. 같은 레포의 검수 경로가 이미 그 관례다 -- ``optionNames``를
``inspect_text()``로 본문에 넣어 ``neutralize_envelope()``를 거치게 한다. 요약 경로만
그것을 깼던 것이다. 카테고리 경로는 백엔드가 리프 id로 렌더하는 값이라 신뢰하기로 했지만,
**신뢰는 0원짜리 방어보다 비싸다.** 같은 함수를 태운다.

남는 공격면은 본문 안에 가짜 머리줄(``[리뷰 9999 / 5점]``)을 끼워 **인용 귀속을 속이는
것** 하나인데, 리뷰별 인용 검증이 그것을 구조적으로 무력화한다 -- 인용은 모델이 지목한
``reviewId``의 본문에서 찾아져야 하므로 귀속을 속이면 검증에 실패해 그 항목이 떨어진다.
비용 0의 방어다. **리뷰 본문은 절대 메타 줄에 두지 않는다.**

중성화 자기검사는 **렌더된 전문**을 봐야 한다. 본문 구간만 쪼개 보는 검사(하네스와 테스트가
둘 다 그랬다)는 머리줄의 탈출을 구조적으로 보지 못한다.

## 왜 출력 필터가 아니라 입력 레닥션인가

백엔드에 PII 마스킹이 없다 -- 돌고 있는 것은 금칙어(비속어) 아호코라식 마스킹이고
전화번호·주소·주민번호 패턴 매칭은 없다. 작성자가 본문에 쓴 전화번호는 지금 그대로
저장되고 그대로 서빙되며, 요약은 그것을 여러 리뷰에서 모아 한 문단으로 압축하는 **새 노출
표면**이다.

출력 필터만 두면 모델이 이미 전화번호를 본 뒤다. 입력에서 가리면 **모델은 전화번호를 애초에
보지 못하고 따라서 인용에 넣을 수 없다** -- 왕복이 구조적으로 막힌다.

정직한 한계: ``rules._redact()``는 실명과 주소를 가리지 못한다. 완화는 인용 길이 상한 +
리터럴 강제 + 항목 열거가 사람이 아니라 상품 속성을 가리키게 고정하는 것 셋이고, **제거가
아니라 완화다.**
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field

from app.inspection import rules
from app.schemas.inspection import ViolationCategory
from app.schemas.summary import ExcludeReason, ReviewInput

# 인용 길이 상한. 모델이 긴 구간을 통째로 옮기면 실명·주소가 따라 나온다.
# ``strict`` 모드가 ``maxLength``를 받지 않아 프롬프트로 누르고 코드가 사후 검사한다.
QUOTE_MAX_CHARS = 80

# 마스킹 기호가 이 비율을 넘는 인용은 버린다. 백엔드 금칙어 마스킹이 ``*``로 치환한 구간이
# 그대로 요약에 올라오면 화면이 읽히지 않고, 무엇을 가렸는지도 전해지지 않는다.
MASK_HEAVY_RATIO = 0.5
_MASK_CHARS = "*"

# 머리줄 조각의 길이 상한. 머리줄은 한 줄이어야 하고, 길면 그 자체로 모델이 읽는 지시가 된다.
OPTION_NAME_MAX_CHARS = 40
CATEGORY_SEGMENT_MAX_CHARS = 30


def header_safe(text: str, *, max_chars: int) -> str:
    """봉투 머리줄·메타줄에 들어갈 조각 하나를 안전하게 만든다.

    네 겹이고 순서가 중요하다.

    1. ``rules.screen().redacted`` -- 연락처와 지시형 문장을 ``[가림]``으로 바꾼다.
    2. ``rules.neutralize_envelope()`` -- 남은 봉투 구분자를 ``[태그]``로 바꾼다.
       **이것이 봉투를 닫는 것을 막는 겹이다.**
    3. 줄바꿈과 제어문자를 공백으로 접는다. 머리줄이 두 줄이 되면 둘째 줄이 봉투 바깥의
       운영자 지시처럼 보인다 -- 토큰을 중성화해도 그 모양은 남는다.
    4. 길이 상한. 긴 머리줄은 그 자체가 모델이 읽는 지시가 된다.

    ``app/seller_draft/prompt.envelope_safe()``와 1·2가 같다. 거기서는 봉투 **안**의
    자유 텍스트를 다루므로 줄바꿈을 지우지 않는다(설명은 여러 줄이 정상이다).
    머리줄은 반대로 한 줄이어야 해서 함수를 따로 둔다.
    """
    safe = rules.neutralize_envelope(rules.screen(text).redacted)
    safe = " ".join(safe.split())
    return safe[:max_chars]


def dedupe_key(text: str) -> str:
    """중복 접기 키. **NFKC + 공백 정규화뿐이고 반복 축약을 하지 않는다.**

    설계는 ``banned.fold()`` 를 쓰자고 했다. 쓰면 안 된다 -- 실행해 확인했다.
    그 함수는 금지 성분의 우회 표기를 잡으려고 **같은 글자 반복을 한 번으로 줄인다.**
    그래서 리뷰 본문에 쓰면 이렇게 된다.

        "2kg 먹였어요"   == "22kg 먹였어요"
        "1살이에요"      == "11살이에요"
        "좋아요"         == "좋아아요"

    반려동물 리뷰에서 체중과 나이 숫자는 **내용을 가르는 바로 그 글자**다. 그것을 접으면
    서로 다른 리뷰가 "중복"으로 분모에서 조용히 빠지고, 그 손실은 어디에도 보고되지 않는
    형태로 센다(``duplicateFolded`` 가 올라가지만 그것이 거짓 중복인지 알 수 없다).
    ``rules.normalize()`` 도 3회 이상 반복을 줄여 "2000원"과 "200원"을 같게 만든다.

    중복 접기가 막으려는 것은 **같은 문구를 여러 계정이 붙인 복붙**이고 그것은 성질상
    글자 그대로다. 그래서 공백 차이와 전각 표기만 흡수하고 나머지는 건드리지 않는다 --
    거짓 중복이 0 인 쪽을 고른다.

    불가시 문자는 그 셋에 **더한다.** 글자 모양이 같은 복붙이 서로 다른 키를 받아 N건이
    분모와 ``mentionCount`` 에 N번 들어갔다. 지우는 것은 "모양이 같은 것만 모으는" 일이라
    위에서 ``fold()`` 를 거부한 선(숫자가 내용을 가른다)을 넘지 않는다.
    """
    return " ".join(unicodedata.normalize("NFKC", rules.strip_invisible(text)).split())


@dataclass(frozen=True, slots=True)
class PreparedReview:
    """게이트를 통과한 리뷰 하나. ``model_text``가 **모델이 실제로 보는 문자열**이다.

    인용 검증을 반드시 이 문자열 기준으로 한다. 원문 기준으로 검증하면 레닥션과 절단
    때문에 전건이 탈락한다 -- ``pipeline._interpret``이 ``redacted.find(quote)``를 쓰는 것과
    같은 이유다.
    """

    review_id: int
    rating: int
    option_name: str
    model_text: str
    truncated: bool
    pii_redacted: bool


@dataclass(frozen=True, slots=True)
class ExcludedReview:
    review_id: int
    reason: ExcludeReason
    note: str


@dataclass(slots=True)
class PreparedBatch:
    prepared: list[PreparedReview] = field(default_factory=list)
    excluded: list[ExcludedReview] = field(default_factory=list)

    @property
    def injection_excluded(self) -> int:
        return sum(1 for e in self.excluded if e.reason is ExcludeReason.INJECTION)

    @property
    def duplicate_folded(self) -> int:
        return sum(1 for e in self.excluded if e.reason is ExcludeReason.DUPLICATE)

    @property
    def truncated(self) -> int:
        return sum(1 for p in self.prepared if p.truncated)

    @property
    def pii_redacted(self) -> int:
        return sum(1 for p in self.prepared if p.pii_redacted)


def _is_strong_injection(hit: object) -> bool:
    """판정기를 겨냥한 지시형 문장 적중인가.

    ``rules.py``를 읽지 않고 분류만 보면 약한 신호까지 걸러 모집단이 조용히 줄어든다.
    ``_INJECTION_STRONG``은 ``SPAM`` + ``blocking=True`` 하나이고 약한 신호는
    ``blocking=False``라서 둘이 이 조건으로 갈린다. 정규식을 import하지 않고 공개된
    적중 모양으로 가르는 것이 ``rules.py`` 무수정 규약에 맞는다.
    """
    category = getattr(hit, "category", None)
    return category is ViolationCategory.SPAM and bool(getattr(hit, "blocking", False))


def prepare(reviews: list[ReviewInput], *, content_chars: int) -> PreparedBatch:
    """추출 입력을 만든다. 순서가 중요해서 그대로 적어 둔다.

    1. ``rules.screen``으로 전화·이메일·계좌·지시형을 ``[가림]``으로 치환한다.
    2. 판정기를 겨냥한 지시형 문장이 든 리뷰는 **추출 입력에서 뺀다.** 그 리뷰는
       ``UNEXTRACTED`` + ``INJECTION``으로 적히고 "항목 없음"에 섞이지 않는다.
    3. 공백·전각만 정규화한 **원문**이 완전히 같은 리뷰는 한 건만 남긴다. 같은 문구를
       여러 계정이 붙인 묶음이 "반복되는 장점"을 혼자 만드는 것을 막는다. 반복 축약을
       하지 않는 이유는 ``dedupe_key`` 에 적었고, 레닥션·절단 **전** 원문으로 접는
       이유는 그 단계 주석에 적었다.
    5. 본문을 절단한다. 절단은 손실이라 플래그로 남긴다.
    6. 봉투 구분자를 중성화한다. **본문을 프롬프트에 끼우기 전**이다.
    7. 옵션명을 ``header_safe`` 로 통과시킨다. 머리줄도 봉투 안이다.

    **모집단을 ``rules.screen().blocking``으로 걸러내지 않는다.** "카톡"이나 외부 URL
    적중을 가진 ``PUBLIC`` 리뷰가 실재하고, 그걸로 걸러내면 분모가 조용히 줄어든다.
    그 본문은 레닥션하고 모집단에는 남긴다. 예외는 지시형 적중 하나뿐이다.
    """
    batch = PreparedBatch()
    seen_folded: dict[str, int] = {}

    for review in reviews:
        raw = review.content or ""
        if not raw.strip():
            batch.excluded.append(ExcludedReview(review.reviewId, ExcludeReason.EMPTY, "본문이 비어 있다"))
            continue

        screened = rules.screen(raw)
        if any(_is_strong_injection(hit) for hit in screened.hits):
            batch.excluded.append(
                ExcludedReview(
                    review.reviewId,
                    ExcludeReason.INJECTION,
                    "판정기를 겨냥한 지시형 문장이 있어 추출 입력에서 뺐다. 항목 없음이 아니다",
                )
            )
            continue

        # **중복 키는 레닥션·절단 전 원문으로 만든다.** 절단본으로 접으면 앞 400자가 같고
        # 뒤가 다른 두 리뷰가 거짓 중복이 되고(백엔드 상한이 1000자라 최대 길이 리뷰의
        # 60%가 키에서 보이지 않는다), 레닥션본으로 접으면 서로 다른 연락처가 같은
        # ``[가림]``이 되어 또 거짓 중복이 된다. 둘 다 실측으로 확인했다. 복붙 탐지에
        # 필요한 것은 **원문 동일성**이고, 레닥션과 절단은 모델 입력용 변환이라 신원
        # 판정에 쓰면 안 된다 -- ``dedupe_key`` 가 ``fold()`` 를 거부한 것과 같은 이유다.
        folded = dedupe_key(raw)
        first = seen_folded.get(folded)
        if first is not None:
            batch.excluded.append(
                ExcludedReview(
                    review.reviewId,
                    ExcludeReason.DUPLICATE,
                    f"원문이 리뷰 {first}과 글자 그대로 같다. 같은 문구를 두 번 세지 않는다",
                )
            )
            continue
        seen_folded[folded] = review.reviewId

        body = screened.redacted
        truncated = len(body) > content_chars
        if truncated:
            body = body[:content_chars]

        batch.prepared.append(
            PreparedReview(
                review_id=review.reviewId,
                rating=review.rating,
                # **머리줄도 봉투 안이다.** 셀러가 타이핑한 값이라 원문을 박으면 봉투가 닫힌다.
                option_name=header_safe(review.optionName or "기본", max_chars=OPTION_NAME_MAX_CHARS)
                or "기본",
                model_text=rules.neutralize_envelope(body),
                truncated=truncated,
                # 레닥션이 돌았는지는 **절단 전** 전문으로 본다. 절단본으로 비교하면
                # 가린 구간이 뒤쪽에 있던 리뷰가 "가린 것 없음"으로 적힌다.
                pii_redacted=screened.redacted != raw,
            )
        )

    return batch


def render_envelope(category_path: list[str], prepared: list[PreparedReview]) -> str:
    """리뷰 N건을 **이미 등록된** 봉투 토큰으로 감싼다.

    머리줄은 새 봉투 토큰을 만들지 않는다 -- 등록하면 ``_INJECTION_STRONG``까지 넓히고
    싶은 압력이 생기고 그러면 과거 리뷰의 차단 비율이 조용히 변한다. 대신 **머리줄에
    들어가는 조각마다 ``header_safe`` 를 태운다.** 옵션명은 ``prepare`` 가 이미 태웠고,
    카테고리 경로는 여기서 태운다(백엔드 렌더 값이지만 0원짜리 방어를 생략할 이유가 없다).
    """
    lines = ['<대상 종류="REVIEW_SUMMARY">']
    if category_path:
        segments = [header_safe(segment, max_chars=CATEGORY_SEGMENT_MAX_CHARS) for segment in category_path]
        lines.append(f"카테고리: {' > '.join(segment for segment in segments if segment)}")
    for item in prepared:
        suffix = " / 본문 절단됨" if item.truncated else ""
        lines.append(f"[리뷰 {item.review_id} / {item.rating}점 / 옵션 {item.option_name}{suffix}]")
        lines.append("<본문>")
        lines.append(item.model_text)
        lines.append("</본문>")
    lines.append("</대상>")
    return "\n".join(lines)


def envelope_wellformed(text: str, *, review_count: int) -> bool:
    """조립된 봉투 전문에 **여분의** 봉투 토큰이 없는가.

    본문 구간만 쪼개 보는 검사(``split("<본문>")``)로는 머리줄·메타줄의 탈출을
    구조적으로 볼 수 없다 -- 위조 토큰이 첫 조각에 들어가 검사 창 밖으로 빠진다.
    실제로 그 맹점 때문에 옵션명 탈출이 "중성화 전부 통과"로 찍혔다.

    그래서 창을 좁히는 대신 **세는 쪽으로 바꾼다.** 정당한 봉투는 토큰 수가 정확히
    정해져 있고, 어느 조각에서 탈출이 일어나도 그 수가 어긋난다.

    토큰끼리 부분 문자열이 아닌 것에 기대고 있다 -- ``</본문>`` 은 ``<본문>`` 을 품지
    않고(가운데 ``/`` 가 끊는다) ``</대상>`` 도 ``<대상`` 을 품지 않는다. 테스트가 그
    전제를 직접 단언한다.
    """
    return {
        "<대상": 1,
        "</대상>": 1,
        "<본문>": review_count,
        "</본문>": review_count,
    } == {token: text.count(token) for token in ("<대상", "</대상>", "<본문>", "</본문>")}


def mask_heavy(quote: str) -> bool:
    """마스킹 기호가 절반 이상인 인용인가."""
    if not quote:
        return False
    masked = sum(1 for ch in quote if ch in _MASK_CHARS)
    return masked / len(quote) >= MASK_HEAVY_RATIO


def batches(prepared: list[PreparedReview], size: int) -> list[list[PreparedReview]]:
    """추출 배치로 쪼갠다. 배치 상한은 폴 간격 예산이 정한 수다."""
    if size <= 0:
        return [list(prepared)] if prepared else []
    return [prepared[i : i + size] for i in range(0, len(prepared), size)]


__all__ = [
    "CATEGORY_SEGMENT_MAX_CHARS",
    "MASK_HEAVY_RATIO",
    "OPTION_NAME_MAX_CHARS",
    "QUOTE_MAX_CHARS",
    "ExcludedReview",
    "PreparedBatch",
    "PreparedReview",
    "batches",
    "dedupe_key",
    "envelope_wellformed",
    "header_safe",
    "mask_heavy",
    "prepare",
    "render_envelope",
]
