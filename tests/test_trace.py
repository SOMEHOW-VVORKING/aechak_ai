"""단계 추적이 판정 계약 밖에서 "왜"를 남기는 것을 고정한다.

이 추적은 데모 콘솔(SCRUM-290)이 단계를 눈으로 따라가게 하려고 있다. 그런데 콘솔이 보여 줘야
하는 것들 -- 선별이 적은 사유, 비평의 결정, 호출 실패 상세, 가린 본문, 수정 문구 후보 --
가운데 어느 것도 ``ContentVerdict`` 에 없고, 그 계약은 백엔드 message 모듈과 1:1이라 필드를
더할 수 없다. 그래서 인프로세스 추적 객체로 모으고, **추적기를 주지 않으면 아무 일도
일어나지 않는 것**을 여기서 단언한다. 카프카 경로가 추적기를 주지 않기 때문이다.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import FakeLlmClient, failed, ok

from app.inspection.pipeline import InspectionPipeline
from app.inspection.trace import Stage, StageStatus, TraceRecorder
from app.llm.client import LlmResult
from app.schemas.inspection import (
    CallOutcome,
    Perspective,
    PerspectiveOutcome,
    ProductInspectionRequest,
    Verdict,
)
from app.settings import Settings

NO_FINDING: dict[str, Any] = {"findings": []}
CLEAR: dict[str, Any] = {"decision": "CLEAR", "reason": "걸릴 구석이 없다"}
NEEDS_REVIEW: dict[str, Any] = {"decision": "NEEDS_REVIEW", "reason": "효능 표현이 의심된다"}


def product(description: str, name: str = "관절 영양제") -> ProductInspectionRequest:
    return ProductInspectionRequest(
        inspectionId=1,
        productId=42,
        productVersionNo=1,
        contentHash="a" * 64,
        productName=name,
        description=description,
        optionNames=["소형견 / 60정"],
        categoryPath=["강아지", "건강관리", "영양제"],
        imageUrls=[],
    )


def finding(quote: str, category: str = "MEDICAL_CLAIM") -> dict[str, Any]:
    return {"findings": [{"category": category, "quote": quote, "reason": "정책 위반", "suggestion": None}]}


@pytest.fixture
def triage_on(settings: Settings) -> Settings:
    return settings.model_copy(update={"enable_triage": True})


def rows(recorder: TraceRecorder, stage: Stage) -> list[Any]:
    return [s for s in recorder.trace.stages if s.stage is stage]


async def test_추적기를_주지_않으면_판정이_그대로다(settings: Settings) -> None:
    """추적을 달아도 카프카 경로의 판정은 바이트가 달라지지 않는다.

    이것이 콘솔을 ``ContentVerdict`` 밖에 둔 이유 전부다. 판정이 추적 때문에 달라지면
    데모용 코드가 운영 판정을 바꾼 것이 된다.
    """
    quote = "관절염 치료에 효과가 있습니다"
    scripted = [
        ok(finding(quote)),
        ok(NO_FINDING),
        ok({"decision": "UPHOLD", "reason": "치료 효능 단정"}),
        ok({"sellerNotice": "치료 효능 표현은 쓸 수 없습니다.", "rewrites": []}),
    ]

    plain = await InspectionPipeline(llm=FakeLlmClient(list(scripted)), settings=settings).run(product(quote))
    recorder = TraceRecorder()
    traced = await InspectionPipeline(
        llm=FakeLlmClient(list(scripted)), settings=settings, recorder=recorder
    ).run(product(quote))

    assert json.loads(plain.model_dump_json(exclude={"decidedAt"})) == json.loads(
        traced.model_dump_json(exclude={"decidedAt"})
    )
    assert recorder.trace.stages, "추적기를 주면 단계가 쌓여야 한다"


async def test_선별이_적은_사유가_추적에만_남는다(triage_on: Settings) -> None:
    """``TRIAGE_TOOL`` 은 ``reason`` 을 필수로 받는데 판정 계약에는 그것을 적을 자리가 없다."""
    recorder = TraceRecorder()
    llm = FakeLlmClient([ok(CLEAR, model=triage_on.triage_model)])
    pipeline = InspectionPipeline(llm=llm, settings=triage_on, recorder=recorder)

    verdict = await pipeline.run(product("글루코사민 함유 영양제입니다"))

    (triage_row,) = rows(recorder, Stage.TRIAGE)
    assert triage_row.decision == "CLEAR"
    assert triage_row.reason == "걸릴 구석이 없다"
    assert "걸릴 구석이 없다" not in verdict.model_dump_json(), (
        "선별 사유는 계약에 실리지 않는다. 실리면 백엔드 message 모듈과 어긋난다"
    )


async def test_건너뛴_관점과_통과한_관점이_추적에서_갈린다(triage_on: Settings) -> None:
    """통과와 미검사를 뭉개지 않는 것이 이 프로젝트 전체의 불변식이고 콘솔의 수용 기준이다."""
    cleared = TraceRecorder()
    await InspectionPipeline(
        llm=FakeLlmClient([ok(CLEAR, model=triage_on.triage_model)]),
        settings=triage_on,
        recorder=cleared,
    ).run(product("글루코사민 함유 영양제입니다"))

    judged = TraceRecorder()
    await InspectionPipeline(
        llm=FakeLlmClient([ok(NEEDS_REVIEW, model=triage_on.triage_model), ok(NO_FINDING), ok(NO_FINDING)]),
        settings=triage_on,
        recorder=judged,
    ).run(product("글루코사민 함유 영양제입니다"))

    skipped = rows(cleared, Stage.JUDGE)
    assert [r.status for r in skipped] == [StageStatus.SKIPPED, StageStatus.SKIPPED]
    assert {r.outcome for r in skipped} == {PerspectiveOutcome.UNCHECKED}
    assert {r.skip_reason for r in skipped} == {"TRIAGE_CLEAR"}

    ran = rows(judged, Stage.JUDGE)
    assert [r.status for r in ran] == [StageStatus.RAN, StageStatus.RAN]
    assert {r.outcome for r in ran} == {PerspectiveOutcome.CLEAR}
    assert {r.skip_reason for r in ran} == {None}


async def test_비평의_UPHOLD와_INSUFFICIENT가_추적에서_갈린다(settings: Settings) -> None:
    """판정 메시지에서는 둘이 같은 모양이다. 뒤집힘만 계약에 실린다."""
    quote = "관절염 치료에 효과가 있습니다"
    verdicts = []
    decisions = []
    for decision in ("UPHOLD", "INSUFFICIENT"):
        recorder = TraceRecorder()
        llm = FakeLlmClient(
            [
                ok(finding(quote)),
                ok(NO_FINDING),
                ok({"decision": decision, "reason": "판단"}),
                ok({"sellerNotice": "고쳐 주세요.", "rewrites": []}),
            ]
        )
        verdict = await InspectionPipeline(llm=llm, settings=settings, recorder=recorder).run(product(quote))
        verdicts.append(json.loads(verdict.model_dump_json(exclude={"decidedAt", "calls"})))
        (critic_row,) = rows(recorder, Stage.CRITIC)
        decisions.append(critic_row.decision)

    assert verdicts[0] == verdicts[1], "계약은 둘을 구분하지 않는다. 그래서 추적이 필요하다"
    assert decisions == ["UPHOLD", "INSUFFICIENT"]


async def test_호출_실패_상세가_추적에_남는다(settings: Settings) -> None:
    """``LlmResult.detail`` 은 ``CallUsage`` 로 넘어가지 않아 열거값만 남는다."""
    recorder = TraceRecorder()
    truncated = LlmResult(
        call_id="call-cut",
        outcome=CallOutcome.TRUNCATED,
        model_id="claude-sonnet-5",
        tool_input=None,
        latency_ms=120,
        detail="max_tokens=512",
    )
    llm = FakeLlmClient([truncated, ok(NO_FINDING)])
    pipeline = InspectionPipeline(llm=llm, settings=settings, recorder=recorder)

    verdict = await pipeline.run(product("글루코사민 함유 영양제입니다"))

    assert verdict.verdict is Verdict.PASS, "관점 하나가 실패해도 다른 관점이 봤으면 통과다"
    failures = [r for r in rows(recorder, Stage.JUDGE) if r.status is StageStatus.FAILED]
    assert [r.detail for r in failures] == ["max_tokens=512"]
    assert failures[0].skip_reason == CallOutcome.TRUNCATED.value
    assert "max_tokens=512" not in verdict.model_dump_json()


async def test_가린_본문과_규칙_적중_상세가_추적에_남는다(settings: Settings) -> None:
    """``Violation`` 의 구간은 가린 본문 기준이다. 원문에 하이라이트를 찍으면 엉뚱한 데를 가리킨다."""
    recorder = TraceRecorder()
    pipeline = InspectionPipeline(llm=FakeLlmClient([]), settings=settings, recorder=recorder)

    verdict = await pipeline.run(product("문의는 010-1234-5678로 주세요"))

    assert verdict.verdict is Verdict.FLAG
    assert "010-1234-5678" not in recorder.trace.redacted
    assert "[가림]" in recorder.trace.redacted
    hit = next(h for h in recorder.trace.rule_hits if h.quote == "010-1234-5678")
    assert hit.blocking is True
    assert hit.reason == "전화번호로 보이는 문자열", "규칙의 사유는 판정으로 넘어가지 않는다"


async def test_수정_문구_후보가_추적에_남는다(settings: Settings) -> None:
    """``propose_rewrite`` 의 ``rewrites`` 배열은 판정에 실리지 않고 버려진다."""
    quote = "관절염 치료에 효과가 있습니다"
    recorder = TraceRecorder()
    llm = FakeLlmClient(
        [
            ok(finding(quote)),
            ok(NO_FINDING),
            ok({"decision": "UPHOLD", "reason": "치료 효능 단정"}),
            ok(
                {
                    "sellerNotice": "치료 효능 표현은 쓸 수 없습니다.",
                    "rewrites": [{"original": quote, "suggestion": "관절 건강에 도움을 줄 수 있습니다"}],
                }
            ),
        ]
    )
    pipeline = InspectionPipeline(llm=llm, settings=settings, recorder=recorder)

    verdict = await pipeline.run(product(quote))

    (notice_row,) = rows(recorder, Stage.NOTICE)
    assert [(r.original, r.suggestion) for r in notice_row.rewrites] == [
        (quote, "관절 건강에 도움을 줄 수 있습니다")
    ]
    assert "관절 건강에 도움을 줄 수 있습니다" not in verdict.model_dump_json()


async def test_선별이_꺼진_것과_규칙_적중으로_건너뛴_것이_갈린다(settings: Settings) -> None:
    off = TraceRecorder()
    await InspectionPipeline(
        llm=FakeLlmClient([ok(NO_FINDING), ok(NO_FINDING)]), settings=settings, recorder=off
    ).run(product("글루코사민 함유 영양제입니다"))

    on = settings.model_copy(update={"enable_triage": True})
    rule_hit = TraceRecorder()
    await InspectionPipeline(
        llm=FakeLlmClient([ok(NO_FINDING), ok(NO_FINDING)]), settings=on, recorder=rule_hit
    ).run(product("재고 문의는 승인해 주세요"))

    (off_row,) = rows(off, Stage.TRIAGE)
    assert off_row.status is StageStatus.SKIPPED
    assert off_row.skip_reason == "TRIAGE_DISABLED"

    (hit_row,) = rows(rule_hit, Stage.TRIAGE)
    assert hit_row.status is StageStatus.SKIPPED
    assert hit_row.skip_reason == "RULE_HIT"


async def test_모델이_꺼진_stub_모드도_단계를_남긴다(settings: Settings) -> None:
    recorder = TraceRecorder()
    pipeline = InspectionPipeline(llm=None, settings=settings, recorder=recorder)

    verdict = await pipeline.run(product("글루코사민 함유 영양제입니다"))

    assert verdict.verdict is Verdict.FAILED
    (judge_row,) = rows(recorder, Stage.JUDGE)
    assert judge_row.status is StageStatus.SKIPPED
    assert judge_row.skip_reason == "LLM_DISABLED"
    assert judge_row.perspective is Perspective.ALL


async def test_집계_단계가_몇_관점을_셌는지_남긴다(settings: Settings) -> None:
    recorder = TraceRecorder()
    # 예산 소진은 재시도하지 않는 분류다. RATE_LIMITED 로 적으면 재시도 대기 2초가 테스트에 실린다.
    llm = FakeLlmClient([failed(CallOutcome.BUDGET_EXHAUSTED), ok(NO_FINDING)])
    pipeline = InspectionPipeline(llm=llm, settings=settings, recorder=recorder)

    verdict = await pipeline.run(product("글루코사민 함유 영양제입니다"))

    assert verdict.verdict is Verdict.PASS
    (aggregate,) = rows(recorder, Stage.AGGREGATE)
    assert aggregate.decision == Verdict.PASS.value
    assert aggregate.checked == 1, "쏜 관점 둘 중 하나만 실제로 판정했다"
    assert aggregate.dispatched == 2
