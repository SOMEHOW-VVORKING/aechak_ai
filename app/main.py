"""FastAPI 진입점.

HTTP로는 헬스체크만 공개한다. 판정은 전부 카프카로 들어오고, 공개 ALB 뒤에 있으므로
관리용 경로를 열어 두면 이름이 internal이어도 외부에서 닿는다.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.inspection.pipeline import InspectionPipeline
from app.kafka.runner import InspectionRunner
from app.kafka.summary_runner import SummaryRunner
from app.llm.client import LlmClient, validate_pricing
from app.review_summary.service import SummaryService
from app.settings import settings

log = logging.getLogger(__name__)


def _silence_external_tracing() -> None:
    """추적 환경변수를 눌러 둔다.

    레거시 변수 하나만 상속돼도 추적이 켜져 리뷰 본문과 상품 설명이 외부로 나간다.
    이 서버는 오케스트레이션 프레임워크를 쓰지 않아 지금은 무해하지만,
    나중에 누가 의존성을 더했을 때 기본값이 켜짐이 되는 것을 막는다.
    """
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_ENDPOINT"):
        os.environ[name] = "false" if name.endswith("TRACING") or name.endswith("V2") else ""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    _silence_external_tracing()

    # 설정 오타를 돈 쓰기 전에 잡는다. 가격표에 없는 모델은 비용이 과대 계상되고,
    # 그 전에 설정 오타가 더 흔한 원인이므로 기동을 거부한다.
    validate_pricing(settings.model_ids())

    llm = (
        LlmClient(
            api_key=settings.anthropic_api_key,
            timeout_seconds=settings.request_timeout_seconds,
        )
        if settings.anthropic_api_key
        else None
    )
    if llm is None:
        log.warning("ANTHROPIC API 키가 없어 stub 모드로 뜬다. 모델을 부르지 않는다.")

    pipeline = InspectionPipeline(llm=llm, settings=settings)
    runner = InspectionRunner(settings=settings, pipeline=pipeline)
    app.state.runner = runner
    await runner.start()

    # 요약은 **기본 꺼짐**이다(``enable_triage`` 선례 -- 구현하고 측정하고 기본은 끈다).
    # 켜면 전용 토픽·전용 컨슈머 그룹으로 별 러너가 붙는다. 같은 프로세스이긴 하므로
    # 요약 쪽 버그가 검수까지 세울 수 있고, 프로세스 분리는 아직 하지 않았다.
    summary_runner: SummaryRunner | None = None
    if settings.enable_review_summary:
        summary_runner = SummaryRunner(
            settings=settings,
            service=SummaryService(llm=llm, settings=settings),
        )
        await summary_runner.start()
    app.state.summary_runner = summary_runner

    try:
        yield
    finally:
        if summary_runner is not None:
            await summary_runner.stop()
        await runner.stop()
        if llm is not None:
            await llm.aclose()


app = FastAPI(title="aechak-ai", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/health")
async def health() -> JSONResponse:
    """소비 루프까지 살아 있는지 본다. 프로세스 생존만 보면 컨슈머가 죽은 것을 놓친다."""
    runner: InspectionRunner | None = getattr(app.state, "runner", None)
    consuming = runner.healthy if runner is not None else False

    # 요약 컨슈머는 **별 축으로 보고한다.** ``consuming`` 에 AND 로 섞으면 요약 러너가
    # 죽었을 때 검수가 멀쩡한데도 전체가 DOWN 이 되어 ECS 가 태스크를 교체하고,
    # 요약을 끈 배포에서는 그 필드가 거짓이 되어 헬스체크가 영구 503 이 된다.
    summary_runner: SummaryRunner | None = getattr(app.state, "summary_runner", None)
    summary_consuming = summary_runner.healthy if summary_runner is not None else False
    status = 200 if consuming else 503
    return JSONResponse(
        status_code=status,
        content={
            "status": "UP" if consuming else "DOWN",
            "consuming": consuming,
            "reviewSummaryEnabled": settings.enable_review_summary,
            "reviewSummaryConsuming": summary_consuming,
            "promptVersion": settings.prompt_version,
            "schemaVersion": settings.supported_schema_version,
        },
    )
