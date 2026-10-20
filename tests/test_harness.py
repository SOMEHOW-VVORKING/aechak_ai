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
