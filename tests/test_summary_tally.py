""" "반복된다"의 판정. **이 파일이 이 기능의 거짓 일반화 방어를 고정한다.**"""

from __future__ import annotations

from app.review_summary import tally as tally_mod
from app.schemas.summary import (
    AspectCode,
    AspectTallyRow,
    ConsAbsenceReason,
    Polarity,
    QuoteSample,
)

THRESHOLDS = tally_mod.Thresholds(
    min_mentions=3,
    min_share=0.10,
    min_reviews=10,
    min_segment_reviews=8,
)


def _tally(*rows: tuple[AspectCode, Polarity, int]) -> tally_mod.Tally:
    return tally_mod.from_rows(
        [
            AspectTallyRow(
                aspectCode=code,
                polarity=polarity,
                mentionCount=count,
                labelSamples=[f"{code.value} 라벨"],
                quoteSamples=[QuoteSample(reviewId=1, text=f"{code.value} 인용")],
            )
            for code, polarity, count in rows
        ]
    )


def test_리뷰_두_건으로_반복되는_장점을_만들_수_없다() -> None:
    """1단 요약이 할 수 있던 거짓 일반화가 **코드에 경로가 없다.**"""
    tally = _tally((AspectCode.PALATABILITY, Polarity.PRO, 2))

    classified = tally_mod.classify(tally, basis_review_count=2, thresholds=THRESHOLDS)

    assert classified.pros == ()
    assert classified.enough_reviews is False
    assert len(classified.minor) == 1
    # 미달 항목은 **정확한 건수와 함께** 따로 적힌다. 버리지 않는다.
    assert classified.minor[0].mentionCount == 2


def test_건수와_비율이_AND로_걸린다() -> None:
    """건수만 쓰면 500건에서 3건이 "반복"이고, 비율만 쓰면 10건에서 2건이 20%다."""
    # 건수는 넘고 비율은 미달 (3/100 = 3% < 10%)
    count_ok = tally_mod.classify(
        _tally((AspectCode.SMELL, Polarity.CON, 3)),
        basis_review_count=100,
        thresholds=THRESHOLDS,
    )
    assert count_ok.cons == ()

    # 비율은 넘고 건수는 미달 (2/10 = 20% >= 10% 이지만 2 < 3)
    share_ok = tally_mod.classify(
        _tally((AspectCode.SMELL, Polarity.CON, 2)),
        basis_review_count=10,
        thresholds=THRESHOLDS,
    )
    assert share_ok.cons == ()

    # 둘 다 넘는다
    both = tally_mod.classify(
        _tally((AspectCode.SMELL, Polarity.CON, 3)),
        basis_review_count=20,
        thresholds=THRESHOLDS,
    )
    assert [item.row.aspectCode for item in both.cons] == [AspectCode.SMELL]
    assert both.cons[0].share == 0.15


def test_분모가_0이면_전건_미달이다() -> None:
    """0으로 나누기를 1.0 으로 접는 흔한 실수가 언급 1건을 "100% 반복"으로 만든다."""
    classified = tally_mod.classify(
        _tally((AspectCode.PRICE, Polarity.PRO, 5)),
        basis_review_count=0,
        thresholds=THRESHOLDS,
    )

    assert classified.pros == ()
    assert classified.cons == ()
    assert classified.enough_reviews is False


def test_단점_부재를_세_갈래로_가른다() -> None:
    """ "단점 없음" 한 가지로 접으면 화면이 서로 다른 사실을 같은 문장으로 적는다."""
    # (1) 분모 미달
    few = tally_mod.classify(
        _tally((AspectCode.PRICE, Polarity.PRO, 2)), basis_review_count=3, thresholds=THRESHOLDS
    )
    assert few.cons_absence_reason is ConsAbsenceReason.NOT_ENOUGH_REVIEWS

    # (2) 단점 언급이 아예 없었다
    none_found = tally_mod.classify(
        _tally((AspectCode.PRICE, Polarity.PRO, 5)), basis_review_count=20, thresholds=THRESHOLDS
    )
    assert none_found.cons_absence_reason is ConsAbsenceReason.NO_NEGATIVE_MENTIONS_FOUND

    # (3) 언급은 있었으나 반복이라 부르기엔 적었다
    below = tally_mod.classify(
        _tally((AspectCode.PRICE, Polarity.PRO, 5), (AspectCode.SMELL, Polarity.CON, 1)),
        basis_review_count=20,
        thresholds=THRESHOLDS,
    )
    assert below.cons_absence_reason is ConsAbsenceReason.BELOW_MENTION_THRESHOLD

    # (4) 단점이 있으면 사유가 없다
    present = tally_mod.classify(
        _tally((AspectCode.SMELL, Polarity.CON, 5)), basis_review_count=20, thresholds=THRESHOLDS
    )
    assert present.cons_absence_reason is None


def test_세그먼트는_더_낮은_임계를_쓴다() -> None:
    tally = _tally((AspectCode.SIZE_FIT, Polarity.PRO, 3))

    product = tally_mod.classify(tally, basis_review_count=9, thresholds=THRESHOLDS)
    segment = tally_mod.classify(tally, basis_review_count=9, thresholds=THRESHOLDS, segment=True)

    assert product.enough_reviews is False
    assert segment.enough_reviews is True


def test_서술_입력이_상한으로_고정된다() -> None:
    """**이 상한이 서술 비용을 리뷰 수에 무관하게 만든다.**

    그래서 "최근 60건만 읽는다" 표본 상한이 불필요해지고, 롱테일 단점을 영구히
    놓치지 않는다.
    """
    rows = [(code, Polarity.PRO, 50) for code in list(AspectCode)[: tally_mod.NARRATE_ROWS_MAX + 5]]
    classified = tally_mod.classify(_tally(*rows), basis_review_count=100, thresholds=THRESHOLDS)

    assert len(tally_mod.narrate_rows(classified)) == tally_mod.NARRATE_ROWS_MAX


def test_서술_입력에_미달_항목이_들어가지_않는다() -> None:
    """넣으면 모델이 그것을 장단점으로 올려 쓸 수 있다. 그 경로를 없애는 것이 요점이다."""
    classified = tally_mod.classify(
        _tally((AspectCode.PRICE, Polarity.PRO, 5), (AspectCode.NOISE, Polarity.CON, 1)),
        basis_review_count=20,
        thresholds=THRESHOLDS,
    )

    codes = {item.row.aspectCode for item in tally_mod.narrate_rows(classified)}
    assert codes == {AspectCode.PRICE}
    assert AspectCode.NOISE in {row.aspectCode for row in classified.minor}


def test_새_항목이_기존_집계에_누적된다() -> None:
    """추출은 리뷰당 평생 1회다. 누적은 백엔드가 보관하는 행에서 온다."""
    tally = _tally((AspectCode.DELIVERY, Polarity.PRO, 4))

    tally_mod.add(
        tally,
        aspect_code=AspectCode.DELIVERY,
        polarity=Polarity.PRO,
        label="배송 빠름",
        quote=QuoteSample(reviewId=99, text="하루 만에 왔어요"),
    )

    row = tally.rows[(AspectCode.DELIVERY, Polarity.PRO)]
    assert row.mentionCount == 5
    assert "배송 빠름" in row.labelSamples


def test_표본이_상한을_넘지_않는다() -> None:
    """집계표 행이 영구 보관되므로 크기가 리뷰 수에 비례해 자라면 안 된다."""
    tally = tally_mod.Tally()
    for index in range(20):
        tally_mod.add(
            tally,
            aspect_code=AspectCode.PRICE,
            polarity=Polarity.PRO,
            label=f"라벨 {index}",
            quote=QuoteSample(reviewId=index, text=f"인용 {index}"),
        )

    row = tally.rows[(AspectCode.PRICE, Polarity.PRO)]
    assert row.mentionCount == 20
    assert len(row.labelSamples) == tally_mod.LABEL_SAMPLES_MAX
    assert len(row.quoteSamples) == tally_mod.QUOTE_SAMPLES_MAX


def test_같은_키가_두_번_와도_셈이_사라지지_않는다() -> None:
    """뒤 줄로 덮으면 앞 줄의 셈이 조용히 없어진다."""
    tally = tally_mod.from_rows(
        [
            AspectTallyRow(aspectCode=AspectCode.PRICE, polarity=Polarity.PRO, mentionCount=3),
            AspectTallyRow(aspectCode=AspectCode.PRICE, polarity=Polarity.PRO, mentionCount=4),
        ]
    )

    row = tally.rows[(AspectCode.PRICE, Polarity.PRO)]
    assert row.mentionCount == 7


def test_지문이_집계에만_반응한다() -> None:
    """서술 재사용 키다. 표본이 바뀌었다고 산문을 다시 쓸 이유가 없다.

    넣으면 사실상 매번 갈라져 재사용이 죽고, 같은 구매자가 새로고침마다 다른 요약을 본다.
    """
    base = _tally((AspectCode.PRICE, Polarity.PRO, 3))
    same_counts = tally_mod.from_rows(
        [
            AspectTallyRow(
                aspectCode=AspectCode.PRICE,
                polarity=Polarity.PRO,
                mentionCount=3,
                labelSamples=["완전히 다른 라벨"],
                quoteSamples=[QuoteSample(reviewId=777, text="완전히 다른 인용")],
            )
        ]
    )
    more = _tally((AspectCode.PRICE, Polarity.PRO, 4))

    assert tally_mod.fingerprint(base) == tally_mod.fingerprint(same_counts)
    assert tally_mod.fingerprint(base) != tally_mod.fingerprint(more)


def test_지문이_행_순서에_흔들리지_않는다() -> None:
    forward = _tally((AspectCode.PRICE, Polarity.PRO, 3), (AspectCode.SMELL, Polarity.CON, 4))
    backward = _tally((AspectCode.SMELL, Polarity.CON, 4), (AspectCode.PRICE, Polarity.PRO, 3))

    assert tally_mod.fingerprint(forward) == tally_mod.fingerprint(backward)


def test_집계_출력_순서가_결정론적이다() -> None:
    """같은 집계가 같은 순서를 내야 재사용 키와 화면이 흔들리지 않는다."""
    tally = _tally(
        (AspectCode.PRICE, Polarity.PRO, 3),
        (AspectCode.SMELL, Polarity.CON, 9),
        (AspectCode.DELIVERY, Polarity.PRO, 3),
    )

    ordered = [(row.aspectCode, row.mentionCount) for row in tally.as_list()]

    assert ordered == [
        (AspectCode.SMELL, 9),
        (AspectCode.DELIVERY, 3),
        (AspectCode.PRICE, 3),
    ]
