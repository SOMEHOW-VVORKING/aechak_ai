"""초안 API. **운영 앱의 라우트 집합을 건드리지 않고, 비밀이 없으면 전건을 거부한다.**

``app/main.py`` 가 "공개 ALB 뒤에 있으므로 관리용 경로를 열어 두면 이름이 internal 이어도
외부에서 닿는다"고 적어 뒀다. 그 문서화된 보안 판단을 숫자 없이 뒤집지 않으므로 초안은
별 ASGI 앱이고, **이 테스트가 그 분리를 고정한다.**
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.schemas.draft import DRAFT_SCHEMA_VERSION
from app.settings import Settings

REPO = Path(__file__).parent.parent
SECRET = "test-shared-secret"


@pytest.fixture
def draft_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """비밀과 토글을 켠 앱. ``settings`` 가 모듈 전역이라 속성으로 덮는다."""
    from app.draft_api import app as draft_module

    monkeypatch.setattr(draft_module.settings, "draft_shared_secret", SECRET)
    monkeypatch.setattr(draft_module.settings, "enable_draft", True)
    monkeypatch.setattr(draft_module.settings, "anthropic_api_key", "")
    with TestClient(draft_module.app) as client:
        yield client


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "requestId": "r-1",
        "productName": "강아지 목줄 M",
        "schemaVersion": DRAFT_SCHEMA_VERSION,
    }
    body.update(overrides)
    return body


# --- 운영 앱과의 분리 ---------------------------------------------------------------


def test_운영_앱의_라우트_집합이_헬스체크_하나로_불변이다() -> None:
    """**초안 라우트를 거기 붙이면 공개 ALB 에 인증 없는 경로가 선다.**

    ``/openapi.json`` 은 ``docs_url=None`` 이어도 FastAPI 가 등록한다. 운영 앱은 라우트가
    ``/health`` 하나라 그 스키마에 담길 것이 없어서 분모에서 뺀다 -- **초안 앱은 다르고,
    거기서는 면제하지 않는다**(``test_초안_앱이_openapi를_열지_않는다``). 면제가 노출을
    못 잡은 자리였다.
    """
    from app.main import app as operational_app

    paths = {getattr(route, "path", "") for route in operational_app.routes}
    assert paths - {"/openapi.json"} == {"/health"}, paths
    assert not any(token in path for path in paths for token in ("draft", "seller", "internal", "demo")), (
        paths
    )


def test_운영_앱이_초안_앱을_import_하지_않는다() -> None:
    """두 앱이 같은 프로세스에 서면 분리가 뜻을 잃는다.

    초안 호출(최악 25초)이 컨슈머 폴을 지연시키고, ``worst_case_handle_seconds()`` 와
    ``kafka_max_poll_interval_ms`` 관계를 단언하는 테스트의 계산에 초안이 들어 있지 않다.
    """
    source = (REPO / "app" / "main.py").read_text(encoding="utf-8")

    assert "draft_api" not in source
    assert "seller_draft" not in source


def test_초안_앱이_카프카_컨슈머를_띄우지_않는다() -> None:
    """**설정으로 끄는 것이 아니라 구조로 가른다.**

    설정 플래그로 두면 그 플래그를 읽는 자리가 하나 더 생기고 잘못 켜질 수 있다.
    """
    source = (REPO / "app" / "draft_api" / "app.py").read_text(encoding="utf-8")
    # **import 줄만 본다.** 주석과 머리글은 왜 그렇게 했는지를 설명하느라 그 이름을 쓴다.
    imports = [
        line
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) or line.strip().startswith(("import ", "from "))
    ]

    assert not any("kafka" in line for line in imports), imports
    assert "InspectionRunner" not in "".join(imports)
    assert "SummaryRunner" not in "".join(imports)
    assert not any("runner.start" in line for line in source.splitlines())


def test_초안_앱도_문서_경로를_닫는다() -> None:
    from app.draft_api.app import app as draft_app

    assert draft_app.docs_url is None
    assert draft_app.redoc_url is None


def test_운영_의존성이_늘지_않았다() -> None:
    """초안은 **새 의존성 0**이다.

    Pillow 를 들이면 1500 사방을 896 으로 줄여 건당 2.59원을 아낄 수 있지만(추정), 그 절감을
    운영 이미지 무게와 바꾸지 않는다. ``imagemeta`` 는 stdlib ``struct`` 만 쓴다.
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


def test_초안_코드가_demo_와_harness_를_import_하지_않는다() -> None:
    """Dockerfile 이 ``app`` 과 ``prompts`` 만 COPY 하므로 import 하면 **운영에서 ImportError** 다.

    효능 사전의 후보 표가 ``demo/mock.py`` 에 있어서 특히 밟기 쉬운 자리다.
    """
    offenders: list[str] = []
    for path in (*(REPO / "app" / "seller_draft").rglob("*.py"), *(REPO / "app" / "draft_api").rglob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(("import demo", "from demo", "import harness", "from harness")):
                offenders.append(f"{path.name}: {line}")

    assert offenders == [], offenders


# --- 인증 (fail closed) -------------------------------------------------------------


def test_비밀이_비어_있으면_전건을_거부한다(monkeypatch: pytest.MonkeyPatch) -> None:
    """**SSM 파라미터가 빠진 배포에서 인증 없는 경로가 열리는 쪽이 더 나쁜 실패다.**"""
    from app.draft_api import app as draft_module

    monkeypatch.setattr(draft_module.settings, "draft_shared_secret", "")
    monkeypatch.setattr(draft_module.settings, "enable_draft", True)
    with TestClient(draft_module.app) as client:
        response = client.post(draft_module.DRAFT_PATH, json=_body())

    assert response.status_code == 403


def test_비밀이_틀리면_거부한다(draft_client: Any) -> None:
    from app.draft_api.app import DRAFT_PATH, SECRET_HEADER

    response = draft_client.post(DRAFT_PATH, json=_body(), headers={SECRET_HEADER: "wrong-secret"})

    assert response.status_code == 403


def test_비밀_검사가_기능_토글보다_먼저다(monkeypatch: pytest.MonkeyPatch) -> None:
    """인증 없는 호출자에게 기능이 켜졌는지 알려 줄 이유가 없다."""
    from app.draft_api import app as draft_module

    monkeypatch.setattr(draft_module.settings, "draft_shared_secret", SECRET)
    monkeypatch.setattr(draft_module.settings, "enable_draft", False)
    with TestClient(draft_module.app) as client:
        no_secret = client.post(draft_module.DRAFT_PATH, json=_body())
        with_secret = client.post(
            draft_module.DRAFT_PATH, json=_body(), headers={draft_module.SECRET_HEADER: SECRET}
        )

    assert no_secret.status_code == 403
    assert with_secret.status_code == 503, "비밀이 맞으면 기능 꺼짐을 알려 줘도 된다"


# --- 계약 ---------------------------------------------------------------------------


def test_기능이_꺼져_있으면_503_이다(monkeypatch: pytest.MonkeyPatch) -> None:
    """셀러센터가 **폼을 빈 상태로 그대로 두게** 한다. 초안 실패가 등록을 막아서는 안 된다."""
    from app.draft_api import app as draft_module

    monkeypatch.setattr(draft_module.settings, "draft_shared_secret", SECRET)
    monkeypatch.setattr(draft_module.settings, "enable_draft", False)
    with TestClient(draft_module.app) as client:
        response = client.post(
            draft_module.DRAFT_PATH, json=_body(), headers={draft_module.SECRET_HEADER: SECRET}
        )

    assert response.status_code == 503
    assert "꺼져" in response.json()["error"]


def test_모르는_계약_버전은_떨어뜨리지_않고_거절한다(draft_client: Any) -> None:
    """``runner.py`` 와 같은 규약이다."""
    from app.draft_api.app import DRAFT_PATH, SECRET_HEADER

    response = draft_client.post(DRAFT_PATH, json=_body(schemaVersion=99), headers={SECRET_HEADER: SECRET})

    assert response.status_code == 409


def test_정상_요청이_초안_모양으로_돌아온다(draft_client: Any) -> None:
    """키가 없으니 stub 모드다 -- 필드가 비고 **사유가 그렇게 적힌다.**"""
    from app.draft_api.app import DRAFT_PATH, SECRET_HEADER

    response = draft_client.post(DRAFT_PATH, json=_body(), headers={SECRET_HEADER: SECRET})

    assert response.status_code == 200
    payload = response.json()
    assert payload["requestId"] == "r-1"
    assert payload["draftVersion"] == "draft-v1+catalog-v1"
    assert payload["skipReason"] == "LLM_UNAVAILABLE"
    assert payload["itemSignal"]["grade"] == "PINNED"
    # 응답이 ``ProductDraftSaveRequest`` 모양 그대로다 -- 백엔드 변환 코드가 사실상 없다.
    assert set(payload["draft"]) == {
        "categoryId",
        "productName",
        "description",
        "regularPrice",
        "discountPrice",
        "discountStartAt",
        "discountEndAt",
        "thumbnailImageKey",
        "additionalImageKeys",
        "detailImageKeys",
        "optionGroups",
        "optionCombinations",
    }
    assert set(payload["fieldStatus"]) == set(payload["draft"])


def test_처리할_수_없는_입력은_422_와_필요_치수를_돌려준다(draft_client: Any) -> None:
    """**AI 서버는 리사이즈하지 않는다.** 셀러센터가 줄여야 하는 수를 적어 돌려준다."""
    import base64

    from app.draft_api import app as draft_module
    from demo.draft_cassettes import synthetic_png

    draft_module.settings.draft_image_mode = "ALWAYS"
    try:
        response = draft_client.post(
            draft_module.DRAFT_PATH,
            json=_body(
                image={
                    "mediaType": "image/png",
                    "base64": base64.b64encode(synthetic_png(1500, 1500)).decode("ascii"),
                }
            ),
            headers={draft_module.SECRET_HEADER: SECRET},
        )
    finally:
        draft_module.settings.draft_image_mode = "OFF"

    assert response.status_code == 422
    assert response.json()["maxLongEdgePx"] > 0


def test_상품명이_비면_422_다(draft_client: Any) -> None:
    from app.draft_api.app import DRAFT_PATH, SECRET_HEADER

    response = draft_client.post(DRAFT_PATH, json=_body(productName="   "), headers={SECRET_HEADER: SECRET})

    assert response.status_code == 422


def test_헬스체크가_비밀_설정_여부를_알려_준다(draft_client: Any) -> None:
    """비밀이 빠진 배포를 운영이 알 수 있어야 한다. **값은 싣지 않는다.**"""
    response = draft_client.get("/health")

    payload = response.json()
    assert response.status_code == 200
    assert payload["role"] == "seller-draft"
    assert payload["secretConfigured"] is True
    assert SECRET not in response.text


def test_상품명_상한이_백엔드_DTO_와_같다() -> None:
    """``ProductDraft.NAME_MAX`` 와 같은 수다."""
    from app.schemas.draft import SellerDraftRequest

    assert SellerDraftRequest.model_fields["productName"].metadata[0].max_length == 255


def test_초안_타임아웃이_전역보다_짧다() -> None:
    """**셀러가 화면에서 기다린다.** 전역 60초는 너무 길다."""
    settings = Settings()

    assert settings.draft_timeout_seconds < settings.request_timeout_seconds


def test_초안이_검수_폴_간격_계산에_들어가지_않는다() -> None:
    """초안은 컨슈머 경로가 아니다. 섞으면 초안 상한을 올릴 때 검수 리밸런스가 흔들린다."""
    settings = Settings()
    before = settings.worst_case_handle_seconds()
    louder = settings.model_copy(update={"draft_timeout_seconds": 600.0})

    assert louder.worst_case_handle_seconds() == before


def test_도커_헬스체크와_CMD_가_같은_포트_변수를_읽는다() -> None:
    """**같은 이미지가 두 서비스를 띄운다.** 포트가 각자 박혀 있으면 한쪽이 영구 unhealthy 다.

    초안 앱은 별 ASGI 앱이고 권장 포트가 8200 인데, 전에는 ``HEALTHCHECK`` 가 8000 을
    이미지에 구워 두었다. 그러면 CMD 만 덮어 초안 서비스를 올릴 때 컨테이너는 8200 을
    듣는데 헬스체크는 8000 을 찔러 ECS 가 태스크를 무한 교체한다.
    """
    from pathlib import Path

    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()

    assert "ENV PORT=8000" in dockerfile
    healthcheck = next(line for line in dockerfile.splitlines() if "/health" in line and "CMD" in line)
    assert "PORT" in healthcheck, "헬스체크가 포트를 박아 두었다"
    assert "127.0.0.1:8000/health" not in dockerfile
    cmd = next(line for line in dockerfile.splitlines() if line.startswith("CMD"))
    assert "${PORT" in cmd, "CMD 가 포트 변수를 읽지 않는다"


def test_초안_앱에_헬스체크_경로가_있다() -> None:
    """포트만 맞으면 되는 문제였다는 근거다. 경로가 없으면 변수로는 못 고친다."""
    from app.draft_api.app import app as draft_app

    assert "/health" in {route.path for route in draft_app.routes}


# --- 스키마를 열어 두지 않는다 ------------------------------------------------------------


def test_초안_앱이_openapi를_열지_않는다() -> None:
    """**초안 앱의 ``/openapi.json`` 은 요청 스키마 전문을 그대로 드러낸다.**

    운영 앱은 ``/health`` 하나라 그 노출이 무해했지만, 초안 앱에는
    ``/internal/seller-draft`` 의 전체 요청 모양(``lockedFields``·이미지 필드·계약 버전)이
    실려 있다. 공유 비밀이 호출을 막지만 **방어를 인프라 한 겹에 걸지 않는다**는 이 모듈의
    원칙이 그 한 겹에도 적용된다 -- 열려 있어서 얻는 것이 없다.

    앞 판 테스트는 ``paths - {"/openapi.json"}`` 으로 그 경로를 **면제**해서 이 노출을
    잡지 못했다.
    """
    from app.draft_api.app import DRAFT_PATH
    from app.draft_api.app import app as draft_app

    paths = {getattr(route, "path", "") for route in draft_app.routes}
    assert "/openapi.json" not in paths, paths
    assert paths == {"/health", DRAFT_PATH}, paths

    client = TestClient(draft_app)
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
