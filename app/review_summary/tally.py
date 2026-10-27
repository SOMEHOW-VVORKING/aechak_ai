""" "반복된다"의 판정. **전부 여기 있고 모델은 한 글자도 거들지 않는다.**

이 모듈이 이 설계의 핵심이다. 1단 요약(모델이 N건을 읽고 장단점을 쓴다)에서는 "반복된다"가
모델의 인상이라 리뷰 2건으로도 "반복되는 장점"이 나온다. 2단에서는 셈이 코드의 것이고
**임계 미달 항목이 장단점 목록에 들어갈 경로가 코드에 없다.**

덤으로 얻는 것이 비용이다. 서술 입력이 집계표(상한 고정)라 **서술 비용이 리뷰 수에
무관해진다.** 그래서 1단 안이 비용을 잡으려 도입했던 "최근 60건만 읽는다" 표본 상한이
불필요해진다 -- 400건 중 5건만 언급한 결함을 영구히 놓치지 않고, 화면 라벨에 정직성을
의존하지도 않는다.

집계는 **누적**이다. 리뷰 본문을 모델이 읽는 것은 리뷰마다 평생 한 번뿐이고, 그때 뽑은
행을 백엔드가 영구 보관해 요청마다 ``existingTally``로 돌려준다. 증분 요약(기존 산문 + 새
리뷰)이 드리프트를 쌓고 출처를 잃는 문제를 이 방식이 피한다 -- 증분 대상이 산문이 아니라
불변인 행이기 때문이다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from app.schemas.summary import (
    AspectCode,
    AspectTallyRow,
    ConsAbsenceReason,
    Polarity,
    QuoteSample,
)

# 집계표 한 줄이 들고 다니는 표본 수. 서술 입력 크기를 이 둘이 정한다.
LABEL_SAMPLES_MAX = 3
QUOTE_SAMPLES_MAX = 3
# 서술에 보내는 집계표 줄 수 상한. **이 상한이 서술 비용을 N 에 무관하게 만든다.**
NARRATE_ROWS_MAX = 12


@dataclass(frozen=True, slots=True)
class Thresholds:
    """ "반복된다"의 정의. 두 조건이 **AND** 다.

    건수만 쓰면 리뷰 500건에서 3건 언급이 "반복"이 되고, 비율만 쓰면 리뷰 10건에서 2건이
    20%로 "반복"이 된다. 둘을 함께 걸어야 양쪽이 막힌다.
    """

    min_mentions: int
    min_share: float
    min_reviews: int
    min_segment_reviews: int


@dataclass(slots=True)
class Tally:
    """집계표. 키는 ``(항목 코드, 극성)``이다."""

    rows: dict[tuple[AspectCode, Polarity], AspectTallyRow] = field(default_factory=dict)

    def as_list(self) -> list[AspectTallyRow]:
        """결정론적 순서로 내보낸다. 언급 많은 것부터, 같으면 코드 순."""
        return sorted(
            self.rows.values(),
            key=lambda row: (-row.mentionCount, row.aspectCode.value, row.polarity.value),
        )

    def get(self, code: AspectCode, polarity: Polarity) -> AspectTallyRow | None:
        return self.rows.get((code, polarity))


def from_rows(rows: list[AspectTallyRow]) -> Tally:
    """백엔드가 보낸 기존 집계표를 읽는다."""
    tally = Tally()
    for row in rows:
        key = (row.aspectCode, row.polarity)
        existing = tally.rows.get(key)
        if existing is None:
            tally.rows[key] = row.model_copy(deep=True)
            continue
        # 같은 키가 두 번 왔다. 합치는 쪽이 안전하다 -- 뒤 줄로 덮으면 앞 줄의 셈이 사라진다.
        existing.mentionCount += row.mentionCount
        existing.labelSamples = _capped([*existing.labelSamples, *row.labelSamples], LABEL_SAMPLES_MAX)
        existing.quoteSamples = _capped_quotes([*existing.quoteSamples, *row.quoteSamples])
    return tally


def add(
    tally: Tally,
    *,
    aspect_code: AspectCode,
    polarity: Polarity,
    label: str,
    quote: QuoteSample,
) -> None:
    """검증을 통과한 항목 하나를 센다. **리뷰 하나가 같은 키를 두 번 세지 않는다.**

    중복 방지는 호출부가 리뷰별로 키를 모아 넘기는 방식으로 한다 -- 여기서 리뷰 id 를
    보고 걸러내면 집계표가 전 리뷰 id 목록을 들고 다녀야 하고, 그러면 영구 보관하는 행의
    크기가 리뷰 수에 비례해 자란다.
    """
    key = (aspect_code, polarity)
    row = tally.rows.get(key)
    if row is None:
        tally.rows[key] = AspectTallyRow(
            aspectCode=aspect_code,
            polarity=polarity,
            mentionCount=1,
            labelSamples=[label] if label else [],
            quoteSamples=[quote],
        )
        return
    row.mentionCount += 1
    row.labelSamples = _capped([*row.labelSamples, *([label] if label else [])], LABEL_SAMPLES_MAX)
    row.quoteSamples = _capped_quotes([*row.quoteSamples, quote])


def _capped(values: list[str], limit: int) -> list[str]:
    """중복을 지우고 앞에서 자른다. 먼저 들어온 표본을 지키는 쪽이 결정론적이다."""
    return list(dict.fromkeys(v for v in values if v))[:limit]


def _capped_quotes(values: list[QuoteSample]) -> list[QuoteSample]:
    seen: set[tuple[int, str]] = set()
    out: list[QuoteSample] = []
    for quote in values:
        key = (quote.reviewId, quote.text)
        if key in seen:
            continue
        seen.add(key)
        out.append(quote)
        if len(out) >= QUOTE_SAMPLES_MAX:
            break
    return out


@dataclass(frozen=True, slots=True)
class Repeated:
    """임계를 넘은 항목 하나. ``share``는 코드가 계산한 값이다."""

    row: AspectTallyRow
    share: float


@dataclass(frozen=True, slots=True)
class Classified:
    """집계 판정 결과. 서술 모델이 고를 수 있는 **전 집합**이 여기 있다."""

    pros: tuple[Repeated, ...]
    cons: tuple[Repeated, ...]
    minor: tuple[AspectTallyRow, ...]
    cons_absence_reason: ConsAbsenceReason | None
    enough_reviews: bool


def classify(
    tally: Tally,
    *,
    basis_review_count: int,
    thresholds: Thresholds,
    segment: bool = False,
) -> Classified:
    """임계를 적용한다. **이 함수가 "반복되는 장단점"의 유일한 출처다.**

    ``basis_review_count``가 0 이면 비율을 계산할 수 없다. 그때 비율 조건을 통과시키면
    (0으로 나누기를 피하려 1.0으로 접는 흔한 실수) 언급 1건이 "100% 반복"이 된다.
    그래서 분모가 없으면 전건이 미달이다.
    """
    minimum = thresholds.min_segment_reviews if segment else thresholds.min_reviews
    enough = basis_review_count >= minimum

    pros: list[Repeated] = []
    cons: list[Repeated] = []
    minor: list[AspectTallyRow] = []
    con_mentions = 0

    for row in tally.as_list():
        if row.polarity is Polarity.CON:
            con_mentions += row.mentionCount
        share = (row.mentionCount / basis_review_count) if basis_review_count > 0 else 0.0
        repeated = row.mentionCount >= thresholds.min_mentions and share >= thresholds.min_share
        if not repeated:
            minor.append(row)
            continue
        (pros if row.polarity is Polarity.PRO else cons).append(Repeated(row=row, share=share))

    reason: ConsAbsenceReason | None = None
    if not enough:
        reason = ConsAbsenceReason.NOT_ENOUGH_REVIEWS
    elif not cons:
        # **"없다"와 "못 찾았다"를 가른다.** 언급이 아예 없었던 것과 있었지만 반복이라
        # 부르기엔 적었던 것은 다른 사실이고, 화면 문장도 달라야 한다.
        reason = (
            ConsAbsenceReason.NO_NEGATIVE_MENTIONS_FOUND
            if con_mentions == 0
            else ConsAbsenceReason.BELOW_MENTION_THRESHOLD
        )

    return Classified(
        pros=tuple(pros),
        cons=tuple(cons),
        minor=tuple(minor),
        cons_absence_reason=reason,
        enough_reviews=enough,
    )


def narrate_rows(classified: Classified) -> list[Repeated]:
    """서술 모델이 보는 줄. 임계를 넘은 것만, 언급 많은 것부터 상한까지.

    미달 항목(``minor``)을 서술 입력에 넣지 않는다. 넣으면 모델이 그것을 장단점으로
    올려 쓸 수 있고, 그 경로를 없애는 것이 이 모듈의 존재 이유다.
    """
    merged = sorted(
        [*classified.pros, *classified.cons],
        key=lambda item: (-item.row.mentionCount, item.row.aspectCode.value),
    )
    return merged[:NARRATE_ROWS_MAX]


def fingerprint(tally: Tally) -> str:
    """서술 재사용 키의 일부.

    결정성 레버가 API 층에 하나도 없다 -- ``temperature``·``top_p``를 보내지 않고
    Haiku 4.5 는 ``effort`` 를 받으면 400 이며 ``seed`` 는 Messages API 에 없다. 그래서
    결정론을 **재사용 키**로 만든다. 집계가 안 바뀌면 산문을 다시 쓰지 않으므로 같은
    구매자가 새로고침마다 다른 요약을 보지 않는다.

    ``labelSamples``와 ``quoteSamples``는 지문에 넣지 않는다. 표본이 바뀌었다고 산문을
    다시 쓸 이유가 없고, 넣으면 사실상 매번 갈라져 재사용이 죽는다.
    """
    parts = [
        f"{row.aspectCode.value}:{row.polarity.value}:{row.mentionCount}"
        for row in sorted(tally.rows.values(), key=lambda r: (r.aspectCode.value, r.polarity.value))
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()
