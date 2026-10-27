"""요약 하네스. **CI 에서 실제 모델을 부르지 않는 부분만 테스트한다.**

하네스 자체는 ``harness/`` 아래 있어 ``testpaths = ["tests"]`` 밖이다. 거기 두지 않으면
CI 가 수집해 유료 호출이 돈다.
"""

from __future__ import annotations

import pytest

from app.settings import Settings
from harness import run_summary
from harness.summary_cases import PRODUCTS, VIEWERS, all_reviews


def test_자기검사가_통과한다() -> None:
    """**측정 전에 멈추는 장치다.** 케이스 라벨이 코드와 어긋나면 종료 코드 1이다.

    짜는 동안 실제로 내 라벨 셋을 잡아냈고 전부 설계 전제를 고쳐야 하는 것이었다 --
    봉투 토큰 본문이 중성화 전에 차단으로 먼저 잡히는 것, 약한 지시형이 레닥션만
    되고 모집단에 남는 것, 400자 절단 케이스가 실은 343자였던 것.
    """
    assert run_summary._self_check() == 0


def test_하네스가_tests_밖에_있다() -> None:
    """안에 두면 pytest 가 수집해 CI 에서 유료 호출이 돈다."""
    from pathlib import Path

    harness_dir = Path(run_summary.__file__).parent
    assert harness_dir.name == "harness"
    assert not (harness_dir / "__init__.py").read_text().startswith("# tests")
    assert Settings.model_config["env_prefix"] == "AECHAK_AI_"


def test_일_예산_상한이_비용표에서_파생된다() -> None:
    """상수로 적어 두면 비용표를 실측으로 갈 때 따라 움직이지 않는다.

    ``harness/run.py`` 의 ``_breakeven_promotion_rate()`` 가 상수로 적히지 않은 이유와
    같다. 그때는 선별 1회 비용이 올라가도 손익분기가 그대로였고 어느 테스트도 깨지지
    않았다.
    """
    assert run_summary.DAILY_NARRATE_CEILING == run_summary._daily_narrate_ceiling()

    original = dict(run_summary._SUMMARY_ESTIMATE_MICRO_USD)
    try:
        run_summary._SUMMARY_ESTIMATE_MICRO_USD["narrate_once"] = 14_600
        # 서술 비용이 두 배가 되면 상한은 절반이어야 한다.
        assert run_summary._daily_narrate_ceiling() == original_ceiling_halved(original)
    finally:
        run_summary._SUMMARY_ESTIMATE_MICRO_USD.clear()
        run_summary._SUMMARY_ESTIMATE_MICRO_USD.update(original)


def original_ceiling_halved(original: dict[str, int]) -> int:
    budget = Settings().summary_daily_budget_usd
    return int(budget * 1_000_000 // (original["narrate_once"] * 2))


def test_추출_건당_비용이_배치_비용에서_파생된다() -> None:
    original = dict(run_summary._SUMMARY_ESTIMATE_MICRO_USD)
    try:
        run_summary._SUMMARY_ESTIMATE_MICRO_USD["extract_batch_10"] = 32_100
        assert run_summary._extract_per_review_micro_usd() == 3_210
    finally:
        run_summary._SUMMARY_ESTIMATE_MICRO_USD.clear()
        run_summary._SUMMARY_ESTIMATE_MICRO_USD.update(original)


def test_요약_접두부가_하한_미달이어도_실패가_아니다() -> None:
    """기본 모델이 Haiku 4.5 라 ``cache_control`` 을 붙여도 **에러 없이 무시된다.**

    하한 미달이 실패가 아니라 **근거**다 -- 그것이 접두부를 짧게 쓰고 패딩하지 않기로
    한 이유다. 패딩은 함정이고 하네스 README 의 숫자가 거부한다: 4,200토큰 패딩은
    따뜻할 때 1.71원이지만 **차가울 때 8.47원으로 기준선보다 비싸다.**
    """
    targets = run_summary.summary_cache_prefix_targets(Settings())

    assert [t.name for t in targets] == ["summary_extract_prefix", "summary_narrate_prefix"]
    assert all(t.required is False for t in targets), "Haiku 기본에서 하한 미달은 실패가 아니다"
    assert all(t.min_tokens == 4_096 for t in targets)


def test_Sonnet으로_뒤집으면_하한이_필수가_된다() -> None:
    """등록해 두면 ``--count-tokens`` 가 하한 미달을 종료 코드로 알린다."""
    sonnet = Settings(summary_extract_model="claude-sonnet-5")
    targets = run_summary.summary_cache_prefix_targets(sonnet)

    extract = next(t for t in targets if t.name == "summary_extract_prefix")
    assert extract.required is True
    assert extract.min_tokens == 1_024


def test_토큰_실측_대상이_실제_접두부다() -> None:
    """``tools`` 가 접두부에 함께 캐시되므로 ``system`` 만 세면 둘 다 재지 않는 셈이다."""
    from app.review_summary.extract_tool import EXTRACT_TOOL
    from app.review_summary.narrate_tool import NARRATE_TOOL
    from app.review_summary.service import extract_system_blocks, narrate_system_blocks

    targets = {t.name: t for t in run_summary.summary_cache_prefix_targets(Settings())}

    assert targets["summary_extract_prefix"].system == extract_system_blocks()
    assert targets["summary_extract_prefix"].tools == [EXTRACT_TOOL]
    assert targets["summary_narrate_prefix"].system == narrate_system_blocks()
    assert targets["summary_narrate_prefix"].tools == [NARRATE_TOOL]


def test_환율_전제가_선별_하네스와_같다() -> None:
    """두 숫자가 갈라지면 두 하네스의 원화 비용으로 같은 이야기를 할 수 없다."""
    from harness.run import KRW_PER_USD

    assert run_summary.KRW_PER_USD == KRW_PER_USD == 1_400


def test_케이스가_필요한_축을_전부_덮는다() -> None:
    """의도적으로 섞어 둔 것이 실제로 들어 있는지 본다. 빠지면 그 축이 측정되지 않는다."""
    reviews = all_reviews()

    assert len(PRODUCTS) >= 4
    assert len(reviews) >= 30
    assert any(r.expect_truncated for r in reviews), "절단 케이스가 없다"
    assert any(r.expect_redacted for r in reviews), "레닥션 케이스가 없다"
    assert any(r.expect_excluded is not None for r in reviews), "제외 케이스가 없다"
    assert any(r.pet is not None for r in reviews), "펫 있는 리뷰가 없다"
    assert any(r.pet is None for r in reviews), "펫 없는 리뷰가 없다"
    assert any(r.rating == 1 for r in reviews), "1점 리뷰가 없다 -- 극성 경계를 못 잰다"
    assert any(r.rating == 5 for r in reviews), "5점 리뷰가 없다"
    assert any(r.rating == 3 for r in reviews), "3점 리뷰가 없다 -- 판정 제외 구간을 못 잰다"
    assert not any(case.expect_narrated for case in PRODUCTS if len(case.reviews) < 10)
    assert any(not case.expect_narrated for case in PRODUCTS), "분모 미달 묶음이 없다"


def test_단점_부재_사유_네_갈래를_케이스가_덮는다() -> None:
    """세 갈래가 서로 다른 화면 문장이 되는 것이 설계의 요점이다.

    케이스 세트가 한 갈래를 못 만들면 그 구분이 코드에만 있고 측정에는 없다. 실제로
    하네스가 "덮지 못한 사유"로 그것을 지적해 케이스를 하나 더했다.
    """
    rows = dict(run_summary._report_thresholds())

    covered = rows["단점 부재 사유"]
    for branch in ("NOT_ENOUGH_REVIEWS", "BELOW_MENTION_THRESHOLD", "NO_NEGATIVE_MENTIONS_FOUND"):
        assert branch in covered, branch
    assert "CONS_PRESENT" in covered
    assert "덮지 못한 사유" not in rows


def test_극성_경계_표가_설계안의_손실을_숫자로_보여준다() -> None:
    """경계를 설계 문서대로 되돌리려는 다음 사람이 주석 대신 이 표를 먼저 보게 한다."""
    rows = dict(run_summary._report_polarity_band())

    assert "SMELL/CON" in rows["극성 경계 손실"], (
        "넓은 경계로 사라지는 반복 단점이 케이스 세트에 없다. 있어야 그 경계가 위험한 것을 보여 준다"
    )
    assert "1점 PRO" in rows["극성 충돌"]
    assert "5점 CON" in rows["극성 충돌"]


def test_티어_리포트가_세그먼트_달성_가능성을_말한다() -> None:
    """**이 범위에서 키 없이 뽑을 수 있는 가장 값비싼 숫자다.**

    백엔드 펫 스냅샷 티켓을 쓸 가치가 있는지가 이 표에서 먼저 드러난다.
    """
    rows = dict(run_summary._report_tiers())

    assert "펫 정보 없는 리뷰" in rows
    assert "세그먼트 임계 달성" in rows
    assert any(key.startswith("티어 분포") for key in rows)
    assert len(VIEWERS) >= 5


def test_게이트_리포트가_접기와_절단을_센다() -> None:
    rows = dict(run_summary._report_gate())

    assert "주입 제외" in rows["입력 게이트"]
    assert "중복 접기" in rows["입력 게이트"]
    assert "절단" in rows["절단과 레닥션"]


def test_추정임을_명시한다() -> None:
    """키가 없어 비용은 전부 단가표 계산이다. 실측으로 읽히게 쓰면 안 된다."""
    rows = dict(run_summary._report_estimate(1))

    assert "추정 건당" in rows
    assert "추정 전체" in rows
    assert "리뷰 수에 무관하다" in rows["추정 건당"]


@pytest.mark.parametrize("case", PRODUCTS, ids=[c.id for c in PRODUCTS])
def test_케이스마다_설명이_붙어_있다(case: object) -> None:
    """라벨만 있고 왜 그 라벨인지가 없으면 다음 사람이 기대값을 고치는 쪽으로 간다."""
    note = getattr(case, "note", "")
    assert len(note) > 10, getattr(case, "id", case)


def test_요약_비용표가_단가_정본에서_파생된다(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_draft`` 쪽과 같은 이유다 -- 표 아래쪽만 파생이고 표 자체가 상수였다."""
    from app.llm import client

    base = run_summary._estimate_micro_usd()
    assert base == run_summary._SUMMARY_ESTIMATE_MICRO_USD

    settings = Settings()
    doubled = dict(client._PRICING)
    for model in {settings.summary_extract_model, settings.summary_narrate_model}:
        doubled[model] = tuple(price * 2 for price in client._PRICING[model])
    monkeypatch.setattr(client, "_PRICING", doubled)

    after = run_summary._estimate_micro_usd()
    assert after["extract_batch_10"] == base["extract_batch_10"] * 2
    assert after["narrate_once"] == base["narrate_once"] * 2


# --- 키가 없을 때 플래그가 조용히 무동작이 되지 않는다 -----------------------------------


def _args(**overrides: object) -> object:
    import argparse

    defaults = {"count_tokens": False, "repeat": 3, "yes": False, "only": None}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_키가_없으면_기본_리포트는_종료_코드_0이다(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    """옵션을 주지 않은 호출은 **키 없이 뽑는 리포트가 산출물**이고 성공이다."""
    import asyncio

    monkeypatch.setattr(run_summary, "OUT_ROOT", tmp_path)
    monkeypatch.setenv("AECHAK_AI_ANTHROPIC_API_KEY", "")

    assert asyncio.run(run_summary._main(_args())) == 0


@pytest.mark.parametrize("flag", ["count_tokens", "yes"])
def test_키가_없으면_키가_필요한_플래그는_종료_코드가_0이_아니다(
    flag: str, tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**이 플래그들은 조용히 무동작이었다.**

    ``_main`` 의 "키가 없으면 리포트를 찍고 return 0" 이 ``--count-tokens`` 분기와 ``--yes``
    분기 **위**에 있어서, 세 호출의 출력이 산출물 경로 줄만 빼고 글자 그대로 같았다
    (diff 로 확인). 사람은 측정을 요청했는데 받은 것은 같은 리포트 한 장이고 종료 코드가
    0 이라 스크립트도 성공으로 읽는다. 요청한 일을 하지 못했으면 그렇게 말해야 한다.
    """
    import asyncio

    monkeypatch.setattr(run_summary, "OUT_ROOT", tmp_path)
    monkeypatch.setenv("AECHAK_AI_ANTHROPIC_API_KEY", "")

    assert asyncio.run(run_summary._main(_args(**{flag: True}))) != 0


def test_산출물에_raw_jsonl이_항상_남는다(tmp_path: object) -> None:
    """``if records:`` 가드 때문에 키 없는 경로에서는 **영구히 안 써졌다.**

    산출물 디렉터리에 ``summary.tsv`` 하나만 있으면, 읽는 사람은 raw 를 잃었는지 애초에
    없었는지 구별할 수 없다. 0줄이라도 파일이 있는 쪽이 사실에 가깝다.
    """
    from pathlib import Path

    out = run_summary._write_out([], [("키", "값")], out_root=Path(str(tmp_path)))

    assert (out / "summary.tsv").is_file()
    assert (out / "raw.jsonl").is_file()
    assert (out / "raw.jsonl").read_text(encoding="utf-8") == ""


def test_같은_초에_두_번_돌아도_산출물이_덮이지_않는다(tmp_path: object) -> None:
    """스탬프가 초 단위이고 ``mkdir(exist_ok=True)`` 라 **조용히 덮었다.**

    동시에 두 번 돌려 확인했다 -- 디렉터리가 2개가 아니라 1개만 늘었다. 측정 결과를
    조용히 잃는 자리라서 스탬프가 겹치면 다른 이름을 쓴다.
    """
    from pathlib import Path

    root = Path(str(tmp_path))
    first = run_summary._write_out([], [("키", "1")], out_root=root)
    second = run_summary._write_out([], [("키", "2")], out_root=root)

    assert first != second
    assert sorted(p.name for p in root.iterdir()) == sorted([first.name, second.name])
