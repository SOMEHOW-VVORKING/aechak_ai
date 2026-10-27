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

남는 공격면은 본문 안에 가짜 머리줄(``[리뷰 9999 / 5점]``)을 끼워 **인용 귀속을 속이는
것** 하나인데, 리뷰별 인용 검증이 그것을 구조적으로 무력화한다 -- 인용은 모델이 지목한
``reviewId``의 본문에서 찾아져야 하므로 귀속을 속이면 검증에 실패해 그 항목이 떨어진다.
비용 0의 방어다. 머리줄은 봉투 토큰이 아니라 평문이고, 메타 줄(카테고리)은 백엔드가 리프
id로 렌더한 신뢰 값이라 봉투 바깥에 둬도 안전하다. **리뷰 본문은 절대 메타 줄에 두지 않는다.**

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

from dataclasses import dataclass, field

from app.inspection import banned, rules
from app.schemas.inspection import ViolationCategory
from app.schemas.summary import ExcludeReason, ReviewInput

# 인용 길이 상한. 모델이 긴 구간을 통째로 옮기면 실명·주소가 따라 나온다.
# ``strict`` 모드가 ``maxLength``를 받지 않아 프롬프트로 누르고 코드가 사후 검사한다.
QUOTE_MAX_CHARS = 80

# 마스킹 기호가 이 비율을 넘는 인용은 버린다. 백엔드 금칙어 마스킹이 ``*``로 치환한 구간이
# 그대로 요약에 올라오면 화면이 읽히지 않고, 무엇을 가렸는지도 전해지지 않는다.
MASK_HEAVY_RATIO = 0.5
_MASK_CHARS = "*"


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
    3. 본문을 절단한다. 절단은 손실이라 플래그로 남긴다.
    4. ``banned.fold``로 접은 문자열이 완전히 같은 리뷰는 한 건만 남긴다. 같은 문구를
       여러 계정이 붙인 묶음이 "반복되는 장점"을 혼자 만드는 것을 막는다.
    5. 봉투 구분자를 중성화한다. **본문을 프롬프트에 끼우기 전**이다.

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

        body = screened.redacted
        truncated = len(body) > content_chars
        if truncated:
            body = body[:content_chars]

        folded = banned.fold(body)
        first = seen_folded.get(folded)
        if first is not None:
            batch.excluded.append(
                ExcludedReview(
                    review.reviewId,
                    ExcludeReason.DUPLICATE,
                    f"접은 본문이 리뷰 {first}과 같다. 같은 문구를 두 번 세지 않는다",
                )
            )
            continue
        seen_folded[folded] = review.reviewId

        batch.prepared.append(
            PreparedReview(
                review_id=review.reviewId,
                rating=review.rating,
                option_name=review.optionName or "기본",
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

    머리줄은 평문이다. 봉투 토큰으로 만들면 ``ENVELOPE_TOKENS``에 등록해야 하고 그것이
    이 모듈이 피한 거래다.
    """
    lines = ['<대상 종류="REVIEW_SUMMARY">']
    if category_path:
        lines.append(f"카테고리: {' > '.join(category_path)}")
    for item in prepared:
        suffix = " / 본문 절단됨" if item.truncated else ""
        lines.append(f"[리뷰 {item.review_id} / {item.rating}점 / 옵션 {item.option_name}{suffix}]")
        lines.append("<본문>")
        lines.append(item.model_text)
        lines.append("</본문>")
    lines.append("</대상>")
    return "\n".join(lines)


def envelope_escaped(text: str) -> bool:
    """중성화되지 않은 봉투 토큰이 남아 있는가. 하네스와 테스트가 단언에 쓴다."""
    return any(token in text for token in rules.ENVELOPE_TOKENS)


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
    "MASK_HEAVY_RATIO",
    "QUOTE_MAX_CHARS",
    "ExcludedReview",
    "PreparedBatch",
    "PreparedReview",
    "batches",
    "envelope_escaped",
    "mask_heavy",
    "prepare",
    "render_envelope",
]
