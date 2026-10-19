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
from app.llm.client import LlmClient
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
    try:
        yield
    finally:
        await runner.stop()
        if llm is not None:
            await llm.aclose()


app = FastAPI(title="aechak-ai", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/health")
async def health() -> JSONResponse:
    """소비 루프까지 살아 있는지 본다. 프로세스 생존만 보면 컨슈머가 죽은 것을 놓친다."""
    runner: InspectionRunner | None = getattr(app.state, "runner", None)
    consuming = runner.healthy if runner is not None else False
    status = 200 if consuming else 503
    return JSONResponse(
        status_code=status,
        content={
            "status": "UP" if consuming else "DOWN",
            "consuming": consuming,
            "promptVersion": settings.prompt_version,
            "schemaVersion": settings.supported_schema_version,
        },
    )
