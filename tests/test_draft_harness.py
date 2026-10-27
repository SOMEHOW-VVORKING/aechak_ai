"""초안 측정 하네스. **키 없이 도는 부분을 테스트가 소유한다.**

하네스가 이 범위에서 값이 가장 큰 산출물이고, 그 값은 **숫자가 사실이라는 신뢰**에서
나온다. 그래서 자기검사와 비용표 파생을 여기서 고정한다.
"""

from __future__ import annotations

import inspect
import tomllib
from pathlib import Path

import pytest

from app.inspection import rules
from app.schemas.draft import ImageMode
from app.seller_draft import catalog
from app.seller_draft.catalog import ItemGrade
from app.seller_draft.tool import DRAFT_TOOL, LEAF_DECLINED
from app.settings import Settings
from harness import draft_cases, run_draft
from harness.draft_cases import CASES, NameReveals, by_stratum, covered_leaves, scorable

REPO = Path(__file__).parent.parent


def test_자기검사가_통과한다() -> None:
    """**측정 전에 멈추는 장치가 지금 녹색인지.**

    틀린 기대값으로 재면 그 숫자는 아무것도 말하지 않는다.
    """
    assert run_draft._self_check() == 0


def test_층_라벨이_실제_게이트_결과와_같다() -> None:
    """**라벨은 사람 직관이고 게이트는 코드다. 둘이 어긋나면 모든 층별 숫자가 거짓이다.**

    카테고리를 빼고 계산한다 -- 그래야 카테고리의 한계 이득을 따로 잴 수 있다.
    """
    expected = {
        NameReveals.EXPLICIT: ItemGrade.PINNED,
        NameReveals.AMBIGUOUS: ItemGrade.NARROWED,
        NameReveals.OPAQUE: ItemGrade.OPAQUE,
    }
    for case in CASES:
        actual = catalog.resolve(case.product_name, None).grade
        assert actual is expected[case.name_reveals], case.id


def test_50_리프를_전부_덮는다() -> None:
    assert covered_leaves() == set(catalog.LEAF_IDS)


def test_세_층에_충분한_건수가_있다() -> None:
    """**평균을 보면 안 된다.** 층을 가르지 않으면 이미지가 값을 내는 구간이 평균에 묻힌다."""
    strata = by_stratum()

    for reveal in NameReveals:
        assert len(strata[reveal]) >= 15, f"{reveal.value} 층이 {len(strata[reveal])}건뿐이다"


def test_카테고리_축이_네_값에_퍼져_있다() -> None:
    """카테고리의 한계 이득을 재려면 **미지정 건이 반드시 있어야 한다.**"""
    kinds = {
        (
            "리프"
            if case.category_given in catalog.LEAF_BY_ID
            else ("없음" if case.category_given is None else "상위")
        )
        for case in CASES
    }

    assert kinds == {"없음", "상위", "리프"}


def test_0단_차단_기대가_실제와_같다() -> None:
    """분모 오염 방지. 모델을 타지 않는 건을 분모에 넣으면 정확도가 희석된다."""
    for case in CASES:
        assert bool(rules.screen(case.product_name).blocking) == case.expect_rule_block, case.id


def test_채점_분모가_차단_건과_리프_주어진_건을_뺀다() -> None:
    """셀러가 리프를 골라 준 건은 **채울 것이 없어서** 분류 정확도를 말할 수 없다."""
    pool = {case.id for case in scorable()}

    for case in CASES:
        if case.expect_rule_block or case.category_given in catalog.LEAF_BY_ID:
            assert case.id not in pool, case.id
        else:
            assert case.id in pool, case.id


def test_고르지_않는_것이_정답인_케이스가_있다() -> None:
    """그 케이스를 빼면 **추측해서 맞히는 쪽이 유리하게 측정된다.**"""
    declines = [case for case in CASES if case.leaf_truth is None]

    assert len(declines) >= 3
    for case in declines:
        assert case.name_reveals is NameReveals.OPAQUE
        assert case.category_given is None, "카테고리를 주면 모델을 불러야 해서 분기가 달라진다"


def test_오사진_케이스가_있다() -> None:
    """``WRONG`` 사진은 **이미지 입장의 핵심 위험 지표**의 분모다."""
    wrong = [case for case in CASES if case.image_variant is draft_cases.ImageVariant.WRONG]

    assert wrong, "오사진 안전율을 잴 케이스가 없다"


def test_툴_enum_이_리프를_전부_덮는다() -> None:
    """하나라도 못 덮으면 모델이 고른 리프가 **조용히 센티넬로 떨어진다.**"""
    enum = DRAFT_TOOL["input_schema"]["properties"]["leafCategoryId"]["enum"]

    assert set(catalog.LEAF_IDS) <= set(enum)
    assert LEAF_DECLINED in enum
    assert len(enum) == len(catalog.LEAVES) + 1


def test_툴_스키마가_요청_데이터로_바뀌지_않는다() -> None:
    """렌더 순서가 ``tools -> system -> messages`` 라 **tools 가 접두부에 함께 캐시된다.**

    요청 데이터가 툴에 들어가면 캐시 엔트리가 입력 수만큼 갈라진다.
    """
    import json

    first = json.dumps(DRAFT_TOOL, sort_keys=True)
    catalog.resolve("강아지 목줄 M", 152)
    assert json.dumps(DRAFT_TOOL, sort_keys=True) == first


def test_0원_기준선이_모델_없이_계산된다() -> None:
    """**키 없이 뽑는 가장 값비싼 숫자다.** 모델이 이걸 못 넘기면 호출은 비용만 더한다."""
    records = run_draft._lexicon_only_records()

    assert len(records) == len(CASES)
    assert all(record.cost_micro_usd == 0 for record in records)
    assert all(record.model_called is False for record in records)
    # EXPLICIT 층은 정의상 게이트가 확정하므로 전건 적중이어야 한다.
    explicit = [r for r in records if r.name_reveals == NameReveals.EXPLICIT.value and r.scorable]
    assert explicit and all(r.leaf_correct for r in explicit)


def test_이미지_비용_비율을_비용표에서_파생한다() -> None:
    """**상수로 적지 않는다.** 적어 두면 비용표를 실측으로 갈 때 따라 움직이지 않는다."""
    table = run_draft._DRAFT_ESTIMATE_MICRO_USD
    expected = table["draft_image_896_haiku"] / table["draft_text_haiku"]

    assert run_draft.IMAGE_COST_RATIO == expected
    source = inspect.getsource(run_draft._image_cost_ratio)
    assert "_DRAFT_ESTIMATE_MICRO_USD" in source, "비율을 리터럴로 적으면 파생이 끊긴다"


def test_일_상한도_비용표에서_파생한다() -> None:
    table = run_draft._DRAFT_ESTIMATE_MICRO_USD
    budget = Settings().draft_daily_budget_usd

    assert run_draft.DAILY_DRAFT_CEILING == int(budget * 1_000_000 // table["draft_text_haiku"])


def test_기계적_중단_규칙이_성립한다() -> None:
    """0단이 공짜인데 모델이 더 못 맞히면 그 호출은 비용만 더한다.

    **비용표 없이도 성립하는 유일한 기계적 규칙이다** -- 이미지 쪽은 이런 규칙을 만들 수
    없고, 만든 척하지 않는다.
    """
    assert run_draft.model_earns_its_keep(0.7, 0.5) is True
    assert run_draft.model_earns_its_keep(0.5, 0.5) is False
    assert run_draft.model_earns_its_keep(0.3, 0.5) is False


def test_모드별_추정_비용이_순서를_지킨다() -> None:
    """``lexicon-only`` 는 0원, ``b`` 가 가장 비싸고 ``gated`` 가 그 사이다."""
    costs = {mode: run_draft._mode_cost(mode, 50, 1) for mode in run_draft.MODES}

    assert costs["lexicon-only"] == 0
    assert costs["a"] == costs["a-blind"]
    assert costs["a"] < costs["gated"] <= costs["b"]


def test_모드가_설정으로만_갈린다() -> None:
    """(가)와 (나)가 **별 코드 경로가 아니다.** 변수가 이미지 블록 하나뿐이어야 공정하다."""
    text = run_draft._settings_for("a", "haiku", 896)
    image = run_draft._settings_for("b", "haiku", 896)
    gated = run_draft._settings_for("gated", "haiku", 896)

    assert text.draft_image_mode == ImageMode.OFF.value
    assert image.draft_image_mode == ImageMode.ALWAYS.value
    assert gated.draft_image_mode == ImageMode.GATED.value
    # 이미지 모드 말고는 같아야 한다.
    ignored = {"draft_image_mode", "draft_image_max_tokens"}
    a = text.model_dump()
    b = image.model_dump()
    assert {k: v for k, v in a.items() if k not in ignored} == {
        k: v for k, v in b.items() if k not in ignored
    }


def test_측정이_재사용_캐시를_끈다() -> None:
    """켜 두면 ``--repeat`` 가 같은 응답을 받아 **반복 불일치율이 항상 0 으로 측정된다.**

    결정성 레버가 API 에 하나도 없는 상태에서 그 숫자가 가장 알고 싶은 것이다.
    """
    for mode in run_draft.MODES:
        assert run_draft._settings_for(mode, "haiku", 896).draft_cache_entries == 0, mode


def test_sonnet_축은_effort_를_싣고_haiku_는_싣지_않는다() -> None:
    """Haiku 4.5 는 ``effort`` 를 받으면 400 이고 그 400 이 ``SCHEMA_INVALID`` 로 숨는다."""
    assert run_draft._settings_for("a", "haiku", 896).draft_effort is None
    assert run_draft._settings_for("a", "sonnet", 896).draft_effort == "low"


def test_접두부_측정_대상이_system_과_tool_둘_다다() -> None:
    """tools 가 접두부에 함께 캐시되므로 **system 만 세면 둘 중 어느 것도 재지 않는 셈**이다."""
    from app.seller_draft.prompt import DRAFT_PREFIX

    targets = run_draft.draft_cache_prefix_targets(Settings())

    assert len(targets) == 1
    target = targets[0]
    assert target.system == [{"type": "text", "text": DRAFT_PREFIX}]
    assert target.tools == [DRAFT_TOOL]
    # Haiku 기본에서는 하한 미달이 **실패가 아니라 근거**다.
    assert target.required is False
    assert target.min_tokens == 4_096


def test_sonnet_으로_뒤집으면_하한을_강제한다() -> None:
    targets = run_draft.draft_cache_prefix_targets(Settings(draft_model="claude-sonnet-5"))

    assert targets[0].required is True
    assert targets[0].min_tokens == 1_024


def test_환율_사본이_하네스와_같다() -> None:
    """콘솔이 보여 주는 비용으로 손익분기를 이야기할 수 있어야 한다."""
    from demo import view
    from harness.run import KRW_PER_USD

    assert run_draft.KRW_PER_USD == KRW_PER_USD == view.KRW_PER_USD


def test_하네스가_tests_밖에_있다() -> None:
    """``testpaths = ["tests"]`` 라서 안에 두면 **CI 에서 실제 모델 호출이 돈다.**"""
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())

    assert pyproject["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]
    assert (REPO / "harness" / "run_draft.py").is_file()
    assert not (REPO / "tests" / "run_draft.py").exists()


def test_이미지_픽스처가_gitignore_되고_생성_스크립트가_커밋됐다() -> None:
    """50 케이스 x 5 등급 JPEG 이 레포를 수십 MB 불린다.

    **없으면 건너뛰고 이유를 적고 종료 코드 0** 이다 -- 조용히 비어 있는 것이 더 나쁘다.
    """
    gitignore = (REPO / ".gitignore").read_text(encoding="utf-8")

    assert "harness/draft_images/" in gitignore
    assert (REPO / "harness" / "make_draft_images.sh").is_file()


def test_하네스가_운영_이미지에_들어가지_않는다() -> None:
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    copied = [line for line in dockerfile.splitlines() if line.strip().startswith("COPY")]

    assert not any("harness" in line for line in copied), copied


def test_비용표가_단가_정본에서_파생된다(monkeypatch: pytest.MonkeyPatch) -> None:
    """**표 아래쪽만 파생이고 표 자체가 상수였다.**

    손계산한 microUSD 를 적어 두면 ``client._PRICING`` 을 고치는 날 모든 비용 줄과 일
    예산 상한이 조용히 과거 값으로 남고 테스트는 전부 초록이다. 그 주석이 "모델 확정 시
    공식 단가로 재확인한다"고 단가 변경을 예고하고 있어 실제로 일어날 일이다.
    """
    from app.llm import client

    base = run_draft._estimate_micro_usd()
    assert base == run_draft._DRAFT_ESTIMATE_MICRO_USD

    model = Settings().draft_model
    doubled = dict(client._PRICING)
    doubled[model] = tuple(price * 2 for price in client._PRICING[model])
    monkeypatch.setattr(client, "_PRICING", doubled)

    after = run_draft._estimate_micro_usd()
    assert after["draft_text_haiku"] == base["draft_text_haiku"] * 2, (
        "단가를 올렸는데 비용표가 따라 움직이지 않았다 -- 표가 상수다"
    )
    assert after["draft_image_896_haiku"] == base["draft_image_896_haiku"] * 2
