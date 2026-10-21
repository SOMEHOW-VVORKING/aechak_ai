"""선별 분기를 고정한다.

여기서 잡는 것은 Haiku의 판단 품질이 아니라 **코드가 선별 응답을 어떻게 쓰는가**다.
건너뜀은 승급률과 누락률을 한꺼번에 움직이는 자리라, 모르는 값이 어느 방향으로 떨어지는지를
테스트가 소유한다.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import FakeLlmClient, failed, ok

from app.inspection import banned, triage
from app.inspection.perspectives import specs_for
from app.inspection.pipeline import InspectionPipeline, countable_perspectives
from app.inspection.triage import TRIAGE_PROMPT, TRIAGE_VERSION
from app.schemas.inspection import (
    CallOutcome,
    Perspective,
    PerspectiveOutcome,
    PerspectiveResult,
    ProductInspectionRequest,
    TargetType,
    Verdict,
    ViolationCategory,
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
    rows = {p.perspective: p for p in verdict.perspectives}
    assert rows[Perspective.TRIAGE].outcome is PerspectiveOutcome.CLEAR

    # 건너뛴 관점도 행을 남기고, 왜 안 봤는지를 사유로 적는다.
    #
    # 행을 생략하면 백엔드는 "선별이 일부러 안 본 것"과 "호출이 죽어서 못 본 것"을 구분할
    # 수 없다. 둘 다 모델 판정이 없는 상태로 보이기 때문이다. 그러면 백엔드가 통과를
    # 받아들이지 못해 건너뛴 건을 전건 사람에게 넘기고, 선별이 비용만 더하는 장치가 된다.
    for perspective in (Perspective.DISPLAY_ADS, Perspective.CONDUCT_TRADE):
        assert rows[perspective].outcome is PerspectiveOutcome.UNCHECKED
        assert rows[perspective].skipReason == triage.TRIAGE_SKIP_REASON


async def test_선별이_더_보라고_하면_Sonnet_두_관점을_탄다(triage_on: Settings) -> None:
    llm = FakeLlmClient([triage_ok(NEEDS_REVIEW, triage_on), ok(NO_FINDING), ok(NO_FINDING)])
    pipeline = InspectionPipeline(llm=llm, settings=triage_on)

    verdict = await pipeline.run(product("글루코사민 함유 영양제입니다"))

    assert verdict.verdict is Verdict.PASS
    assert len(llm.calls) == 3
    rows = {p.perspective: p for p in verdict.perspectives}
    assert rows[Perspective.TRIAGE].outcome is PerspectiveOutcome.UNCHECKED, (
        "승급은 판정을 내린 것이 아니라 더 보라고 넘긴 것이라 CLEAR로 적으면 거짓이 된다"
    )
    assert rows[Perspective.TRIAGE].skipReason == "PROMOTED", (
        "미검사 사유가 없으면 레이트 리밋 손상과 정상 승급을 가를 수 없다"
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

    # 불변식 둘이 동시에 성립해야 한다. 승급 행을 남기는 것과, 그 행을 "봤다"로 세지 않는 것.
    rows = {p.perspective: p for p in verdict.perspectives}
    assert rows[Perspective.TRIAGE].outcome is PerspectiveOutcome.UNCHECKED
    assert rows[Perspective.TRIAGE].skipReason == "PROMOTED"
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


def test_집계는_선별_관점을_결과와_무관하게_뺀다() -> None:
    """선별 행이 CLEAR든 UNCHECKED든 "모델 관점이 봤다"로 세지 않는다.

    파이프라인은 TRIAGE CLEAR와 Sonnet 호출이 함께 있는 상태를 만들 수 없지만, 집계가
    제외 목록으로 되돌아가면 바로 그 상태에서 PASS가 샌다. 그래서 집계 함수를 직접 고정한다.
    """
    dispatched = {Perspective.DISPLAY_ADS, Perspective.CONDUCT_TRADE}
    rows = [
        PerspectiveResult(perspective=Perspective.RULE, outcome=PerspectiveOutcome.CLEAR),
        PerspectiveResult(perspective=Perspective.TRIAGE, outcome=PerspectiveOutcome.CLEAR),
        PerspectiveResult(
            perspective=Perspective.TRIAGE,
            outcome=PerspectiveOutcome.UNCHECKED,
            skipReason="PROMOTED",
        ),
        PerspectiveResult(
            perspective=Perspective.DISPLAY_ADS,
            outcome=PerspectiveOutcome.UNCHECKED,
            skipReason="RATE_LIMITED",
        ),
        PerspectiveResult(
            perspective=Perspective.CONDUCT_TRADE,
            outcome=PerspectiveOutcome.UNCHECKED,
            skipReason="RATE_LIMITED",
        ),
    ]

    assert countable_perspectives(rows, dispatched) == []


async def test_상한에_걸려_끊긴_선별_응답은_승급하고_실패로_적힌다(triage_on: Settings) -> None:
    """``max_tokens``로 끊긴 응답을 SUCCESS로 적으면 승급률 분자가 오염된다.

    모델이 "더 보라"고 판단한 것과 상한에 걸려 아무 답도 못 한 것이 같은 값이 된다.
    1순위 지표가 그 숫자다.
    """
    truncated = failed(CallOutcome.TRUNCATED, model=triage_on.triage_model)
    llm = FakeLlmClient([truncated, ok(NO_FINDING), ok(NO_FINDING)])
    pipeline = InspectionPipeline(llm=llm, settings=triage_on)

    verdict = await pipeline.run(product("글루코사민 함유 영양제입니다"))

    assert len(llm.calls) == 3, "끊긴 응답은 판단이 아니므로 승급해야 한다"
    triage_calls = [c for c in verdict.calls if c.perspective is Perspective.TRIAGE]
    assert triage_calls[0].outcome is CallOutcome.TRUNCATED, (
        "SUCCESS로 적으면 하네스가 이 건을 FAILED가 아니라 PROMOTED로 센다"
    )


def test_선별_프롬프트는_하류가_판정할_수_없는_분류를_승급_신호로_쓰지_않는다() -> None:
    """금지 성분을 승급 신호로 쓰면 호출 셋을 태우고 아무 판정도 늘지 않는다.

    두 관점과 단일 관점 모두 ``BANNED_INGREDIENT``를 일부러 제외한다 — 규칙이 이미
    결정론적으로 판정했기 때문이다. 그래서 성분 때문에 올라간 건은 무조건 통과가 되고
    승급률만 부풀린다.
    """
    judgeable = {
        c
        for target in TargetType
        for fanout in (True, False)
        for spec in specs_for(target, fanout=fanout)
        for c in spec.categories
    }
    assert ViolationCategory.BANNED_INGREDIENT not in judgeable, (
        "관점이 이 분류를 보게 됐다면 이 테스트의 전제가 바뀐 것이다"
    )

    folded = banned.fold(TRIAGE_PROMPT)
    leaked = [
        term.canonical
        for term in (*banned.BLOCKING_TERMS, *banned.ADVISORY_TERMS)
        if any(form in folded for form in term.folded_forms())
    ]
    assert leaked == [], f"프롬프트가 성분으로 승급을 유발한다: {leaked}"


async def test_규칙이_비차단으로라도_적중하면_선별을_건너뛰고_승급한다(triage_on: Settings) -> None:
    """결정론 계층이 의심한 건이 가장 약한 심사를 받지 않게 한다.

    선별에 물으면 Haiku 1회로 CLEAR가 나올 수 있고, 그러면 규칙이 찾아 둔 건이 모델을
    아예 타지 않는다. 규칙은 결정론이라 주입으로 뚫리지 않으므로 그 적중을 더 믿는다.
    """
    llm = FakeLlmClient([ok(NO_FINDING), ok(NO_FINDING)])
    pipeline = InspectionPipeline(llm=llm, settings=triage_on)

    verdict = await pipeline.run(product("코코넛오일이 들어간 강아지 샴푸입니다"))

    rule_row = next(p for p in verdict.perspectives if p.perspective is Perspective.RULE)
    assert rule_row.outcome is PerspectiveOutcome.VIOLATION, "이 입력은 비차단으로 적중해야 한다"
    assert len(llm.calls) == 2, "선별을 부르지 않고 관점 둘만 쏴야 한다"
    assert all(c["model"] != triage_on.triage_model for c in llm.calls)
    rows = {p.perspective: p for p in verdict.perspectives}
    assert rows[Perspective.TRIAGE].outcome is PerspectiveOutcome.UNCHECKED
    assert rows[Perspective.TRIAGE].skipReason == "RULE_HIT", (
        "건너뛴 이유를 남기지 않으면 승급률 분모에서 이 건을 가려낼 수 없다"
    )
    assert not [c for c in verdict.calls if c.perspective is Perspective.TRIAGE], (
        "쏘지 않은 호출이 원장에 있으면 비용과 승급률이 둘 다 틀어진다"
    )
