"""반려동물 유사도 선별의 정본. **모델을 0회 부르고 0원이다.**

품종·나이·체중은 열거와 숫자이고 의미 판단이 아니다. 금지 성분을 0단 사전에 둔 것과 같은
계층이라, 여기에 모델이나 임베딩을 쓰면 "5kg과 6kg의 유사도"를 뺄셈이 아닌 것으로 재는
셈이 된다.

정렬은 실제로는 백엔드 읽기 경로(SQL)에서 일어난다 -- 페이지뷰마다 카프카 왕복은 말이 안
된다. 그런데 **버킷 경계와 티어 함수의 정본은 여기 하나다.** 골든 벡터를
``tests/contract/review-selection-vectors.json``으로 내보내 양 레포의 테스트가 같은 파일을
읽게 하고, 술어 사본이 두 벌 생기는 것을 그 방식으로 막는다.

코드로 피하는 함정 셋.

1. **나이는 연 단위로만 버킷팅한다.** 백엔드 ``BirthYearMonthNormalizer``가 주석으로
   명시한다 -- 저장 후 "월 모름"과 "1월생"이 구분되지 않는다. 월 단위로 계산하면 1월
   쏠림이 점수에 들어온다.
2. **체중 null은 ``UNKNOWN``이고 "불일치"가 아니다.** ``PetProfile.weight``는 nullable이다.
   체중을 모르는 아이를 "체중대 일치"로도 "불일치"로도 세지 않는다.
3. **품종 388행 -> 체급 매핑표를 만들지 않는다.** ``breeds`` 테이블은 ``id``·``species``·
   ``label`` 셋뿐이고 체급 분류가 없다. 388행 수기 분류는 검증 불가능한 부채이고 틀려도
   조용히 틀린다. 체급은 실제 기록된 ``weight``로만 잡는다.

그리고 **점수 함수의 입력 타입에 평점·사진·추천수 필드를 두지 않는다.** 넣을 수 없게
만드는 것이 넣지 않기로 하는 것보다 강하다. ``PetFacts``에 그 필드가 없다는 사실 자체를
테스트가 단언한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

_YEAR_MONTH = re.compile(r"^(\d{4})(?:-(0[1-9]|1[0-2]))?$")


class Species(StrEnum):
    DOG = "DOG"
    CAT = "CAT"


class AgeBucket(StrEnum):
    PUPPY = "PUPPY"
    YOUNG = "YOUNG"
    ADULT = "ADULT"
    SENIOR = "SENIOR"
    UNKNOWN = "UNKNOWN"


class WeightBand(StrEnum):
    """체중대. 종마다 구간이 다르다 -- 10kg 개와 10kg 고양이는 같은 체급이 아니다."""

    UNKNOWN = "UNKNOWN"
    DOG_UNDER_5 = "DOG_UNDER_5"
    DOG_5_10 = "DOG_5_10"
    DOG_10_25 = "DOG_10_25"
    DOG_25_PLUS = "DOG_25_PLUS"
    CAT_UNDER_4 = "CAT_UNDER_4"
    CAT_4_6 = "CAT_4_6"
    CAT_6_PLUS = "CAT_6_PLUS"


class MatchTier(StrEnum):
    """사전식 티어 사다리. **가중 합계 점수를 쓰지 않는다.**

    가중치를 정할 데이터가 없고, 티어는 테스트로 고정되며 사용자에게 설명 가능하다
    ("같은 품종" / "비슷한 체급").

    ``NO_PET_INFO``은 ``OTHER``와 **절대 섞지 않는다.** 전자는 미검사이고 후자는 조건
    불일치다. 합치면 펫 정보 없는 리뷰가 "다른 아이 보호자 후기"로 내려가고, 그것은 통과와
    미검사를 섞는 것과 같은 종류의 오류다.
    """

    BREED_EXACT = "BREED_EXACT"
    BREED = "BREED"
    SIZE_AGE = "SIZE_AGE"
    SPECIES = "SPECIES"
    OTHER = "OTHER"
    NO_PET_INFO = "NO_PET_INFO"


# 정렬 우선순위. 작은 값이 위로 간다. ``NO_PET_INFO``는 중립(중간)이다 --
# 미검사를 조건 불일치 아래로 내리면 "정보가 없는 아이"가 "다른 종 아이"보다 나쁘게 취급된다.
_TIER_RANK: dict[MatchTier, int] = {
    MatchTier.BREED_EXACT: 0,
    MatchTier.BREED: 1,
    MatchTier.SIZE_AGE: 2,
    MatchTier.SPECIES: 3,
    MatchTier.NO_PET_INFO: 4,
    MatchTier.OTHER: 5,
}


@dataclass(frozen=True, slots=True)
class PetFacts:
    """티어 판정의 **전 입력**이다.

    평점·사진 유무·추천수 필드가 여기 없는 것이 설계다. 선별 점수가 평점과 상관되면
    구매자를 속이는 기능이 되는데, 그 사고를 "넣지 않기로 한다"가 아니라 "넣을 수 없다"로
    막는다. ``tests``가 이 데이터클래스의 필드 집합을 단언한다.
    """

    species: Species
    breed_id: int | None = None
    birth_year_month: str | None = None
    weight_kg: float | None = None


def age_bucket(birth_year_month: str | None, *, today: date) -> AgeBucket:
    """생년월에서 나이대를 만든다. **연 단위로만 본다.**

    월을 쓰면 "월 모름"이 1월로 저장된 아이들이 모두 같은 달 생일로 몰려 경계에서
    쏠림이 생긴다. 연 차이만 쓰면 그 쏠림이 구조적으로 사라진다.
    """
    if not birth_year_month:
        return AgeBucket.UNKNOWN
    matched = _YEAR_MONTH.match(birth_year_month.strip())
    if matched is None:
        return AgeBucket.UNKNOWN
    years = today.year - int(matched.group(1))
    if years < 0:
        return AgeBucket.UNKNOWN
    if years < 1:
        return AgeBucket.PUPPY
    if years <= 2:
        return AgeBucket.YOUNG
    if years <= 6:
        return AgeBucket.ADULT
    return AgeBucket.SENIOR


def weight_band(species: Species, weight_kg: float | None) -> WeightBand:
    """체중대를 만든다. 모르면 ``UNKNOWN``이고 그것은 불일치가 아니다."""
    if weight_kg is None or weight_kg <= 0:
        return WeightBand.UNKNOWN
    if species is Species.DOG:
        if weight_kg < 5:
            return WeightBand.DOG_UNDER_5
        if weight_kg < 10:
            return WeightBand.DOG_5_10
        if weight_kg < 25:
            return WeightBand.DOG_10_25
        return WeightBand.DOG_25_PLUS
    if weight_kg < 4:
        return WeightBand.CAT_UNDER_4
    if weight_kg < 6:
        return WeightBand.CAT_4_6
    return WeightBand.CAT_6_PLUS


def match_tier(viewer: PetFacts | None, reviewer: PetFacts | None, *, today: date) -> MatchTier:
    """조회자와 리뷰어의 아이가 얼마나 비슷한지. 사다리를 위에서 아래로 내려온다.

    리뷰어 쪽이 ``None``이면 ``NO_PET_INFO``다. 조회자 쪽이 ``None``이면 비교할 기준이
    없어서 전건이 ``NO_PET_INFO``이고, 그것이 "선별을 적용하지 않는다"와 같은 뜻이다.
    """
    if reviewer is None or viewer is None:
        return MatchTier.NO_PET_INFO

    if viewer.species is not reviewer.species:
        return MatchTier.OTHER

    viewer_band = weight_band(viewer.species, viewer.weight_kg)
    reviewer_band = weight_band(reviewer.species, reviewer.weight_kg)
    # 양쪽이 **모두 알려졌을 때만** 체중대 일치를 센다. 한쪽이 UNKNOWN 이면
    # UNKNOWN == UNKNOWN 이 "일치"로 읽혀 체중을 모르는 아이들이 서로 비슷하다고 판정된다.
    band_known = viewer_band is not WeightBand.UNKNOWN and reviewer_band is not WeightBand.UNKNOWN
    band_match = band_known and viewer_band == reviewer_band

    breed_match = (
        viewer.breed_id is not None and reviewer.breed_id is not None and viewer.breed_id == reviewer.breed_id
    )
    if breed_match and band_match:
        return MatchTier.BREED_EXACT
    if breed_match:
        return MatchTier.BREED

    viewer_age = age_bucket(viewer.birth_year_month, today=today)
    reviewer_age = age_bucket(reviewer.birth_year_month, today=today)
    age_known = viewer_age is not AgeBucket.UNKNOWN and reviewer_age is not AgeBucket.UNKNOWN
    if band_match and age_known and viewer_age == reviewer_age:
        return MatchTier.SIZE_AGE

    return MatchTier.SPECIES


def tier_rank(tier: MatchTier) -> int:
    """정렬 키. 작을수록 위다. ``NO_PET_INFO``가 중립 위치인 것이 요점이다."""
    return _TIER_RANK[tier]


def segment_key(facts: PetFacts | None) -> str | None:
    """세그먼트 키. ``species x weightBand``로 최대 7개다.

    체중대가 ``UNKNOWN``이면 **세그먼트를 만들지 않는다.** 만들면 "체중을 모르는 아이
    보호자 n명의 후기"라는 세그먼트가 생기는데, 그것은 조건이 아니라 결측이고
    "비슷한 아이" 라벨을 달 수 없다.

    나이 축을 더하지 않았다. 퍼짐이 커져 세그먼트 임계 미달이 늘어난다 -- 사람이 정할
    사항으로 남겼고, 바꾸면 저장된 세그먼트 요약 전량이 무효가 된다.
    """
    if facts is None:
        return None
    band = weight_band(facts.species, facts.weight_kg)
    if band is WeightBand.UNKNOWN:
        return None
    return f"{facts.species.value}:{band.value}"


def all_segment_keys() -> list[str]:
    """만들 수 있는 세그먼트 전부. 상한이 7인 것을 테스트가 단언한다."""
    keys = []
    for species in Species:
        for band in WeightBand:
            if band is WeightBand.UNKNOWN or not band.value.startswith(species.value):
                continue
            keys.append(f"{species.value}:{band.value}")
    return keys


# --- 골든 벡터 -----------------------------------------------------------------------

# 양 레포가 함께 읽는 벡터의 기준일. 고정값이어야 Kotlin 미러가 같은 답을 낸다 --
# ``today``를 넣지 않으면 나이 버킷이 실행 날짜에 따라 달라져 계약이 흔들린다.
GOLDEN_TODAY = date(2026, 10, 27)

_GOLDEN_CASES: tuple[tuple[str, PetFacts | None, PetFacts | None], ...] = (
    (
        "같은 품종 같은 체급",
        PetFacts(Species.DOG, breed_id=12, birth_year_month="2022-03", weight_kg=4.2),
        PetFacts(Species.DOG, breed_id=12, birth_year_month="2021-07", weight_kg=4.8),
    ),
    (
        "같은 품종 다른 체급",
        PetFacts(Species.DOG, breed_id=12, weight_kg=4.2),
        PetFacts(Species.DOG, breed_id=12, weight_kg=12.0),
    ),
    (
        "같은 품종이지만 리뷰어 체중 미기재",
        PetFacts(Species.DOG, breed_id=12, weight_kg=4.2),
        PetFacts(Species.DOG, breed_id=12, weight_kg=None),
    ),
    (
        "다른 품종 같은 체급 같은 나이대",
        PetFacts(Species.DOG, breed_id=12, birth_year_month="2022-01", weight_kg=4.2),
        PetFacts(Species.DOG, breed_id=99, birth_year_month="2022-11", weight_kg=4.9),
    ),
    (
        "다른 품종 같은 체급 나이 미기재",
        PetFacts(Species.DOG, breed_id=12, weight_kg=4.2),
        PetFacts(Species.DOG, breed_id=99, weight_kg=4.9),
    ),
    (
        "양쪽 체중 미기재는 체급 일치가 아니다",
        PetFacts(Species.DOG, breed_id=12, birth_year_month="2022-01"),
        PetFacts(Species.DOG, breed_id=99, birth_year_month="2022-05"),
    ),
    (
        "종이 다르다",
        PetFacts(Species.DOG, breed_id=12, weight_kg=4.2),
        PetFacts(Species.CAT, breed_id=301, weight_kg=4.2),
    ),
    (
        "리뷰어 펫 정보 없음",
        PetFacts(Species.DOG, breed_id=12, weight_kg=4.2),
        None,
    ),
    (
        "조회자 펫 정보 없음",
        None,
        PetFacts(Species.DOG, breed_id=12, weight_kg=4.2),
    ),
    (
        "고양이 같은 체급 같은 나이대",
        PetFacts(Species.CAT, breed_id=301, birth_year_month="2019-02", weight_kg=4.5),
        PetFacts(Species.CAT, breed_id=350, birth_year_month="2019-09", weight_kg=5.5),
    ),
)


def _facts_json(facts: PetFacts | None) -> dict[str, Any] | None:
    if facts is None:
        return None
    return {
        "species": facts.species.value,
        "breedId": facts.breed_id,
        "birthYearMonth": facts.birth_year_month,
        "weightKg": facts.weight_kg,
    }


def golden_vectors() -> dict[str, Any]:
    """티어 판정의 골든 벡터. 백엔드 Kotlin 미러가 같은 파일을 읽는다.

    ``today``를 파일에 적어 내보낸다. 적지 않으면 나이 버킷이 실행 날짜에 따라 달라져
    같은 파일이 두 레포에서 다른 답을 내고, 그 어긋남은 테스트가 잡아 주지 못한다.
    """
    return {
        "today": GOLDEN_TODAY.isoformat(),
        "note": (
            "펫 유사도 티어의 골든 벡터다. 정본은 app/review_summary/selection.py 이고 "
            "백엔드 Kotlin 미러가 이 파일을 읽어 같은 답을 내는지 단언한다. "
            "NO_PET_INFO 와 OTHER 를 섞으면 미검사가 조건 불일치로 적힌다."
        ),
        "ageBuckets": {
            "PUPPY": "연 차이 0",
            "YOUNG": "연 차이 1~2",
            "ADULT": "연 차이 3~6",
            "SENIOR": "연 차이 7 이상",
            "UNKNOWN": "생년월이 없거나 모양이 틀렸다",
        },
        "weightBands": {
            "DOG": {"DOG_UNDER_5": "<5", "DOG_5_10": "5~10", "DOG_10_25": "10~25", "DOG_25_PLUS": "25+"},
            "CAT": {"CAT_UNDER_4": "<4", "CAT_4_6": "4~6", "CAT_6_PLUS": "6+"},
        },
        "tierRank": {tier.value: rank for tier, rank in _TIER_RANK.items()},
        "segmentKeys": all_segment_keys(),
        "vectors": [
            {
                "name": name,
                "viewer": _facts_json(viewer),
                "reviewer": _facts_json(reviewer),
                "tier": match_tier(viewer, reviewer, today=GOLDEN_TODAY).value,
                "viewerSegment": segment_key(viewer),
                "reviewerSegment": segment_key(reviewer),
            }
            for name, viewer, reviewer in _GOLDEN_CASES
        ],
    }
