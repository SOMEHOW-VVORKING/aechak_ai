"""선별 분기를 고정한다.

여기서 잡는 것은 Haiku의 판단 품질이 아니라 **코드가 선별 응답을 어떻게 쓰는가**다.
건너뜀은 승급률과 누락률을 한꺼번에 움직이는 자리라, 모르는 값이 어느 방향으로 떨어지는지를
테스트가 소유한다.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import FakeLlmClient, failed, ok

from app.inspection.pipeline import InspectionPipeline
from app.inspection.triage import TRIAGE_VERSION
from app.schemas.inspection import (
    CallOutcome,
    Perspective,
    PerspectiveOutcome,
    ProductInspectionRequest,
    Verdict,
)
from app.settings import Settings

CLEAR: dict[str, Any] = {"decision": "CLEAR", "reason": "걸릴 구석이 없다"}
NEEDS_REVIEW: dict[str, Any] = {"decision": "NEEDS_REVIEW", "reason": "효능 표현이 의심된다"}
NO_FINDING: dict[str, Any] = {"findings": []}


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


@pytest.fixture
def triage_on(settings: Settings) -> Settings:
    return settings.model_copy(update={"enable_triage": True})


def triage_ok(tool_input: dict[str, Any], settings: Settings) -> Any:
    return ok(tool_input, model=settings.triage_model)


async def test_선별이_깨끗하면_Sonnet을_건너뛴다(triage_on: Settings) -> None:
    llm = FakeLlmClient([triage_ok(CLEAR, triage_on)])
    pipeline = InspectionPipeline(llm=llm, settings=triage_on)

    verdict = await pipeline.run(product("글루코사민 함유 영양제입니다"))

    assert verdict.verdict is Verdict.PASS
    assert len(llm.calls) == 1, "건너뛴다는 것은 Sonnet 호출을 쏘지 않는다는 뜻이다"
    outcomes = {p.perspective: p.outcome for p in verdict.perspectives}
    assert outcomes[Perspective.TRIAGE] is PerspectiveOutcome.CLEAR
    assert Perspective.DISPLAY_ADS not in outcomes
    assert Perspective.CONDUCT_TRADE not in outcomes


async def test_선별이_더_보라고_하면_Sonnet_두_관점을_탄다(triage_on: Settings) -> None:
    llm = FakeLlmClient([triage_ok(NEEDS_REVIEW, triage_on), ok(NO_FINDING), ok(NO_FINDING)])
    pipeline = InspectionPipeline(llm=llm, settings=triage_on)

    verdict = await pipeline.run(product("글루코사민 함유 영양제입니다"))

    assert verdict.verdict is Verdict.PASS
    assert len(llm.calls) == 3
    perspectives = {p.perspective for p in verdict.perspectives}
    assert Perspective.TRIAGE not in perspectives, (
        "정상 승급이 미검사 신호에 섞이면 레이트 리밋 손상을 그 신호로 잡을 수 없다"
    )
    assert len([c for c in verdict.calls if c.perspective is Perspective.TRIAGE]) == 1, (
        "원장에는 선별 호출이 남아야 승급률을 셀 수 있다"
    )


@pytest.mark.parametrize(
    "outcome",
    [
        CallOutcome.TIMEOUT,
        CallOutcome.RATE_LIMITED,
        CallOutcome.BUDGET_EXHAUSTED,
        CallOutcome.REFUSED,
        CallOutcome.SCHEMA_INVALID,
        CallOutcome.ERROR,
    ],
)
async def test_선별_호출_실패는_전부_승급한다(triage_on: Settings, outcome: CallOutcome) -> None:
    llm = FakeLlmClient([failed(outcome, model=triage_on.triage_model), ok(NO_FINDING), ok(NO_FINDING)])
    pipeline = InspectionPipeline(llm=llm, settings=triage_on)

    verdict = await pipeline.run(product("글루코사민 함유 영양제입니다"))

    assert verdict.verdict is Verdict.PASS
    assert len(llm.calls) == 3, "선별은 재시도하지 않는다. 4회면 재시도 경로를 타고 있다"


@pytest.mark.parametrize(
    "tool_input",
    [
        {"decision": "APPROVE", "reason": "r"},
        {"decision": "", "reason": "r"},
        {"decision": None, "reason": "r"},
        {"reason": "r"},
        {"decision": "clear", "reason": "r"},
    ],
)
async def test_모르는_선별_값은_승급_방향으로_떨어진다(
    triage_on: Settings, tool_input: dict[str, Any]
) -> None:
    llm = FakeLlmClient([triage_ok(tool_input, triage_on), ok(NO_FINDING), ok(NO_FINDING)])
    pipeline = InspectionPipeline(llm=llm, settings=triage_on)

    verdict = await pipeline.run(product("글루코사민 함유 영양제입니다"))

    assert len(llm.calls) == 3, "선별 대상이 곧 적대적 입력이므로 모르는 값은 더 보는 쪽으로 가야 한다"
    assert verdict.verdict is Verdict.PASS


async def test_선별_기록은_집계에서_세지_않는다(triage_on: Settings) -> None:
    # 선별이 승급시킨 뒤 관점 둘이 최초와 재시도까지 네 번 모두 실패한다
    llm = FakeLlmClient([triage_ok(NEEDS_REVIEW, triage_on), failed(), failed(), failed(), failed()])
    pipeline = InspectionPipeline(llm=llm, settings=triage_on)

    verdict = await pipeline.run(product("글루코사민 함유 영양제입니다"))

    assert verdict.verdict is Verdict.FAILED, (
        "선별 기록을 '모델 관점이 봤다'로 세면 아무도 보지 않은 상품에 통과가 나가고, "
        "백엔드가 숨겨 둔 상품의 노출을 되돌린다"
    )


async def test_선별_사용량은_전용_관점으로_적힌다(triage_on: Settings) -> None:
    llm = FakeLlmClient([triage_ok(CLEAR, triage_on)])
    pipeline = InspectionPipeline(llm=llm, settings=triage_on)

    verdict = await pipeline.run(product("글루코사민 함유 영양제입니다"))

    triage_calls = [c for c in verdict.calls if c.modelId == triage_on.triage_model]
    assert triage_calls, "선별 호출이 원장에 없으면 비용과 승급률을 모두 못 센다"
    for call in triage_calls:
        assert call.perspective is Perspective.TRIAGE, (
            "ALL로 적으면 원장에서 비평·수정문구와 한 덩어리가 된다"
        )


async def test_선별이_꺼져_있으면_TRIAGE를_내보내지_않는다(settings: Settings) -> None:
    assert settings.enable_triage is False, "기본값이 켜짐이면 배포 순서가 어긋날 때 판정 전건이 격리된다"
    llm = FakeLlmClient([ok(NO_FINDING), ok(NO_FINDING)])
    pipeline = InspectionPipeline(llm=llm, settings=settings)

    verdict = await pipeline.run(product("글루코사민 함유 영양제입니다"))

    assert len(llm.calls) == 2
    assert all(p.perspective is not Perspective.TRIAGE for p in verdict.perspectives)
    assert all(c.perspective is not Perspective.TRIAGE for c in verdict.calls)


async def test_규칙이_확정한_건은_선별도_부르지_않는다(triage_on: Settings) -> None:
    llm = FakeLlmClient([])
    pipeline = InspectionPipeline(llm=llm, settings=triage_on)

    verdict = await pipeline.run(product("문의는 010-1234-5678로 주세요"))

    assert verdict.verdict is Verdict.FLAG
    assert llm.calls == [], "규칙이 확정한 건에 선별을 부르면 비용이 공짜로 늘어난다"


async def test_선별_호출은_전용_상한과_전용_타임아웃과_전용_모델을_쓴다(triage_on: Settings) -> None:
    llm = FakeLlmClient([triage_ok(CLEAR, triage_on)])
    pipeline = InspectionPipeline(llm=llm, settings=triage_on)

    await pipeline.run(product("글루코사민 함유 영양제입니다"))

    call = llm.calls[0]
    assert call["model"] == triage_on.triage_model
    assert call["max_tokens"] == triage_on.max_tokens_triage
    assert call["timeout_seconds"] == triage_on.triage_timeout_seconds
    assert call["effort"] is None, "Haiku 4.5는 effort를 받으면 400이다"


async def test_선별_호출에는_캐시_지점이_없다(triage_on: Settings) -> None:
    llm = FakeLlmClient([triage_ok(CLEAR, triage_on)])
    pipeline = InspectionPipeline(llm=llm, settings=triage_on)

    await pipeline.run(product("글루코사민 함유 영양제입니다"))

    blocks = llm.calls[0]["system_blocks"]
    assert all("cache_control" not in b for b in blocks), (
        "Haiku 4.5의 최소 캐시 접두부가 4,096토큰이라 붙여도 무시되고, "
        "client.py의 캐시 미적중 로그만 매 호출 울려 진짜 신호를 묻는다"
    )


async def test_선별을_켜면_판정_재사용_키에_선별_버전이_섞인다(triage_on: Settings) -> None:
    llm = FakeLlmClient([triage_ok(CLEAR, triage_on)])
    pipeline = InspectionPipeline(llm=llm, settings=triage_on)

    verdict = await pipeline.run(product("글루코사민 함유 영양제입니다"))

    assert verdict.promptVersion.endswith(f"+{TRIAGE_VERSION}"), (
        "선별을 켜도 키가 그대로면 선별 전 판정이 재사용된다"
    )
    assert len(verdict.promptVersion) <= 40, "백엔드의 prompt_version 컬럼이 VARCHAR(40)이다"
