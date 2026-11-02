"""품목 어휘 게이트. **모델 0회, 0원이라 전부 키 없이 고정된다.**

이 게이트가 넷을 정한다 -- 모델 호출 여부, 이미지 사용 여부, 어느 필드를 채울지,
하네스의 층화 축. 넷이 **같은 함수**를 쓰므로 여기서 틀리면 운영과 측정이 함께 틀린다.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.inspection.banned import fold
from app.seller_draft import catalog
from app.seller_draft.catalog import ItemGrade, ResolvedBy, Species

CONTRACT = Path(__file__).parent / "contract" / "category-leaves.json"


def test_리프가_정확히_50개다() -> None:
    """선행 조사 둘이 48과 50으로 갈렸고 50이 맞다. 툴 enum 의 길이가 여기서 정해진다."""
    assert len(catalog.LEAVES) == 50
    assert len(catalog.LEAF_IDS) == len(set(catalog.LEAF_IDS))

    by_species = {species: 0 for species in Species}
    for leaf in catalog.LEAVES:
        by_species[leaf.species] += 1
    assert by_species == {Species.DOG: 22, Species.CAT: 16, Species.COMMON: 12}


def test_리프_표가_커밋된_시드_사본과_같다() -> None:
    """정본은 백엔드 ``V2__seed_categories.sql`` 이고 이 파일은 사본이다.

    CI 가 백엔드 레포를 볼 수 없어서 사본을 둔다. **백엔드가 리프를 더하면 깨지는 쪽이
    조용히 어긋나는 쪽보다 낫다.**
    """
    stored = json.loads(CONTRACT.read_text(encoding="utf-8"))

    assert stored["leafCount"] == len(catalog.LEAVES)
    assert stored["leaves"] == catalog.contract_rows()


def test_리프_라벨_6개가_강아지_고양이에_중복이다() -> None:
    """**모델이 라벨을 내고 서버가 id 로 맵핑하는 설계를 거부한 근거다.**"""
    labels: dict[str, list[int]] = {}
    for leaf in catalog.LEAVES:
        labels.setdefault(leaf.leaf_label, []).append(leaf.category_id)
    shared = {label: ids for label, ids in labels.items() if len(ids) > 1}

    assert set(shared) == {
        "건식사료",
        "처방식사료",
        "종합영양제",
        "유산균/장건강",
        "장난감",
        "하우스/방석",
    }
    for ids in shared.values():
        species = {catalog.LEAF_BY_ID[i].species for i in ids}
        assert species == {Species.DOG, Species.CAT}, "중복은 전부 강아지/고양이 쌍이어야 한다"


def test_품목_어휘_1층이_리프에서_파생된다() -> None:
    """**손으로 적지 않는다.** 적으면 시드와 드리프트하고 그 드리프트는 조용하다."""
    derived: dict[str, set[int]] = {}
    for leaf in catalog.LEAVES:
        for surface in leaf.leaf_label.split("/"):
            key = fold(surface)
            if key:
                derived.setdefault(key, set()).add(leaf.category_id)

    stopped = {key for key in derived if key in catalog._SURFACE_STOPWORDS}
    expected = {key: frozenset(ids) for key, ids in derived.items() if key not in stopped}
    assert catalog.ITEM_TERMS == expected
    # 2층은 **비어 있는 것이 측정 전의 정직한 상태다.** 추측으로 채우면 검증되지 않은
    # 수기 데이터가 측정 기준선에 섞인다.
    assert catalog.MANUAL_ITEM_TERMS == {}


def test_불용어가_하나이고_그것이_기타다() -> None:
    """``모래삽/기타`` 의 ``기타`` 는 "그 밖"이라는 뜻이고 품목이 아니다.

    부분 문자열로 두면 ``기타 용품`` 같은 평범한 이름이 253 으로 **조용히 확정된다.**
    """
    assert catalog._SURFACE_STOPWORDS == frozenset({fold("기타")})
    assert catalog.resolve("기타 용품 세트", None).grade is ItemGrade.OPAQUE


def test_종을_알면_모호성이_0이_된다() -> None:
    """**이 설계의 척추다.** 모호한 표면형 전부가 종 한 축으로 풀린다."""
    stats = catalog.lexicon_stats()

    assert len(stats["multiLeafSurfaces"]) == 13
    assert stats["speciesCannotResolve"] == []
    assert len(stats["leavesWithoutUniqueSurface"]) == 14


def test_접은_종_단서_키가_한_글자가_아니다() -> None:
    """``fold()`` 가 같은 글자 반복을 줄여 ``멍멍`` 이 ``멍`` 한 글자가 됐던 자리다.

    한 글자 종 단서가 틀리면 13개 모호 표면형이 **틀린 쪽으로 확정되어** 조용히 잘못된
    ``PINNED`` 이 되는데 그것이 이 설계가 가장 피하려는 실패 모양이다. 원문 길이로는
    보장되지 않으므로 **접은 키**를 검사한다.
    """
    short = {key for key in catalog.SPECIES_TERMS if len(key) < catalog.SPECIES_TERM_MIN_FOLDED_LEN}

    assert short <= catalog.SPECIES_TERM_SHORT_ALLOWED, (
        f"접은 키가 한 글자인 종 단서가 늘었다: {sorted(short - catalog.SPECIES_TERM_SHORT_ALLOWED)}. "
        "예외를 늘리려면 그 글자를 품은 다른 종의 상품명이 없는지 먼저 확인해라"
    )
    # 예외로 남긴 글자가 가리키는 리프는 전부 고양이여야 한다 -- 적중이 어긋날 수 없다.
    for allowed in catalog.SPECIES_TERM_SHORT_ALLOWED:
        for surface, ids in catalog.ITEM_TERMS.items():
            if allowed in surface:
                assert all(catalog.LEAF_BY_ID[i].species is Species.CAT for i in ids), surface


def test_가장_긴_적중만_쓴다() -> None:
    """길이 선호가 선택이 아니라 필수다.

    ``자동급식기`` 는 ``급식기`` 에도 걸리고 ``덴탈케어`` 는 ``덴탈`` 에도 걸린다.
    길이를 보지 않으면 **사전이 더 자세한 표면형을 가진 것이 벌이 된다** --
    확정 가능한 이름이 모호로 떨어진다.
    """
    assert catalog.resolve("자동급식기 4L 타이머", None).candidate_leaf_ids == (332,)
    assert catalog.resolve("덴탈케어 치약 세트", None).candidate_leaf_ids == (313,)
    # 같은 길이의 적중이 여러 리프를 가리키면 **진짜 모호성이라 후보에 남긴다.**
    signal = catalog.resolve("소뼈껌", None)
    assert signal.grade is ItemGrade.NARROWED
    assert set(signal.candidate_leaf_ids) == {121, 132}


def test_셀러가_고른_리프가_상품명보다_먼저다() -> None:
    """라벨 자체가 품목이다. 상품명에서 다시 찾을 이유가 없다."""
    signal = catalog.resolve("강아지 목줄 M", 151)

    assert signal.grade is ItemGrade.PINNED
    assert signal.resolved_by is ResolvedBy.SELLER_LEAF_CATEGORY
    assert signal.candidate_leaf_ids == (151,)


def test_종이_모호한_표면형을_확정한다() -> None:
    assert catalog.resolve("건식사료 2kg", None).grade is ItemGrade.NARROWED
    assert catalog.resolve("고양이 건식사료 2kg", None).candidate_leaf_ids == (211,)
    assert catalog.resolve("강아지 건식사료 2kg", None).candidate_leaf_ids == (111,)


def test_카테고리가_종보다_신뢰도가_높다() -> None:
    """셀러가 고른 값이고 상품명은 타이핑된 값이다."""
    signal = catalog.resolve("건식사료 2kg", 1)

    assert signal.grade is ItemGrade.PINNED
    assert signal.resolved_by is ResolvedBy.NAME_TERM_WITH_CATEGORY
    assert signal.candidate_leaf_ids == (111,)


def test_종_단서가_둘_섞이면_판정하지_않는다() -> None:
    """하나를 고르면 13개 모호 표면형이 틀린 쪽으로 확정될 수 있다."""
    assert catalog.match_species("강아지 고양이 공용 건식사료") is None
    assert catalog.resolve("강아지 고양이 공용 건식사료", None).grade is ItemGrade.NARROWED


def test_품목어가_없으면_카테고리_서브트리를_후보로_적는다() -> None:
    """모델이 고를 범위가 되고, 카테고리도 없으면 모델을 아예 부르지 않는 분기가 된다."""
    with_category = catalog.resolve("모모의 하루", 11)
    assert with_category.grade is ItemGrade.OPAQUE
    assert set(with_category.candidate_leaf_ids) == {111, 112, 113, 114}

    without = catalog.resolve("모모의 하루", None)
    assert without.grade is ItemGrade.OPAQUE
    assert without.candidate_leaf_ids == ()


def test_서브트리를_접두로_계산한다() -> None:
    """id 가 계층을 인코딩하므로 조인도 테이블도 필요 없다."""
    assert catalog.subtree_leaf_ids(152) == (152,)
    assert set(catalog.subtree_leaf_ids(15)) == {151, 152, 153, 154}
    assert len(catalog.subtree_leaf_ids(1)) == 22
    assert catalog.subtree_leaf_ids(None) == ()
    assert catalog.subtree_leaf_ids(9999) == ()


def test_띄어쓰기와_기호_우회가_무력화된다() -> None:
    """조회와 사전에 **같은 ``fold()``** 를 쓰는 것이 ``banned.py`` 의 원칙이다."""
    for name in ("강아지 목  줄 M", "강아지 목-줄 M", "강아지 ＭＯ목줄"):
        assert 152 in catalog.resolve(name, None).candidate_leaf_ids, name


def test_리프_경로_블록이_요청과_무관한_고정_문자열이다() -> None:
    """접두부에 들어가므로 요청 데이터가 섞이면 캐시 엔트리가 입력 수만큼 갈라진다."""
    first = catalog.render_leaf_paths()
    second = catalog.render_leaf_paths()

    assert first == second
    for leaf in catalog.LEAVES:
        assert str(leaf.category_id) in first
        assert leaf.leaf_label in first


def test_사전의_알려진_구멍을_숫자로_남긴다() -> None:
    """``'연어 건사료 2kg'`` 은 무적중이다 -- 라벨이 ``건식사료`` 라서 ``건사료`` 가 안 걸린다.

    이 구멍을 2층 동의어로 **미리 메우지 않기로 했으므로**, 그 사실이 테스트로 보여야
    다음 사람이 "사전이 완전하다"고 오해하지 않는다.
    """
    assert catalog.resolve("연어 건사료 2kg", None).grade is ItemGrade.OPAQUE
