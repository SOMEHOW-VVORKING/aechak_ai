"""데모 콘솔을 고정한다.

콘솔은 발표에서 쓰는 화면이지만 여기서 잡는 것은 그림이 아니라 **판단 코드**다. 셋이다.

1. 탈출 지점 다섯이 판정 하나에서 갈리는가. 추적 없는 얕은 재생에서도 같은 답이 나오는가.
2. 통과와 미검사가 끝까지 갈라져 있는가. 이것이 수용 기준이고 프로젝트 전체의 불변식이다.
3. 재생이 결정론적인가. 카세트가 빠진 자리를 조용히 통과 방향으로 접지 않는가.

콘솔 자체의 배치도 테스트가 지킨다 -- 운영 이미지와 판정 계약이 데모 때문에 움직이지 않는
것을 ``test_운영_...`` 들이 단언한다.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
from conftest import backend_model_verified
from fastapi.testclient import TestClient

from app.inspection.perspectives import findings_tool, specs_for
from app.inspection.pipeline import InspectionPipeline
from app.inspection.trace import TraceRecorder
from app.schemas.inspection import (
    ContentVerdict,
    Perspective,
    PerspectiveOutcome,
    TargetType,
    Verdict,
)
from app.settings import Settings
from demo import view
from demo.app import app as demo_app
from demo.cassettes import BY_ID, SCENARIOS, Scenario
from demo.mock import MockLlmClient
from demo.replay import CassetteMiss, ReplayLlmClient, cassette_key
from demo.view import ExitPoint
from harness.run import KRW_PER_USD as HARNESS_KRW_PER_USD

REPO = Path(__file__).parent.parent
CONTRACT_DIR = Path(__file__).parent / "contract"


async def run_scenario(scenario: Scenario) -> tuple[ContentVerdict, TraceRecorder, ReplayLlmClient | None]:
    """시나리오를 재생으로 돌린다. 네트워크도 대기도 없다."""
    settings = Settings(anthropic_api_key="test", enable_triage=scenario.enable_triage)
    llm = None if scenario.llm_disabled else ReplayLlmClient(entries=dict(scenario.entries))
    recorder = TraceRecorder()
    pipeline = InspectionPipeline(llm=llm, settings=settings, recorder=recorder)
    verdict = await pipeline.run(scenario.to_request())
    return verdict, recorder, llm


def built(verdict: ContentVerdict, recorder: TraceRecorder, *, mode: str = "replay") -> view.ConsoleView:
    return view.build(verdict, mode=mode, source="test", trace=recorder.trace)


def stages_of(built_view: view.ConsoleView, stage: str) -> list[view.StageView]:
    return [s for s in built_view.stages if s.stage == stage]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
async def test_시나리오가_적어_둔_탈출_지점에서_끝난다(scenario: Scenario) -> None:
    verdict, recorder, _ = await run_scenario(scenario)

    assert built(verdict, recorder).exit_point == scenario.expected_exit


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
async def test_카세트에_쓰지_않는_응답이_남아_있지_않다(scenario: Scenario) -> None:
    """남은 카세트는 파이프라인 분기가 바뀌었다는 신호다. 조용히 두면 시나리오가 썩는다."""
    _, _, llm = await run_scenario(scenario)

    if llm is None:
        assert not scenario.entries, "stub 시나리오는 호출이 없으므로 카세트도 비어야 한다"
        return
    used = {call["key"] for call in llm.calls}
    assert used == set(scenario.entries), f"쓰이지 않은 카세트: {set(scenario.entries) - used}"


async def test_탈출_지점_다섯을_시나리오가_모두_덮는다() -> None:
    covered = set()
    for scenario in SCENARIOS:
        verdict, recorder, _ = await run_scenario(scenario)
        covered.add(built(verdict, recorder).exit_point)

    five = {
        ExitPoint.RULE_BLOCKED.value,
        ExitPoint.TRIAGE_CLEARED.value,
        ExitPoint.TRIAGE_FLAGGED.value,
        ExitPoint.MODEL_CLEAR.value,
        ExitPoint.MODEL_FLAGGED.value,
    }
    assert five <= covered, f"덮이지 않은 탈출 지점: {five - covered}"


async def test_선별이_건너뛴_관점을_통과로_그리지_않는다() -> None:
    """콘솔의 수용 기준 가운데 가장 중요한 줄이다.

    건너뛴 관점을 통과로 그리면 아무도 보지 않은 상품이 "문제 없음"으로 보인다. 색만 다르고
    라벨이 같아도 안 된다 -- 둘 다 달라야 한다.
    """
    verdict, recorder, _ = await run_scenario(BY_ID["triage-clear"])
    console = built(verdict, recorder)

    judged = stages_of(console, "N1_JUDGE")
    assert len(judged) == 2
    for row in judged:
        assert row.status == "SKIPPED"
        assert row.outcome == PerspectiveOutcome.UNCHECKED.value
        assert row.outcome_label == "미검사"
        assert row.skip_reason == "TRIAGE_CLEAR"
        assert row.skip_reason_label == "선별이 깨끗하다고 해서 보지 않았다"

    cleared = stages_of(console, "N0_5_TRIAGE")[0]
    assert cleared.outcome == PerspectiveOutcome.CLEAR.value
    assert cleared.outcome_label == "통과"
    assert cleared.outcome_label != judged[0].outcome_label, "통과와 미검사의 라벨이 같으면 뭉갠 것이다"
    assert console.backend_accepts, "이 모양이어야 백엔드가 관리자 확인 없이 통과를 받는다"


async def test_모델이_읽고_낸_통과는_미검사가_아니다() -> None:
    verdict, recorder, _ = await run_scenario(BY_ID["model-clear"])
    console = built(verdict, recorder)

    judged = stages_of(console, "N1_JUDGE")
    assert [row.status for row in judged] == ["RAN", "RAN"]
    assert {row.outcome for row in judged} == {PerspectiveOutcome.CLEAR.value}
    assert {row.outcome_label for row in judged} == {"통과"}
    assert console.verdict == Verdict.PASS.value


async def test_단계가_파이프라인_순서대로_나온다() -> None:
    verdict, recorder, _ = await run_scenario(BY_ID["model-flag"])
    console = built(verdict, recorder)

    order = [stage.stage for stage in console.stages]
    canonical = [stage.value for stage, _ in view.CANONICAL_STAGES]
    assert [s for s in canonical if s in order] == sorted(set(order), key=canonical.index)
    assert order.index("N0_RULE") < order.index("N0_5_TRIAGE") < order.index("N1_JUDGE")
    assert order.index("N1_JUDGE") < order.index("N2_AGGREGATE") < order.index("N4_NOTICE")


async def test_규칙이_확정한_건은_뒤_단계가_도달하지_않음으로_남는다() -> None:
    verdict, recorder, _ = await run_scenario(BY_ID["rule-block"])
    console = built(verdict, recorder)

    assert console.exit_point == ExitPoint.RULE_BLOCKED.value
    assert verdict.calls == [], "규칙이 확정한 건에 모델을 부르면 비용과 지연이 공짜로 늘어난다"
    assert console.ledger.total_cost_micro_usd == 0
    for stage in ("N0_5_TRIAGE", "N1_JUDGE", "N2_AGGREGATE"):
        assert stages_of(console, stage)[0].status == "NOT_REACHED", stage
    hit = next(h for h in console.rule_hits if h.category == "CONTACT_INFO")
    assert hit.blocking is True
    assert hit.blocking_label == "차단"
    assert "010-1234-5678" not in (console.redacted or ""), "모델에 보낼 본문에서는 가려야 한다"


async def test_걸린_구절과_사유와_수정_문구가_한_화면에_모인다() -> None:
    verdict, recorder, _ = await run_scenario(BY_ID["model-flag"])
    console = built(verdict, recorder)

    assert console.exit_point == ExitPoint.MODEL_FLAGGED.value
    quotes = {v.quote for v in console.violations}
    assert "관절염 치료에 효과가 있습니다" in quotes
    # 구간은 가린 본문 기준이고, 그 본문도 함께 내려보낸다
    assert console.highlight_base == "REDACTED"
    violation = next(v for v in console.violations if v.quote == "관절염 치료에 효과가 있습니다")
    assert (console.redacted or "")[violation.span_start : violation.span_end] == violation.quote

    judged = stages_of(console, "N1_JUDGE")[0]
    reasons = {f.reason for f in judged.findings}
    assert "질병 치료를 단정한다" in reasons, "모델이 적은 사유는 판정 계약에 실리지 않는다"
    assert console.seller_notice is not None
    notice = stages_of(console, "N4_NOTICE")[0]
    assert [r.suggestion for r in notice.rewrites] == [
        "관절 건강에 도움을 줄 수 있습니다",
        "꾸준히 급여해 주세요",
    ]


async def test_인용이_본문에_없으면_버린_판정으로_그린다() -> None:
    verdict, recorder, _ = await run_scenario(BY_ID["evidence-unverified"])
    console = built(verdict, recorder)

    dropped = next(s for s in stages_of(console, "N1_JUDGE") if s.evidence_dropped)
    assert dropped.outcome == PerspectiveOutcome.UNCHECKED.value
    assert dropped.skip_reason == "EVIDENCE_UNVERIFIED"
    assert [f.verified for f in dropped.findings] == [False]
    assert dropped.findings[0].verified_label == "인용이 본문에 없어 폐기"
    assert console.violations == [], "검증되지 않은 인용은 판정에 실리지 않는다"


async def test_선별이_확정한_보류를_모델_보류와_다른_배지로_그린다() -> None:
    """같은 배지로 그리면 Haiku 한 번이 확정한 것이 Sonnet 둘이 읽고 낸 것으로 보인다."""
    verdict, recorder, _ = await run_scenario(BY_ID["triage-certain"])
    console = built(verdict, recorder)

    assert console.exit_point == ExitPoint.TRIAGE_FLAGGED.value
    assert console.exit_label != view._EXIT_LABELS[ExitPoint.MODEL_FLAGGED.value][0]
    assert console.verdict == Verdict.FLAG.value
    triage = stages_of(console, "N0_5_TRIAGE")[0]
    assert triage.decision == "CERTAIN_VIOLATION"
    assert triage.outcome == PerspectiveOutcome.VIOLATION.value

    judged = stages_of(console, "N1_JUDGE")
    assert [row.status for row in judged] == ["SKIPPED", "SKIPPED"]
    assert {row.outcome for row in judged} == {PerspectiveOutcome.UNCHECKED.value}
    assert {row.outcome_label for row in judged} == {"미검사"}
    assert {row.skip_reason for row in judged} == {"TRIAGE_CERTAIN"}
    assert {row.skip_reason_label for row in judged} == {"선별이 위반을 확정해서 보지 않았다"}
    assert [v.perspective for v in console.violations] == ["TRIAGE"]
    assert console.seller_notice, "상품이므로 수정 안내가 함께 나간다"


async def test_예산_소진은_통과가_아니라_판정_실패로_그린다() -> None:
    verdict, recorder, _ = await run_scenario(BY_ID["budget-exhausted"])
    console = built(verdict, recorder)

    assert console.exit_point == ExitPoint.AGGREGATE_FAILED.value
    assert console.verdict == Verdict.FAILED.value
    aggregate = stages_of(console, "N2_AGGREGATE")[0]
    assert (aggregate.dispatched, aggregate.checked) == (2, 0)
    failed = stages_of(console, "N1_JUDGE")
    assert {row.status for row in failed} == {"FAILED"}
    assert {row.detail for row in failed} == {"workspace spend limit reached"}
    assert not console.backend_accepts


async def test_규칙_비차단_적중은_선별에_묻지_않는다() -> None:
    verdict, recorder, llm = await run_scenario(BY_ID["rule-hit-promoted"])
    console = built(verdict, recorder)

    triage = stages_of(console, "N0_5_TRIAGE")[0]
    assert triage.status == "SKIPPED"
    assert triage.skip_reason == "RULE_HIT"
    assert llm is not None
    assert all(call["key"][0] != "triage_content" for call in llm.calls)
    assert "[가림]" in (console.redacted or ""), "지시형 문장은 모델에 보낼 본문에서도 가린다"


async def test_리뷰는_수정_문구를_만들지_않는다() -> None:
    verdict, recorder, _ = await run_scenario(BY_ID["review-flag"])
    console = built(verdict, recorder)

    assert console.target == TargetType.REVIEW.value
    assert console.exit_point == ExitPoint.MODEL_FLAGGED.value
    assert console.seller_notice is None
    assert stages_of(console, "N4_NOTICE")[0].status == "NOT_REACHED"


async def test_재생이_결정론적이다() -> None:
    """데모 중 레이트 리밋이나 모델 흔들림에 화면이 달라지지 않아야 한다."""
    scenario = BY_ID["model-flag"]
    first_verdict, first_recorder, _ = await run_scenario(scenario)
    second_verdict, second_recorder, _ = await run_scenario(scenario)

    assert json.loads(first_verdict.model_dump_json(exclude={"decidedAt"})) == json.loads(
        second_verdict.model_dump_json(exclude={"decidedAt"})
    )
    assert built(first_verdict, first_recorder).as_dict() == built(second_verdict, second_recorder).as_dict()


async def test_카세트가_빠진_호출은_조용히_실패로_접히지_않는다() -> None:
    """선별이 실패를 승급으로 접는다. 조용히 접으면 거짓 단계가 그려진다."""
    scenario = BY_ID["model-clear"]
    missing = {k: v for k, v in scenario.entries.items() if k[0] != "triage_content"}
    settings = Settings(anthropic_api_key="test", enable_triage=True)
    pipeline = InspectionPipeline(llm=ReplayLlmClient(entries=missing), settings=settings)

    with pytest.raises(CassetteMiss):
        await pipeline.run(scenario.to_request())


def test_관점_호출의_카세트_키가_툴_스키마에서_갈린다() -> None:
    """두 관점의 툴 이름이 같다. 분류 열거 집합이 관점의 지문이다."""
    display, conduct = specs_for(fanout=True)
    (single,) = specs_for(fanout=False)

    assert cassette_key(findings_tool(display)) == ("report_findings", Perspective.DISPLAY_ADS)
    assert cassette_key(findings_tool(conduct)) == ("report_findings", Perspective.CONDUCT_TRADE)
    assert cassette_key(findings_tool(single)) == ("report_findings", Perspective.ALL)

    broken = findings_tool(display)
    broken["input_schema"]["properties"]["findings"]["items"]["properties"]["category"]["enum"] = ["X"]
    with pytest.raises(CassetteMiss):
        cassette_key(broken)


async def test_원장_합계가_호출의_합과_같다() -> None:
    verdict, recorder, _ = await run_scenario(BY_ID["model-flag"])
    ledger = built(verdict, recorder).ledger

    assert ledger.total_cost_micro_usd == sum(c.costMicroUsd for c in verdict.calls)
    assert ledger.total_latency_ms == sum(c.latencyMs for c in verdict.calls)
    assert ledger.total_cache_read_tokens == sum(c.cacheReadInputTokens for c in verdict.calls)
    assert ledger.triage_cost_micro_usd == sum(
        c.costMicroUsd for c in verdict.calls if c.perspective is Perspective.TRIAGE
    )
    assert 0 < ledger.triage_cost_micro_usd < ledger.total_cost_micro_usd

    # 선별 호출은 캐시를 타지 못한다. Haiku 4.5의 최소 접두부가 4,096토큰이다
    triage_row = next(c for c in ledger.calls if c.perspective == Perspective.TRIAGE.value)
    assert triage_row.cache_hit is False
    assert ledger.cache_hits == sum(1 for c in verdict.calls if c.cacheReadInputTokens > 0)


def test_원화_환산_전제가_하네스와_같다() -> None:
    """콘솔이 보여 주는 원화로 손익분기를 이야기할 수 있어야 한다."""
    assert view.KRW_PER_USD == HARNESS_KRW_PER_USD


async def test_백엔드_게이트_술어가_테스트_사본과_같은_답을_낸다() -> None:
    """같은 규칙의 사본이 둘이다. 갈라지면 콘솔이 거짓 배지를 그린다."""
    for scenario in SCENARIOS:
        verdict, _, _ = await run_scenario(scenario)
        assert view.backend_accepts(verdict) == backend_model_verified(verdict), scenario.id

    for path in sorted(CONTRACT_DIR.glob("content-verdict*.json")):
        sample = ContentVerdict.model_validate(json.loads(path.read_text()))
        assert view.backend_accepts(sample) == backend_model_verified(sample), path.name


def test_선별_통과_표본은_탈출_지점과_게이트_통과를_따로_보여_준다() -> None:
    """탈출 지점(어디서 끝났는가)과 게이트 통과(그 모양이 증거로 인정되는가)는 다른 축이다.

    한 축으로 합치면 "선별이 통과시켰는데 전건이 관리자 큐로 간다"는 상태가 화면에서 사라진다.
    표본이 생산자 모양과 맞는 지금은 두 축이 함께 참이고, 뒤처지면 탈출 지점만 참이 된다.
    """
    raw = json.loads((CONTRACT_DIR / "content-verdict-triage.json").read_text())
    console = view.build(ContentVerdict.model_validate(raw), mode="verdict", source="sample")

    assert console.exit_point == ExitPoint.TRIAGE_CLEARED.value
    assert console.backend_accepts is True, (
        "표본이 생산자 모양과 어긋나 게이트를 넘지 못한다. 관점 행과 사유를 대조해라"
    )
    assert "관리자 검토 큐" not in console.backend_note


def test_판정만으로_다시_그려도_단계와_탈출_지점이_나온다() -> None:
    raw = json.loads((CONTRACT_DIR / "content-verdict.json").read_text())
    console = view.build(ContentVerdict.model_validate(raw), mode="verdict", source="sample")

    assert console.trace_available is False
    assert console.exit_point == ExitPoint.MODEL_FLAGGED.value
    assert console.highlight_base == "NONE", "가린 본문이 없으면 하이라이트를 찍을 수 없다"
    rows = {s.stage: s for s in console.stages}
    assert rows["N1_JUDGE"].status in ("RAN", "FAILED")
    assert len(console.missing_without_trace) == len(view.WITHOUT_TRACE_MISSING)
    failed = [s for s in console.stages if s.stage == "N1_JUDGE" and s.status == "FAILED"]
    assert failed and failed[0].skip_reason == "RATE_LIMITED"
    assert failed[0].skip_reason_label == "레이트 리밋에 걸렸다"


def test_판정만으로는_선별_사유를_되살릴_수_없다() -> None:
    """얕은 재생의 한계를 화면이 숨기지 않는지 본다."""
    raw = json.loads((CONTRACT_DIR / "content-verdict-triage-promoted.json").read_text())
    console = view.build(ContentVerdict.model_validate(raw), mode="verdict", source="sample")

    triage = next(s for s in console.stages if s.stage == "N0_5_TRIAGE")
    assert triage.reason is None
    assert triage.decision is None, "선별의 결정 열거는 계약에 남지 않는다"
    assert "선별이 적은 사유" in console.missing_without_trace[0]


async def test_모의_판정기는_내_문장에서_인용을_떼어_온다() -> None:
    """꾸며 쓴 인용은 검증에서 떨어진다. 대역이 거짓 단계를 그리는 자리다."""
    scenario = BY_ID["model-flag"]
    settings = Settings(anthropic_api_key="", enable_triage=True)
    recorder = TraceRecorder()
    pipeline = InspectionPipeline(llm=MockLlmClient(), settings=settings, recorder=recorder)

    verdict = await pipeline.run(scenario.to_request())
    console = built(verdict, recorder, mode="mock")

    assert console.exit_point == ExitPoint.MODEL_FLAGGED.value
    judged = next(s for s in stages_of(console, "N1_JUDGE") if s.findings)
    assert judged.evidence_dropped == 0
    for item in judged.findings:
        assert item.verified
        assert item.quote in (console.redacted or "")
    assert console.seller_notice


async def test_모의_판정기도_깨끗한_본문은_선별에서_끝낸다() -> None:
    settings = Settings(anthropic_api_key="", enable_triage=True)
    recorder = TraceRecorder()
    pipeline = InspectionPipeline(llm=MockLlmClient(), settings=settings, recorder=recorder)

    verdict = await pipeline.run(BY_ID["triage-clear"].to_request())

    assert built(verdict, recorder, mode="mock").exit_point == ExitPoint.TRIAGE_CLEARED.value


# --- 콘솔 HTTP 표면 ---------------------------------------------------------------


@pytest.fixture
def client() -> TestClient:
    return TestClient(demo_app)


def test_콘솔_한_장이_외부_자원을_불러오지_않는다(client: TestClient) -> None:
    """CDN·폰트·외부 스크립트를 쓰면 발표장 네트워크에 데모가 걸린다."""
    response = client.get("/")

    assert response.status_code == 200
    html = response.text
    assert "애착 검수 데모 콘솔" in html
    assert "http://" not in html and "https://" not in html
    assert "<script src" not in html


def test_기본_모드가_재생이다(client: TestClient) -> None:
    """데모 중에 사람이 실수로 유료 경로를 밟지 않는 쪽이 기본이어야 한다."""
    response = client.post("/api/inspect", json={"scenario": "triage-clear"})

    assert response.status_code == 200
    assert response.json()["view"]["mode"] == "replay"


def test_라이브는_키가_없으면_호출하지_않고_거절한다(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AECHAK_AI_ANTHROPIC_API_KEY", raising=False)

    response = client.post("/api/inspect", json={"mode": "live", "description": "치료에 효과"})

    assert response.status_code == 400
    assert "AECHAK_AI_ANTHROPIC_API_KEY" in response.json()["error"]


def test_카세트가_빠지면_콘솔이_오류로_답한다(client: TestClient) -> None:
    response = client.post("/api/inspect", json={"scenario": "없는-시나리오"})

    assert response.status_code == 400
    assert "시나리오" in response.json()["error"]


def test_빈_본문은_파이프라인을_돌리지_않는다(client: TestClient) -> None:
    response = client.post("/api/inspect", json={"mode": "mock", "productName": "", "description": " "})

    assert response.status_code == 400


def test_시나리오_목록이_탈출_지점과_함께_내려온다(client: TestClient) -> None:
    payload = client.get("/api/scenarios").json()

    assert {s["id"] for s in payload["scenarios"]} == set(BY_ID)
    assert all(s["expectedExit"] for s in payload["scenarios"])
    assert isinstance(payload["liveAvailable"], bool)
    assert payload["samples"], "저장된 판정 표본이 없으면 얕은 재생을 보여 줄 수 없다"
    assert payload["defaults"]["krwPerUsd"] == view.KRW_PER_USD


def test_저장된_판정_재생이_표본_이름만_받는다(client: TestClient) -> None:
    ok = client.post("/api/verdict", json={"sample": "content-verdict-triage.json"})
    assert ok.status_code == 200
    assert ok.json()["view"]["trace_available"] is False

    # 경로를 섞어 보내도 표본 디렉터리 밖으로 나가지 않는다
    escaped = client.post("/api/verdict", json={"sample": "../../pyproject.toml"})
    assert escaped.status_code == 400


def test_붙여_넣은_판정도_다시_그린다(client: TestClient) -> None:
    raw = json.loads((CONTRACT_DIR / "content-verdict.json").read_text())

    response = client.post("/api/verdict", json={"payload": raw})

    assert response.status_code == 200
    assert response.json()["view"]["exit_point"] == ExitPoint.MODEL_FLAGGED.value


def test_판정_계약을_어긴_입력은_거절한다(client: TestClient) -> None:
    response = client.post("/api/verdict", json={"payload": {"inspectionId": 1}})

    assert response.status_code == 400
    assert "판정 계약" in response.json()["error"]


# --- 운영 이미지와 계약이 데모 때문에 움직이지 않는다 ---------------------------------


def test_운영_이미지가_데모를_담지_않는다() -> None:
    """Dockerfile 이 app 과 prompts 만 COPY 한다. harness 가 이미 같은 규칙으로 밖에 있다."""
    dockerfile = (REPO / "Dockerfile").read_text()

    copied = [line.strip() for line in dockerfile.splitlines() if line.strip().startswith("COPY")]
    assert not any("demo" in line for line in copied), copied
    assert any("COPY app ./app" in line for line in copied)


def test_운영_의존성이_늘지_않았다() -> None:
    """데모는 새 의존성을 0으로 둔다. FastAPI 와 uvicorn 은 이미 main 의존성이다.

    Streamlit 을 ``[project.dependencies]`` 에 넣으면 pandas·pyarrow·altair 가 따라와
    운영 이미지에 그대로 실린다. 비기본 그룹으로 둬도 ``uv.lock`` 전체가 다시 해석된다.
    """
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())

    assert {name.split(">=")[0].split("[")[0] for name in pyproject["project"]["dependencies"]} == {
        "fastapi",
        "uvicorn",
        "pydantic",
        "pydantic-settings",
        "aiokafka",
        "anthropic",
    }
    assert set(pyproject["dependency-groups"]) == {"dev"}
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["app"]


def test_운영_앱에_콘솔_경로가_붙지_않았다() -> None:
    """``app/main.py`` 가 공개 ALB 뒤라서 관리용 경로를 열면 외부에서 닿는다."""
    from app.main import app as operational_app

    paths = {getattr(route, "path", "") for route in operational_app.routes}
    assert not any("demo" in path or "console" in path for path in paths), paths

    importers = [
        path.relative_to(REPO).as_posix()
        for path in (REPO / "app").rglob("*.py")
        for line in path.read_text().splitlines()
        if line.startswith(("import demo", "from demo"))
    ]
    assert importers == [], (
        f"운영 코드가 demo 패키지를 import 한다: {importers}. 이미지에 없는 모듈을 부르게 된다"
    )


def test_선별_사유_문자열을_건드리지_않았다() -> None:
    """백엔드와의 약속이다. 콘솔이 라벨을 붙여도 값은 그대로 나가야 한다."""
    from app.inspection.triage import TRIAGE_SKIP_REASON

    assert TRIAGE_SKIP_REASON == "TRIAGE_CLEAR"
    assert view._SKIP_REASON_LABELS[TRIAGE_SKIP_REASON] != TRIAGE_SKIP_REASON


def test_화면_모델이_JSON으로_나간다() -> None:
    """``as_dict`` 가 그대로 직렬화되지 않으면 콘솔이 빈 화면을 띄운다."""
    raw = json.loads((CONTRACT_DIR / "content-verdict.json").read_text())
    console = view.build(ContentVerdict.model_validate(raw), mode="verdict", source="sample")

    dumped: dict[str, Any] = json.loads(json.dumps(console.as_dict(), ensure_ascii=False))

    assert dumped["exit_label"] == "확정 위반"
    assert dumped["ledger"]["calls"][0]["perspective"] == Perspective.DISPLAY_ADS.value


async def test_게이트_배지가_통과에만_적용된다() -> None:
    """보류와 판정 실패는 어차피 사람이 본다. 거기에 불통과 배지를 띄우면 거짓 경보다."""
    flagged, flagged_recorder, _ = await run_scenario(BY_ID["rule-block"])
    passed, passed_recorder, _ = await run_scenario(BY_ID["triage-clear"])

    blocked_view = built(flagged, flagged_recorder)
    assert blocked_view.backend_gate_applies is False
    assert "해당 없음" in blocked_view.backend_note or "자리가 아니다" in blocked_view.backend_note

    cleared_view = built(passed, passed_recorder)
    assert cleared_view.backend_gate_applies is True
    assert cleared_view.backend_accepts is True


# --- 부분 미검사 통과, 모의 판정기 경고, 재생 재현성 ----------------------------------


async def test_관점_하나가_미검사인_통과를_다_읽은_통과로_단정하지_않는다() -> None:
    """콘솔이 통과와 미검사를 **머리줄에서** 섞지 않아야 한다.

    종전에는 PASS 이고 TRIAGE 가 CLEAR 가 아니면 무조건 MODEL_CLEAR 였고, 그 설명문이
    "관점이 실제로 읽고 위반을 찾지 못했다. 이 통과는 미검사가 아니다."였다. 쏜 관점
    가운데 몇이 미검사인지는 보지 않았다. 의료·효능 주장을 보는 유일한 관점이 아무 판정도
    못 했는데 머리줄이 그 반대를 단정하는 상태다. 발표장에서 읽히는 것은 N2 집계 줄이
    아니라 배지와 그 밑 한 줄이다.
    """
    verdict, recorder, _ = await run_scenario(BY_ID["evidence-unverified"])
    console = built(verdict, recorder)

    assert console.verdict == Verdict.PASS.value
    assert console.exit_point == ExitPoint.MODEL_PARTIAL.value
    assert "미검사가 아니다" not in console.exit_note, console.exit_note
    assert "1" in console.exit_note and "2" in console.exit_note, (
        f"몇 건 중 몇 건을 읽었는지 수치로 적어야 한다: {console.exit_note}"
    )


async def test_관점_하나가_죽은_통과도_부분_미검사로_그린다() -> None:
    """운영에서 더 흔한 모양이다. 하나가 레이트 리밋으로 죽고 하나가 통과하는 경우."""
    from app.schemas.inspection import CallOutcome
    from demo.replay import ScriptedCall

    scenario = BY_ID["model-clear"]
    entries = dict(scenario.entries)
    entries[("report_findings", Perspective.DISPLAY_ADS)] = ScriptedCall(
        tool_input=None,
        outcome=CallOutcome.RATE_LIMITED,
        latency_ms=90,
    )
    settings = Settings(anthropic_api_key="test", enable_triage=scenario.enable_triage)
    recorder = TraceRecorder()
    pipeline = InspectionPipeline(llm=ReplayLlmClient(entries=entries), settings=settings, recorder=recorder)
    verdict = await pipeline.run(scenario.to_request())
    console = built(verdict, recorder)

    assert console.verdict == Verdict.PASS.value
    assert console.exit_point == ExitPoint.MODEL_PARTIAL.value
    assert "미검사가 아니다" not in console.exit_note


async def test_두_관점이_다_읽은_통과는_여전히_미검사가_아니다() -> None:
    """부분 미검사를 가르느라 온전한 통과의 뜻을 흐리지 않는다."""
    verdict, recorder, _ = await run_scenario(BY_ID["model-clear"])
    console = built(verdict, recorder)

    assert console.exit_point == ExitPoint.MODEL_CLEAR.value
    assert "미검사가 아니다" in console.exit_note


async def test_모의_모드는_판정이_키워드_표에서_나온_것임을_배지로_알린다() -> None:
    """모의 모드는 README 가 '사람이 자기 문장을 넣어 보는 자리'로 규정한 모드다.

    종전에는 모의임을 알리는 표시가 원장 밑의 '토큰과 비용은 추정값이다' 한 줄뿐이라
    **판정**이 추정이라는 경고가 어디에도 없었다. 심사위원이 자기 문장을 넣으면
    '이 시스템은 개인정보 노출 리뷰를 자동 승인한다'로 읽힌다.
    """
    verdict, recorder, _ = await run_scenario(BY_ID["model-clear"])

    mock_view = built(verdict, recorder, mode="mock")
    assert mock_view.judgment_simulated is True
    assert "키워드 표" in mock_view.simulated_note
    assert "모델 판단이 아니" in mock_view.simulated_note

    replay_view = built(verdict, recorder, mode="replay")
    assert replay_view.judgment_simulated is False


async def test_모의_선별은_표에_안_걸리면_더_보라고_넘긴다() -> None:
    """실제 프롬프트의 '확신이 서지 않으면 NEEDS_REVIEW' 와 같은 방향이어야 한다.

    13행 키워드 표에는 ABUSE·PII·IRRELEVANT 항목이 0개였고 SPAM 은 하나였다. 표에 안
    걸리면 CLEAR 를 내므로, 정책의 위반 **예시 그대로** 넣은 문장 셋이 전부 선별 통과 +
    백엔드 게이트 통과로 떨어졌다.
    """
    from app.schemas.inspection import ReviewInspectionRequest

    settings = Settings(anthropic_api_key="", enable_triage=True)
    for text in (
        "배송기사 김철수님이 서울시 강남구 101동 202호에 잘못 두고 갔어요",
        "오늘 날씨 참 좋네요",
        "모든 강아지에게 효과 있습니다. 부작용은 전혀 없습니다.",
    ):
        request = ReviewInspectionRequest(
            inspectionId=1,
            reviewId=77,
            productId=42,
            contentHash="a" * 64,
            content=text,
            rating=5,
            categoryPath=["강아지"],
        )
        recorder = TraceRecorder()
        pipeline = InspectionPipeline(llm=MockLlmClient(), settings=settings, recorder=recorder)
        verdict = await pipeline.run(request)
        console = built(verdict, recorder, mode="mock")

        assert console.exit_point != ExitPoint.TRIAGE_CLEARED.value, (
            f"표에 안 걸리는 문장이 선별 통과로 떨어진다: {text!r}"
        )


def test_모의_키워드_표에_분류_축이_비어_있지_않다() -> None:
    from app.schemas.inspection import ViolationCategory
    from demo.mock import KEYWORD_TABLE

    covered = {category for _k, category, _r, _s in KEYWORD_TABLE}
    for category in (
        ViolationCategory.ABUSE,
        ViolationCategory.PII,
        ViolationCategory.IRRELEVANT,
    ):
        assert category in covered, f"{category.value} 축이 비어 있다"


def test_시나리오가_파이프라인_모양을_전부_들고_있다() -> None:
    """앰비언트 환경이 재생을 흔들면 콘솔이 자기가 안 지난 경로를 지났다고 보여 준다."""
    for scenario in SCENARIOS:
        settings = scenario.settings()
        assert settings.enable_triage == scenario.enable_triage
        # 리터럴에서 조립해야 환경변수가 섞이지 않는다
        assert settings.anthropic_api_key == ""


@pytest.mark.parametrize(
    "env",
    [
        {"AECHAK_AI_PERSPECTIVE_FANOUT": "false"},
        {"AECHAK_AI_ENABLE_NOTICE": "false"},
        {"AECHAK_AI_ENABLE_TRIAGE": "true"},
        {"AECHAK_AI_SCREENING_MODEL": "claude-haiku-4-5"},
    ],
)
def test_환경변수가_있어도_시나리오가_같은_탈출_지점을_낸다(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    """실측으로 ``PERSPECTIVE_FANOUT=false`` 는 네 시나리오를 400으로 죽였다.

    카세트 키가 (툴 이름, 관점)이고 관점은 ``findings_tool`` 의 분류 enum 집합으로
    역산되므로 모양 토글 하나가 키를 바꾼다. 시나리오가 설정을 전부 들고 있어야 한다.
    """
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    for scenario in SCENARIOS:
        response = client.post("/api/inspect", json={"mode": "replay", "scenario": scenario.id})
        assert response.status_code == 200, (scenario.id, response.json())
        assert response.json()["view"]["exit_point"] == scenario.expected_exit, scenario.id


def test_시나리오가_적어_둔_경로와_다르게_돌면_그림을_내보내지_않는다(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """틀린 그림이 정상 응답으로 나가는 것이 가장 나쁘다."""
    from demo import app as demo_module

    monkeypatch.setattr(demo_module, "_verify_expected_exit", lambda *_a, **_k: "일부러 어긋남")
    response = client.post("/api/inspect", json={"mode": "replay", "scenario": "model-clear"})

    assert response.status_code == 400
    assert "view" not in response.json()


def test_게이트_술어_사본이_하나다() -> None:
    """같은 규칙을 파이썬에 두 번 베껴 두고 둘끼리 같은지만 보는 테스트는 항진명제였다.

    백엔드가 ``modelVerified()`` 를 바꾸면 두 사본은 여전히 서로 같아 전부 녹색이고,
    콘솔은 백엔드가 거부할 통과에 초록 배지를 계속 띄운다. 사본을 하나로 줄이고, 갈라짐은
    계약 표본에 대한 기대값으로 잡는다.
    """
    import conftest

    assert conftest.backend_model_verified is view.backend_accepts, (
        "사본이 둘이면 둘끼리 같은지 보는 테스트는 아무것도 지키지 않는다"
    )


def test_계약_표본마다_게이트_통과_여부를_표로_고정한다() -> None:
    """정본이 바뀌면 사본끼리가 아니라 **표본에 대한 기대**가 깨져야 한다."""
    expected = {
        "content-verdict.json": True,
        "content-verdict-triage.json": True,
        "content-verdict-triage-promoted.json": True,
    }
    found = {p.name for p in CONTRACT_DIR.glob("content-verdict*.json")}
    assert found == set(expected), f"표본이 늘거나 줄었다. 표를 고쳐라: {found}"

    for name, accepts in expected.items():
        sample = ContentVerdict.model_validate(json.loads((CONTRACT_DIR / name).read_text()))
        assert view.backend_accepts(sample) is accepts, name


def test_콘솔이_선별_체크박스를_운영_기본값으로_맞춘다() -> None:
    """실제 배포는 선별이 꺼져 있어 전건이 Sonnet 두 관점을 탄다.

    종전에는 ``checked`` 가 하드코딩이고 ``boot()`` 가 ``defaults.enableTriage`` 를 읽지
    않아서, 발표를 보는 사람이 선별 통과를 현재 배포된 동작으로 이해했다.
    """
    html = (REPO / "demo" / "console.html").read_text(encoding="utf-8")

    assert 'id="enableTriage" checked' not in html, "운영 기본값이 아닌 구성을 데모한다"
    assert "defaults.enableTriage" in html, "boot() 가 운영 기본값을 반영해야 한다"
    assert "운영 기본값" in html, "체크박스 옆에 운영 기본값을 적어야 한다"


def test_요청_본문의_선별_기본값이_설정과_같다() -> None:
    """두 자리의 기본값이 갈라지면 콘솔과 API 가 서로 다른 동작을 한다."""
    from demo.app import InspectBody

    assert InspectBody().enableTriage == Settings().enable_triage


def test_캐시_배지가_통과_미검사와_색_어휘를_겹치지_않는다() -> None:
    """이 프로젝트가 가장 피하려는 혼동이 통과와 미검사의 색 혼동이다.

    선별 캐시는 Haiku 최소 접두부 미달로 **항상** 미적중이라, 캐시 축에 미검사 색을 쓰면
    선별 시나리오마다 '미검사(회색 점선)'와 '미적중(회색 점선)'이 한 화면에 같이 선다.
    """
    html = (REPO / "demo" / "console.html").read_text(encoding="utf-8")

    assert 'pill(call.cache_hit ? "CLEAR" : "UNCHECKED"' not in html
    assert ".pill.cache-hit" in html
    assert ".pill.cache-miss" in html
