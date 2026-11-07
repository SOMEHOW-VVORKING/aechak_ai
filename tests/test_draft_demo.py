"""초안 데모 콘솔. **두 방식을 나란히 놓는 것이 이 탭의 존재 이유다.**

고정하는 것 셋.

1. 시나리오 전건이 **적어 둔 경로로 돈다.** 틀린 그림이 정상 응답으로 나가는 것이 가장 나쁘다.
2. 비교표가 **비교할 수 없는 것을 0 으로 그리지 않는다.**
3. 운영 가드 넷이 깨지지 않는다(Dockerfile / 운영 의존성 / 운영 앱 라우트 / app 이 demo 를
   import 하지 않음).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.schemas.draft import FieldStatus
from demo import draft_view
from demo.app import app
from demo.draft_cassettes import DRAFT_SCENARIOS, synthetic_png
from demo.draft_view import DraftExit

REPO = Path(__file__).parent.parent
CONSOLE = REPO / "demo" / "console.html"


@pytest.fixture
def client() -> Any:
    with TestClient(app) as test_client:
        yield test_client


def _run(client: TestClient, scenario_id: str) -> dict[str, Any]:
    response = client.post("/api/draft", json={"mode": "replay", "scenario": scenario_id})
    assert response.status_code == 200, (scenario_id, response.json())
    return response.json()["view"]


# --- 시나리오 ------------------------------------------------------------------------


def test_시나리오_전건이_적어_둔_경로로_돈다(client: TestClient) -> None:
    """``expected_exit`` 가 그 장치다. 틀린 그림이 정상 응답으로 나가는 것이 가장 나쁘다."""
    for scenario in DRAFT_SCENARIOS:
        view = _run(client, scenario.id)
        exits = [variant.exit_point for variant in _variants(view)]

        assert exits[0] == scenario.expected_exit, scenario.id
        if scenario.image_entries is not None:
            assert len(exits) == 2, scenario.id
            assert exits[1] == scenario.expected_image_exit, scenario.id
        else:
            assert len(exits) == 1, scenario.id


def _variants(view: dict[str, Any]) -> list[Any]:
    from types import SimpleNamespace

    return [SimpleNamespace(**variant) for variant in view["variants"]]


def test_적어_둔_경로와_다르게_돌면_그림을_내보내지_않는다(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """사이드바에 시나리오 설명과 expected_exit 가 떠 있으므로 **화면이 자기를 부정한다.**"""
    monkeypatch.setattr(draft_view, "classify_exit", lambda _response: DraftExit.REUSED)
    response = client.post("/api/draft", json={"mode": "replay", "scenario": "draft-pinned-text"})

    assert response.status_code == 400
    assert "view" not in response.json()


def test_모르는_시나리오는_거절한다(client: TestClient) -> None:
    response = client.post("/api/draft", json={"mode": "replay", "scenario": "없는것"})

    assert response.status_code == 400


def test_0원으로_끝난_세_길이_서로_다른_라벨이다(client: TestClient) -> None:
    """상품명 차단 / 품목 미식별 / 사진 거부가 한 라벨로 뭉개지면 안 된다."""
    blocked = _variants(_run(client, "draft-name-rule-blocked"))[0]
    opaque = _variants(_run(client, "draft-opaque-no-call"))[0]
    rejected = _variants(_run(client, "draft-image-too-large"))[1]

    assert blocked.exit_point == DraftExit.NAME_RULE_BLOCKED.value
    assert opaque.exit_point == DraftExit.ITEM_UNIDENTIFIED.value
    assert rejected.exit_point == DraftExit.IMAGE_REJECTED.value
    assert len({blocked.exit_label, opaque.exit_label, rejected.exit_label}) == 3
    assert all(not variant.model_called for variant in (blocked, opaque, rejected))


def test_사진_거부를_호출_실패와_섞지_않는다(client: TestClient) -> None:
    """**뜻이 정반대다.** 전자는 사진을 줄여 다시 보내면 되고 후자는 다시 시도해야 한다."""
    rejected = _variants(_run(client, "draft-image-too-large"))[1]
    failed = _variants(_run(client, "draft-call-failed"))[0]

    assert rejected.exit_point != failed.exit_point
    assert "리사이즈하지 않" in rejected.exit_note
    assert "다시 시도" in failed.exit_note


def test_호출_실패에서_모든_필드가_확인하지_못했다로_적힌다(client: TestClient) -> None:
    variant = _variants(_run(client, "draft-call-failed"))[0]

    statuses = {row["name"]: row["status"] for row in variant.fields}
    assert statuses["categoryId"] == FieldStatus.EMPTY_CALL_FAILED.value
    # 채우지 않기로 한 필드는 실패에 물들지 않는다.
    assert statuses["regularPrice"] == FieldStatus.EMPTY_NOT_ATTEMPTED.value
    loud = [row for row in variant.fields if row["loud"]]
    assert loud, "호출 실패는 화면에서 조용히 지나가면 안 된다"


def test_효능과_금지성분이_겹별로_다른_사유를_낸다(client: TestClient) -> None:
    claim = _variants(_run(client, "draft-claim-dropped"))[0]
    banned = _variants(_run(client, "draft-banned-dropped"))[0]

    assert {drop["kind"] for drop in claim.guard_drops} == {"CLAIM"}
    assert {drop["kind"] for drop in banned.guard_drops} == {"BANNED"}
    # 걸린 슬롯만 떨어지고 나머지가 살아남는다.
    assert claim.description and "치료" not in claim.description
    assert banned.description and "자일리톨" not in banned.description


def test_옵션_불변식_위반이_화면에_사유로_뜬다(client: TestClient) -> None:
    variant = _variants(_run(client, "draft-option-rule"))[0]

    assert variant.option_groups == []
    assert any(drop["kind"] == "OPTION_RULE" for drop in variant.guard_drops)
    labels = {drop["kind_label"] for drop in variant.guard_drops}
    assert all(label for label in labels), "겹 이름을 사람 말로 보여 줘야 한다"


def test_사진_근거_불일치가_필드를_폐기한다(client: TestClient) -> None:
    text, image = _variants(_run(client, "draft-basis-mismatch"))

    assert text.description is not None
    assert image.description is None
    assert any(drop["kind"] == "BASIS_MISMATCH" for drop in image.guard_drops)


def test_오사진에서_모델이_불일치를_적는다(client: TestClient) -> None:
    """**이미지 입장의 최악 실패 모양**이 사진 쪽 상품의 초안을 조용히 써 주는 것이다."""
    _text, image = _variants(_run(client, "draft-wrong-photo"))

    assert image.image_usability == "MISMATCHES_NAME"
    # **사진을 근거로 적은 필드가 하나도 남지 않아야 한다.** 남으면 사진 쪽 상품의 초안이다.
    assert [row["name"] for row in image.fields if row["basis"] == "IMAGE"] == []
    assert image.seller_notice and "사진" in image.seller_notice


def test_분류_불일치가_비우고_셀러에게_넘긴다(client: TestClient) -> None:
    variant = _variants(_run(client, "draft-category-conflict"))[0]

    assert variant.category_id is None
    assert variant.exit_point == DraftExit.CATEGORY_CONFLICT.value
    statuses = {row["name"]: row["status"] for row in variant.fields}
    assert statuses["categoryId"] == FieldStatus.EMPTY_CATEGORY_CONFLICT.value


def test_잠근_필드를_화면이_다른_사유로_그린다(client: TestClient) -> None:
    """``EMPTY_NOT_ATTEMPTED`` 로 접으면 셀러가 쓴 글을 안 보여 준다."""
    variant = _variants(_run(client, "draft-locked-fields"))[0]

    statuses = {row["name"]: row["status"] for row in variant.fields}
    assert statuses["description"] == FieldStatus.KEPT_LOCKED.value
    assert "직접 고친" in draft_view.FIELD_STATUS_SENTENCES[FieldStatus.KEPT_LOCKED.value]


def test_다시_쓰기가_셀러_원문을_모델_문구로_바꾼다(client: TestClient) -> None:
    """``KEPT_LOCKED`` 로 남으면 다시 쓰기가 아무 일도 하지 않은 것이다.

    옵션은 같은 응답에서 ``KEPT_LOCKED`` 로 남아야 한다 -- 모드의 범위가 설명 하나다.
    """
    variant = _variants(_run(client, "draft-refine"))[0]

    statuses = {row["name"]: row["status"] for row in variant.fields}
    assert statuses["description"] == FieldStatus.FILLED.value
    assert statuses["optionGroups"] == FieldStatus.KEPT_LOCKED.value
    assert "응고형 벤토나이트" in (variant.description or "")
    assert "재구매 많아요" not in (variant.description or "")


# --- 비교표 -------------------------------------------------------------------------


def test_비교표가_비용과_지연과_초안을_한_화면에_놓는다(client: TestClient) -> None:
    """**사람이 이 표를 읽고 입력 방식을 고른다.**"""
    view = _run(client, "draft-pinned-text")

    metrics = [row["metric"] for row in view["compare"]]
    assert "건당 비용" in metrics
    assert "지연" in metrics
    assert "고른 소분류" in metrics
    assert "채운 필드 수" in metrics
    assert "사진 자기보고" in metrics


def test_이미지_변형이_없으면_0_으로_그리지_않는다(client: TestClient) -> None:
    """빈 칸을 0 으로 그리면 **"이미지가 비용을 0 더한다"로 읽힌다.**"""
    view = _run(client, "draft-name-rule-blocked")

    assert len(view["compare"]) == 1
    row = view["compare"][0]
    assert row["with_image"] == "없음"
    assert row["delta"] == "-"


def test_비교표가_한_건의_차이는_결론이_아니라고_적는다(client: TestClient) -> None:
    view = _run(client, "draft-pinned-text")

    assert "결론이 아니" in view["note"]
    assert "변수가 하나" in view["note"]


def test_비용_증분이_실제_단가_함수로_계산된다(client: TestClient) -> None:
    """데모가 자기 단가표를 들면 원장의 숫자가 운영과 어긋난다."""
    from app.llm.client import cost_micro_usd

    text, image = _variants(_run(client, "draft-pinned-text"))

    assert text.ledger["total_cost_micro_usd"] == cost_micro_usd(
        "claude-haiku-4-5", input_tokens=2_340, cache_read=0, cache_creation=0, output_tokens=450
    )
    assert image.ledger["total_cost_micro_usd"] > text.ledger["total_cost_micro_usd"]


def test_캐시_적중이_0_이고_그_사실을_적는다(client: TestClient) -> None:
    """Haiku 4.5 의 최소 캐시 접두부가 4,096토큰이라 초안 접두부는 캐시되지 않는다.

    원장에 캐시 적중을 꾸며 넣으면 콘솔이 그 근거를 **거짓으로** 보여 준다.
    """
    for variant in _variants(_run(client, "draft-pinned-text")):
        assert variant.ledger["cache_reads"] == 0


def test_화면_모델이_JSON으로_나간다(client: TestClient) -> None:
    """``as_dict`` 가 그대로 직렬화되지 않으면 콘솔이 빈 화면을 띄운다."""
    view = _run(client, "draft-pinned-text")

    assert json.loads(json.dumps(view, ensure_ascii=False))["variants"][0]["exit_label"]


def test_모든_필드가_한_줄씩_그려진다(client: TestClient) -> None:
    """**사유 없이 빈 필드가 화면에 남지 않는다.**"""
    from app.schemas.draft import DRAFT_FIELDS

    variant = _variants(_run(client, "draft-pinned-text"))[0]

    assert [row["name"] for row in variant.fields] == list(DRAFT_FIELDS)
    assert all(row["sentence"] for row in variant.fields)


def test_모든_사유에_사람_문장이_있다() -> None:
    """계수 이름만 보여 주면 발표장에서 읽히지 않는다."""
    for status in FieldStatus:
        assert status.value in draft_view.FIELD_STATUS_SENTENCES, status


def test_모든_탈출값에_라벨이_있다() -> None:
    for exit_point in DraftExit:
        assert exit_point.value in draft_view._EXIT_LABELS, exit_point


def test_모든_가드_종류에_라벨이_있다() -> None:
    from app.schemas.draft import GuardKind

    for kind in GuardKind:
        assert kind.value in draft_view.GUARD_KIND_LABELS, kind


# --- 시나리오 메타와 라이브 ----------------------------------------------------------


def test_시나리오_목록이_운영_기본값을_내려보낸다(client: TestClient) -> None:
    """발표를 보는 사람이 **현재 배포된 동작**을 오해하지 않아야 한다."""
    from app.settings import Settings

    payload = client.get("/api/draft/scenarios").json()

    assert len(payload["scenarios"]) == len(DRAFT_SCENARIOS)
    assert payload["defaults"]["imageMode"] == Settings().draft_image_mode
    assert payload["defaults"]["enabled"] == Settings().enable_draft
    assert payload["defaults"]["krwPerUsd"] == draft_view.KRW_PER_USD


def test_키가_없으면_라이브를_거절한다(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AECHAK_AI_ANTHROPIC_API_KEY", raising=False)
    response = client.post("/api/draft", json={"mode": "live", "productName": "강아지 목줄"})

    assert response.status_code == 400
    assert "AECHAK_AI_ANTHROPIC_API_KEY" in response.json()["error"]


def test_시나리오가_앰비언트_환경을_읽지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    """임계나 이미지 모드가 환경에서 들어오면 **같은 시나리오가 셸에 따라 다른 경로를 그린다.**"""
    monkeypatch.setenv("AECHAK_AI_DRAFT_IMAGE_MODE", "ALWAYS")
    monkeypatch.setenv("AECHAK_AI_ENABLE_DRAFT", "false")
    monkeypatch.setenv("AECHAK_AI_DRAFT_DESCRIPTION_CHARS", "9")

    settings = DRAFT_SCENARIOS[0].settings(with_image=False)

    assert settings.draft_image_mode == "OFF"
    assert settings.enable_draft is True
    assert settings.draft_description_chars == 400
    assert settings.anthropic_api_key == "", "재생은 키가 필요 없다"


def test_합성_사진이_실제_헤더_경로를_태운다(client: TestClient) -> None:
    """합성 헤더로 **보여 줄 수 없는 것**(모델이 사진을 읽는 능력)은 하네스가 잰다."""
    _text, image = _variants(_run(client, "draft-pinned-text"))

    assert image.image_attached is True
    assert image.image_tokens > 0
    from app.seller_draft import imagemeta

    assert imagemeta.read_dimensions(synthetic_png(896, 896)) == ("image/png", 896, 896)


# --- 운영 가드 ----------------------------------------------------------------------


def test_운영_이미지가_데모를_담지_않는다() -> None:
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    copied = [line.strip() for line in dockerfile.splitlines() if line.strip().startswith("COPY")]

    assert not any("demo" in line for line in copied), copied


def test_운영_앱에_초안_콘솔_경로가_붙지_않았다() -> None:
    from app.main import app as operational_app

    paths = {getattr(route, "path", "") for route in operational_app.routes}
    assert not any("draft" in path for path in paths), paths


def test_콘솔_탭이_셋이고_중복_id_가_없다() -> None:
    html = CONSOLE.read_text(encoding="utf-8")
    ids = re.findall(r'\bid="([^"]+)"', html)

    assert len(ids) == len(set(ids)), {i for i in ids if ids.count(i) > 1}
    assert html.count('data-feature="') == 3
    assert "feature-draft" in ids
    assert "main-draft" in ids


def test_콘솔이_외부_자원을_불러오지_않는다() -> None:
    """자체완결 한 장이다. 외부 스크립트도 폰트도 CDN도 쓰지 않는다."""
    html = CONSOLE.read_text(encoding="utf-8")

    assert re.findall(r'(?:src|href)="(https?://[^"]+)"', html) == []


def test_콘솔이_참조하는_초안_id_가_실재한다() -> None:
    """``$("x")`` 가 없는 요소를 집으면 화면이 조용히 빈다."""
    html = CONSOLE.read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([^"]+)"', html))

    for dynamic in ("dmode-replay", "dmode-live", "dpanel-replay", "dpanel-live", "draft-out"):
        assert dynamic in ids, dynamic


def test_콘솔이_빈_필드의_사유를_색으로_가른다() -> None:
    """**"못 봤다"가 "없다"와 같은 색이면 거짓이다.** 이 기능에서 가장 틀리기 쉬운 자리다."""
    html = CONSOLE.read_text(encoding="utf-8")

    for rule in (".field.filled", ".field.locked", ".field.notattempted", ".field.guard", ".field.loud"):
        assert rule in html, rule


def test_두_방식이_나란히_그려진다() -> None:
    """좁은 화면에서는 한 열로 쌓아야 읽힌다."""
    html = CONSOLE.read_text(encoding="utf-8")

    assert ".side { display: grid; grid-template-columns: 1fr 1fr" in html
    assert "grid-template-columns: 1fr; }" in html
