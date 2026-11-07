"""초안 측정 하네스. **키 없이 도는 부분을 테스트가 소유한다.**

하네스가 이 범위에서 값이 가장 큰 산출물이고, 그 값은 **숫자가 사실이라는 신뢰**에서
나온다. 그래서 자기검사와 비용표 파생을 여기서 고정한다.
"""

from __future__ import annotations

import inspect
import re
import tomllib
from pathlib import Path

import pytest

from app.inspection import rules
from app.schemas.draft import ImageMode
from app.seller_draft import catalog, claims
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


def test_호출_건수가_서비스와_같은_이름_가드를_쓴다(monkeypatch: pytest.MonkeyPatch) -> None:
    """하네스가 ``claims.screen`` 을, 서비스는 ``claims.screen_name`` 을 쓰고 있었다.

    두 함수의 비대칭은 의도된 것이고 테스트가 고정한다(``test_이름_0단이_출력_가드보다_좁다``).
    하네스만 오래된 판정을 들고 있으면 정상 상품명이 0원 분기로 세어져 **예상 호출·비용·
    이미지 도달 분모가 서비스보다 작게** 찍힌다. 현재 ``CASES`` 에는 그런 이름이 없어
    오늘의 리포트 숫자는 바뀌지 않는다 -- 그런 케이스를 한 건 더하는 날을 막는다.
    """
    ordinary = draft_cases.DraftCase(
        "name-gate-ordinary",
        "보장된 내구성 하네스",
        152,
        NameReveals.EXPLICIT,
    )
    assert claims.screen(ordinary.product_name), "출력 가드 사전이 이 이름을 잡는 것이 전제다"
    assert not claims.screen_name(ordinary.product_name), "이름 0단은 이 이름을 통과시킨다"
    monkeypatch.setattr(run_draft, "CASES", (ordinary,))

    assert run_draft.model_called_cases("a") == [ordinary]


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


# --- 픽스처 자기검사: 치수만 보면 플레이스홀더를 못 걸러낸다 -------------------------------


def _distinct_png(width: int, height: int, seed: int) -> bytes:
    """**그림 내용이 실제로 다른** PNG. ``IDAT`` 이 seed 마다 갈린다."""
    import struct
    import zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(
        b"\x00" + bytes([(row * 7 + seed) % 256, (row * 13) % 256, 90]) * width for row in range(4)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 1))
        + chunk(b"IEND", b"")
    )


def _write_fixtures(root: Path, *, distinct: bool, metadata_only: bool = False) -> None:
    """896 픽스처를 만든다.

    - ``distinct`` 거짓: **전건이 같은 한 장**이다.
    - ``metadata_only``: 파일 바이트는 전건 다르지만 **그림 내용이 같다.** 플레이스홀더를
      깔아 두고 자기검사를 통과시키던 우회가 바로 이 모양이다 -- ``synthetic_png`` 가 패딩을
      ``tEXt`` 청크에 담으므로 같은 치수의 합성 PNG 는 ``IHDR``·``IDAT`` 이 전부 같다.
    """
    from demo.draft_cassettes import synthetic_png

    for index, case in enumerate(c for c in CASES if c.image_variant is not None):
        folder = root / case.id
        folder.mkdir(parents=True, exist_ok=True)
        # 치수는 전부 896 으로 맞춘다 -- 자기검사가 보는 유일한 축이 그것이었다.
        if metadata_only:
            data = synthetic_png(896, 896) + b"\x00" * (index + 1)
        elif distinct:
            data = _distinct_png(896, 896, index + 1)
        else:
            data = synthetic_png(896, 896)
        (folder / "896.jpg").write_bytes(data)


def test_같은_사진_한_장을_전건에_깔면_자기검사가_멈춘다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**측정 하네스가 스스로 "통과와 미검사를 섞지 않는다"를 깨는 자리였다.**

    자기검사가 **치수만** 봤다. 그래서 같은 합성 PNG 한 장을 29건에 복사해 두면
    "29/29건 준비됨 / 자기검사 통과" 로 찍히고, 키를 등재한 뒤 (나)·gated 모드를 돌리면
    **같은 무늬 한 장을 29번 보낸 결과가 "이미지 이득"으로 리포트에 올라간다.**

    가장 나쁜 자리는 WRONG 등급이다. "오사진 안전율"의 분모인데 GOOD 과 완전히 같은
    이미지를 받으므로 그 안전율이 무의미해진다. ``harness/draft_images/`` 가 gitignore 라
    안을 감사할 사람도 없다.
    """
    monkeypatch.setattr(run_draft, "IMAGE_ROOT", tmp_path)
    _write_fixtures(tmp_path, distinct=False)

    assert run_draft._self_check() != 0, "같은 사진을 전건에 깔았는데 자기검사가 통과했다"


def test_서로_다른_사진이면_자기검사가_통과한다(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """유일성 검사가 **정상 픽스처를 막지 않는지**도 함께 고정한다."""
    monkeypatch.setattr(run_draft, "IMAGE_ROOT", tmp_path)
    _write_fixtures(tmp_path, distinct=True)

    assert run_draft._self_check() == 0


def test_메타데이터만_다른_사진은_자기검사가_막는다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**앞 수정이 파일 전체 해시였고 그것이 한 청크로 우회됐다.**

    ``demo.draft_cassettes.synthetic_png`` 가 패딩을 ``tEXt`` 청크에 담으므로 같은 치수의
    합성 PNG 는 ``IHDR``·``IDAT`` 이 전부 동일하고 주석만 다르다. 그런 파일 32장을 깔면
    파일 전체 해시가 32종으로 갈려 "32/32건 준비됨 / 자기검사 통과" 가 찍히고 리포트의
    "같은 사진을 복사해 두면 자기검사가 막는다" 가 거짓이 된다. 앞 테스트가 **완전히 동일한
    파일만** 깔았기 때문에 그 사실을 보지 못했다.
    """
    monkeypatch.setattr(run_draft, "IMAGE_ROOT", tmp_path)
    _write_fixtures(tmp_path, distinct=False, metadata_only=True)

    assert run_draft._self_check() != 0, "바이트만 다른 플레이스홀더가 자기검사를 통과했다"
    assert "같은 그림" in capsys.readouterr().out


def test_896이_아닌_등급도_자기검사가_본다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """유일성 검사가 ``variant == 896`` 안에만 있었다. 다른 등급은 아무도 안 봤다."""
    from demo.draft_cassettes import synthetic_png

    monkeypatch.setattr(run_draft, "IMAGE_ROOT", tmp_path)
    _write_fixtures(tmp_path, distinct=True)
    for index, case in enumerate(c for c in CASES if c.image_variant is not None):
        (tmp_path / case.id / "640.jpg").write_bytes(synthetic_png(640, 640) + b"\x00" * (index + 1))

    assert run_draft._self_check() != 0
    assert "640" in capsys.readouterr().out


def test_확장자와_실제_미디어_타입이_어긋나면_자기검사가_말한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``read_dimensions`` 가 돌려준 미디어 타입을 ``_media`` 로 받아 **버렸다.**

    치명은 아니다 -- 하네스가 그 타입으로 보내므로 동작은 한다. 그러나 ``.jpg`` 안에 PNG 가
    들어 있는 것은 **사람이 만든 사진이 아니라는 신호**이고, 자기검사가 그것을 보고 있었는데
    말하지 않았다.
    """
    monkeypatch.setattr(run_draft, "IMAGE_ROOT", tmp_path)
    _write_fixtures(tmp_path, distinct=True)  # 전부 PNG 인데 경로는 .jpg 다

    run_draft._self_check()
    assert "image/png" in capsys.readouterr().out


# --- 오사진 분모: 모델에 닿지 않는 건을 분모로 세지 않는다 --------------------------------


def test_오사진_분모가_모델에_닿는_건만_센다() -> None:
    """**게이트가 스스로 선언한 유일한 출시 숫자가 영구히 판정 불가였다.**

    ``WRONG_PHOTO_MIN_CASES = 6`` 인데 실제 측정 분모는 5다. WRONG 6건 중
    ``opaque-wrong-photo`` 가 OPAQUE + 카테고리 없음이라 ``service.run()`` 의
    ``ITEM_UNIDENTIFIED`` 분기에서 **모델을 0회 부른다** -- a·b·gated 세 모드 전부에서
    ``model_called=False`` 다. 그런데 키 없는 리포트는 "WRONG 6건이 분모다 (최소 6건
    충족)" 이라고 찍었다. **그 줄이 거짓이었다.**
    """
    reachable = run_draft.wrong_photo_reachable_cases()

    assert len(reachable) >= run_draft.WRONG_PHOTO_MIN_CASES, (
        f"모델에 닿는 오사진이 {len(reachable)}건뿐이다. 그 집합으로는 오사진 안전율이 영구히 판정 불가다"
    )
    for case in reachable:
        signal = catalog.resolve(case.product_name, case.category_given)
        assert not case.expect_rule_block
        assert not (signal.grade is ItemGrade.OPAQUE and case.category_given is None)


def test_오사진_리포트가_닿지_않는_건을_분모로_세지_않는다() -> None:
    rows = dict(run_draft._report_images())
    line = rows["사진 등급 분포"]

    assert "모델에 닿는" in line, line
    # 선언 최소를 충족한다고 적을 때는 **닿는 건수**를 근거로 적어야 한다.
    if "충족" in line:
        assert str(len(run_draft.wrong_photo_reachable_cases())) in line, line


def test_이미지_이득_천장이_사진이_모델에_닿는_건수다() -> None:
    """``OPAQUE 19/72`` 는 약 3배 과대였다. 그 층 19건 중 13건은 모델을 0회 부른다."""
    rows = dict(run_draft._report_gate_matrix())
    line = rows["이미지 이득 천장"]
    reachable = run_draft.image_reachable_cases("b")

    assert f"{len(reachable)}" in line, line
    assert len(reachable) < len(by_stratum()[NameReveals.OPAQUE]) + 20


# --- 옵션 그룹 적중: 모델이 아무것도 안 내면 행이 사라졌다 --------------------------------


def _record(**overrides: object) -> run_draft.Record:
    base: dict[str, object] = {
        "mode": "a",
        "repeat": 1,
        "case_id": "c1",
        "name_reveals": "EXPLICIT",
        "category_given": None,
        "image_variant": None,
        "leaf_truth": 111,
        "gate_grade": "PINNED",
        "gate_leaf": 111,
        "predicted_leaf": 111,
        "leaf_correct": True,
        "scorable": True,
        "model_called": True,
        "image_usability": "NO_IMAGE_SENT",
        "option_group_names": [],
        "option_group_hit": None,
        "option_group_scorable": True,
        "field_status": {},
        "guard_kinds": [],
        "basis": {},
        "cost_micro_usd": 100,
        "latency_ms": 10,
        "cache_read_tokens": 0,
    }
    base.update(overrides)
    return run_draft.Record(**base)  # type: ignore[arg-type]


def test_모델이_옵션을_안_내면_미측정으로_적힌다() -> None:
    """**절반만 실패하면 리포트가 거짓이 된다.**

    ``option_group_hit`` 이 ``None`` 이면 분모에서 빠지고 행 자체가 사라졌다. 53건 중
    10건만 옵션을 내고 그 10건이 맞으면 ``10/10(100%)`` 으로 찍히고 **43건이 아무것도
    못 냈다는 사실은 어디에도 안 적힌다.** 레포 불변식("통과와 미검사를 절대 섞지 않는다")을
    측정 쪽에서 그대로 어기는 모양이고, 고친 결함(읽지 못한 리뷰를 "단점 없음"으로)과 같다.
    """
    records = [_record(case_id=f"hit{i}", option_group_hit=True) for i in range(10)]
    records += [_record(case_id=f"none{i}", option_group_hit=None) for i in range(43)]
    rows = dict(run_draft._accuracy_rows("a", records))

    line = rows.get("[a] 옵션 그룹 적중")
    assert line is not None, "모델이 옵션을 낸 건만 있을 때 행이 사라진다"
    assert "10/53" in line, line
    assert "43" in line, f"아무것도 내지 않은 43건이 적히지 않았다: {line}"


def test_옵션_정답이_없는_케이스는_분모에_들어가지_않는다() -> None:
    records = [_record(case_id="x", option_group_scorable=False, option_group_hit=None)]
    rows = dict(run_draft._accuracy_rows("a", records))

    assert "[a] 옵션 그룹 적중" not in rows


# --- 키 없이도 모드가 갈린다 -------------------------------------------------------------


def test_모드별_계획이_네_모드를_갈라_찍는다() -> None:
    """**이 티켓의 핵심 산출물인 비교표가 키 없이는 한 줄도 생산되지 않았다.**

    ``--mode a``/``a-blind``/``b``/``gated`` 네 리포트가 산출물 경로 한 줄만 빼고 **바이트
    동일**했다(diff 확인). 모델 호출 건수·사진이 모델에 닿는 건수·채점 분모·추정 비용은
    전부 키 없이 계산되는데 아무 줄도 그것을 모드별로 보여 주지 않았다.
    """
    rows = dict(run_draft._report_mode_plan(repeat=1, variant=896))
    line = rows["모드별 계획"]

    for mode in run_draft.MODES:
        assert mode in line, line
    # 네 모드의 숫자가 실제로 갈린다.
    assert len(run_draft.image_reachable_cases("gated")) != len(run_draft.image_reachable_cases("b"))


# --- 산출물 ----------------------------------------------------------------------------


def test_산출물에_raw_jsonl이_항상_남고_덮지_않는다(tmp_path: Path) -> None:
    first = run_draft._write_out([], [("키", "1")], "nokey", out_root=tmp_path)
    second = run_draft._write_out([], [("키", "2")], "nokey", out_root=tmp_path)

    assert (first / "raw.jsonl").is_file()
    assert first != second


# --- 키가 없을 때 플래그가 조용히 무동작이 되지 않는다 -----------------------------------


def _args(**overrides: object) -> object:
    import argparse

    defaults: dict[str, object] = {
        "mode": "lexicon-only",
        "model": "haiku",
        "variant": 896,
        "repeat": 1,
        "count_tokens": False,
        "count_image_tokens": False,
        "compare_from": None,
        "only": None,
        "yes": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _run(args: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    import asyncio

    monkeypatch.setattr(run_draft, "OUT_ROOT", tmp_path)
    monkeypatch.setenv("AECHAK_AI_ANTHROPIC_API_KEY", "")
    return asyncio.run(run_draft._main(args))  # type: ignore[arg-type]


def test_키가_없어도_기본_리포트는_종료_코드_0이다(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _run(_args(), tmp_path, monkeypatch) == 0


@pytest.mark.parametrize("flag", ["count_tokens", "count_image_tokens"])
def test_키가_필요한_플래그는_키가_없으면_종료_코드가_0이_아니다(
    flag: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**세 플래그가 조용히 무시됐다.**

    ``_main()`` 의 순서가 (1) 리포트 (2) ``mode == lexicon-only`` 면 return (3) 키 없으면
    return (4) 그 다음에야 플래그 처리였다. 그래서 기본 모드에서 ``--count-tokens`` 를
    주면 2번이 먼저 return 하고 플래그는 **아예 읽히지 않으며** 경고도 없고 종료 코드는
    0 이다. 모듈 독스트링은 그 플래그를 "거의 무료" 로 광고하는데 실제로는 키가 필수다.
    """
    assert _run(_args(mode="a", **{flag: True}), tmp_path, monkeypatch) != 0


@pytest.mark.parametrize("flag", ["count_tokens", "count_image_tokens"])
def test_기본_모드에서도_키가_필요한_플래그는_종료_코드가_0이_아니다(
    flag: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**앞 테스트가 ``mode="a"`` 만 고정해 재현 경로를 한 번도 돌지 않았다.**

    키 blocker 가 ``args.mode != "lexicon-only"`` 에 묶여 있고 ``--mode`` 기본값이
    ``lexicon-only`` 라서, 보고서가 재현 명령으로 적은 ``python -m harness.run_draft
    --count-tokens`` 가 그대로 **바이트 동일한 리포트 + 종료 코드 0** 이었다. 그 리포트가
    사람에게 "``--count-tokens`` 가 치환한다"고 권하는데 그 모드에서 아무것도 하지 않는다.
    """
    code = _run(_args(**{flag: True}), tmp_path, monkeypatch)
    out = capsys.readouterr().out

    assert code != 0, out
    assert "AECHAK_AI_ANTHROPIC_API_KEY" in out, out


def test_없는_비교_파일을_주면_조용히_끝나지_않는다(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--compare-from /nope/does/not/exist.jsonl`` 이 종료 코드 0 으로 조용히 끝났다."""
    assert _run(_args(mode="a", compare_from="/nope/does/not/exist.jsonl"), tmp_path, monkeypatch) != 0


def test_픽스처와_키가_둘_다_없으면_둘_다_말한다(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """**두 선결 조건이 동시에 비어 있는데 사용자는 하나만 들었다.**

    키 없음 게이트가 이미지 픽스처 게이트보다 **먼저** 와서, ``--mode b`` 를 돌리면
    "픽스처가 없다" 가 아니라 "키가 없다" 만 말했다. 픽스처 검사 메시지는 잘 쓰여 있었지만
    키가 생기기 전까지 절대 보이지 않는다.
    """
    monkeypatch.setattr(run_draft, "IMAGE_ROOT", tmp_path / "none")
    code = _run(_args(mode="b"), tmp_path, monkeypatch)
    out = capsys.readouterr().out

    assert code != 0
    assert "픽스처" in out, out
    assert "AECHAK_AI_ANTHROPIC_API_KEY" in out, out


# --- 측정·집계 함수에 회귀 방어를 둔다 ----------------------------------------------------
#
# 고친 지표 행(옵션 그룹 적중·오사진 분모)을 타는 테스트가 **0건**이었다. 하네스 테스트
# 27개 중 ``_measure``·``_summarize``·``_accuracy_rows`` 를 부르는 것이 하나도 없었고,
# 그래서 "리포트에 찍지 않아 없는 숫자였다"는 결함을 고친 뒤에도 그 행을 지우는 변경이
# 아무 테스트도 깨지 않았다.


class _OracleClient:
    """정답을 돌려주는 가짜. **품질을 재는 것이 아니라 배관을 고정한다.**

    이 가짜가 만드는 정확도 숫자는 품질에 관해 아무 뜻이 없다(정답을 알고 답한다).
    재는 것은 요청이 조립되고 가드를 지나고 레코드가 집계되어 **지표 행이 찍히는가**다.
    """

    def __init__(self, *, groups: str = "right") -> None:
        self.calls: list[dict[str, object]] = []
        self._groups = groups

    async def call_tool(self, **kwargs: object) -> object:
        from app.llm.client import LlmResult
        from app.schemas.inspection import CallOutcome

        request_id = str(kwargs["request_id"])
        case_id = request_id.split("-", 2)[-1]
        case = draft_cases.BY_ID[case_id]
        self.calls.append({"request_id": request_id})
        names = sorted(case.acceptable_option_group_names)[:1]
        if self._groups == "empty":
            groups: list[dict[str, object]] = []
        elif self._groups == "half":
            # 케이스 id 의 해시로 절반만 옵션을 낸다. **절반만 실패할 때가 거짓이 나던 모양이다.**
            groups = (
                [{"name": n, "values": [{"name": "A"}, {"name": "B"}]} for n in names]
                if sum(ord(ch) for ch in case_id) % 2 == 0
                else []
            )
        elif self._groups == "wrong":
            groups = [{"name": "엉뚱한그룹", "values": [{"name": "A"}, {"name": "B"}]}]
        else:
            groups = [{"name": n, "values": [{"name": "A"}, {"name": "B"}]} for n in names]
        return LlmResult(
            call_id=request_id,
            outcome=CallOutcome.SUCCESS,
            model_id="claude-haiku-4-5",
            tool_input={
                "leafCategoryId": case.leaf_truth or LEAF_DECLINED,
                "descriptionParts": {
                    "itemSummary": "품목 설명입니다.",
                    "materialOrSpec": "나일론 소재입니다.",
                    "usage": "산책할 때 씁니다.",
                    "careAndStorage": "그늘에 말려 보관합니다.",
                },
                "optionGroups": groups,
                "basis": {"categoryId": "NAME", "description": "NAME", "optionGroups": "NAME"},
                "imageUsability": "NO_IMAGE_SENT",
                "sellerNotice": "",
            },
            input_tokens=2_340,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            output_tokens=450,
            cost_micro_usd=4_590,
            latency_ms=1_200,
        )

    async def aclose(self) -> None:
        return None


async def _measure_a(**client_kwargs: object) -> list[run_draft.Record]:
    client = _OracleClient(**client_kwargs)  # type: ignore[arg-type]
    return await run_draft._measure(
        mode="a", model="haiku", variant=896, repeat=1, llm=client, cases=list(CASES)
    )


async def test_측정이_레코드를_케이스마다_하나씩_만든다() -> None:
    records = await _measure_a()

    assert len(records) == len(CASES)
    assert {r.case_id for r in records} == {case.id for case in CASES}
    # 0원 분기는 모델을 부르지 않는다. 그 수가 ``model_called_cases`` 와 같아야 한다.
    called = [r for r in records if r.model_called]
    assert len(called) == len(run_draft.model_called_cases("a"))


async def test_집계가_옵션_미측정을_숨기지_않는다() -> None:
    """**모델이 옵션을 하나도 내지 않아도 행이 사라지지 않는다.**"""
    records = await _measure_a(groups="empty")
    rows = dict(run_draft._summarize(records, "a"))

    line = rows["[a] 옵션 그룹 적중"]
    assert "미측정" in line, line
    assert line.startswith("미측정 "), line


async def test_분모_전체가_미측정이면_백분율을_찍지_않는다() -> None:
    """**통과와 미검사를 섞지 않는다를 측정 쪽에서 반쯤 되돌린 모양이었다.**

    분모를 "정답이 있는 케이스 전수" 로 바꾼 것은 옳다. 그런데 백분율을 그 분모로 계산해
    모델을 한 번도 부르지 않는 ``lexicon-only`` 리포트에 ``0/66(0%)`` 이 찍혔다. 분모
    전체가 미측정인데 '적중 0%' 라는 수치가 앞에 선다 -- TSV 컬럼만 보는 사람에게 그것은
    '0% 정확도'로 읽힌다. 절반만 미측정이면 ``10/66(15%)`` 가 되어 '하한'처럼 보이는 것도
    같은 문제다.
    """
    rows = dict(run_draft._summarize(await _measure_a(groups="empty"), "a"))

    line = rows["[a] 옵션 그룹 적중"]
    assert "%" not in line, f"분모 전체가 미측정인데 백분율을 찍었다: {line}"
    assert "미측정" in line, line


async def test_일부만_측정되면_두_숫자를_갈라_찍는다() -> None:
    """하나의 분수로 합치면 어느 쪽 숫자인지 알 수 없다."""
    records = await _measure_a(groups="half")
    rows = dict(run_draft._summarize(records, "a"))

    line = rows["[a] 옵션 그룹 적중"]
    # 측정된 건수를 분모로 한 백분율이어야 한다 -- 전수를 분모로 하면 '하한'처럼 읽힌다.
    shape = re.search(r"(\d+)/(\d+) — 측정 (\d+)건 중 (\d+) 적중\((\d+)%\) / \*\*미측정 (\d+)건", line)
    assert shape is not None, line
    hit, total, measured, measured_hit, percent, unmeasured = (int(g) for g in shape.groups())
    assert measured > 0 and unmeasured > 0, line
    assert measured + unmeasured == total, line
    assert hit == measured_hit
    assert percent == round(measured_hit / measured * 100)


async def test_0단_리포트의_옵션_행에_백분율이_없다() -> None:
    """키 없이 뽑는 리포트가 사람이 실제로 읽는 유일한 산출물이다."""
    rows = dict(run_draft._summarize(run_draft._lexicon_only_records(), "lexicon-only"))

    assert "%" not in rows["[lexicon-only] 옵션 그룹 적중"]


async def test_집계가_틀린_옵션을_맞힌_것으로_세지_않는다() -> None:
    right = dict(run_draft._summarize(await _measure_a(), "a"))["[a] 옵션 그룹 적중"]
    wrong = dict(run_draft._summarize(await _measure_a(groups="wrong"), "a"))["[a] 옵션 그룹 적중"]

    assert right != wrong
    assert wrong.startswith("0/"), wrong


async def test_집계가_1순위_지표_행을_전부_찍는다() -> None:
    """지표 행이 사라지는 변경을 테스트가 막는다. summary.tsv 를 읽는 사람의 전부다."""
    rows = dict(run_draft._summarize(await _measure_a(), "a"))

    for key in (
        "[a] 리프 정확도",
        "[a] 층별",
        "[a] 옵션 그룹 적중",
        "[a] 옵션 그룹 적중 층별",
        "[a] 비용",
        "[a] 가드 탈락",
        "[a] 반복 불일치",
    ):
        assert key in rows, key


# --- 다시 쓰기 측정 ---------------------------------------------------------------------
#
# **이 모드만 측정 장치가 없었다.** 레포가 ``draft_image_mode``·``enable_triage`` 에 적용한
# "구현하고 측정하고 기본은 끈다" 에서 다시 쓰기가 빠져 있었다 -- 러너가 ``mode`` 도
# ``lockedFields`` 도 넣지 않는 요청을 만들어 **되돌림률이 한 번도 측정되지 않았다.**


class _RefineClient:
    """다시 쓴 문구를 돌려주는 가짜. ``mode`` 로 세 가지 종료 모양을 만든다.

    품질을 재는 것이 아니라 **되돌림률 행이 실제로 찍히는가**를 고정한다.
    """

    def __init__(self, *, slots: str = "rewritten") -> None:
        self.calls: list[dict[str, object]] = []
        self._slots = slots

    async def call_tool(self, **kwargs: object) -> object:
        from app.llm.client import LlmResult
        from app.schemas.inspection import CallOutcome

        request_id = str(kwargs["request_id"])
        self.calls.append({"request_id": request_id})
        if self._slots == "empty":
            parts = {"itemSummary": "", "materialOrSpec": "", "usage": "", "careAndStorage": ""}
        elif self._slots == "marker":
            parts = {
                "itemSummary": f"{rules.REDACTION_MARKER} 로 문의 주세요.",
                "materialOrSpec": "",
                "usage": "",
                "careAndStorage": "",
            }
        else:
            parts = {
                "itemSummary": "다시 쓴 품목 설명입니다.",
                "materialOrSpec": "나일론 소재입니다.",
                "usage": "",
                "careAndStorage": "",
            }
        case = draft_cases.BY_ID[request_id.split("-", 2)[-1]]
        return LlmResult(
            call_id=request_id,
            outcome=CallOutcome.SUCCESS,
            model_id="claude-haiku-4-5",
            tool_input={
                "leafCategoryId": case.leaf_truth or LEAF_DECLINED,
                "descriptionParts": parts,
                "optionGroups": [],
                "basis": {"categoryId": "NAME", "description": "NAME", "optionGroups": "NONE"},
                "imageUsability": "NO_IMAGE_SENT",
                "sellerNotice": "",
            },
            input_tokens=2_340,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            output_tokens=450,
            cost_micro_usd=4_590,
            latency_ms=1_200,
        )

    async def aclose(self) -> None:
        return None


async def _measure_refine(**client_kwargs: object) -> list[run_draft.Record]:
    client = _RefineClient(**client_kwargs)  # type: ignore[arg-type]
    return await run_draft._measure(
        mode="refine",
        model="haiku",
        variant=896,
        repeat=1,
        llm=client,
        cases=draft_cases.refine_cases(),
    )


def test_다시_쓰기_corpus_가_모델에_닿는_케이스만_가리킨다() -> None:
    """0원 케이스에 원문을 붙이면 그 건은 모델에 닿지 않고 **되돌림률 분모만 부풀린다.**

    자기검사가 같은 것을 막지만, 그 검사가 지워지는 변경을 이 테스트가 막는다.
    """
    reachable = {case.id for case in run_draft.model_called_cases("refine")}

    assert draft_cases.REFINE_DESCRIPTIONS, "corpus 가 비면 되돌림률을 영구히 못 잰다"
    for case_id in draft_cases.REFINE_DESCRIPTIONS:
        assert case_id in draft_cases.BY_ID, case_id
        assert case_id in reachable, f"{case_id}: 0원 케이스에 원문이 붙었다"


def test_다시_쓰기_요청이_원문과_모드를_싣는다() -> None:
    """**러너가 모드도 잠근 값도 넣지 않으면 영원히 FILL 을 측정한다.** 그 상태였다."""
    from app.schemas.draft import DraftMode

    case = draft_cases.BY_ID[next(iter(draft_cases.REFINE_DESCRIPTIONS))]
    refine = run_draft._request_for(case, "refine", 896)
    plain = run_draft._request_for(case, "a", 896)

    assert refine.mode is DraftMode.REFINE
    assert refine.lockedFields is not None
    assert refine.lockedFields.description == draft_cases.REFINE_DESCRIPTIONS[case.id]
    # (가)는 그대로다 -- 변수가 "원문을 다시 쓸 대상으로 주는가" 하나여야 공정하다.
    assert plain.mode is DraftMode.FILL
    assert plain.lockedFields is None
    assert run_draft._settings_for("refine", "haiku", 896) == run_draft._settings_for("a", "haiku", 896)


async def test_되돌림률이_숫자로_찍힌다() -> None:
    """다시 쓴 비율과 되돌린 비율이 **둘 다** 한 줄에 나온다."""
    rewritten = dict(run_draft._summarize(await _measure_refine(), "refine"))
    reverted = dict(run_draft._summarize(await _measure_refine(slots="empty"), "refine"))

    assert "[refine] 다시 쓰기 반영률" in rewritten
    assert "다시 씀 10/10(100%)" in rewritten["[refine] 다시 쓰기 반영률"]
    assert "원문 유지(되돌림) 10(100%)" in reverted["[refine] 다시 쓰기 반영률"]
    # 케이스별 줄이 있어야 "어느 모양의 원문에서 되돌아갔는지" 를 사람이 읽을 수 있다.
    assert "되돌림" in reverted["[refine] 다시 쓰기 케이스별"]
    assert "다시 씀" in rewritten["[refine] 다시 쓰기 케이스별"]


async def test_표식_유출이_0_으로_찍힌다() -> None:
    """**가드가 막는다를 주장이 아니라 측정으로 남긴다.**

    모델이 서버 레닥션 표식을 그대로 옮겨 적어도 초안에 실려서는 안 된다. 이 줄이 0 이
    아니면 정확도와 무관하게 모드를 켜지 않는다.
    """
    rows = dict(run_draft._summarize(await _measure_refine(slots="marker"), "refine"))

    assert rows["[refine] 표식 유출"].startswith("0/10 — 없음")
    # 표식 때문에 떨어졌으므로 되돌림으로 센다.
    assert "원문 유지(되돌림) 10" in rows["[refine] 다시 쓰기 반영률"]


async def test_다시_쓰기_행이_다른_모드에서는_나오지_않는다() -> None:
    """분모가 0 인 모드에 빈 줄을 찍지 않는다 -- ``0/0`` 은 숫자가 아니다."""
    rows = dict(run_draft._summarize(await _measure_a(), "a"))

    assert not any(key.startswith("[a] 다시 쓰기") for key in rows)


async def test_0단_기준선과의_비교가_층별로_나온다() -> None:
    rows = dict(run_draft._compare(run_draft._lexicon_only_records(), await _measure_a(), label="(x)"))

    assert "한계 이득 (x)" in rows
    assert "n=" in rows["한계 이득 (x)"]


def _refine_record(
    case_id: str,
    *,
    description_status: str,
    guard_kinds: list[str],
    description_guard_kinds: list[str],
    model_produced: bool = True,
) -> run_draft.Record:
    """되돌림 집계만 보기 위한 최소 기록. 필드 대부분은 이 집계가 읽지 않는다."""
    return run_draft.Record(
        mode="refine",
        repeat=1,
        case_id=case_id,
        name_reveals="CLEAR",
        category_given=None,
        image_variant=None,
        leaf_truth=None,
        gate_grade="CLEAR",
        gate_leaf=None,
        predicted_leaf=None,
        leaf_correct=False,
        scorable=False,
        model_called=True,
        image_usability="NO_IMAGE_SENT",
        option_group_names=[],
        option_group_hit=None,
        field_status={"description": description_status},
        guard_kinds=guard_kinds,
        basis={},
        cost_micro_usd=1,
        latency_ms=1,
        cache_read_tokens=0,
        refine_locked=True,
        description_guard_kinds=description_guard_kinds,
        model_produced=model_produced,
    )


def test_되돌림_사유는_설명_필드_적중만_센다() -> None:
    """``guardDrops`` 전부로 세면 **옵션 쪽 적중이 되돌림 사유로 찍힌다.**

    되돌림은 설명 한 필드의 사건이다. 옵션 그룹이 가드에 걸린 건은 설명을 되돌린 이유가
    아닌데, 축을 좁히지 않으면 그 적중이 사유 표에 들어가고 거꾸로 **사유 없이 되돌아간
    건이 "사유 있는 되돌림" 으로 세어진다** -- 그 수가 "프롬프트가 다시 쓰기를 못 끌어낸다"
    는 신호라서, 거짓이면 모드를 켤지 판단하는 근거가 사라진다.
    """
    records = [
        # 설명은 사유 없이 되돌아갔고, 적중은 옵션 쪽에만 있다.
        _refine_record(
            next(iter(draft_cases.REFINE_DESCRIPTIONS)),
            description_status="KEPT_LOCKED",
            guard_kinds=["BANNED"],
            description_guard_kinds=[],
        ),
    ]

    rows = dict(run_draft._refine_rows("refine", records))

    reason = rows["[refine] 되돌림 사유"]
    assert "사유 없는 되돌림 1건" in reason, "옵션 적중이 설명 되돌림 사유로 세어졌다"
    assert "BANNED" not in reason.split("/")[0], "옵션 쪽 적중이 사유 표에 들어갔다"


def test_설명_적중은_되돌림_사유로_센다() -> None:
    """위 테스트의 짝. 설명 필드에서 떨어진 건은 사유가 있는 되돌림이다."""
    records = [
        _refine_record(
            next(iter(draft_cases.REFINE_DESCRIPTIONS)),
            description_status="KEPT_LOCKED",
            guard_kinds=["RULE", "BANNED"],
            description_guard_kinds=["RULE"],
        ),
    ]

    rows = dict(run_draft._refine_rows("refine", records))

    reason = rows["[refine] 되돌림 사유"]
    assert "RULE=1" in reason
    assert "사유 없는 되돌림 0건" in reason


def test_호출이_안_된_되돌림을_프롬프트_탓으로_세지_않는다() -> None:
    """0원 분기와 호출 실패도 셀러 원문을 에코하므로 응답만 보면 ``KEPT_LOCKED`` 로 같다.

    뭉개면 **타임아웃이 "프롬프트가 다시 쓰기를 못 끌어낸다" 로 세어진다.** 그 수를 보고
    모드를 켤지 판단하므로 거짓이면 판단이 무너진다.
    """
    case_id = next(iter(draft_cases.REFINE_DESCRIPTIONS))
    records = [
        _refine_record(
            case_id,
            description_status="KEPT_LOCKED",
            guard_kinds=[],
            description_guard_kinds=[],
            model_produced=False,
        ),
    ]

    rows = dict(run_draft._refine_rows("refine", records))

    reason = rows["[refine] 되돌림 사유"]
    assert "사유 없는 되돌림 0건" in reason, "호출 실패가 프롬프트 탓으로 세어졌다"
    assert "호출이 안 된 되돌림 1건" in reason


async def test_되돌림_사유가_실제_응답에서도_한_번만_세어진다() -> None:
    """위 둘은 기록을 손으로 만든다. **그 경로만 보면 중복 집계를 놓친다.**

    되돌림 기록은 ``GuardKind`` 를 유발한 겹에서 빌려 쓰므로, 서비스 응답에는 같은 종류가
    적중과 요약으로 **두 번** 들어 있다. 기록 시점에서 요약을 가리지 않으면 10건에서
    ``RULE=20`` 이 찍힌다. 서비스부터 집계까지 이어 확인한다.
    """
    records = await _measure_refine(slots="marker")

    assert records, "표식 경로가 기록을 만들지 못했다"
    for record in records:
        assert record.description_guard_kinds == ["RULE"], record.description_guard_kinds

    rows = dict(run_draft._summarize(records, "refine"))

    assert f"RULE={len(records)}" in rows["[refine] 되돌림 사유"]
