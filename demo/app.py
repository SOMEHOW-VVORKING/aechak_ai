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
from app.review_summary.service import SummaryService
from app.review_summary.trace import SummaryTraceRecorder
from app.schemas.draft import SellerDraftRequest
from app.schemas.inspection import (
    ContentVerdict,
    ProductInspectionRequest,
    ReviewInspectionRequest,
    TargetType,
)
from app.schemas.summary import RequestedScope, ReviewInput, ReviewSummaryRequest, SummaryScope
from app.seller_draft.service import DraftService, DraftUnprocessable
from app.seller_draft.trace import DraftTraceRecorder
from app.settings import Settings
from demo import draft_view, summary_view, view
from demo.cassettes import BY_ID, SCENARIOS, Scenario
from demo.draft_cassettes import DRAFT_BY_ID, DRAFT_SCENARIOS, DraftScenario, draft_image
from demo.mock import MockLlmClient
from demo.replay import CassetteMiss, ReplayLlmClient, SequencedReplayClient
from demo.summary_cassettes import SUMMARY_BY_ID, SUMMARY_SCENARIOS
from demo.summary_mock import MockSummaryLlmClient

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
    # 운영 기본값을 그대로 쓴다. 여기에 True 를 박아 두면 콘솔이 보내지 않을 때 배포된
    # 동작과 다른 경로가 돌고, 두 자리의 기본값이 갈라진다.
    enableTriage: bool = Field(default_factory=lambda: Settings().enable_triage)


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
                "noticeModel": settings.notice_model,
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
            # 시나리오가 파이프라인 모양 전부를 든다. 앰비언트 환경을 한 칸도 읽지 않는다.
            settings=scenario.settings(),
            mode="replay",
            source=scenario.id,
            expected_exit=scenario.expected_exit,
        )

    request = _request_from(body)
    if not request.inspect_text().strip():
        return _error("본문이 비어 있다. 상품 설명이나 리뷰 내용을 넣어라")

    if body.mode == "mock":
        return await _run(
            request=request,
            llm=MockLlmClient(),
            settings=Settings().model_copy(update={"enable_triage": body.enableTriage}),
            mode="mock",
            source="custom",
        )

    settings = Settings()
    if not settings.anthropic_api_key:
        # 키 이름이 ANTHROPIC_API_KEY 가 아니라 이것이다(Settings 의 env_prefix 가 AECHAK_AI_).
        return _error("AECHAK_AI_ANTHROPIC_API_KEY 가 없어 라이브 모드를 쓸 수 없다. 재생이나 모의로 돌려라")
    llm = LlmClient(
        api_key=settings.anthropic_api_key,
        timeout_seconds=settings.request_timeout_seconds,
        local_base_url=settings.local_llm_base_url,
    )
    try:
        return await _run(
            request=request,
            llm=llm,
            settings=settings.model_copy(update={"enable_triage": body.enableTriage}),
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


class SummaryBody(BaseModel):
    """요약 콘솔이 보내는 입력.

    ``mode`` 의 기본값이 재생인 것이 안전장치다 -- 검수 콘솔과 같은 논리이고, 사람이
    발표 중에 실수로 유료 경로를 밟지 않는 쪽이 기본이어야 한다.

    **``mock`` 이 검수 탭에만 있고 여기 없던 것이 비대칭이었다.** 키가 없으면 라이브가
    내려가므로 요약은 적어 둔 시나리오 9개밖에 돌 수 없었고, 사람이 자기 리뷰를 타이핑해
    결과를 보는 일이 이 기능에서만 불가능했다.
    """

    mode: Literal["replay", "mock", "live"] = "replay"
    scenario: str | None = None
    # 모의·라이브 모드에서 사람이 직접 넣는 리뷰. "5|본문" 한 줄에 한 건이다.
    reviews: list[str] = Field(default_factory=list)
    categoryPath: list[str] = Field(default_factory=lambda: ["강아지", "사료", "건식사료"])
    basisReviewCount: int = 20


@app.get("/api/summary/scenarios")
async def summary_scenarios() -> JSONResponse:
    settings = Settings()
    return JSONResponse(
        {
            "scenarios": [
                {
                    "id": scenario.id,
                    "title": scenario.title,
                    "note": scenario.note,
                    "expectedExit": scenario.expected_exit,
                    "reviews": len(scenario.reviews),
                    "llmDisabled": scenario.llm_disabled,
                }
                for scenario in SUMMARY_SCENARIOS
            ],
            "liveAvailable": bool(settings.anthropic_api_key),
            "defaults": {
                "extractModel": settings.summary_extract_model,
                "narrateModel": settings.summary_narrate_model,
                "minReviews": settings.summary_min_reviews,
                "minSegmentReviews": settings.summary_min_segment_reviews,
                "minAspectMentions": settings.summary_min_aspect_mentions,
                "minAspectShare": settings.summary_min_aspect_share,
                "contentChars": settings.summary_content_chars,
                "enabled": settings.enable_review_summary,
                # 환율을 다시 적지 않는다. demo/view 의 것을 그대로 내려보낸다.
                "krwPerUsd": view.KRW_PER_USD,
            },
        }
    )


@app.post("/api/summary")
async def summarize(body: SummaryBody) -> JSONResponse:
    if body.mode == "replay":
        scenario = SUMMARY_BY_ID.get(body.scenario or "")
        if scenario is None:
            return _error(f"모르는 요약 시나리오다. scenario={body.scenario}")
        return await _run_summary(
            request=scenario.to_request(),
            llm=(None if scenario.llm_disabled else SequencedReplayClient(entries=dict(scenario.entries))),
            # 시나리오가 임계와 모델 전부를 든다. 앰비언트 환경을 한 칸도 읽지 않는다.
            settings=scenario.settings(),
            mode="replay",
            source=scenario.id,
            expected_exit=scenario.expected_exit,
        )

    reviews = _parse_reviews(body.reviews)
    if not reviews:
        return _error('리뷰를 한 줄에 한 건씩 "별점|본문" 모양으로 넣어라. 예: 5|잘 먹어요')

    # **분모를 사람 입력에서 그대로 믿지 않는다.** 이 자리가 레포 1순위 불변식을 깼다.
    #
    # 앞 판은 ``totalVisibleReviews`` 와 ``basisReviewCount`` 양쪽에 사람이 자유 입력하는
    # 수(기본 20)를 박고 ``unextractedReviewCount=0`` 을 하드코딩했다. 실제 투입 리뷰는
    # textarea 줄 수(기본 5)다. 그래서 **콘솔 출고 기본 상태에서** 분석 20건 / 못 읽은
    # 리뷰 0건이 나왔고, ``summary_min_reviews`` 임계도 그 조작된 분모가 통과시켜 서술
    # 호출까지 났다. 긍정 리뷰 3건이면 ``NO_NEGATIVE_MENTIONS_FOUND`` -- 17건을 한 글자도
    # 읽지 않고 "단점 언급을 찾지 못했다"로 내보내는 것이고, 서비스에서 없앤 바로 그 결함이
    # 입력 계층에서 되살아난 모양이다.
    #
    # 분모를 입력으로 남겨 두는 것 자체는 쓸모가 있다 -- "전체 리뷰 1,000건 가운데 신규
    # 12건만 추출됐다"는 상황을 사람이 재현해 볼 수 있어야 한다. 그래서 받되 **못 읽은
    # 리뷰 수를 그 차이로 채우고**, 분모가 투입 리뷰보다 작으면 거부한다(음수가 된다).
    basis = max(body.basisReviewCount, 0)
    if basis < len(reviews):
        return _error(
            f"분모({basis})가 투입 리뷰({len(reviews)}건)보다 작다. "
            "분모는 '이 상품의 전체 리뷰 수'이고 투입 리뷰는 그 가운데 지금 추출할 신규 건이다"
        )
    request = ReviewSummaryRequest(
        requestId=f"demo-{body.mode}",
        productId=42,
        categoryPath=body.categoryPath,
        newReviews=reviews,
        requestedScopes=[
            RequestedScope(
                scope=SummaryScope.PRODUCT,
                basisReviewCount=basis,
                # 분모에서 지금 투입한 것을 뺀 나머지는 **한 글자도 읽지 않았다.**
                unextractedReviewCount=basis - len(reviews),
                tally=[],
            )
        ],
    )

    if body.mode == "mock":
        # 대역은 **모델이 아니다.** 키워드 표로 답하고 비용이 0이다. 임계와 모델 ID 는
        # 운영 기본값을 그대로 쓴다 -- 콘솔이 보여 주는 임계와 돌아가는 임계가 같아야 한다.
        return await _run_summary(
            request=request,
            llm=MockSummaryLlmClient(),
            settings=Settings(),
            mode="mock",
            source="custom",
        )

    settings = Settings()
    if not settings.anthropic_api_key:
        return _error("AECHAK_AI_ANTHROPIC_API_KEY 가 없어 라이브 모드를 쓸 수 없다. 재생이나 모의로 돌려라")
    llm = LlmClient(api_key=settings.anthropic_api_key, timeout_seconds=settings.summary_timeout_seconds)
    try:
        return await _run_summary(
            request=request,
            llm=llm,
            settings=settings,
            mode="live",
            source="custom",
        )
    finally:
        await llm.aclose()


def _parse_reviews(lines: list[str]) -> list[ReviewInput]:
    """``"5|잘 먹어요"`` 한 줄을 리뷰 한 건으로 접는다. 별점을 빼면 5점으로 둔다."""
    reviews: list[ReviewInput] = []
    for index, line in enumerate(lines, start=1):
        text = line.strip()
        if not text:
            continue
        rating = 5
        if "|" in text:
            head, _, rest = text.partition("|")
            if head.strip().isdigit():
                rating = max(1, min(5, int(head.strip())))
                text = rest.strip()
        if text:
            reviews.append(ReviewInput(reviewId=index, rating=rating, content=text))
    return reviews


async def _run_summary(
    *,
    request: ReviewSummaryRequest,
    llm: Any,
    settings: Settings,
    mode: str,
    source: str,
    expected_exit: str | None = None,
) -> JSONResponse:
    recorder = SummaryTraceRecorder()
    service = SummaryService(llm=llm, settings=settings, recorder=recorder)
    try:
        message = await service.run(request)
    except CassetteMiss as miss:
        # 조용히 실패로 접지 않는다. 추출 실패는 UNEXTRACTED 이고 서술 실패는
        # narrateFailure 라서, 빠진 카세트를 실패로 돌리면 콘솔이 거짓 단계를 그린다.
        return _error(str(miss))

    built = summary_view.build(
        message,
        mode=mode,
        source=source,
        reviews_in=len(request.newReviews),
        trace=recorder.trace,
    )
    if expected_exit is not None and built.exit_point != expected_exit:
        # 틀린 그림을 정상 응답으로 내보내지 않는다. 사이드바에 시나리오 설명과
        # expected_exit 가 떠 있으므로 다른 경로를 그리면 화면이 자기를 부정한다.
        return _error(
            f"시나리오가 적어 둔 경로와 다르게 돌았다. 적어 둔 곳={expected_exit} "
            f"실제={built.exit_point}. 카세트가 시나리오와 어긋났을 수 있다"
        )
    return JSONResponse({"view": built.as_dict(), "message": json.loads(message.model_dump_json())})


async def _run(
    *,
    request: ProductInspectionRequest | ReviewInspectionRequest,
    llm: Any,
    settings: Settings,
    mode: str,
    source: str,
    expected_exit: str | None = None,
) -> JSONResponse:
    recorder = TraceRecorder()
    pipeline = InspectionPipeline(llm=llm, settings=settings, recorder=recorder)
    try:
        verdict = await pipeline.run(request)
    except CassetteMiss as miss:
        # 조용히 실패로 접지 않는다. 선별과 비평이 실패를 각각 승급과 유지로 접기 때문에
        # 빠진 카세트를 실패로 돌리면 콘솔이 거짓 단계를 그린다.
        return _error(str(miss))

    built = view.build(verdict, mode=mode, source=source, trace=recorder.trace)
    mismatch = _verify_expected_exit(built.exit_point, expected_exit)
    if mismatch is not None:
        # 틀린 그림을 정상 응답으로 내보내지 않는다. 사이드바에는 시나리오의 설명과
        # expected_exit 가 떠 있으므로, 다른 경로를 그리면 화면이 자기를 부정한다.
        return _error(mismatch)
    return JSONResponse({"view": built.as_dict(), "verdict": json.loads(verdict.model_dump_json())})


def _verify_expected_exit(actual: str, expected: str | None) -> str | None:
    """시나리오가 적어 둔 경로로 돌았는지 본다. 어긋나면 오류 문구를, 맞으면 None."""
    if expected is None or actual == expected:
        return None
    return (
        f"시나리오가 적어 둔 경로와 다르게 돌았다. 적어 둔 곳={expected} 실제={actual}. "
        "앰비언트 환경이 파이프라인 모양을 바꿨거나 카세트가 시나리오와 어긋났다"
    )


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


# --- 셀러 폼 초안(SCRUM-311) ---------------------------------------------------------
#
# **두 입력 방식을 나란히 돌린다.** 사람이 아직 (가)와 (나) 중 무엇을 켤지 고르지 못했고,
# 고르려면 비용·지연·초안을 같은 입력에서 나란히 봐야 한다. 두 방식이 별 코드 경로가
# 아니라 이미지 블록 하나의 유무만 다르다는 것이 이 비교를 공정하게 만든다.


class DraftBody(BaseModel):
    """초안 콘솔이 보내는 입력. ``mode`` 의 기본값이 재생인 것이 안전장치다."""

    mode: Literal["replay", "live"] = "replay"
    scenario: str | None = None
    productName: str = "강아지 목줄 M 네이비"
    categoryId: int | None = None
    # 라이브에서 두 방식을 모두 돌릴지. 재생은 시나리오가 가진 카세트가 정한다.
    withImage: bool = False


@app.get("/api/draft/scenarios")
async def draft_scenarios() -> JSONResponse:
    settings = Settings()
    return JSONResponse(
        {
            "scenarios": [
                {
                    "id": scenario.id,
                    "title": scenario.title,
                    "note": scenario.note,
                    "productName": scenario.product_name,
                    "categoryId": scenario.category_id,
                    "expectedExit": scenario.expected_exit,
                    "expectedImageExit": scenario.expected_image_exit,
                    "hasImageVariant": scenario.image_entries is not None,
                    "imagePx": scenario.image_px,
                }
                for scenario in DRAFT_SCENARIOS
            ],
            "liveAvailable": bool(settings.anthropic_api_key),
            "defaults": {
                "draftModel": settings.draft_model,
                "imageMode": settings.draft_image_mode,
                "imageMaxTokens": settings.draft_image_max_tokens,
                "descriptionChars": settings.draft_description_chars,
                "maxTokens": settings.max_tokens_draft,
                "timeoutSeconds": settings.draft_timeout_seconds,
                "enabled": settings.enable_draft,
                # 환율을 다시 적지 않는다. demo/view 의 것을 그대로 내려보낸다.
                "krwPerUsd": view.KRW_PER_USD,
            },
        }
    )


@app.post("/api/draft")
async def draft(body: DraftBody) -> JSONResponse:
    if body.mode == "replay":
        scenario = DRAFT_BY_ID.get(body.scenario or "")
        if scenario is None:
            return _error(f"모르는 초안 시나리오다. scenario={body.scenario}")
        return await _run_draft_compare(scenario)

    settings = Settings()
    if not settings.anthropic_api_key:
        return _error("AECHAK_AI_ANTHROPIC_API_KEY 가 없어 라이브 모드를 쓸 수 없다. 재생으로 돌려라")
    if not body.productName.strip():
        return _error("상품명을 넣어라. 초안을 만들 입력이 없다")

    llm = LlmClient(api_key=settings.anthropic_api_key, timeout_seconds=settings.draft_timeout_seconds)
    try:
        text = await _run_draft_one(
            llm=llm,
            settings=settings.model_copy(update={"enable_draft": True, "draft_image_mode": "OFF"}),
            product_name=body.productName,
            category_id=body.categoryId,
            label="(가) 이름 + 카테고리",
            mode="live",
            with_image=False,
        )
        image = None
        if body.withImage:
            image = await _run_draft_one(
                llm=llm,
                settings=settings.model_copy(update={"enable_draft": True, "draft_image_mode": "ALWAYS"}),
                product_name=body.productName,
                category_id=body.categoryId,
                label="(나) + 사진 1장",
                mode="live",
                with_image=True,
            )
    finally:
        await llm.aclose()

    if isinstance(text, JSONResponse):
        return text
    if isinstance(image, JSONResponse):
        return image
    built = draft_view.build_compare(
        product_name=body.productName,
        category_given=body.categoryId,
        mode="live",
        source="custom",
        text=text,
        image=image,
    )
    return JSONResponse({"view": built.as_dict()})


async def _run_draft_compare(scenario: DraftScenario) -> JSONResponse:
    """시나리오를 두 변형으로 돌린다. **변형마다 새 클라이언트와 새 서비스를 쓴다.**

    서비스를 공유하면 재사용 캐시가 둘째 변형을 가로채고, 클라이언트를 공유하면 카세트
    순번이 이어져 둘째 변형이 첫째의 응답을 받는다.
    """
    text = await _run_draft_variant(scenario, with_image=False, label="(가) 이름 + 카테고리")
    if isinstance(text, JSONResponse):
        return text

    image: draft_view.DraftVariantView | None = None
    if scenario.image_entries is not None:
        result = await _run_draft_variant(scenario, with_image=True, label="(나) + 사진 1장")
        if isinstance(result, JSONResponse):
            return result
        image = result

    built = draft_view.build_compare(
        product_name=scenario.product_name,
        category_given=scenario.category_id,
        mode="replay",
        source=scenario.id,
        text=text,
        image=image,
    )
    return JSONResponse({"view": built.as_dict()})


async def _run_draft_variant(
    scenario: DraftScenario, *, with_image: bool, label: str
) -> draft_view.DraftVariantView | JSONResponse:
    expected = scenario.expected_image_exit if with_image else scenario.expected_exit
    settings = scenario.settings(with_image=with_image)
    entries = scenario.image_entries if with_image else scenario.entries
    recorder = DraftTraceRecorder()
    service = DraftService(
        llm=(None if scenario.llm_disabled else SequencedReplayClient(entries=dict(entries or {}))),
        settings=settings,
        recorder=recorder,
    )
    try:
        response = await service.run(scenario.to_request(with_image=with_image))
    except CassetteMiss as miss:
        # 조용히 실패로 접지 않는다. 호출 실패는 EMPTY_CALL_FAILED 라서 빠진 카세트를
        # 실패로 돌리면 콘솔이 거짓 단계를 그린다.
        return _error(str(miss))
    except DraftUnprocessable as error:
        # 거부도 보여 줘야 하는 그림이다 -- 셀러센터가 사진을 줄여 다시 보내야 한다는 사실이
        # 응답에 있다. **적어 둔 경로 검사를 똑같이 받는다**(아래 공통 분기).
        built = _unprocessable_variant(label, error)
    else:
        built = draft_view.build_variant(
            response,
            label=label,
            mode="replay" if settings.anthropic_api_key == "" else "live",
            trace=recorder.trace,
        )
    if expected is not None and expected != built.exit_point:
        # 틀린 그림을 정상 응답으로 내보내지 않는다. 사이드바에 시나리오 설명과
        # expected_exit 가 떠 있으므로 다른 경로를 그리면 화면이 자기를 부정한다.
        return _error(
            f"시나리오가 적어 둔 경로와 다르게 돌았다. 변형={label} "
            f"적어 둔 곳={expected} 실제={built.exit_point}. 카세트가 시나리오와 어긋났을 수 있다"
        )
    return built


def _unprocessable_variant(label: str, error: DraftUnprocessable) -> draft_view.DraftVariantView:
    """거부된 입력을 화면 모델로 옮긴다. **빈 성공으로 그리지 않는다.**"""
    from datetime import UTC, datetime

    from app.schemas.draft import (
        DraftState,
        ImageMode,
        ImageReport,
        ImageUsability,
        ItemSignalOut,
        SellerDraftResponse,
    )
    from app.seller_draft.catalog import ItemGrade, ResolvedBy
    from app.seller_draft.prompt import draft_version_string

    detail = str(error)
    if error.max_long_edge_px is not None:
        detail += f" (긴 변 {error.max_long_edge_px}px 이하로 줄여 보내라)"
    response = SellerDraftResponse(
        requestId="demo-unprocessable",
        draftVersion=draft_version_string(),
        state=DraftState.FAILED,
        itemSignal=ItemSignalOut(grade=ItemGrade.OPAQUE, resolvedBy=ResolvedBy.NONE),
        image=ImageReport(usability=ImageUsability.NO_IMAGE_SENT, rejected=True, detail=detail),
        imageMode=ImageMode.ALWAYS,
        failureDetail=detail,
        generatedAt=datetime.now(UTC),
    )
    built = draft_view.build_variant(response, label=label, mode="replay", trace=None)
    return built


async def _run_draft_one(
    *,
    llm: Any,
    settings: Settings,
    product_name: str,
    category_id: int | None,
    label: str,
    mode: str,
    with_image: bool,
) -> draft_view.DraftVariantView | JSONResponse:
    """라이브 변형 하나. 사진은 **합성 헤더**라 모델이 읽을 그림은 없다.

    그 한계를 콘솔이 적는다 -- 라이브에서 사진의 실제 효과를 보려면 하네스의 실제 픽스처가
    필요하고, 여기서 보여 주는 것은 비용 구조와 분기다.
    """
    recorder = DraftTraceRecorder()
    service = DraftService(llm=llm, settings=settings, recorder=recorder)
    request = SellerDraftRequest(
        requestId=f"demo-live-{'image' if with_image else 'text'}",
        productName=product_name,
        categoryId=category_id,
        image=(draft_image() if with_image else None),
    )
    try:
        response = await service.run(request)
    except DraftUnprocessable as error:
        return _unprocessable_variant(label, error)
    return draft_view.build_variant(response, label=label, mode=mode, trace=recorder.trace)
