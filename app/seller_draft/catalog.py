"""리프 카테고리 상수표와 **0원 결정론 품목 어휘 게이트.**

``banned.py`` 머리글이 세운 원칙을 그대로 가져온다 -- "우회 표기는 정규화로 잡는다. 모델도
임베딩도 쓰지 않는다. 결정론적이라 프롬프트 주입으로 뚫리지 않는다." 품목 식별은 그 분류에
정확히 들어간다. 리프 라벨에서 파생한 표면형 매칭이고 의미 판단이 아니다.

## 이 모듈이 서 있는 실측 사실 둘

**(1) 리프는 정확히 50개다.** 대분류 3 / 중분류 14 / 리프 50(강아지 22 · 고양이 16 · 공통 12).
정본은 백엔드 ``V2__seed_categories.sql`` 이고, 그 파일은 런타임에 추가되지 않는 시드 전용
데이터다. 이 레포는 CI 에서 백엔드 레포를 볼 수 없으므로 사본을
``tests/contract/category-leaves.json`` 에 두고 테스트가 그 사본과 아래 표를 대조한다 --
백엔드가 리프를 더하면 사본을 갱신해야 하고, 갱신하지 않으면 깨진다.

**(2) 리프 라벨 6개가 강아지/고양이에 중복이다.** 건식사료(111·211) 처방식사료(114·213)
종합영양제(131·231) 유산균/장건강(134·234) 장난감(141·241) 하우스/방석(142·243).
그래서 **모델에게 카테고리 라벨을 내게 하고 서버가 id 로 맵핑하는 설계는 쓰지 않는다** --
거기서 조용히 틀린다. 툴은 리프 id 정수 열거를 받고 서버는 이 표로 **대조**만 한다.

## 왜 모호성의 전부가 "종" 한 축인가

리프 라벨을 ``/`` 로 쪼개 ``banned.fold()`` 로 접으면 표면형이 나오는데, 여러 리프에 걸리는
표면형이 13개이고 **그 13개 전부가 강아지/고양이 쌍이다.** 종을 알면 남는 후보가 1개가 된다.
종은 셀러가 카테고리 UI 에서 가장 먼저 고르는 값이거나 상품명에 흔히 드러난다("강아지 목줄").
즉 상품명에서 품목어가 읽히는 순간 리프는 거의 확정되고, 남는 한 축은 입력이 공짜로 메운다.
이 사실이 "(가) 상품명+카테고리만으로도 쓸 만하다"는 가설의 근거이고,
**그 가설이 실제 상품명 분포에서 성립하는지는 하네스가 재는 1순위 숫자다.**

## 사전의 알려진 구멍

``'연어 건사료 2kg'`` 은 무적중이다 -- 라벨이 ``건식사료`` 라서 ``건사료`` 가 걸리지 않는다.
2층(수기 동의어)을 **추측으로 미리 채우지 않는다.** 채우면 검증되지 않은 수기 데이터가
측정 기준선에 섞인다. 하네스의 미적중 목록을 받아 채우고 재측정하는 것이 순서다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.inspection.banned import fold


class Species(StrEnum):
    """상품 트리의 대분류 축.

    ``app/review_summary/selection.py`` 의 ``Species`` 와 **다른 열거다.** 그쪽은 펫 프로필의
    종이라 ``DOG|CAT`` 둘뿐이고, 상품 트리에는 ``공통`` 대분류가 있다. 한쪽을 import 해
    재사용하면 ``COMMON`` 을 표현할 수 없고, 두 패키지가 서로를 끌어들인다.
    """

    DOG = "DOG"
    CAT = "CAT"
    COMMON = "COMMON"


class ItemGrade(StrEnum):
    """품목 신호 등급. **이 설계의 단일 분기축이다.**

    넷을 한꺼번에 정한다. (ㄱ) 모델 호출 여부 (ㄴ) 이미지 사용 여부(``GATED`` 모드)
    (ㄷ) 어느 필드를 채울지 (ㄹ) 하네스의 층화 축. 넷에 **같은 함수**를 쓰는 것이 중요하다 --
    층화 라벨과 운영 분기가 다른 코드로 계산되면 하네스가 재는 층이 운영의 층과 어긋나고,
    그러면 층별 숫자 전부가 거짓이다. 하네스 자기검사가 그 일치를 측정 전에 확인한다.
    """

    PINNED = "PINNED"
    NARROWED = "NARROWED"
    OPAQUE = "OPAQUE"


class ResolvedBy(StrEnum):
    """무엇이 리프를 확정했는가. 사람이 사전을 키울 때 읽는 값이다."""

    SELLER_LEAF_CATEGORY = "SELLER_LEAF_CATEGORY"
    NAME_TERM = "NAME_TERM"
    NAME_TERM_WITH_SPECIES = "NAME_TERM_WITH_SPECIES"
    NAME_TERM_WITH_CATEGORY = "NAME_TERM_WITH_CATEGORY"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class Leaf:
    category_id: int
    species: Species
    mid_label: str
    leaf_label: str

    @property
    def path(self) -> str:
        return f"{_TOP_LABELS[self.species]} > {self.mid_label} > {self.leaf_label}"


_TOP_LABELS: dict[Species, str] = {
    Species.DOG: "강아지",
    Species.CAT: "고양이",
    Species.COMMON: "공통",
}

_D = Species.DOG
_C = Species.CAT
_X = Species.COMMON

# 리프 50행. ``V2__seed_categories.sql`` 에서 받아 적었다. id 가 계층을 인코딩한다 --
# 대분류 n, 중분류 n0+m, 리프 (n0+m)0+k.
LEAVES: tuple[Leaf, ...] = (
    Leaf(111, _D, "사료", "건식사료"),
    Leaf(112, _D, "사료", "습식/소프트사료"),
    Leaf(113, _D, "사료", "동결건조/생식"),
    Leaf(114, _D, "사료", "처방식사료"),
    Leaf(121, _D, "간식", "껌/덴탈"),
    Leaf(122, _D, "간식", "건조/수제간식"),
    Leaf(123, _D, "간식", "캔/파우치"),
    Leaf(124, _D, "간식", "트릿/져키"),
    Leaf(131, _D, "영양제", "종합영양제"),
    Leaf(132, _D, "영양제", "관절/뼈"),
    Leaf(133, _D, "영양제", "피부/모질"),
    Leaf(134, _D, "영양제", "유산균/장건강"),
    Leaf(141, _D, "용품", "장난감"),
    Leaf(142, _D, "용품", "하우스/방석"),
    Leaf(143, _D, "용품", "훈련용품"),
    Leaf(151, _D, "산책/외출", "하네스/가슴줄"),
    Leaf(152, _D, "산책/외출", "목줄/리드줄"),
    Leaf(153, _D, "산책/외출", "산책가방"),
    Leaf(154, _D, "산책/외출", "이동장/유모차"),
    Leaf(161, _D, "의류/패션", "상의/아우터"),
    Leaf(162, _D, "의류/패션", "신발/양말"),
    Leaf(163, _D, "의류/패션", "악세서리"),
    Leaf(211, _C, "사료", "건식사료"),
    Leaf(212, _C, "사료", "습식/파우치"),
    Leaf(213, _C, "사료", "처방식사료"),
    Leaf(221, _C, "간식", "츄르/캔"),
    Leaf(222, _C, "간식", "건조간식"),
    Leaf(223, _C, "간식", "캣닢/트릿"),
    Leaf(231, _C, "영양제", "종합영양제"),
    Leaf(232, _C, "영양제", "헤어볼/모질"),
    Leaf(233, _C, "영양제", "신장/비뇨기"),
    Leaf(234, _C, "영양제", "유산균/장건강"),
    Leaf(241, _C, "용품", "장난감"),
    Leaf(242, _C, "용품", "스크래처/캣타워"),
    Leaf(243, _C, "용품", "하우스/방석"),
    Leaf(251, _C, "모래/화장실", "고양이모래"),
    Leaf(252, _C, "모래/화장실", "화장실"),
    Leaf(253, _C, "모래/화장실", "모래삽/기타"),
    Leaf(311, _X, "미용/목욕", "샴푸/린스"),
    Leaf(312, _X, "미용/목욕", "미용도구/이발기"),
    Leaf(313, _X, "미용/목욕", "구강/덴탈케어"),
    Leaf(321, _X, "위생용품", "배변패드/배변판"),
    Leaf(322, _X, "위생용품", "탈취제/소독제"),
    Leaf(323, _X, "위생용품", "물티슈/티슈"),
    Leaf(324, _X, "위생용품", "청소/세정"),
    Leaf(331, _X, "리빙/가전", "식기/급식기"),
    Leaf(332, _X, "리빙/가전", "자동급식기"),
    Leaf(333, _X, "리빙/가전", "정수기/급수기"),
    Leaf(334, _X, "리빙/가전", "펫드라이룸"),
    Leaf(335, _X, "리빙/가전", "홈카메라"),
)

LEAF_BY_ID: dict[int, Leaf] = {leaf.category_id: leaf for leaf in LEAVES}
LEAF_IDS: tuple[int, ...] = tuple(leaf.category_id for leaf in LEAVES)

# 품목어로 쓰지 않는 파생 표면형. **집합이 하나이고 테스트가 그 이유를 들고 있다.**
#
# 1층은 코드가 ``LEAVES`` 에서 파생시키므로 손으로 적을 자리가 없는데, 파생된 표면형 가운데
# 품목을 가리키지 않는 것이 하나 있다 -- ``모래삽/기타`` 의 ``기타``. 그것은 "그 밖"이라는
# 뜻이고 부분 문자열로 두면 ``기타 용품`` 같은 평범한 이름이 리프 253 으로 확정된다.
# 조용히 틀린 ``PINNED`` 는 이 설계가 가장 피하려는 실패 모양이라 뺀다.
_SURFACE_STOPWORDS: frozenset[str] = frozenset({fold("기타")})


def _derive_item_terms() -> dict[str, frozenset[int]]:
    """1층 품목 어휘를 ``LEAVES`` 에서 **파생시킨다. 손으로 적지 않는다.**

    손으로 적으면 시드와 드리프트하고 그 드리프트는 조용하다. 리프 라벨이 이미 ``/`` 로
    표면형을 갈라 뒀으므로(``목줄/리드줄`` -> ``목줄``, ``리드줄``) 쪼개서 접으면 끝난다.
    조회와 사전에 **같은 ``fold()``** 를 쓰는 것이 ``banned.py`` 의 원칙이다 -- 그 덕에
    띄어쓰기·기호·반복·전각·동형문자 우회가 공짜로 무력화된다.
    """
    table: dict[str, set[int]] = {}
    for leaf in LEAVES:
        for surface in leaf.leaf_label.split("/"):
            key = fold(surface)
            if not key or key in _SURFACE_STOPWORDS:
                continue
            table.setdefault(key, set()).add(leaf.category_id)
    return {key: frozenset(ids) for key, ids in table.items()}


# 1층(자동 파생). 2층(수기 동의어)은 **일부러 비어 있다.** 머리글의 "사전의 알려진 구멍" 참조.
ITEM_TERMS: dict[str, frozenset[int]] = _derive_item_terms()

# 2층. 하네스의 미적중 목록을 받아 채운다. 비어 있는 것이 측정 전의 정직한 상태다.
MANUAL_ITEM_TERMS: dict[str, tuple[int, ...]] = {}

# 종 단서. **접은 키가 한 글자인 표기를 넣지 않는다.**
#
# ``독`` 을 넣으면 ``소독제``(322)가 강아지 상품이 되고, 그 오류는 조용하다. 종 단서가
# 틀리면 13개 모호 표면형이 **틀린 쪽으로 확정되어** 조용히 잘못된 ``PINNED`` 이 되는데,
# 그것이 이 설계가 가장 피하려는 실패 모양이다. 과소 적중이 과대 적중보다 안전하다.
#
# **``fold()`` 가 같은 글자 반복을 한 번으로 줄인다는 것을 여기서 밟았다.** 처음에는
# ``멍멍``·``댕댕`` 을 넣었는데 접히면서 키가 ``멍``·``댕`` 한 글자가 됐고, 바로 위 규칙을
# 코드가 스스로 어기고 있었다(실행해 확인했다). 둘을 뺀 이유는 ``강아지``·``반려견``·
# ``애견``·``도그``·``퍼피`` 가 실제 상품명 대부분을 덮어 한계 이득이 작은데, 한 글자 단서가
# 틀릴 때의 손해는 위에 적은 그 손해이기 때문이다. ``멍뭉이``·``댕댕이`` 같은 표기는 모델이
# 읽을 수 있고 어휘 게이트는 못 읽는 단서로 남는다 -- 하네스의 ``AMBIGUOUS`` 층이 바로
# 그 차이를 재는 자리다. 테스트가 접은 키 길이 하한을 고정해 재발을 막는다.
SPECIES_TERMS: dict[str, Species] = {
    fold(term): species
    for term, species in (
        ("강아지", Species.DOG),
        ("반려견", Species.DOG),
        ("애견", Species.DOG),
        ("도그", Species.DOG),
        ("dog", Species.DOG),
        ("퍼피", Species.DOG),
        ("puppy", Species.DOG),
        ("고양이", Species.CAT),
        ("반려묘", Species.CAT),
        ("애묘", Species.CAT),
        ("냥이", Species.CAT),
        ("캣", Species.CAT),
        ("cat", Species.CAT),
        ("키튼", Species.CAT),
        ("kitten", Species.CAT),
    )
}

# 접은 종 단서 키의 최소 길이. ``fold()`` 의 반복 축약 때문에 **원문 길이로는 보장되지
# 않는다** -- 테스트가 이 값으로 ``SPECIES_TERMS`` 를 검사한다.
SPECIES_TERM_MIN_FOLDED_LEN = 2

# 위 하한의 **유일한 예외.** 집합으로 두고 테스트가 고정한다 -- 하한을 1 로 내려 규칙을
# 무력화하는 대신 예외를 눈에 보이게 두는 쪽을 고른다.
#
# ``캣`` 을 남기는 근거: 이 글자를 품은 리프 표면형은 ``캣닢``(223)과 ``캣타워``(242) 뿐이고
# 둘 다 고양이라 적중이 어긋날 수 없다. 그리고 ``캣 캔`` 처럼 품목어가 모호할 때 종을
# 메우는 실제 값이 있다(``캔`` 은 123·221 에 걸린다). **못 막는 것:** 이 글자를 품은
# 강아지·공통 상품명이 들어오면 틀린 쪽으로 확정된다. 그런 이름을 실제로 만나면 예외에서
# 빼는 것이 맞는 대응이고, 하네스의 층별 오답 목록이 그것을 먼저 보여 준다.
SPECIES_TERM_SHORT_ALLOWED: frozenset[str] = frozenset({fold("캣")})


@dataclass(frozen=True, slots=True)
class ItemSignal:
    """어휘 게이트의 산출물. 모델을 0회 부르고 비용이 0이다."""

    grade: ItemGrade
    resolved_by: ResolvedBy
    matched_terms: tuple[str, ...]
    candidate_leaf_ids: tuple[int, ...]
    species: Species | None
    given_category_id: int | None

    @property
    def pinned_leaf_id(self) -> int | None:
        if self.grade is ItemGrade.PINNED and len(self.candidate_leaf_ids) == 1:
            return self.candidate_leaf_ids[0]
        return None


def subtree_leaf_ids(category_id: int | None) -> tuple[int, ...]:
    """주어진 카테고리 아래의 리프 전부. depth 1·2·3 어느 것이든 받는다.

    id 가 계층을 인코딩하므로 접두 비교로 끝난다 -- 조인도 테이블도 필요 없다.
    """
    if category_id is None:
        return ()
    if category_id in LEAF_BY_ID:
        return (category_id,)
    prefix = str(category_id)
    if len(prefix) not in (1, 2):
        return ()
    return tuple(leaf_id for leaf_id in LEAF_IDS if str(leaf_id).startswith(prefix))


def species_of_category(category_id: int | None) -> Species | None:
    """카테고리의 대분류 종. 셀러가 고른 값이라 상품명 단서보다 신뢰도가 높다."""
    if category_id is None:
        return None
    top = str(category_id)[0]
    return {"1": Species.DOG, "2": Species.CAT, "3": Species.COMMON}.get(top)


def match_item_terms(product_name: str) -> tuple[tuple[str, ...], frozenset[int]]:
    """상품명에서 품목 표면형을 찾는다. **가장 긴 적중만 쓴다.**

    길이 선호가 선택이 아니라 필수다. ``자동급식기`` 는 ``자동급식기``(332)와
    ``급식기``(331)에 둘 다 걸리고, ``덴탈케어`` 는 ``덴탈케어``(313)와 ``덴탈``(121)에 둘 다
    걸린다. 길이를 보지 않으면 그 둘이 "모호"로 떨어져 실제로는 확정 가능한 이름이
    ``NARROWED`` 가 된다 -- 사전이 더 자세한 표면형을 가진 것이 벌이 되는 셈이다.

    같은 길이의 적중이 여러 리프를 가리키면 그대로 후보에 남긴다. ``소뼈껌`` 이 ``뼈``(132)와
    ``껌``(121)에 걸리는 것이 그 경우이고, 그것은 사전으로 가릴 수 없는 진짜 모호성이다.
    """
    folded = fold(product_name)
    if not folded:
        return (), frozenset()

    hits: list[tuple[int, str, frozenset[int]]] = []
    for term, ids in ITEM_TERMS.items():
        if term in folded:
            hits.append((len(term), term, ids))
    for term, manual_ids in MANUAL_ITEM_TERMS.items():
        key = fold(term)
        if key in folded:
            hits.append((len(key), key, frozenset(manual_ids)))
    if not hits:
        return (), frozenset()

    longest = max(length for length, _term, _ids in hits)
    kept = [(term, ids) for length, term, ids in hits if length == longest]
    candidates: set[int] = set()
    for _term, ids in kept:
        candidates |= ids
    return tuple(sorted({term for term, _ids in kept})), frozenset(candidates)


def match_species(product_name: str) -> Species | None:
    """상품명의 종 단서. 둘 이상이 섞이면 **판정하지 않는다.**

    ``강아지 고양이 공용`` 이 그 경우다. 하나를 고르면 13개 모호 표면형이 틀린 쪽으로
    확정될 수 있고, 모른다고 적으면 ``NARROWED`` 로 떨어져 모델이 고른다.
    """
    folded = fold(product_name)
    found = {species for term, species in SPECIES_TERMS.items() if term in folded}
    if len(found) == 1:
        return found.pop()
    return None


def resolve(product_name: str, category_id: int | None = None) -> ItemSignal:
    """품목 신호를 낸다. **모델 0회, 0원.**

    순서가 설계다.

    1. 셀러가 리프를 골랐으면 그것으로 끝난다 -- 라벨 자체가 품목이다.
    2. 상품명에서 표면형을 찾는다(가장 긴 적중).
    3. 후보가 둘 이상이면 종으로 좁힌다. 종은 상품명 단서보다 **셀러가 고른 카테고리**를
       먼저 믿는다. 셀러가 고른 값이고 상품명은 타이핑된 값이다.
    4. 그래도 둘 이상이면 주어진 카테고리 서브트리로 좁힌다.
    5. 하나면 PINNED, 둘 이상이면 NARROWED, 하나도 못 찾으면 OPAQUE.
    """
    given_species = species_of_category(category_id)
    name_species = match_species(product_name)
    species = given_species or name_species

    if category_id is not None and category_id in LEAF_BY_ID:
        terms, _ids = match_item_terms(product_name)
        return ItemSignal(
            grade=ItemGrade.PINNED,
            resolved_by=ResolvedBy.SELLER_LEAF_CATEGORY,
            matched_terms=terms,
            candidate_leaf_ids=(category_id,),
            species=species,
            given_category_id=category_id,
        )

    terms, candidates = match_item_terms(product_name)
    if not candidates:
        # 품목어가 하나도 안 걸렸다. 카테고리가 있으면 그 서브트리를 후보로 적어 둔다 --
        # 모델이 고를 범위가 되고, 없으면 모델을 아예 부르지 않는 분기가 된다.
        return ItemSignal(
            grade=ItemGrade.OPAQUE,
            resolved_by=ResolvedBy.NONE,
            matched_terms=(),
            candidate_leaf_ids=subtree_leaf_ids(category_id),
            species=species,
            given_category_id=category_id,
        )

    resolved_by = ResolvedBy.NAME_TERM
    narrowed = set(candidates)

    if len(narrowed) > 1 and species is not None:
        by_species = {leaf_id for leaf_id in narrowed if LEAF_BY_ID[leaf_id].species is species}
        if by_species:
            narrowed = by_species
            resolved_by = (
                ResolvedBy.NAME_TERM_WITH_CATEGORY
                if given_species is not None
                else ResolvedBy.NAME_TERM_WITH_SPECIES
            )

    subtree = set(subtree_leaf_ids(category_id))
    if len(narrowed) > 1 and subtree:
        within = narrowed & subtree
        if within:
            narrowed = within
            resolved_by = ResolvedBy.NAME_TERM_WITH_CATEGORY

    grade = ItemGrade.PINNED if len(narrowed) == 1 else ItemGrade.NARROWED
    if grade is ItemGrade.NARROWED:
        resolved_by = ResolvedBy.NONE
    return ItemSignal(
        grade=grade,
        resolved_by=resolved_by,
        matched_terms=terms,
        candidate_leaf_ids=tuple(sorted(narrowed)),
        species=species,
        given_category_id=category_id,
    )


def leaf_path(category_id: int) -> str | None:
    leaf = LEAF_BY_ID.get(category_id)
    return leaf.path if leaf is not None else None


def render_leaf_paths() -> str:
    """리프 50개를 프롬프트에 넣을 모양으로 렌더한다. **요청과 무관한 고정 문자열이다.**

    접두부에 들어가므로 요청 데이터가 섞이면 캐시 엔트리가 입력 수만큼 갈라진다.
    후보를 좁혀 넣고 싶은 유혹이 여기서 거부된다 -- 좁히면 0단이 틀렸을 때 모델이 정답을
    고를 수단을 잃어 0단의 구멍이 그대로 최종 오답이 된다. 좁히는 것은 user content 에서
    힌트로 한다.
    """
    lines: list[str] = []
    current: tuple[Species, str] | None = None
    for leaf in LEAVES:
        head = (leaf.species, leaf.mid_label)
        if head != current:
            lines.append(f"[{_TOP_LABELS[leaf.species]} > {leaf.mid_label}]")
            current = head
        lines.append(f"  {leaf.category_id} {leaf.leaf_label}")
    return "\n".join(lines)


def contract_rows() -> list[dict[str, Any]]:
    """``tests/contract/category-leaves.json`` 과 대조하는 모양.

    백엔드 시드가 정본이고 이 파일은 사본이다. 사본을 테스트가 읽어 대조하는 이유는 CI 가
    백엔드 레포를 볼 수 없기 때문이고, 그래서 **백엔드가 리프를 더하면 사람이 사본을
    갱신해야 한다.** 갱신하지 않으면 깨지는 쪽이 조용히 어긋나는 쪽보다 낫다.
    """
    return [
        {
            "categoryId": leaf.category_id,
            "species": leaf.species.value,
            "midLabel": leaf.mid_label,
            "leafLabel": leaf.leaf_label,
        }
        for leaf in LEAVES
    ]


def lexicon_stats() -> dict[str, Any]:
    """사전의 모양을 숫자로 낸다. 하네스가 키 없이 찍는 리포트의 재료다."""
    single = [term for term, ids in ITEM_TERMS.items() if len(ids) == 1]
    multi = {term: sorted(ids) for term, ids in ITEM_TERMS.items() if len(ids) > 1}
    unresolvable_with_species = {
        term: sorted(ids)
        for term, ids in multi.items()
        if len({LEAF_BY_ID[i].species for i in ids}) < len(ids)
    }
    covered = {leaf_id for ids in ITEM_TERMS.values() for leaf_id in ids}
    unique_covered = {next(iter(ITEM_TERMS[term])) for term in single}
    return {
        "leaves": len(LEAVES),
        "surfaces": len(ITEM_TERMS),
        "stopwords": sorted(_SURFACE_STOPWORDS),
        "singleLeafSurfaces": len(single),
        "multiLeafSurfaces": sorted(multi),
        # 종을 알고도 후보가 둘 이상 남는 표면형. **비어 있는 것이 이 설계의 척추다.**
        "speciesCannotResolve": sorted(unresolvable_with_species),
        "leavesWithoutOwnSurface": sorted(set(LEAF_IDS) - covered),
        # 자기만의 고유 표면형이 없는 리프. 종을 모르면 가릴 근거가 없는 리프들이다.
        "leavesWithoutUniqueSurface": sorted(set(LEAF_IDS) - unique_covered),
        "manualTerms": len(MANUAL_ITEM_TERMS),
    }


__all__ = [
    "ITEM_TERMS",
    "LEAF_BY_ID",
    "LEAF_IDS",
    "LEAVES",
    "MANUAL_ITEM_TERMS",
    "SPECIES_TERMS",
    "SPECIES_TERM_MIN_FOLDED_LEN",
    "ItemGrade",
    "ItemSignal",
    "Leaf",
    "ResolvedBy",
    "Species",
    "contract_rows",
    "leaf_path",
    "lexicon_stats",
    "match_item_terms",
    "match_species",
    "render_leaf_paths",
    "resolve",
    "species_of_category",
    "subtree_leaf_ids",
]
