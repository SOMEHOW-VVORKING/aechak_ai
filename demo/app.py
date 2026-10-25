"""데모 콘솔의 ASGI 앱. **운영 앱(``app/main.py``)에 붙이지 않는다.**

배치 근거를 적어 둔다. 셋 다 지금 코드와 설정에서 읽은 사실이다.

1. ``app/main.py`` 가 "HTTP 로는 헬스체크만 공개한다. 공개 ALB 뒤에 있으므로 관리용 경로를
   열어 두면 이름이 internal 이어도 외부에서 닿는다"고 명시하고 ``docs_url`` 까지 끈 상태다.
   콘솔 라우트를 거기 붙이면 인증 없는 콘솔이 공개 ALB 에 선다.
2. Dockerfile 은 ``COPY app`` 과 ``COPY prompts`` 만 한다. 그래서 ``demo/`` 는 **운영 이미지에
   애초에 들어가지 않는다**(``harness/`` 가 이미 같은 규칙으로 밖에 있다).
3. 새 의존성이 0이다. FastAPI 와 uvicorn 은 이미 main 의존성이라 ``uv.lock`` 이 그대로다.
   Streamlit 을 쓰면 pandas·pyarrow·altair 가 따라오고, ``[project.dependencies]`` 에 넣는 순간
   운영 이미지에 실린다.

띄우는 법은 ``demo/README.md`` 에 있다. 요약하면 별 프로세스다.

    uv run uvicorn demo.app:app --port 8100     (또는 python -m demo)

기본 모드는 **재생**이다. 데모 중에 사람이 실수로 유료 경로를 밟지 않는 쪽이 기본이어야
한다(하네스의 ``--yes`` 와 같은 논리). 라이브는 키가 있을 때만 켜진다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app.inspection.pipeline import InspectionPipeline
from app.inspection.trace import TraceRecorder
from app.llm.client import LlmClient
from app.schemas.inspection import (
    ContentVerdict,
    ProductInspectionRequest,
    ReviewInspectionRequest,
    TargetType,
)
from app.settings import Settings
from demo import view
from demo.cassettes import BY_ID, SCENARIOS, Scenario
from demo.mock import MockLlmClient
from demo.replay import CassetteMiss, ReplayLlmClient

CONSOLE_HTML = Path(__file__).parent / "console.html"

# 저장된 판정 표본. 정본은 백엔드 레포가 소유하고 tests/contract 가 사본을 들고 있다.
# 사본을 또 만들지 않고 그 디렉터리를 읽는다 -- 파일이 셋이 되면 어느 것이 정본인지 흐려진다.
VERDICT_SAMPLES_DIR = Path(__file__).parent.parent / "tests" / "contract"
_SAMPLE_PREFIX = "content-verdict"

DEFAULT_CATEGORY_PATH = ["강아지", "건강관리", "영양제"]

app = FastAPI(title="aechak-ai demo console", docs_url=None, redoc_url=None)


class InspectBody(BaseModel):
    """콘솔이 보내는 입력. ``mode`` 의 기본값이 재생인 것이 안전장치다."""

    mode: Literal["replay", "mock", "live"] = "replay"
    scenario: str | None = None
    target: TargetType = TargetType.PRODUCT
    productName: str = "관절 영양제"
    description: str = ""
    optionNames: list[str] = Field(default_factory=list)
    categoryPath: list[str] = Field(default_factory=lambda: list(DEFAULT_CATEGORY_PATH))
    content: str = ""
    rating: int = 5
    enableTriage: bool = True


class VerdictBody(BaseModel):
    """기록된 판정을 다시 그리는 입력. 표본 이름이나 판정 JSON 본문을 받는다."""

    sample: str | None = None
    payload: dict[str, Any] | None = None


@app.get("/", response_class=HTMLResponse)
async def console() -> HTMLResponse:
    return HTMLResponse(CONSOLE_HTML.read_text(encoding="utf-8"))


@app.get("/health")
async def health() -> JSONResponse:
    """데모 프로세스가 떴는지만 본다. 운영 헬스체크(``/health`` on 8000)와는 다른 포트다."""
    return JSONResponse({"status": "UP", "mode": "demo-console"})


@app.get("/api/scenarios")
async def scenarios() -> JSONResponse:
    settings = Settings()
    return JSONResponse(
        {
            "scenarios": [
                {
                    "id": scenario.id,
                    "title": scenario.title,
                    "expectedExit": scenario.expected_exit,
                    "note": scenario.note,
                    "target": scenario.target.value,
                    "text": scenario.text,
                    "enableTriage": scenario.enable_triage,
                    "llmDisabled": scenario.llm_disabled,
                }
                for scenario in SCENARIOS
            ],
            "samples": sorted(p.name for p in VERDICT_SAMPLES_DIR.glob(f"{_SAMPLE_PREFIX}*.json")),
            # 키가 없으면 라이브 버튼 자체를 내린다. 기본은 언제나 재생이다.
            "liveAvailable": bool(settings.anthropic_api_key),
            "defaults": {
                "triageModel": settings.triage_model,
                "screeningModel": settings.screening_model,
                "escalationModel": settings.escalation_model,
                "enableTriage": settings.enable_triage,
                "krwPerUsd": view.KRW_PER_USD,
            },
        }
    )


@app.post("/api/inspect")
async def inspect(body: InspectBody) -> JSONResponse:
    if body.mode == "replay":
        scenario = BY_ID.get(body.scenario or "")
        if scenario is None:
            return _error(f"모르는 시나리오다. scenario={body.scenario}")
        return await _run(
            request=scenario.to_request(),
            llm=None if scenario.llm_disabled else ReplayLlmClient(entries=dict(scenario.entries)),
            enable_triage=scenario.enable_triage,
            mode="replay",
            source=scenario.id,
        )

    request = _request_from(body)
    if not request.inspect_text().strip():
        return _error("본문이 비어 있다. 상품 설명이나 리뷰 내용을 넣어라")

    if body.mode == "mock":
        return await _run(
            request=request,
            llm=MockLlmClient(),
            enable_triage=body.enableTriage,
            mode="mock",
            source="custom",
        )

    settings = Settings()
    if not settings.anthropic_api_key:
        # 키 이름이 ANTHROPIC_API_KEY 가 아니라 이것이다(Settings 의 env_prefix 가 AECHAK_AI_).
        return _error("AECHAK_AI_ANTHROPIC_API_KEY 가 없어 라이브 모드를 쓸 수 없다. 재생이나 모의로 돌려라")
    llm = LlmClient(api_key=settings.anthropic_api_key, timeout_seconds=settings.request_timeout_seconds)
    try:
        return await _run(
            request=request,
            llm=llm,
            enable_triage=body.enableTriage,
            mode="live",
            source="custom",
        )
    finally:
        await llm.aclose()


@app.post("/api/verdict")
async def replay_verdict(body: VerdictBody) -> JSONResponse:
    """기록된 판정을 추적 없이 다시 그린다.

    **얕은 재생이다.** 모양은 되살아나지만 이유는 상당 부분 남지 않는다 -- 무엇이 비는지를
    화면 모델이 ``missingWithoutTrace`` 로 함께 내려 콘솔이 그 목록을 띄운다.
    """
    raw: dict[str, Any]
    if body.payload is not None:
        raw = body.payload
        source = "붙여 넣은 판정"
    elif body.sample:
        name = Path(body.sample).name
        if not name.startswith(_SAMPLE_PREFIX) or not name.endswith(".json"):
            return _error(f"판정 표본이 아니다. sample={body.sample}")
        path = VERDICT_SAMPLES_DIR / name
        if not path.is_file():
            return _error(f"표본을 찾을 수 없다. sample={name}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        source = name
    else:
        return _error("sample 이나 payload 가 필요하다")

    try:
        verdict = ContentVerdict.model_validate(raw)
    except ValueError as error:
        return _error(f"판정 계약을 읽을 수 없다: {error}")

    built = view.build(verdict, mode="verdict", source=source)
    return JSONResponse({"view": built.as_dict(), "verdict": json.loads(verdict.model_dump_json())})


async def _run(
    *,
    request: ProductInspectionRequest | ReviewInspectionRequest,
    llm: Any,
    enable_triage: bool,
    mode: str,
    source: str,
) -> JSONResponse:
    settings = Settings().model_copy(update={"enable_triage": enable_triage})
    recorder = TraceRecorder()
    pipeline = InspectionPipeline(llm=llm, settings=settings, recorder=recorder)
    try:
        verdict = await pipeline.run(request)
    except CassetteMiss as miss:
        # 조용히 실패로 접지 않는다. 선별과 비평이 실패를 각각 승급과 유지로 접기 때문에
        # 빠진 카세트를 실패로 돌리면 콘솔이 거짓 단계를 그린다.
        return _error(str(miss))

    built = view.build(verdict, mode=mode, source=source, trace=recorder.trace)
    return JSONResponse({"view": built.as_dict(), "verdict": json.loads(verdict.model_dump_json())})


def _request_from(body: InspectBody) -> ProductInspectionRequest | ReviewInspectionRequest:
    """콘솔 입력을 백엔드가 보내는 요청과 같은 모양으로 접는다.

    카테고리를 받는 이유가 있다. 모델이 그것으로 위생용품의 에탄올과 간식의 알코올을
    가르기 때문이다(SCRUM-276 결정). 카테고리를 비우면 그 판단 재료가 사라진다.
    """
    category_path = body.categoryPath or list(DEFAULT_CATEGORY_PATH)
    if body.target is TargetType.PRODUCT:
        return ProductInspectionRequest(
            inspectionId=9999,
            productId=42,
            productVersionNo=1,
            contentHash=f"{9999:064d}",
            productName=body.productName,
            description=body.description,
            optionNames=body.optionNames,
            categoryPath=category_path,
            imageUrls=[],
        )
    return ReviewInspectionRequest(
        inspectionId=9999,
        reviewId=77,
        productId=42,
        contentHash=f"{9999:064d}",
        content=body.content,
        rating=body.rating,
        categoryPath=category_path,
    )


def _error(message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=400)


def scenario_of(scenario_id: str) -> Scenario | None:
    """테스트와 스크립트가 시나리오를 집어 올 때 쓴다."""
    return BY_ID.get(scenario_id)
