"""하네스가 판정 메시지에서 선별 결과를 되살리는 부분을 고정한다.

1순위 지표가 승급률이고, 그 분자·분모를 만드는 것이 이 함수 하나다. 여기가 틀리면 측정이
조용히 뒤집히는데 하네스에는 실제 API 키가 필요해서 사람이 눈으로 잡기 어렵다.

``harness/``는 ``testpaths`` 밖이라 pytest가 수집하지 않지만, 이 함수만은 키 없이 돌 수 있어
여기서 단언한다.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.schemas.inspection import (
    CallOutcome,
    CallUsage,
    ContentVerdict,
    Perspective,
    PerspectiveOutcome,
    PerspectiveResult,
    TargetType,
    Verdict,
)
from app.settings import Settings
from harness.run import _triage_outcome

TRIAGE_MODEL = "claude-haiku-4-5"


def verdict_of(
    perspectives: list[PerspectiveResult],
    calls: list[CallUsage],
) -> ContentVerdict:
    return ContentVerdict(
        inspectionId=1,
        targetType=TargetType.PRODUCT,
        targetRef=42,
        contentHash="a" * 64,
        verdict=Verdict.PASS,
        perspectives=perspectives,
        violations=[],
        calls=calls,
        promptVersion="inspection-v1+policy-v1+triage-v2",
        decidedAt=datetime.now(UTC),
    )


def triage_call(outcome: CallOutcome = CallOutcome.SUCCESS) -> CallUsage:
    return CallUsage(
        requestId="insp-1",
        callId="insp-1-triage",
        perspective=Perspective.TRIAGE,
        modelId=TRIAGE_MODEL,
        outcome=outcome,
    )


def sonnet_call() -> CallUsage:
    return CallUsage(
        requestId="insp-1",
        callId="insp-1-display",
        perspective=Perspective.DISPLAY_ADS,
        modelId="claude-sonnet-5",
    )


def test_건너뛴_건은_CLEAR로_센다() -> None:
    verdict = verdict_of(
        [PerspectiveResult(perspective=Perspective.TRIAGE, outcome=PerspectiveOutcome.CLEAR)],
        [triage_call()],
    )

    assert _triage_outcome(verdict) == ("CLEAR", "SUCCESS")


def test_승급한_건은_TRIAGE_행이_있어도_PROMOTED로_센다() -> None:
    """승급 경로에도 TRIAGE 행이 남는다. 행의 유무로 가르면 승급률이 0%로 뒤집힌다."""
    verdict = verdict_of(
        [
            PerspectiveResult(
                perspective=Perspective.TRIAGE,
                outcome=PerspectiveOutcome.UNCHECKED,
                skipReason="PROMOTED",
            ),
            PerspectiveResult(perspective=Perspective.DISPLAY_ADS, outcome=PerspectiveOutcome.CLEAR),
        ],
        [triage_call(), sonnet_call()],
    )

    assert _triage_outcome(verdict) == ("PROMOTED", "SUCCESS")


def test_선별_호출이_실패한_건은_FAILED로_센다() -> None:
    verdict = verdict_of(
        [
            PerspectiveResult(
                perspective=Perspective.TRIAGE,
                outcome=PerspectiveOutcome.UNCHECKED,
                skipReason="PROMOTED",
            )
        ],
        [triage_call(CallOutcome.RATE_LIMITED), sonnet_call()],
    )

    assert _triage_outcome(verdict) == ("FAILED", "RATE_LIMITED")


def test_상한에_걸려_끊긴_선별_호출은_승급이_아니라_실패다() -> None:
    """모델이 "더 보라"고 판단한 것과 아무 답도 못 한 것을 분자에서 가른다."""
    verdict = verdict_of(
        [
            PerspectiveResult(
                perspective=Perspective.TRIAGE,
                outcome=PerspectiveOutcome.UNCHECKED,
                skipReason="PROMOTED",
            )
        ],
        [triage_call(CallOutcome.TRUNCATED), sonnet_call()],
    )

    assert _triage_outcome(verdict) == ("FAILED", "TRUNCATED")


def test_선별이_꺼진_건은_OFF다() -> None:
    verdict = verdict_of(
        [PerspectiveResult(perspective=Perspective.DISPLAY_ADS, outcome=PerspectiveOutcome.CLEAR)],
        [sonnet_call()],
    )

    assert _triage_outcome(verdict) == ("OFF", None)


def test_토큰_실측_대상이_실제_캐시_접두부다() -> None:
    """``POLICY_PREFIX``만 세면 실제 캐시 접두부 둘 중 어느 것도 재지 않는다.

    캐시 렌더 순서가 ``tools -> system -> messages``라서 마지막 system 블록에 붙은
    ``cache_control``은 tools까지 함께 캐시한다. ``findings_tool``은 관점마다 enum이 달라
    tools가 다르고, 그래서 관점별로 캐시 엔트리가 갈라진다.
    """
    from harness.run import cache_prefix_targets

    targets = {t.name: t for t in cache_prefix_targets(Settings())}

    assert "triage_prompt_on_haiku" in targets
    judged = [t for name, t in targets.items() if name.startswith("perspective_")]
    assert len(judged) == 2, "관점별로 따로 재야 한다. 하나로 뭉치면 갈라진 엔트리를 못 본다"
    for target in judged:
        assert target.tools, "tools를 빼면 접두부를 실제보다 짧게 잰다"
    assert judged[0].tools != judged[1].tools, (
        "관점마다 tools가 같다면 접두부를 공유할 수 있다는 뜻이고, 직렬 실행의 근거가 달라진다"
    )


def test_규칙이_비차단으로_적중해_선별을_안_물은_건은_OFF와_다르다() -> None:
    """승급률 분모를 '규칙이 차단하지 않은 전건'으로 두려면 이 둘을 가려야 한다.

    선별이 꺼진 건(OFF)은 분모 밖이고, 규칙 적중으로 묻지 않고 승급한 건은 분모 안이다
    (파이프라인이 실제로 하는 일이 '묻지 않고 승급'이므로 그것이 사실에 맞는 셈이다).
    둘을 같은 값으로 접으면 full 모드와 triage-only 모드의 분모가 어긋난다.
    """
    verdict = verdict_of(
        [
            PerspectiveResult(
                perspective=Perspective.TRIAGE,
                outcome=PerspectiveOutcome.UNCHECKED,
                skipReason="RULE_HIT",
            )
        ],
        [sonnet_call()],
    )

    assert _triage_outcome(verdict) == ("SKIPPED_RULE_HIT", None)


def test_승급률_분모가_규칙_비차단_적중_건을_승급으로_센다() -> None:
    """분모에서 빼면 '선별이 아무것도 아끼지 못하는 건'의 비중이 지표에서 사라진다.

    코코넛오일·우유·아보카도처럼 반려동물 상품에 흔한 성분 한 단어가 그 상태를 만들 수
    있어서, 실코퍼스에서는 이 비중이 코퍼스보다 훨씬 크다.
    """
    from harness.run import Record, _summarize

    def record(case_id: str, outcome: str) -> Record:
        return Record(
            mode="full",
            repeat=1,
            case_id=case_id,
            label="VIOLATING",
            target=TargetType.PRODUCT.value,
            text_len=20,
            expect_rule_block=False,
            verdict=Verdict.PASS.value,
            triage_outcome=outcome,
            perspectives={},
            cost_micro_usd=0,
            triage_cost_micro_usd=0,
            latency_ms=0,
            haiku_cache_read=0,
            triage_call_outcome=None,
        )

    rows = dict(
        _summarize(
            [record("a", "CLEAR"), record("b", "SKIPPED_RULE_HIT")],
            baseline={},
        )
    )

    assert "2" in rows["승급률 전체"], f"분모가 2건이어야 한다: {rows['승급률 전체']}"
    assert "50.0%" in rows["승급률 전체"], rows["승급률 전체"]


def test_끊긴_선별_호출을_실패_안에_묻지_않는다() -> None:
    """상한을 올려야 하는 신호와 모델이 더 보라고 한 신호가 같은 칸에 있으면 안 된다."""
    from harness.run import Record, _summarize

    truncated = Record(
        mode="full",
        repeat=1,
        case_id="t",
        label="CLEAN",
        target=TargetType.PRODUCT.value,
        text_len=20,
        expect_rule_block=False,
        verdict=Verdict.PASS.value,
        triage_outcome="FAILED",
        perspectives={},
        cost_micro_usd=0,
        triage_cost_micro_usd=0,
        latency_ms=0,
        haiku_cache_read=0,
        triage_call_outcome=CallOutcome.TRUNCATED.value,
    )

    rows = dict(_summarize([truncated], baseline={}))

    assert "선별 상한 초과" in rows, f"TRUNCATED 를 따로 세지 않는다: {sorted(rows)}"
    assert "1" in rows["선별 상한 초과"]


def test_하네스가_운영이_보내지_않는_입력을_선별에_태우지_않는다() -> None:
    """triage-only 모드가 ``hits`` 를 건너뛰어야 full 모드와 분모가 같아진다."""
    import inspect

    from harness import run

    source = inspect.getsource(run._run_triage_only)
    assert "rule_result.triage_trumping" in source, (
        "파이프라인이 선별을 부르는 조건과 같아야 두 모드의 분모가 같다"
    )


def test_코퍼스에_선별이_못_아끼는_건이_들어있다() -> None:
    """advisory 성분 적중은 선별을 건너뛰게 하므로 표에 그 비중이 드러나야 한다."""
    from app.inspection import rules
    from harness.cases import CASES

    skipped = [
        c
        for c in CASES
        if not c.expect_rule_block and rules.screen(c.text).hits and not rules.screen(c.text).blocking
    ]
    advisory = [
        c for c in skipped if any(h.category.value == "BANNED_INGREDIENT" for h in rules.screen(c.text).hits)
    ]
    assert len(advisory) >= 2, f"advisory 성분 케이스가 모자라다: {[c.id for c in skipped]}"


def test_케이스의_차단_기대가_실제_규칙과_맞는다() -> None:
    """``expect_rule_block`` 이 틀어지면 승급률 분모가 조용히 어긋난다."""
    from app.inspection import rules
    from harness.cases import CASES

    mismatched = [c.id for c in CASES if bool(rules.screen(c.text).blocking) != c.expect_rule_block]
    assert mismatched == [], mismatched


def test_손익분기가_비용표에서_파생된다() -> None:
    """비용표를 실측으로 갈아 끼우면 손익분기도 따라 움직여야 한다.

    둘이 따로 적혀 있던 동안 선별 1회 비용이 올라가도 손익분기는 그대로였고, 어느 테스트도
    깨지지 않았다. 백엔드가 이 숫자의 사본을 들고 선별 on/off 판단에 쓰므로 어긋나면 양쪽이
    반대 결론을 낸다.
    """
    from harness import run

    assert run.BREAKEVEN_PROMOTION_RATE == run._breakeven_promotion_rate()

    original = dict(run._ESTIMATE_MICRO_USD)
    try:
        run._ESTIMATE_MICRO_USD["triage"] = 2_000
        # 1 - 2000/3680 = 0.4565... 를 셋째 자리로 올린다(적어 둔 0.620과 같은 규약)
        assert run._breakeven_promotion_rate() == 0.457
    finally:
        run._ESTIMATE_MICRO_USD.clear()
        run._ESTIMATE_MICRO_USD.update(original)


def test_같은_초에_두_번_돌아도_산출물이_덮이지_않는다(tmp_path: object, monkeypatch: object) -> None:
    """스탬프가 초 단위이고 ``mkdir(exist_ok=True)`` 라 **조용히 덮었다.**

    형제 둘(``run_summary``·``run_draft``)이 독스트링에 적어 둔 결함이 검수 하네스에만
    남아 있었다. 측정 결과를 조용히 잃는 자리라서 스탬프가 겹치면 다른 이름을 쓴다.
    """
    from pathlib import Path

    from harness import run

    root = Path(str(tmp_path))
    monkeypatch.setattr(run, "OUT_ROOT", root)  # type: ignore[attr-defined]
    first = run._write_out([], [("키", "1")])
    second = run._write_out([], [("키", "2")])

    assert first != second
    assert sorted(p.name for p in root.iterdir()) == sorted([first.name, second.name])
    assert (first / "summary.tsv").read_text(encoding="utf-8").startswith("키\t1")
