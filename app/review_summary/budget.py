"""발행 메시지 크기의 **구조적 상한.** 선판정과 리포트가 같은 함수를 본다.

## 왜 운영 코드에 있는가

이 숫자가 하네스 전용이던 것이 결함의 원인이었다. 크기 검사가 ``_publish`` **안**에 있어서
결과가 발행 상한을 넘는 요청은 **추출 배치 + 서술 N회를 다 과금한 뒤** 커밋되지 않고
떨어졌다. 파티션 blocked 는 프로세스 로컬 집합이라 배포·스케일·리밸런스로 태스크가 교체되면
그 레코드가 다시 와서 같은 비용을 처음부터 다시 낸다. 요약 모듈 머리글이 "유료 호출이 들어간
경로에서 커밋을 빼면 지출이 무한 루프가 된다"를 원칙으로 적어 두었고, 크기 하나가 그 원칙을
어기고 있었다. 그래서 **모델을 부르기 전에** 같은 상한을 보고 가른다.

## 무엇을 세는가

요청의 모양(스코프 수·신규 리뷰 수)만으로 결과가 가질 수 있는 **최대** 바이트를 조립한다.
리뷰 한 건의 항목은 ``(코드, 극성)`` 으로 중복이 접히므로 열거 18종 x 2 가 상한이고,
인용 80자(``QUOTE_MAX_CHARS``)·라벨 40자(``LABEL_MAX_CHARS``)·한 줄 요약
60자(``HEADLINE_MAX_CHARS``)·표본 3개가 코드의 상한이다. 한글은 UTF-8 에서 글자당
3바이트라 그 전제로 채운다 -- **추정이 아니라 조립해서 센 값이다.**

``summary_content_chars`` 는 결과 크기를 키우지 않는다. 인용·라벨·한 줄 요약의 길이를 코드가
자르기 때문이다 -- 그 설정은 **요청** 쪽 레버이고 ``harness.run_summary`` 가 따로 센다.

## 비용

조립은 1MiB 가까운 문자열을 만든다. 메시지마다 다시 조립하면 낭비이므로 ``(스코프 수,
신규 리뷰 수, 두 설정)`` 으로 외운다 -- 실제로 나타나는 조합이 몇 가지뿐이다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache

from app.settings import Settings


@lru_cache(maxsize=256)
def _assembled_bytes(scopes: int, new_reviews: int) -> int:
    from app.review_summary import envelope as env
    from app.review_summary.evidence import HEADLINE_MAX_CHARS, LABEL_MAX_CHARS
    from app.review_summary.extract_prompt import EXTRACT_VERSION
    from app.review_summary.narrate_prompt import NARRATE_VERSION
    from app.review_summary.selection import all_segment_keys
    from app.review_summary.tally import LABEL_SAMPLES_MAX, NARRATE_ROWS_MAX, QUOTE_SAMPLES_MAX
    from app.schemas.envelope import Envelope
    from app.schemas.summary import (
        SUMMARY_AGGREGATE_TYPE,
        SUMMARY_RESULT_EVENT_TYPE,
        AspectCode,
        AspectTallyRow,
        ExtractedAspect,
        ExtractedReview,
        ExtractStatus,
        PetContext,
        Polarity,
        QuoteSample,
        ReviewSummaryMessage,
        ScopeSummary,
        SummaryAspect,
        SummaryScope,
    )

    quote = "가" * env.QUOTE_MAX_CHARS
    label = "가" * LABEL_MAX_CHARS
    headline = "가" * HEADLINE_MAX_CHARS
    # 가장 긴 세그먼트 키를 **생성기에서 받는다.** 글자로 박아 두면 상한이 상한이 아니게
    # 된다 -- ``"DOG_10_25"``(9자)가 박혀 있었고 실제 최장은 ``"DOG:DOG_UNDER_5"``(15자)라
    # 스코프마다 6바이트를 덜 셌다. 체급을 하나 더하는 날 같은 자리가 다시 어긋나므로
    # 숫자를 고치지 않고 ``all_segment_keys()`` 를 보게 한다.
    segment_key = max(all_segment_keys(), key=len)
    pairs = [(code, polarity) for code in AspectCode for polarity in Polarity]

    def samples(review_id: int) -> list[QuoteSample]:
        return [QuoteSample(reviewId=review_id, text=quote) for _ in range(QUOTE_SAMPLES_MAX)]

    def tally_row(code: AspectCode, polarity: Polarity) -> AspectTallyRow:
        return AspectTallyRow(
            aspectCode=code,
            polarity=polarity,
            mentionCount=max(1, new_reviews),
            labelSamples=[label] * LABEL_SAMPLES_MAX,
            quoteSamples=samples(1),
        )

    extracted = [
        ExtractedReview(
            extracted=ExtractStatus.EXTRACTED,
            reviewId=review_id,
            aspects=[
                ExtractedAspect(
                    aspectCode=code,
                    aspectLabel=label,
                    polarity=polarity,
                    quote=quote,
                    quoteStart=0,
                    quoteEnd=env.QUOTE_MAX_CHARS,
                )
                for code, polarity in pairs
            ],
            petContext=PetContext(species="DOG", breedLabel=label, weightKg=15.0, ageYears=3, quote=quote),
            truncated=True,
            note="가" * 80,
        )
        for review_id in range(1, new_reviews + 1)
    ]
    summaries = [
        ScopeSummary(
            scope=SummaryScope.SEGMENT,
            segmentKey=segment_key,
            basisReviewCount=10_000,
            unextractedReviewCount=10,
            analyzedReviewCount=max(1, new_reviews),
            pros=[
                SummaryAspect(
                    aspectCode=code,
                    headline=headline,
                    mentionCount=10,
                    mentionShare=0.5,
                    quotes=samples(1),
                )
                for code, _ in pairs[:3]
            ],
            cons=[
                SummaryAspect(
                    aspectCode=code,
                    headline=headline,
                    mentionCount=10,
                    mentionShare=0.5,
                    quotes=samples(1),
                )
                for code, _ in pairs[3:6]
            ],
            minorMentions=[tally_row(code, polarity) for code, polarity in pairs],
            unnarratedAspects=[tally_row(code, polarity) for code, polarity in pairs[:NARRATE_ROWS_MAX]],
            tallyFingerprint="0" * 12,
        )
        for _ in range(scopes)
    ]
    message = ReviewSummaryMessage(
        requestId="r" * 64,
        productId=10_000_000,
        extractVersion=EXTRACT_VERSION,
        narrateVersion=NARRATE_VERSION,
        extracted=extracted,
        summaries=summaries,
        generatedAt=datetime.now(UTC),
    )
    out = Envelope.wrap(
        event_id=message.event_id(),
        event_type=SUMMARY_RESULT_EVENT_TYPE,
        aggregate_type=SUMMARY_AGGREGATE_TYPE,
        ordering_key=str(message.productId),
        payload_obj=message,
        trace_id="t" * 36,
    )
    return len(out.to_json().encode())


def worst_case_result_bytes_for(settings: Settings, *, scopes: int, new_reviews: int | None = None) -> int:
    """이 모양의 요청이 만들 수 있는 **결과 엔벨로프 최대 바이트.**

    ``scopes`` 와 ``new_reviews`` 는 요청이 들고 온 실제 수이고, 설정 상한으로 자른다 --
    계약이 상한을 두고 있으므로 그보다 큰 수가 들어오면 스키마가 먼저 거부한다.
    """
    bounded_scopes = max(1, min(scopes, settings.summary_scopes_per_message))
    bounded_reviews = settings.summary_max_new_reviews if new_reviews is None else new_reviews
    bounded_reviews = max(0, min(bounded_reviews, settings.summary_max_new_reviews))
    return _assembled_bytes(bounded_scopes, bounded_reviews)


def worst_case_result_bytes(settings: Settings) -> int:
    """계약 상한을 꽉 채운 결과의 바이트 수. 리포트가 쓰는 전역 상한이다."""
    return worst_case_result_bytes_for(
        settings,
        scopes=settings.summary_scopes_per_message,
        new_reviews=settings.summary_max_new_reviews,
    )


__all__ = ["worst_case_result_bytes", "worst_case_result_bytes_for"]
