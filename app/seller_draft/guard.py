"""생성후 가드. **0원이고 재생성 0회.**

## 네 겹 가운데 이 모듈이 셋을 든다

1겹은 스키마다(효능 슬롯 없음, 상품명 필드 없음 -- ``tool.py``). 2겹은 프롬프트의 금지
예시다(``prompt.py``). 여기는 **3겹과 4겹**이다.

- 3겹: ``rules.screen(text).blocking`` -- 연락처·이메일·외부URL·계좌·봉투탈출과 **차단
  등급 금지 성분**. 순수 함수 하나이고 ``Settings`` 도 ``LlmClient`` 도 받지 않는다.
- 4겹: ``claims.screen(text)`` -- 0단이 **하나도 잡지 못하는** 효능·의료·인증 단정.

``banned.find_terms`` 를 따로 부르지 않는다. ``rules.screen`` 이 이미 그 사전을 태워
적중마다 ``blocking`` 플래그를 붙여 돌려주므로, 따로 부르면 같은 적중을 두 번 센다.
그리고 **권고 등급(``ADVISORY_TERMS``) 적중은 떨어뜨리지 않는다** -- 코코넛오일·우유·감귤은
반려동물 상품에 정당하게 쓰이고, 막으면 사료·간식 리프의 설명이 거의 항상 빈다.

## 왜 재생성하지 않는가

가드에 걸린 필드는 비우고 사유를 적는다. 근거 넷.

1. **비용이 고정된다**(최악 = 평균). 재생성 1회면 최악이 2배이고 둘째 시도도 걸릴 수 있다.
2. ``ProductDraftSaveRequest`` 의 전 필드가 널 허용이라 비움이 계약에 자연스럽다.
3. **통과와 미검사를 섞지 않는 규약에 맞는다** -- 통과한 필드만 채워 주고 걸린 필드는
   비우는 것이 "두 번째 시도에서 겨우 통과한 문구"보다 정직하다.
4. 부수 이득: 호출이 1회라 재생 카세트의 키가 충돌하지 않는다.

**재생성이 필요한지는 하네스의 가드 탈락률이 답한다.** 탈락률이 낮으면 재생성 논의 자체가
불필요하고, 높으면 재생성이 아니라 프롬프트·슬롯 설계를 고치라는 신호다.

## 슬롯 단위로 돌리는 이유

설명을 자유 텍스트 한 덩어리로 받지 않고 사실 슬롯 넷으로 쪼갰기 때문에, **가드가 한 슬롯만
떨어뜨리고 나머지를 살릴 수 있다.** 한 덩어리면 "치료" 한 단어가 설명 전체를 지운다.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from app.inspection import rules
from app.schemas.draft import (
    FieldStatus,
    GuardDrop,
    GuardKind,
    OptionCombinationDraft,
    OptionGroupDraft,
)
from app.schemas.inspection import ViolationCategory
from app.seller_draft import claims

# 옵션 제안 상한. **툴 스키마가 아니라 코드로 건다** -- ``strict`` 모드가 개수 제약을 받지
# 않는다. 백엔드 임시저장 DTO 의 상한은 그룹 5 · 조합 200 · 그룹당 값 50 이라 훨씬 넉넉한데,
# 초안이 그만큼 제안할 근거가 없고 데카르트곱이 커지면 셀러가 지우는 데 더 오래 걸린다.
MAX_OPTION_GROUPS = 2
MAX_VALUES_PER_GROUP = 6
MAX_COMBINATIONS = 36
# ``OptionGroup.NAME_MAX`` / ``OptionValue.NAME_MAX`` 와 같은 수다.
OPTION_NAME_MAX = 100


@dataclass(frozen=True, slots=True)
class TextVerdict:
    """텍스트 한 조각의 가드 결과. 통과면 ``status`` 가 ``None`` 이다."""

    status: FieldStatus | None
    kind: GuardKind | None
    detail: str

    @property
    def ok(self) -> bool:
        return self.status is None


_PASS = TextVerdict(None, None, "")


def screen_text(text: str) -> TextVerdict:
    """생성 문구 한 조각을 3겹과 4겹에 태운다. **차단 적중만 본다.**

    권고 등급 성분 적중은 통과시킨다 -- 이유는 모듈 머리에 적었다. 규칙 적중과 성분 적중을
    다른 사유로 가르는 것도 중요하다. 셀러에게 전할 말이 "연락처를 뺐다"와 "그 성분을 뺐다"로
    다르고, 하네스가 겹별 탈락률을 재는 축이 그 구분이다.
    """
    if not text or not text.strip():
        return _PASS

    for hit in rules.screen(text).blocking:
        if hit.category is ViolationCategory.BANNED_INGREDIENT:
            return TextVerdict(
                FieldStatus.EMPTY_GUARD_BANNED,
                GuardKind.BANNED,
                f"금지 성분 '{hit.quote}' 이 들어 있어 비웠다 ({hit.reason})",
            )
        return TextVerdict(
            FieldStatus.EMPTY_GUARD_RULE,
            GuardKind.RULE,
            f"0단 규칙에 걸려 비웠다: {hit.reason} ('{hit.quote}')",
        )

    hits = claims.screen(text)
    if hits:
        first = hits[0]
        return TextVerdict(
            FieldStatus.EMPTY_GUARD_CLAIM,
            GuardKind.CLAIM,
            f"효능 단정 표현 '{first.canonical}' 이 들어 있어 비웠다 ({first.reason})",
        )
    return _PASS


@dataclass(slots=True)
class DescriptionResult:
    """설명 조립 결과. **살아남은 슬롯과 떨어진 슬롯을 함께 든다.**"""

    text: str | None = None
    status: FieldStatus = FieldStatus.EMPTY_SCHEMA_INVALID
    drops: list[GuardDrop] = field(default_factory=list)
    kept_slots: list[str] = field(default_factory=list)
    truncated: bool = False


def build_description(
    slots: dict[str, str],
    *,
    order: tuple[str, ...],
    max_chars: int,
) -> DescriptionResult:
    """슬롯을 가드에 태우고 살아남은 것만 이어 붙인다.

    길이 초과는 **폐기가 아니라 문장 경계 절단**이다. 상품명이면 절단이 뜻을 바꾸지만
    설명은 뒤를 자르는 것이 비우는 것보다 셀러에게 쓸모 있다. 그리고 절단은 손실이라
    플래그로 남긴다 -- 조용히 자르면 셀러가 모델이 그만큼만 썼다고 읽는다.
    """
    result = DescriptionResult()
    survivors: list[str] = []
    hit_status: FieldStatus | None = None

    for key in order:
        piece = (slots.get(key) or "").strip()
        if not piece:
            continue
        verdict = screen_text(piece)
        if not verdict.ok:
            assert verdict.status is not None and verdict.kind is not None
            hit_status = hit_status or verdict.status
            result.drops.append(
                GuardDrop(field="description", kind=verdict.kind, detail=f"{key}: {verdict.detail}")
            )
            continue
        survivors.append(piece)
        result.kept_slots.append(key)

    if not survivors:
        # 슬롯이 전부 떨어졌으면 **가드 사유를 쓴다.** 하나도 안 왔으면 모델이 안 낸 것이다.
        result.status = hit_status or FieldStatus.EMPTY_SCHEMA_INVALID
        return result

    joined = "\n\n".join(survivors)
    if len(joined) > max_chars:
        joined, result.truncated = _truncate_at_sentence(joined, max_chars)
        result.drops.append(
            GuardDrop(
                field="description",
                kind=GuardKind.LENGTH,
                detail=f"설명이 {max_chars}자를 넘겨 문장 경계에서 잘랐다",
            )
        )
    result.text = joined
    result.status = FieldStatus.FILLED
    return result


def _truncate_at_sentence(text: str, limit: int) -> tuple[str, bool]:
    """문장 경계에서 자른다. 경계가 없으면 글자 수로 자른다."""
    head = text[:limit]
    for mark in ("다.", ".", "!", "?", "\n"):
        at = head.rfind(mark)
        if at >= limit // 2:
            return head[: at + len(mark)].rstrip(), True
    return head.rstrip(), True


@dataclass(frozen=True, slots=True)
class OptionVerdict:
    """옵션 제안의 판정. **부분 수정을 하지 않는다.**

    중복 값 이름만 떼어 살리는 길이 있지만 그러면 셀러가 의도하지 않은 옵션 구성이 만들어지고,
    그게 저장되면 셀러가 왜 그렇게 됐는지 알 수 없다. 전부 또는 전무로 두고, 그 규칙이
    옵션 초안을 거의 항상 비우게 만드는지는 **하네스의 option-rule 탈락률이 답한다.**
    """

    groups: list[OptionGroupDraft]
    combinations: list[OptionCombinationDraft]
    status: FieldStatus
    drops: list[GuardDrop]


def check_options(groups: list[OptionGroupDraft]) -> OptionVerdict:
    """등록 불변식을 코드로 만족시키고, 못 하면 전체를 비운다.

    ``ProductOptions`` 의 불변식(실측): 조합 최소 1개 / 그룹명 중복 불가 / **옵션값 이름은
    그룹이 달라도 중복 불가** / 각 조합은 그룹마다 정확히 하나씩 지목 / 같은 값 집합 중복 금지.

    **값 이름 전역 유일은 모델이 자연스럽게 위반하는 규칙이다**(색상의 "블랙"과 패턴의
    "블랙"). 그걸 통과시켜 셀러가 저장하면 등록이 거부된다.
    """
    drops: list[GuardDrop] = []

    def reject(kind: GuardKind, status: FieldStatus, detail: str) -> OptionVerdict:
        drops.append(GuardDrop(field="optionGroups", kind=kind, detail=detail))
        return OptionVerdict([], [], status, drops)

    cleaned: list[OptionGroupDraft] = []
    for group in groups:
        name = (group.name or "").strip()
        values = [value.strip() for value in group.values if value and value.strip()]
        if not name or not values:
            continue
        cleaned.append(OptionGroupDraft(name=name, values=list(dict.fromkeys(values))))

    if not cleaned:
        return OptionVerdict([], [], FieldStatus.EMPTY_SCHEMA_INVALID, drops)

    if len(cleaned) > MAX_OPTION_GROUPS:
        return reject(
            GuardKind.OPTION_RULE,
            FieldStatus.EMPTY_GUARD_OPTION_RULE,
            f"그룹이 {len(cleaned)}개로 상한 {MAX_OPTION_GROUPS}를 넘겨 옵션 제안을 비웠다",
        )

    names = [group.name for group in cleaned]
    if len(set(names)) != len(names):
        return reject(
            GuardKind.OPTION_RULE,
            FieldStatus.EMPTY_GUARD_OPTION_RULE,
            "그룹 이름이 겹쳐 옵션 제안을 비웠다",
        )

    for group in cleaned:
        if len(group.values) > MAX_VALUES_PER_GROUP:
            return reject(
                GuardKind.OPTION_RULE,
                FieldStatus.EMPTY_GUARD_OPTION_RULE,
                f"'{group.name}' 의 값이 {len(group.values)}개로 상한 "
                f"{MAX_VALUES_PER_GROUP}를 넘겨 옵션 제안을 비웠다",
            )

    flat = [value for group in cleaned for value in group.values]
    if len(set(flat)) != len(flat):
        dupes = sorted({value for value in flat if flat.count(value) > 1})
        return reject(
            GuardKind.OPTION_RULE,
            FieldStatus.EMPTY_GUARD_OPTION_RULE,
            f"값 이름 {', '.join(dupes)} 이 그룹을 넘어 겹친다. 등록이 거부되므로 전체를 비웠다",
        )

    too_long = [text for text in (*names, *flat) if len(text) > OPTION_NAME_MAX]
    if too_long:
        return reject(
            GuardKind.LENGTH,
            FieldStatus.EMPTY_GUARD_LENGTH,
            f"이름이 {OPTION_NAME_MAX}자를 넘는 항목이 있어 옵션 제안을 비웠다",
        )

    for text in (*names, *flat):
        verdict = screen_text(text)
        if not verdict.ok:
            assert verdict.status is not None and verdict.kind is not None
            return reject(verdict.kind, verdict.status, f"'{text}': {verdict.detail}")

    combos = build_combinations(cleaned)
    if not combos:
        return reject(
            GuardKind.OPTION_RULE,
            FieldStatus.EMPTY_GUARD_OPTION_RULE,
            "조합을 하나도 만들 수 없어 옵션 제안을 비웠다",
        )
    if len(combos) > MAX_COMBINATIONS:
        return reject(
            GuardKind.OPTION_RULE,
            FieldStatus.EMPTY_GUARD_OPTION_RULE,
            f"데카르트곱이 {len(combos)}개로 상한 {MAX_COMBINATIONS}를 넘겨 옵션 제안을 비웠다",
        )
    return OptionVerdict(cleaned, combos, FieldStatus.FILLED, drops)


def build_combinations(groups: list[OptionGroupDraft]) -> list[OptionCombinationDraft]:
    """조합을 **서버가** 전수 데카르트곱으로 만든다.

    각 조합이 그룹마다 정확히 하나씩 지목하고 같은 값 집합이 두 번 나오지 않는 것이
    데카르트곱의 성질로 보장된다 -- 모델에게 맡기면 반드시 하나는 깬다.
    ``additionalPrice`` 는 0, ``stockQuantity`` 는 비운다(돈과 재고는 채우지 않는다).
    """
    if not groups:
        return []
    pools = [group.values for group in groups]
    return [
        OptionCombinationDraft(optionValues=list(combo), additionalPrice=0, stockQuantity=None)
        for combo in itertools.product(*pools)
    ]


__all__ = [
    "MAX_COMBINATIONS",
    "MAX_OPTION_GROUPS",
    "MAX_VALUES_PER_GROUP",
    "OPTION_NAME_MAX",
    "DescriptionResult",
    "OptionVerdict",
    "TextVerdict",
    "build_combinations",
    "build_description",
    "check_options",
    "screen_text",
]
