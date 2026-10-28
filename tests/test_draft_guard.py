"""생성후 가드. **0원이고 재생성 0회.** 걸린 것만 비우고 사유를 적는다.

가드가 막는 것은 **셀러가 자기 상품을 차단당하는 일**이다. 초안이 효능 주장이나 금지 성분을
권하고 셀러가 그대로 저장하면 등록 시점 검수가 그 상품을 막는다.
"""

from __future__ import annotations

from app.schemas.draft import FieldStatus, GuardKind, OptionGroupDraft
from app.seller_draft import guard


def _groups(*pairs: tuple[str, list[str]]) -> list[OptionGroupDraft]:
    return [OptionGroupDraft(name=name, values=values) for name, values in pairs]


# --- 텍스트 세 겹 -------------------------------------------------------------------


def test_깨끗한_문구는_통과한다() -> None:
    verdict = guard.screen_text("국내산 고구마를 저온 건조한 수제 간식입니다.")

    assert verdict.ok is True
    assert verdict.status is None


def test_연락처는_0단_규칙_사유로_비운다() -> None:
    verdict = guard.screen_text("구매 문의는 010-1234-5678 로 주세요.")

    assert verdict.status is FieldStatus.EMPTY_GUARD_RULE
    assert verdict.kind is GuardKind.RULE


def test_금지_성분은_별_사유로_비운다() -> None:
    """셀러에게 전할 말이 "연락처를 뺐다"와 "그 성분을 뺐다"로 다르다.

    그리고 하네스가 겹별 탈락률을 재는 축이 이 구분이다.
    """
    verdict = guard.screen_text("고구마 98%, 자일리톨 2% 함유.")

    assert verdict.status is FieldStatus.EMPTY_GUARD_BANNED
    assert verdict.kind is GuardKind.BANNED
    assert "자일리톨" in verdict.detail


def test_효능_단정은_네_번째_겹이_잡는다() -> None:
    """0단이 **하나도 잡지 못하는** 축이다."""
    verdict = guard.screen_text("관절염 치료에 효과가 있습니다.")

    assert verdict.status is FieldStatus.EMPTY_GUARD_CLAIM
    assert verdict.kind is GuardKind.CLAIM


def test_권고_등급_성분은_떨어뜨리지_않는다() -> None:
    """코코넛오일·우유·감귤은 반려동물 상품에 정당하게 쓰인다.

    막으면 사료·간식 리프의 설명이 **거의 항상 빈다.**
    """
    assert guard.screen_text("코코넛오일을 소량 넣었습니다.").ok is True
    assert guard.screen_text("우유 성분이 들어 있습니다.").ok is True


def test_빈_문구는_통과로_센다() -> None:
    """모델이 비운 슬롯은 가드가 떨어뜨린 것이 아니다. 섞으면 사유가 거짓이 된다."""
    assert guard.screen_text("").ok is True
    assert guard.screen_text("   ").ok is True


# --- 설명 슬롯 ---------------------------------------------------------------------

_ORDER = ("itemSummary", "materialOrSpec", "usage", "careAndStorage")


def test_걸린_슬롯만_떨어뜨리고_나머지를_살린다() -> None:
    """설명을 한 덩어리로 받지 않고 슬롯 넷으로 쪼갠 **부수 이득**이다.

    한 덩어리면 "치료" 한 단어가 설명 전체를 지운다.
    """
    result = guard.build_description(
        {
            "itemSummary": "관절염 치료에 효과가 있습니다.",
            "materialOrSpec": "글루코사민 500mg 함유. 60정.",
            "usage": "1일 1정을 사료에 섞어 급여하세요.",
            "careAndStorage": "",
        },
        order=_ORDER,
        max_chars=400,
    )

    assert result.status is FieldStatus.FILLED
    assert result.text == "글루코사민 500mg 함유. 60정.\n\n1일 1정을 사료에 섞어 급여하세요."
    assert "치료" not in (result.text or "")
    assert [drop.kind for drop in result.drops] == [GuardKind.CLAIM]


def test_슬롯이_전부_떨어지면_가드_사유를_쓴다() -> None:
    result = guard.build_description({"itemSummary": "2주 만에 완치됩니다."}, order=_ORDER, max_chars=400)

    assert result.text is None
    assert result.status is FieldStatus.EMPTY_GUARD_CLAIM


def test_슬롯이_하나도_안_오면_스키마_사유다() -> None:
    """가드가 떨어뜨린 것과 모델이 안 낸 것은 **뜻이 다르다.**"""
    result = guard.build_description({}, order=_ORDER, max_chars=400)

    assert result.status is FieldStatus.EMPTY_SCHEMA_INVALID
    assert result.drops == []


def test_길이_초과는_폐기가_아니라_문장_경계_절단이다() -> None:
    """설명은 뒤를 자르는 것이 비우는 것보다 셀러에게 쓸모 있다. **절단은 플래그로 남긴다.**"""
    long_text = "첫 문장입니다. " * 30
    result = guard.build_description({"itemSummary": long_text}, order=_ORDER, max_chars=100)

    assert result.status is FieldStatus.FILLED
    assert len(result.text or "") <= 100
    assert len(result.text or "") < len(long_text)
    assert (result.text or "").endswith("다.")
    assert [drop.kind for drop in result.drops] == [GuardKind.LENGTH]


def test_슬롯을_정해진_순서로_이어_붙인다() -> None:
    result = guard.build_description(
        {
            "careAndStorage": "넷",
            "usage": "셋",
            "materialOrSpec": "둘",
            "itemSummary": "하나",
        },
        order=_ORDER,
        max_chars=400,
    )

    assert result.text == "하나\n\n둘\n\n셋\n\n넷"


# --- 옵션 불변식 -------------------------------------------------------------------


def test_정상_옵션은_조합을_서버가_만든다() -> None:
    """모델에게는 그룹명과 값 이름만 받는다. 조합을 내면 불변식을 반드시 하나는 깬다."""
    verdict = guard.check_options(_groups(("사이즈", ["S", "M"]), ("색상", ["블랙", "레드"])))

    assert verdict.status is FieldStatus.FILLED
    assert len(verdict.combinations) == 4
    assert all(len(combo.optionValues) == 2 for combo in verdict.combinations)
    # 돈과 재고는 채우지 않는다.
    assert all(combo.additionalPrice == 0 for combo in verdict.combinations)
    assert all(combo.stockQuantity is None for combo in verdict.combinations)
    # 같은 값 집합이 두 번 나오지 않는다 -- 데카르트곱의 성질로 보장된다.
    signatures = {tuple(sorted(combo.optionValues)) for combo in verdict.combinations}
    assert len(signatures) == len(verdict.combinations)


def test_그룹을_넘어_값_이름이_겹치면_전체를_비운다() -> None:
    """**모델이 자연스럽게 위반하는 규칙이다**(색상의 "블랙"과 패턴의 "블랙").

    부분 수정을 하지 않는다 -- 셀러가 의도하지 않은 옵션 구성이 만들어지고, 그게 저장되면
    셀러가 왜 그렇게 됐는지 알 수 없다.
    """
    verdict = guard.check_options(_groups(("색상", ["블랙", "베이지"]), ("패턴", ["블랙", "체크"])))

    assert verdict.status is FieldStatus.EMPTY_GUARD_OPTION_RULE
    assert verdict.groups == []
    assert verdict.combinations == []
    assert "블랙" in verdict.drops[0].detail


def test_그룹_이름_중복도_전체를_비운다() -> None:
    verdict = guard.check_options(_groups(("사이즈", ["S"]), ("사이즈", ["M"])))

    assert verdict.status is FieldStatus.EMPTY_GUARD_OPTION_RULE


def test_그룹과_값_상한을_코드로_건다() -> None:
    """``strict`` 모드가 개수 제약을 받지 않아서 **툴 스키마가 아니라 코드로** 건다."""
    too_many_groups = guard.check_options(_groups(("가", ["1"]), ("나", ["2"]), ("다", ["3"])))
    assert too_many_groups.status is FieldStatus.EMPTY_GUARD_OPTION_RULE

    too_many_values = guard.check_options(_groups(("사이즈", [f"v{i}" for i in range(7)])))
    assert too_many_values.status is FieldStatus.EMPTY_GUARD_OPTION_RULE


def test_그룹_값_상한의_곱이_조합_상한과_정확히_같다() -> None:
    """**상한 셋이 맞물려 있다.** 앞의 둘을 통과한 최악 입력이 조합 상한을 정확히 포화시킨다.

    그래서 조합을 만든 뒤 다시 세는 분기가 필요 없다(도달 불가라 지웠다). 그 대신 셋의
    맞물림을 여기서 지킨다 -- ``MAX_VALUES_PER_GROUP`` 을 하나만 올리면 이 테스트가 red 가
    되고, 그것이 사후 분기를 지운 자리를 메우는 방지선이다.
    """
    worst = _groups(
        *(
            (f"그룹{g}", [f"값{g}-{v}" for v in range(guard.MAX_VALUES_PER_GROUP)])
            for g in range(guard.MAX_OPTION_GROUPS)
        )
    )
    verdict = guard.check_options(worst)

    assert guard.MAX_VALUES_PER_GROUP**guard.MAX_OPTION_GROUPS == guard.MAX_COMBINATIONS
    assert verdict.status is FieldStatus.FILLED
    assert len(verdict.combinations) == guard.MAX_COMBINATIONS


def test_이름_길이_상한을_넘기면_길이_사유다() -> None:
    verdict = guard.check_options(_groups(("사이즈", ["x" * (guard.OPTION_NAME_MAX + 1)])))

    assert verdict.status is FieldStatus.EMPTY_GUARD_LENGTH


def test_옵션_이름에_섞인_금지_문구도_전체를_비운다() -> None:
    verdict = guard.check_options(_groups(("문의", ["카톡 petshop99"])))

    assert verdict.status is FieldStatus.EMPTY_GUARD_RULE


def test_빈_옵션은_스키마_사유이고_가드_탈락이_아니다() -> None:
    """모델이 제안하지 않은 것을 "가드가 떨어뜨렸다"로 세면 탈락률이 부풀려진다."""
    verdict = guard.check_options([])

    assert verdict.status is FieldStatus.EMPTY_SCHEMA_INVALID
    assert verdict.drops == []


def test_공백만_든_값은_정리하고_중복은_접는다() -> None:
    verdict = guard.check_options(_groups(("사이즈", [" S ", "S", "", "  ", "M"])))

    assert verdict.groups[0].values == ["S", "M"]


def test_조합이_하나도_안_나오면_비운다() -> None:
    assert guard.build_combinations([]) == []


def test_옵션_상한의_계약_사본이_가드와_같다() -> None:
    """**사본이 표류하면 계약이 받아들인 입력이 데카르트곱을 폭발시킨다.**

    ``app/schemas/draft.py`` 는 ``guard`` 에서 상한을 import 할 수 없다(``guard`` 가 그
    모듈의 ``OptionGroupDraft`` 를 쓰므로 순환이다). 그래서 사본을 두고 여기서 고정한다.
    """
    from app.schemas import draft as draft_schema

    assert draft_schema._LOCKED_MAX_OPTION_GROUPS == guard.LOCKED_MAX_OPTION_GROUPS
    assert draft_schema._LOCKED_MAX_VALUES_PER_GROUP == guard.LOCKED_MAX_VALUES_PER_GROUP
    assert draft_schema._OPTION_NAME_MAX == guard.OPTION_NAME_MAX


# --- 잠근 옵션은 제안 상한이 아니라 등록 불변식으로 본다 ----------------------------------


def test_잠근_옵션은_제안_상한에_걸리지_않는다() -> None:
    """**제안 상한을 잠근 값에 적용하면 셀러의 입력이 사라진다.**

    그룹 2개는 "초안이 얼마나 제안할지"의 수다. 셀러가 셀러센터에서 3그룹을 타이핑했으면
    그것은 등록이 허용하는 값이고(백엔드 DTO 상한은 5), 비울 근거가 없다.
    """
    groups = _groups(("용량", ["5L", "10L"]), ("향", ["무향"]), ("색", ["흰색"]))

    assert guard.check_options(groups).status is FieldStatus.EMPTY_GUARD_OPTION_RULE
    verdict = guard.check_locked_options(groups)
    assert verdict.status is FieldStatus.FILLED
    assert [group.name for group in verdict.groups] == ["용량", "향", "색"]


def test_잠근_옵션도_값_이름_전역_유일을_지켜야_한다() -> None:
    """등록이 실제로 거부되는 조건은 잠근 값에도 적용된다. **같은 함수를 쓴다.**"""
    groups = _groups(("색상", ["블랙", "화이트"]), ("패턴", ["블랙", "스트라이프"]))
    verdict = guard.check_locked_options(groups)

    assert verdict.status is FieldStatus.EMPTY_GUARD_OPTION_RULE
    assert verdict.groups == []
    assert "블랙" in verdict.drops[0].detail


def test_데카르트곱을_만들기_전에_곱으로_거부한다() -> None:
    """**만든 뒤 길이를 보면 늦다.** 거부 판정 전에 메모리가 이미 터진다.

    4그룹 x 20값은 160,000개 객체이고, 백엔드 DTO 상한만큼(5 x 50) 보내면
    312,500,000개라 셀러가 기다리는 동기 엔드포인트가 OOM 으로 죽는다.
    """
    groups = _groups(*((f"g{i}", [f"v{i}_{j}" for j in range(20)]) for i in range(4)))
    verdict = guard.check_locked_options(groups)

    assert verdict.status is FieldStatus.EMPTY_GUARD_OPTION_RULE
    assert verdict.combinations == [], "곱을 만들고 나서 거부했다"
    assert "160000" in verdict.drops[0].detail
    assert str(guard.LOCKED_MAX_COMBINATIONS) in verdict.drops[0].detail
