"""파이프라인 분기 논리를 고정한다.

여기서 검증하는 것은 모델의 판정 품질이 아니라 **코드가 모델 응답을 어떻게 해석하는가**다.
집계·승급·실패 처리를 프롬프트가 아니라 코드가 소유하기 때문에 전부 단위 테스트로 잡힌다.
"""

from __future__ import annotations

from typing import Any

import pytest
from conftest import FakeLlmClient, failed, ok

from app.inspection.pipeline import InspectionPipeline
from app.schemas.inspection import (
    CallOutcome,
    Perspective,
    PerspectiveOutcome,
    ProductInspectionRequest,
    ReviewInspectionRequest,
    Verdict,
)
from app.settings import Settings


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


def review(content: str) -> ReviewInspectionRequest:
    return ReviewInspectionRequest(
        inspectionId=2,
        reviewId=77,
        productId=42,
        contentHash="b" * 64,
        content=content,
        rating=5,
        categoryPath=["강아지", "건강관리", "영양제"],
    )


def finding(quote: str, category: str = "MEDICAL_CLAIM", suggestion: str | None = None) -> dict[str, Any]:
    return {
        "findings": [{"category": category, "quote": quote, "reason": "정책 위반", "suggestion": suggestion}]
    }


NO_FINDING: dict[str, Any] = {"findings": []}


async def test_결정론적_위반은_모델을_부르지_않는다(settings: Settings) -> None:
    llm = FakeLlmClient([])
    pipeline = InspectionPipeline(llm=llm, settings=settings)

    verdict = await pipeline.run(product("문의는 010-1234-5678로 주세요"))

    assert verdict.verdict is Verdict.FLAG
    assert llm.calls == [], "규칙이 확정한 건에 모델을 부르면 비용과 지연이 공짜로 늘어난다"
    assert verdict.calls == []
    assert any(v.perspective is Perspective.RULE for v in verdict.violations)
    assert verdict.sellerNotice is not None


async def test_깨끗한_상품은_두_관점을_타고_통과한다(settings: Settings) -> None:
    llm = FakeLlmClient([ok(NO_FINDING), ok(NO_FINDING)])
    pipeline = InspectionPipeline(llm=llm, settings=settings)

    verdict = await pipeline.run(product("글루코사민 함유 영양제입니다"))

    assert verdict.verdict is Verdict.PASS
    assert len(llm.calls) == 2, "관점 둘을 모두 태워야 한다"
    outcomes = {p.perspective: p.outcome for p in verdict.perspectives}
    assert outcomes[Perspective.RULE] is PerspectiveOutcome.CLEAR
    assert outcomes[Perspective.DISPLAY_ADS] is PerspectiveOutcome.CLEAR
    assert outcomes[Perspective.CONDUCT_TRADE] is PerspectiveOutcome.CLEAR


async def test_반박이_유지하면_수정_문구가_붙는다(settings: Settings) -> None:
    quote = "관절염 치료에 효과가 있습니다"
    llm = FakeLlmClient(
        [
            ok(finding(quote)),
            ok(NO_FINDING),
            ok({"decision": "UPHOLD", "reason": "치료 효능 단정"}),
            ok({"sellerNotice": "치료 효능 표현은 쓸 수 없습니다.", "rewrites": []}),
        ]
    )
    pipeline = InspectionPipeline(llm=llm, settings=settings)

    verdict = await pipeline.run(product(quote))

    assert verdict.verdict is Verdict.FLAG
    assert verdict.sellerNotice == "치료 효능 표현은 쓸 수 없습니다."
    assert len(llm.calls) == 4
    assert verdict.violations[0].spanStart is not None


async def test_반박이_뒤집으면_통과로_내려간다(settings: Settings) -> None:
    quote = "관절 건강에 도움을 줄 수 있습니다"
    llm = FakeLlmClient(
        [
            ok(finding(quote)),
            ok(NO_FINDING),
            ok({"decision": "OVERTURN", "reason": "정상적인 표현"}),
        ]
    )
    pipeline = InspectionPipeline(llm=llm, settings=settings)

    verdict = await pipeline.run(product(quote))

    assert verdict.verdict is Verdict.PASS
    assert verdict.sellerNotice is None, "뒤집혔으면 수정 문구를 만들 이유가 없다"
    assert len(llm.calls) == 3, "뒤집힌 뒤에 수정 문구를 부르면 비용만 쓴다"


async def test_반박_실패는_위반_판정을_유지한다(settings: Settings) -> None:
    quote = "관절염 치료에 효과가 있습니다"
    llm = FakeLlmClient(
        [
            ok(finding(quote)),
            ok(NO_FINDING),
            failed(CallOutcome.TIMEOUT),
            ok({"sellerNotice": "수정이 필요합니다.", "rewrites": []}),
        ]
    )
    pipeline = InspectionPipeline(llm=llm, settings=settings)

    verdict = await pipeline.run(product(quote))

    assert verdict.verdict is Verdict.FLAG, (
        "반박 실패를 통과로 읽으면 모델을 느리게 만드는 것만으로 검수를 끌 수 있다"
    )


async def test_관점_실패는_통과가_아니라_미검사로_기록된다(settings: Settings) -> None:
    # 첫 관점이 두 번 실패하고(최초 + 재시도 1회) 둘째 관점은 정상
    llm = FakeLlmClient([failed(), failed(), ok(NO_FINDING)])
    pipeline = InspectionPipeline(llm=llm, settings=settings)

    verdict = await pipeline.run(product("글루코사민 함유"))

    outcomes = {p.perspective: p for p in verdict.perspectives}
    display = outcomes[Perspective.DISPLAY_ADS]
    assert display.outcome is PerspectiveOutcome.UNCHECKED
    assert display.skipReason == CallOutcome.RATE_LIMITED.value
    assert verdict.verdict is Verdict.PASS, "미검사가 있어도 위반이 없으면 통과다"
    assert len(llm.calls) == 3, "관점당 재시도는 한 번까지다"


async def test_예산_소진은_재시도하지_않는다(settings: Settings) -> None:
    llm = FakeLlmClient([failed(CallOutcome.BUDGET_EXHAUSTED), ok(NO_FINDING)])
    pipeline = InspectionPipeline(llm=llm, settings=settings)

    verdict = await pipeline.run(product("글루코사민 함유"))

    assert len(llm.calls) == 2, "예산이 없는데 다시 부르면 400을 한 번 더 받을 뿐이다"
    outcomes = {p.perspective: p for p in verdict.perspectives}
    assert outcomes[Perspective.DISPLAY_ADS].skipReason == CallOutcome.BUDGET_EXHAUSTED.value


async def test_원문에_없는_인용은_판정으로_쓰지_않는다(settings: Settings) -> None:
    llm = FakeLlmClient([ok(finding("이 문장은 본문에 없다")), ok(NO_FINDING)])
    pipeline = InspectionPipeline(llm=llm, settings=settings)

    verdict = await pipeline.run(product("글루코사민 함유 영양제입니다"))

    outcomes = {p.perspective: p for p in verdict.perspectives}
    assert outcomes[Perspective.DISPLAY_ADS].outcome is PerspectiveOutcome.UNCHECKED
    assert outcomes[Perspective.DISPLAY_ADS].skipReason == "EVIDENCE_UNVERIFIED"
    assert verdict.violations == [], "검증 못 한 인용을 저장하면 근거 없는 보류가 셀러에게 간다"


async def test_리뷰에는_수정_문구를_만들지_않는다(settings: Settings) -> None:
    quote = "이거 먹고 관절염 나았어요"
    llm = FakeLlmClient(
        [
            ok(finding(quote)),
            ok(NO_FINDING),
            ok({"decision": "UPHOLD", "reason": "치료 효능 단정"}),
        ]
    )
    pipeline = InspectionPipeline(llm=llm, settings=settings)

    verdict = await pipeline.run(review(quote))

    assert verdict.verdict is Verdict.FLAG
    assert verdict.sellerNotice is None, "리뷰는 작성자가 고쳐 올리는 흐름이 없다"
    assert len(llm.calls) == 3


async def test_stub_모드는_모델_없이_왕복한다(settings: Settings) -> None:
    pipeline = InspectionPipeline(llm=None, settings=settings)

    verdict = await pipeline.run(product("글루코사민 함유"))

    assert verdict.verdict is Verdict.PASS
    assert verdict.calls == []
    outcomes = {p.perspective: p for p in verdict.perspectives}
    assert outcomes[Perspective.ALL].skipReason == "LLM_DISABLED"


async def test_캐시_지점은_접두부에_하나만_둔다(settings: Settings) -> None:
    llm = FakeLlmClient([ok(NO_FINDING), ok(NO_FINDING)])
    pipeline = InspectionPipeline(llm=llm, settings=settings)

    await pipeline.run(product("글루코사민 함유"))

    for call in llm.calls:
        blocks = call["system_blocks"]
        cached = [b for b in blocks if "cache_control" in b]
        assert len(cached) == 1, "캐시 지점이 여러 개면 접두부 과금이 나뉜다"
        # 관점 질문이 시스템에 들어가면 캐시가 관점마다 갈라진다
        assert all("당신의 관점은" not in b["text"] for b in blocks)
    questions = [call["user_content"][-1]["text"] for call in llm.calls]
    assert len(set(questions)) == 2, "관점 질문은 사용자 메시지 접미부에 와야 한다"


async def test_샘플링_파라미터를_절대_보내지_않는다(settings: Settings) -> None:
    llm = FakeLlmClient([ok(NO_FINDING), ok(NO_FINDING)])
    pipeline = InspectionPipeline(llm=llm, settings=settings)

    await pipeline.run(product("글루코사민 함유"))

    for call in llm.calls:
        for banned in ("temperature", "top_p", "top_k"):
            assert banned not in call, f"{banned}는 현세대 모델에서 400이다"
        assert call["effort"] in ("low", "medium", "high", "xhigh", "max")


@pytest.mark.parametrize("fanout", [True, False])
async def test_팬아웃_스위치가_호출_수를_바꾼다(settings: Settings, fanout: bool) -> None:
    tuned = settings.model_copy(update={"perspective_fanout": fanout})
    llm = FakeLlmClient([ok(NO_FINDING), ok(NO_FINDING)])
    pipeline = InspectionPipeline(llm=llm, settings=tuned)

    await pipeline.run(product("글루코사민 함유"))

    assert len(llm.calls) == (2 if fanout else 1)
