"""펫 유사도 선별. 모델을 부르지 않으므로 전부 순수 함수 테스트다."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.review_summary.selection import (
    AgeBucket,
    MatchTier,
    PetFacts,
    Species,
    WeightBand,
    age_bucket,
    all_segment_keys,
    golden_vectors,
    match_tier,
    pet_facts_field_names,
    segment_key,
    tier_rank,
    weight_band,
)

TODAY = date(2026, 10, 27)
CONTRACT = Path(__file__).parent / "contract" / "review-selection-vectors.json"


def test_점수_입력에_평점_축이_없다() -> None:
    """선별이 평점과 상관되면 구매자를 속이는 기능이 된다.

    "평점을 넣지 않기로 한다"가 아니라 **넣을 수 없게** 둔 것이 이 설계다. 누군가
    ``PetFacts`` 에 ``rating`` 이나 ``has_photo`` 를 더하면 이 테스트가 떨어진다.
    """
    assert pet_facts_field_names() == ("species", "breed_id", "birth_year_month", "weight_kg")

    forbidden = {"rating", "has_photo", "helpful_count", "hasPhoto", "ratingAverage"}
    assert forbidden.isdisjoint(pet_facts_field_names())


def test_선별이_평점과_독립이면_상위_평균이_전체_평균과_같다() -> None:
    """펫 속성과 평점이 독립인 픽스처에서 상위 선별의 평균 평점이 전체와 어긋나면 버그다.

    티어 함수가 평점을 **구조적으로** 볼 수 없으므로 이 단언은 사실 항진에 가깝다.
    그래도 둔다 -- 누군가 정렬 키에 평점을 섞으면 여기서 깨지고, 그것이 이 기능에서
    제품 신뢰를 깨뜨릴 수 있는 유일한 자리다.
    """
    viewer = PetFacts(Species.DOG, breed_id=12, birth_year_month="2021-05", weight_kg=4.2)
    # 같은 펫 속성에 평점만 1~5로 돌린다. 티어가 평점을 보면 순서가 평점에 쏠린다.
    reviews = [
        (rating, PetFacts(Species.DOG, breed_id=12, birth_year_month="2021-05", weight_kg=4.2))
        for rating in (1, 2, 3, 4, 5)
    ] + [(rating, PetFacts(Species.CAT, breed_id=301, weight_kg=4.5)) for rating in (1, 2, 3, 4, 5)]

    ordered = sorted(reviews, key=lambda item: tier_rank(match_tier(viewer, item[1], today=TODAY)))
    top = ordered[:5]

    assert sum(r for r, _ in top) / len(top) == sum(r for r, _ in reviews) / len(reviews)


def test_펫_정보_없음을_조건_불일치로_내리지_않는다() -> None:
    """미검사와 조건 불일치를 섞으면 "정보가 없는 아이"가 "다른 종 아이"보다 나빠진다."""
    viewer = PetFacts(Species.DOG, breed_id=12, weight_kg=4.2)
    other_species = PetFacts(Species.CAT, breed_id=301, weight_kg=4.5)

    assert match_tier(viewer, None, today=TODAY) is MatchTier.NO_PET_INFO
    assert match_tier(viewer, other_species, today=TODAY) is MatchTier.OTHER
    assert tier_rank(MatchTier.NO_PET_INFO) < tier_rank(MatchTier.OTHER)


def test_양쪽_체중을_모르면_체급_일치가_아니다() -> None:
    """``UNKNOWN == UNKNOWN`` 을 "일치"로 읽으면 체중을 모르는 아이들이 서로 비슷해진다."""
    viewer = PetFacts(Species.DOG, breed_id=12, birth_year_month="2022-01", weight_kg=None)
    reviewer = PetFacts(Species.DOG, breed_id=99, birth_year_month="2022-05", weight_kg=None)

    assert match_tier(viewer, reviewer, today=TODAY) is MatchTier.SPECIES


def test_한쪽_체중만_알아도_체급_일치가_아니다() -> None:
    same_breed = PetFacts(Species.DOG, breed_id=12, weight_kg=4.2)
    no_weight = PetFacts(Species.DOG, breed_id=12, weight_kg=None)

    # 품종이 같아 BREED 는 되지만 BREED_EXACT 로 올라가지 않는다.
    assert match_tier(same_breed, no_weight, today=TODAY) is MatchTier.BREED


@pytest.mark.parametrize(
    ("birth", "expected"),
    [
        ("2026-01", AgeBucket.PUPPY),
        ("2026-12", AgeBucket.PUPPY),
        ("2025-06", AgeBucket.YOUNG),
        ("2024-06", AgeBucket.YOUNG),
        ("2023-06", AgeBucket.ADULT),
        ("2020-06", AgeBucket.ADULT),
        ("2019-06", AgeBucket.SENIOR),
        (None, AgeBucket.UNKNOWN),
        ("", AgeBucket.UNKNOWN),
        # 연도만 와도 받는다. 2026-2021=5 라서 ADULT 다.
        ("2021", AgeBucket.ADULT),
        ("엉뚱한값", AgeBucket.UNKNOWN),
        ("2026-13", AgeBucket.UNKNOWN),
        ("2030-01", AgeBucket.UNKNOWN),
    ],
)
def test_나이는_연_단위로만_버킷팅한다(birth: str | None, expected: AgeBucket) -> None:
    """백엔드 ``BirthYearMonthNormalizer`` 가 "월 모름"과 "1월생"을 구분하지 못한다.

    월 단위로 계산하면 월을 비운 프로필이 전부 1월생으로 몰려 경계에 쏠림이 생긴다.
    같은 해면 1월이든 12월이든 같은 버킷이어야 한다 -- 위 두 줄이 그것을 고정한다.
    """
    assert age_bucket(birth, today=TODAY) is expected


@pytest.mark.parametrize(
    ("species", "weight", "expected"),
    [
        (Species.DOG, None, WeightBand.UNKNOWN),
        (Species.DOG, 0.0, WeightBand.UNKNOWN),
        (Species.DOG, 4.9, WeightBand.DOG_UNDER_5),
        (Species.DOG, 5.0, WeightBand.DOG_5_10),
        (Species.DOG, 9.9, WeightBand.DOG_5_10),
        (Species.DOG, 10.0, WeightBand.DOG_10_25),
        (Species.DOG, 25.0, WeightBand.DOG_25_PLUS),
        (Species.CAT, 3.9, WeightBand.CAT_UNDER_4),
        (Species.CAT, 4.0, WeightBand.CAT_4_6),
        (Species.CAT, 6.0, WeightBand.CAT_6_PLUS),
    ],
)
def test_체중대가_종마다_다른_구간을_쓴다(
    species: Species, weight: float | None, expected: WeightBand
) -> None:
    """10kg 개와 10kg 고양이는 같은 체급이 아니다."""
    assert weight_band(species, weight) is expected


def test_체중을_모르면_세그먼트를_만들지_않는다() -> None:
    """결측은 조건이 아니다. "체중 모르는 보호자 세그먼트"에 "비슷한 아이" 라벨을 달 수 없다."""
    assert segment_key(PetFacts(Species.DOG, breed_id=12, weight_kg=None)) is None
    assert segment_key(None) is None
    assert segment_key(PetFacts(Species.DOG, breed_id=12, weight_kg=4.2)) == "DOG:DOG_UNDER_5"


def test_세그먼트가_일곱개를_넘지_않는다() -> None:
    """나이 축을 더하면 퍼짐이 커져 임계 미달이 늘어난다. 상한을 숫자로 고정한다."""
    keys = all_segment_keys()

    assert len(keys) == 7, keys
    assert len(set(keys)) == len(keys)
    assert all("UNKNOWN" not in key for key in keys)


def test_골든_벡터_파일이_실제_함수와_같다() -> None:
    """백엔드 Kotlin 미러가 이 파일을 읽는다. 어긋나면 양 레포가 다른 답을 낸다.

    파일에 ``today`` 를 적어 내보낸 것이 중요하다 -- 적지 않으면 나이 버킷이 실행
    날짜에 따라 달라져 같은 파일이 두 레포에서 다른 답을 낸다.
    """
    stored = json.loads(CONTRACT.read_text(encoding="utf-8"))

    assert stored == golden_vectors()
    assert stored["today"] == "2026-10-27"
    assert {v["tier"] for v in stored["vectors"]} >= {
        MatchTier.BREED_EXACT.value,
        MatchTier.BREED.value,
        MatchTier.SIZE_AGE.value,
        MatchTier.SPECIES.value,
        MatchTier.OTHER.value,
        MatchTier.NO_PET_INFO.value,
    }, "벡터가 티어 전건을 덮지 않는다"


def test_사다리가_위에서_아래로_내려온다() -> None:
    """같은 입력이 더 높은 티어 조건을 만족하면 반드시 높은 쪽이 나와야 한다."""
    viewer = PetFacts(Species.DOG, breed_id=12, birth_year_month="2022-03", weight_kg=4.2)

    exact = PetFacts(Species.DOG, breed_id=12, birth_year_month="2022-07", weight_kg=4.8)
    breed_only = PetFacts(Species.DOG, breed_id=12, weight_kg=20.0)
    size_age = PetFacts(Species.DOG, breed_id=99, birth_year_month="2022-11", weight_kg=4.9)
    species_only = PetFacts(Species.DOG, breed_id=99, weight_kg=20.0)

    tiers = [
        match_tier(viewer, reviewer, today=TODAY) for reviewer in (exact, breed_only, size_age, species_only)
    ]

    assert tiers == [MatchTier.BREED_EXACT, MatchTier.BREED, MatchTier.SIZE_AGE, MatchTier.SPECIES]
    assert [tier_rank(t) for t in tiers] == sorted(tier_rank(t) for t in tiers)
