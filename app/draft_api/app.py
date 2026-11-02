"""초안 API. 셀러가 화면에서 기다리는 **동기** 요청 하나를 받는다.

## 왜 카프카가 아닌가

초안은 셀러가 버튼을 누르고 기다리는 요청이다. 카프카 요청·응답으로 만들면 셋이 따라온다.

1. FE 가 폴링해야 한다.
2. ``app/kafka/runner.py`` 의 ``_REQUEST_TYPES`` 가 모르는 eventType 을 **경고 후 ``return``
   하고 바깥 루프가 그대로 커밋해** 구버전 AI 가 그 이벤트를 **영구히 버린다** -- 배포 순서가
   데이터 손실을 만든다.
3. 검수 컨슈머의 ``worst_case_handle_seconds()`` 와 ``kafka_max_poll_interval_ms`` 관계에
   초안 호출 시간이 들어가 리밸런스가 **검수**를 재배달한다.

## 왜 운영 앱에 라우트를 더하지 않는가

``app/main.py`` 가 공개 ALB 뒤라서 관리용 경로를 열면 외부에서 닿는다고 적어 뒀고
``docs_url`` 까지 끈 상태다. 그 판단을 뒤집지 않고 별 앱·별 포트로 간다. 그래서 운영 앱의
라우트 집합은 ``/health`` 하나로 **불변**이고, 테스트가 그 사실을 단언한다.

## 방어를 인프라 한 겹에 걸지 않는다

노출은 내부 리스너나 서비스 디스커버리로 셀러센터 BE 만 닿게 하고, **거기에 더해** 이 앱이
공유 비밀 헤더를 직접 검증한다. 리스너를 잘못 매면 조용히 열린 문이 되는 것을 ``main.py``
머리글이 이미 말하고 있다.

**비밀이 비어 있으면 전건을 거부한다(fail closed).** SSM 파라미터가 빠진 배포에서 인증 없는
경로가 열리는 쪽이, 기능이 안 되는 쪽보다 나쁜 실패다. 그리고 비밀 검사를 기능 토글보다
**먼저** 본다 -- 인증 없는 호출자에게 기능이 켜졌는지 알려 줄 이유가 없다.

## 카프카 컨슈머를 띄우지 않는다

설정으로 끄는 것이 아니라 **이 앱의 lifespan 이 러너를 아예 만들지 않는다.** 설정 플래그로
두면 그 플래그를 읽는 자리가 하나 더 생기고 잘못 켜질 수 있다. 구조로 가르는 것이
설정으로 가르는 것보다 강하다.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from app.llm.client import LlmClient, validate_pricing
from app.schemas.draft import SellerDraftRequest
from app.seller_draft.prompt import draft_version_string
from app.seller_draft.service import DraftService, DraftUnprocessable
from app.settings import settings

log = logging.getLogger(__name__)

DRAFT_PATH = "/internal/seller-draft"
SECRET_HEADER = "x-aechak-draft-secret"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    # 설정 오타를 돈 쓰기 전에 잡는다. 초안 모델이 ``model_ids()`` 에 들어 있어서 같은
    # 검증을 받는다 -- 안 넣으면 오타 난 모델 ID 가 최고 단가로 조용히 15배 계상된다.
    validate_pricing(settings.model_ids())

    llm = (
        LlmClient(api_key=settings.anthropic_api_key, timeout_seconds=settings.draft_timeout_seconds)
        if settings.anthropic_api_key
        else None
    )
    if llm is None:
        log.warning("ANTHROPIC API 키가 없어 초안 앱이 stub 모드로 뜬다. 모델을 부르지 않는다.")
    if not settings.draft_shared_secret:
        log.error(
            "AECHAK_AI_DRAFT_SHARED_SECRET 이 비어 있다. 초안 경로는 **전건을 거부한다.** "
            "SSM 에 비밀을 등재해야 한다"
        )
    app.state.llm = llm
    app.state.service = DraftService(llm=llm, settings=settings)
    try:
        yield
    finally:
        if llm is not None:
            await llm.aclose()


# ``openapi_url=None`` 까지 끈다. **방어를 인프라 한 겹에 걸지 않는다**는 이 모듈의 원칙이
# 이 한 겹에도 적용된다 -- ``/openapi.json`` 은 ``docs_url`` 을 꺼도 FastAPI 가 등록하고,
# 거기에 ``/internal/seller-draft`` 의 전체 요청 모양(lockedFields·이미지 필드·계약 버전)이
# 실린다. 운영 앱은 ``/health`` 하나라 그 노출이 무해했지만 이 앱은 아니다. 공유 비밀이
# 호출을 막지만, 열어 두어서 얻는 것이 없다.
app = FastAPI(
    title="aechak-ai seller draft",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health")
async def health() -> JSONResponse:
    """이 프로세스가 떴는지만 본다. 컨슈머가 없으므로 소비 축이 없다."""
    return JSONResponse(
        {
            "status": "UP",
            "role": "seller-draft",
            "draftEnabled": settings.enable_draft,
            "draftVersion": draft_version_string(),
            "imageMode": settings.draft_image_mode,
            "secretConfigured": bool(settings.draft_shared_secret),
            "schemaVersion": settings.supported_schema_version,
        }
    )


@app.post(DRAFT_PATH)
async def seller_draft(
    body: SellerDraftRequest,
    request: Request,
    x_aechak_draft_secret: str = Header(default=""),
) -> JSONResponse:
    if not _authorized(x_aechak_draft_secret):
        # 왜 거부됐는지 자세히 적지 않는다. 비밀이 비어 있는지 틀렸는지를 알려 주면
        # 미설정 배포를 찾는 데 쓸 수 있다.
        return JSONResponse({"error": "허용되지 않은 호출이다"}, status_code=403)

    if body.schemaVersion != settings.supported_schema_version:
        # 모르는 계약 버전을 떨어뜨리지 않고 거절한다. ``runner.py`` 와 같은 규약이다.
        return JSONResponse(
            {
                "error": (
                    f"지원하지 않는 계약 버전이다. got={body.schemaVersion} "
                    f"supported={settings.supported_schema_version}"
                )
            },
            status_code=409,
        )

    if not settings.enable_draft:
        # 기본 꺼짐이다. 503 으로 돌려 셀러센터가 **폼을 빈 상태로 그대로 두게** 한다 --
        # 초안 실패가 셀러의 등록을 막아서는 안 된다. 초안은 보조 기능이다.
        return JSONResponse(
            {"error": "초안 기능이 꺼져 있다. AECHAK_AI_ENABLE_DRAFT 를 켜야 한다"},
            status_code=503,
        )

    service: DraftService = request.app.state.service
    try:
        response = await service.run(body)
    except DraftUnprocessable as error:
        payload: dict[str, object] = {"error": str(error)}
        if error.max_long_edge_px is not None:
            # 셀러센터가 고쳐 보낼 수 있는 수를 적어 돌려준다. AI 서버는 리사이즈하지 않는다.
            payload["maxLongEdgePx"] = error.max_long_edge_px
        return JSONResponse(payload, status_code=422)

    return JSONResponse(response.model_dump(mode="json"))


def _authorized(provided: str) -> bool:
    """공유 비밀을 상수 시간으로 비교한다. **비어 있으면 거부한다.**"""
    expected = settings.draft_shared_secret
    if not expected:
        return False
    return hmac.compare_digest(provided, expected)


__all__ = ["DRAFT_PATH", "SECRET_HEADER", "app"]
