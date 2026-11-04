"""요약 데모 콘솔. **운영 이미지와 계약이 콘솔 때문에 움직이지 않는 것을 함께 고정한다.**"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from demo import summary_view, view
from demo.app import app
from demo.summary_cassettes import SUMMARY_BY_ID, SUMMARY_SCENARIOS
from demo.summary_view import SummaryExit

REPO = Path(__file__).parent.parent
CONSOLE = REPO / "demo" / "console.html"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# --- 시나리오가 적어 둔 경로로 돈다 -----------------------------------------------------


@pytest.mark.parametrize("scenario", SUMMARY_SCENARIOS, ids=[s.id for s in SUMMARY_SCENARIOS])
def test_시나리오가_적어_둔_경로로_돈다(client: TestClient, scenario: Any) -> None:
    """**틀린 그림이 정상 응답으로 나가는 것이 가장 나쁘다.**

    사이드바에 시나리오 설명과 ``expected_exit`` 가 떠 있으므로 다른 경로를 그리면
    화면이 자기를 부정한다. 라우트가 그 어긋남을 400 으로 막는다.
    """
    response = client.post("/api/summary", json={"mode": "replay", "scenario": scenario.id})

    assert response.status_code == 200, response.json()
    assert response.json()["view"]["exit_point"] == scenario.expected_exit


def test_시나리오가_탈출_지점_전건을_덮는다() -> None:
    """한 경로라도 시나리오가 없으면 그 화면을 아무도 본 적이 없다는 뜻이다."""
    covered = {scenario.expected_exit for scenario in SUMMARY_SCENARIOS}

    assert covered == {exit_point.value for exit_point in SummaryExit}


def test_환경변수가_시나리오_경로를_바꾸지_못한다(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """임계가 앰비언트 환경에서 들어오면 같은 시나리오가 셸에 따라 다른 경로를 그린다.

    검수 콘솔에서 실측으로 겪은 함정이다 -- ``PERSPECTIVE_FANOUT=false`` 를 셸에 둔 채
    콘솔을 띄우면 네 시나리오가 전부 400 으로 죽었다.
    """
    for key, value in {
        "AECHAK_AI_SUMMARY_MIN_REVIEWS": "1",
        "AECHAK_AI_SUMMARY_MIN_ASPECT_MENTIONS": "1",
        "AECHAK_AI_SUMMARY_EXTRACT_BATCH_MAX": "1",
        "AECHAK_AI_SUMMARY_EXTRACT_MODEL": "claude-opus-5",
    }.items():
        monkeypatch.setenv(key, value)

    for scenario in SUMMARY_SCENARIOS:
        response = client.post("/api/summary", json={"mode": "replay", "scenario": scenario.id})
        assert response.status_code == 200, (scenario.id, response.json())
        assert response.json()["view"]["exit_point"] == scenario.expected_exit, scenario.id


def test_모르는_시나리오는_거절한다(client: TestClient) -> None:
    response = client.post("/api/summary", json={"mode": "replay", "scenario": "없는것"})

    assert response.status_code == 400
    assert "모르는 요약 시나리오" in response.json()["error"]


def test_카세트가_빠지면_그림을_내보내지_않는다(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """조용히 실패로 접으면 콘솔이 거짓 단계를 그린다.

    추출 실패는 ``UNEXTRACTED`` 이고 서술 실패는 ``narrateFailure`` 라서, 빠진 카세트를
    실패로 돌리면 둘 다 "정상적으로 비어 있는 요약"과 화면에서 구별되지 않는다.
    """
    scenario = SUMMARY_BY_ID["summary-ok"]
    monkeypatch.setitem(
        SUMMARY_BY_ID,
        "broken",
        scenario.__class__(
            **{
                **{f.name: getattr(scenario, f.name) for f in scenario.__dataclass_fields__.values()},
                "id": "broken",
                "entries": {},
            }
        ),
    )

    response = client.post("/api/summary", json={"mode": "replay", "scenario": "broken"})

    assert response.status_code == 400
    assert "카세트에 없는 호출" in response.json()["error"]
    assert "view" not in response.json()


def test_키가_없으면_라이브를_막는다(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AECHAK_AI_ANTHROPIC_API_KEY", raising=False)

    response = client.post("/api/summary", json={"mode": "live", "reviews": ["5|잘 먹어요"]})

    assert response.status_code == 400
    assert "AECHAK_AI_ANTHROPIC_API_KEY" in response.json()["error"]


def test_시나리오_목록이_임계를_함께_내려준다(client: TestClient) -> None:
    """발표를 보는 사람이 "왜 저 항목은 안 실렸나"를 물을 때 답이 화면에 있어야 한다."""
    meta = client.get("/api/summary/scenarios").json()

    assert len(meta["scenarios"]) == len(SUMMARY_SCENARIOS)
    defaults = meta["defaults"]
    assert defaults["minReviews"] == 10
    assert defaults["minAspectMentions"] == 3
    assert defaults["extractModel"].startswith("claude-haiku")
    # **운영 기본값을 그대로 내려준다.** 하드코딩하면 발표를 보는 사람이 꺼진 기능을
    # 배포된 동작으로 이해한다.
    from app.settings import Settings

    assert defaults["enabled"] == Settings().enable_review_summary


# --- 화면 모델이 섞지 않는 것 -----------------------------------------------------------


def test_단점_부재_전_갈래가_서로_다른_문장이다() -> None:
    """뭉개면 "분석한 12건에 단점이 없었다"와 "리뷰가 아직 적다"가 같은 말로 읽힌다.

    **열거를 늘릴 때 화면 문장을 빠뜨리지 않게 열거에서 분모를 가져온다.** 수를 적어 두면
    새 사유가 ``.get()`` 으로 ``None`` 이 되어 화면이 아무 문장도 그리지 않고, 그러면
    "단점 없음"으로 읽힌다 -- 이 기능에서 가장 비싼 거짓이다.
    """
    from app.schemas.summary import ConsAbsenceReason

    sentences = set(summary_view.CONS_ABSENCE_SENTENCES.values())

    assert len(sentences) == len(ConsAbsenceReason)
    assert set(summary_view.CONS_ABSENCE_SENTENCES) == {r.value for r in ConsAbsenceReason}


def test_반복인데_문장이_없는_것과_임계_미달을_라벨로_가른다(client: TestClient) -> None:
    """**이 기능에서 가장 틀리기 쉬운 자리다.**

    섞으면 코드가 반복이라고 센 장단점이 화면에서 "미달"로 읽힌다.
    """
    response = client.post("/api/summary", json={"mode": "replay", "scenario": "summary-narration-missing"})
    scope = response.json()["view"]["scopes"][0]

    kinds = {row["kind"]: row["kind_label"] for row in scope["counted"]}
    assert "UNNARRATED" in kinds
    assert kinds["UNNARRATED"] != kinds.get("MINOR")
    assert any(row["kind"] == "UNNARRATED" and row["mention_count"] > 0 for row in scope["counted"])


def test_반드시_띄워야_하는_계수가_표시로_구분된다(client: TestClient) -> None:
    """조용히 지나가면 요약이 거짓이 된다. ``loud`` 가 그 표시다."""
    response = client.post("/api/summary", json={"mode": "replay", "scenario": "summary-adversarial"})
    dropped = response.json()["view"]["dropped"]

    loud = {row["key"] for row in dropped if row["loud"]}
    assert "injectionExcluded" in loud
    # 계수 이름만 보여 주면 발표장에서 읽히지 않는다.
    assert all(row["label"] != row["key"] for row in dropped)


def test_추출됨과_분석되지_않음이_다른_라벨이다(client: TestClient) -> None:
    response = client.post("/api/summary", json={"mode": "replay", "scenario": "summary-adversarial"})
    reviews = response.json()["view"]["reviews"]

    labels = {row["status"]: row["status_label"] for row in reviews}
    assert labels["EXTRACTED"] != labels["UNEXTRACTED"]
    # 제외 사유가 있는 리뷰는 "항목 없음"이 아니라 사유째로 보인다.
    excluded = [row for row in reviews if row["exclude_reason"]]
    assert excluded and all(row["aspects"] == [] for row in excluded)
    assert all(row["note"] for row in excluded)


def test_추적이_없으면_단계를_그리지_않는다() -> None:
    """요약 결과에는 단계가 없어서 되살릴 재료가 없다.

    억지로 만들면 화면이 추측을 사실처럼 그린다. 검수 콘솔은 관점 행이 계약에 있어서
    얕은 재생이 가능했지만 여기는 다르다.
    """
    from datetime import UTC, datetime

    from app.schemas.summary import ReviewSummaryMessage

    message = ReviewSummaryMessage(
        requestId="r",
        productId=1,
        extractVersion="extract-v1",
        narrateVersion="narrate-v1",
        generatedAt=datetime.now(UTC),
    )
    built = summary_view.build(message, mode="verdict", source="s", reviews_in=0, trace=None)

    assert built.stages == []
    assert built.trace_available is False
    assert built.envelope_text is None


def test_화면_모델이_JSON으로_나간다(client: TestClient) -> None:
    """``as_dict`` 가 그대로 직렬화되지 않으면 콘솔이 빈 화면을 띄운다."""
    response = client.post("/api/summary", json={"mode": "replay", "scenario": "summary-ok"})

    dumped = json.loads(json.dumps(response.json()["view"], ensure_ascii=False))
    assert dumped["exit_label"] == "요약 완료"
    assert dumped["ledger"]["calls"][0]["stage"] == "EXTRACT"


def test_환율_사본을_또_만들지_않았다() -> None:
    """두 숫자가 갈라지면 콘솔이 보여 주는 비용으로 손익분기를 이야기할 수 없다."""
    from harness.run import KRW_PER_USD

    assert summary_view.KRW_PER_USD is view.KRW_PER_USD
    assert view.KRW_PER_USD == KRW_PER_USD

    source = (REPO / "demo" / "summary_view.py").read_text(encoding="utf-8")
    assert "KRW_PER_USD = 1_400" not in source, "환율을 또 적었다"
    assert "1_400" not in source and "1400" not in source


def test_검수_화면_모델을_건드리지_않았다() -> None:
    """``demo/view.py`` 는 검수 5단계 전용이다. 값을 더하면 검수 화면이 흔들린다.

    ``N3_CRITIC`` 이 빠진 자리를 메우지 않았다. 번호가 ``Stage`` 값이고 콘솔·테스트의
    키라서, 뜻이 같은 재배열이 그 사본들을 전부 흔든다.
    """
    assert [stage.value for stage, _ in view.CANONICAL_STAGES] == [
        "N0_RULE",
        "N0_5_TRIAGE",
        "N1_JUDGE",
        "N2_AGGREGATE",
        "N4_NOTICE",
    ]
    assert [stage.value for stage, _ in summary_view.CANONICAL_SUMMARY_STAGES] == [
        "GATE",
        "EXTRACT",
        "NARRATE",
    ]
    # **검수 ``Stage`` 열거에 요약 단계를 더하지 않았다.** 더하면 콘솔의
    # ``CANONICAL_STAGES`` 가 그 값을 "도달하지 않음"으로 그린다.
    from app.inspection.trace import Stage
    from app.review_summary.trace import SummaryStage

    assert {s.value for s in Stage}.isdisjoint({s.value for s in SummaryStage})


# --- 운영 이미지와 계약이 움직이지 않는다 -----------------------------------------------


def test_운영_앱에_요약_콘솔_경로가_없다() -> None:
    """``app/main.py`` 가 공개 ALB 뒤라서 관리용 경로를 열면 외부에서 닿는다.

    요약은 배치성이라 동기 경로가 필요 없고, 전용 카프카 토픽으로만 들어온다.
    """
    from app.main import app as operational_app

    paths = {getattr(route, "path", "") for route in operational_app.routes}
    assert not any("summary" in path or "demo" in path or "console" in path for path in paths), paths
    assert "/health" in paths


# --- 콘솔 한 장이 지키는 것 -------------------------------------------------------------


def test_콘솔에_중복_id가_없다() -> None:
    """기능 탭마다 같은 레이아웃을 한 번씩 쓴다.

    ``id`` 로 두면 중복 ``id`` 가 되고 CSS 가 첫 섹션만 칠한다 -- 뒤쪽 탭이 여백 없이
    그려진다. 레이아웃은 클래스로, 자바스크립트가 집는 것만 ``id`` 로 둔다.

    **탭 수를 상수로 적지 않고 탭 버튼에서 센다.** 적어 두면 탭을 더할 때마다 이 테스트를
    고쳐야 하고, 고치는 동안 잡으려던 중복 ``id`` 를 못 보게 된다.
    """
    html = CONSOLE.read_text(encoding="utf-8")
    ids = re.findall(r'\bid="([^"]+)"', html)
    tabs = len(re.findall(r'data-feature="', html))

    assert len(ids) == len(set(ids)), {i for i in ids if ids.count(i) > 1}
    assert tabs >= 2, "기능 탭을 버튼에서 세지 못했다"
    assert html.count('class="controls"') == tabs
    assert html.count('class="results"') == tabs


def test_콘솔이_참조하는_id가_실재한다() -> None:
    """``$("x")`` 가 없는 요소를 집으면 화면이 조용히 빈다."""
    html = CONSOLE.read_text(encoding="utf-8")
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    used = set(re.findall(r'\$\("([^"]+)"\)', html))

    assert used <= ids, sorted(used - ids)
    # 문자열을 이어 만드는 id 도 실재해야 한다.
    for dynamic in (
        "feature-inspection",
        "feature-summary",
        "feature-draft",
        "main-summary",
        "main-draft",
        "smode-replay",
        "dmode-replay",
    ):
        assert dynamic in ids, dynamic


def test_콘솔이_요약_탭을_띄운다() -> None:
    html = CONSOLE.read_text(encoding="utf-8")

    assert "리뷰 요약" in html
    assert "/api/summary" in html
    assert "bootSummary()" in html


def test_콘솔이_캐시_0이_정상임을_적는다() -> None:
    """Haiku 4.5 는 최소 접두부 4,096토큰을 넘을 수 없다. 숫자를 보여 주고 사유를 적는다."""
    html = CONSOLE.read_text(encoding="utf-8")

    assert "4,096" in html
    assert "0 이 정상" in html


def test_콘솔이_분석되지_않은_리뷰_수를_띄운다() -> None:
    """안 띄우면 분모가 전수로 읽힌다 -- "대부분의 보호자가"가 거짓이 된다."""
    html = CONSOLE.read_text(encoding="utf-8")

    assert "분석되지 않음" in html
    assert "reviews_unextracted" in html


# --- 모의 모드 (사람이 자기 리뷰를 타이핑해 결과를 본다) ----------------------------------


def test_요약_탭에_모의_모드가_있다() -> None:
    """**검수 탭에는 있고 요약 탭에는 없던 비대칭이다.**

    검수는 ``replay``·``mock``·``live`` 셋이고 요약은 ``replay``·``live`` 둘이었다. 키가
    없으면 라이브 버튼이 내려가므로, 요약 기능에서는 **사람이 자기 리뷰를 넣어 보는 일이
    아예 불가능했다** -- 적어 둔 시나리오 9개만 돌 수 있었다. 발표의 절반이 그 일이다.
    """
    from demo.app import SummaryBody

    annotation = SummaryBody.model_fields["mode"].annotation
    import typing

    assert set(typing.get_args(annotation)) == {"replay", "mock", "live"}


def test_모의_모드가_사람이_쓴_리뷰로_요약을_만든다(client: TestClient) -> None:
    """대역은 **모델이 아니다.** 키워드 표로 답하는 결정론적 대역이고 비용이 0이다."""
    reviews = [f"5|잘 먹어요 기호성이 좋아요 {i}번" for i in range(1, 13)]
    response = client.post("/api/summary", json={"mode": "mock", "reviews": reviews})

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["view"]["mode"] == "mock"
    message = body["message"]
    assert [row["extracted"] for row in message["extracted"]] == ["EXTRACTED"] * len(reviews)
    assert message["summaries"], message
    assert message["summaries"][0]["pros"], "대역이 집계표의 PRO 줄에 한 줄도 붙이지 않았다"


def test_모의_모드가_본문_없이는_거부한다(client: TestClient) -> None:
    response = client.post("/api/summary", json={"mode": "mock", "reviews": []})

    assert response.status_code == 400, response.json()


# --- 모의·라이브 모드가 분모를 지어내지 않는다 ---------------------------------------------
#
# **이 레포 1순위 불변식("통과와 미검사를 섞지 않는다")을 입력 계층에서 깨던 자리다.**
# 서비스는 멀쩡했다 -- 정직한 숫자를 주면 ``PARTIALLY_UNEXTRACTED`` 와 "모델이 이 리뷰를
# 돌려주지 않았다"가 나온다. 거짓말은 데모의 요청 조립 한 자리였다: 사람이 자유 입력하는
# ``basisReviewCount``(기본 20)를 ``totalVisibleReviews``·``basisReviewCount`` 양쪽에 박고
# ``unextractedReviewCount=0`` 을 하드코딩했다. 실제 투입 리뷰는 textarea 줄 수(기본 5)다.


def test_모의_모드가_투입_리뷰보다_큰_분석_수를_보고하지_않는다(client: TestClient) -> None:
    """콘솔 **출고 기본 상태**(리뷰 5건 + 분모 20)에서 바로 재현되던 거짓이다.

    ``analyzed == basis`` 라서 화면의 '전수' pill 도, ``unextracted == 0`` 이라 '분석 안 됨'
    pill 도 뜨지 않았다. 즉 화면이 "분석 20건"만 그렸다.
    """
    reviews = ["5|잘 먹어요", "5|배송 빨라요", "4|포장 좋아요", "5|냄새 안 나요", "5|재구매 할게요"]
    response = client.post("/api/summary", json={"mode": "mock", "reviews": reviews, "basisReviewCount": 20})

    assert response.status_code == 200, response.text
    scope = response.json()["view"]["scopes"][0]
    assert scope["analyzed_review_count"] <= len(reviews)
    assert scope["basis_review_count"] + 0 >= scope["analyzed_review_count"]
    assert scope["unextracted_review_count"] == scope["basis_review_count"] - len(reviews)


def test_모의_모드가_투입_리뷰가_임계_아래면_그렇게_적는다(client: TestClient) -> None:
    """``summary_min_reviews`` 임계를 조작된 분모가 통과시켰고 서술 호출까지 났다."""
    response = client.post(
        "/api/summary", json={"mode": "mock", "reviews": ["5|좋아요", "5|또 살게요", "4|괜찮아요"]}
    )

    assert response.status_code == 200, response.text
    view_model = response.json()["view"]
    scope = view_model["scopes"][0]
    assert scope["not_enough_reason"] == "분석한 리뷰 3건이 최소 10건에 못 미친다"
    assert scope["cons_absence_reason"] == "NOT_ENOUGH_REVIEWS"
    # 서술은 유료 단계다. 임계 아래에서 그 호출이 나면 조작된 분모가 그것을 통과시킨 것이다.
    narrate = [stage for stage in view_model["stages"] if stage["stage"] == "NARRATE"]
    assert [stage["status"] for stage in narrate] == ["SKIPPED"]
    assert all(not stage["paid"] for stage in narrate)


def test_모의_모드가_투입_리뷰보다_작은_분모를_거부한다(client: TestClient) -> None:
    """분모가 투입 리뷰보다 작으면 ``unextracted`` 가 음수가 된다. 조립하지 않고 거부한다."""
    response = client.post(
        "/api/summary",
        json={"mode": "mock", "reviews": ["5|좋아요", "5|또 살게요"], "basisReviewCount": 1},
    )

    assert response.status_code == 400, response.text
    assert "분모" in response.json()["error"]


def test_집계표가_분석_수를_투입_리뷰로_적는다(client: TestClient) -> None:
    """모델이 보는 문장이 "분석한 리뷰: 20건 / 아직 분석되지 않은 리뷰: 0건" 이었다."""
    reviews = [f"5|잘 먹어요 기호성이 좋아요 {i}번" for i in range(1, 13)]
    response = client.post("/api/summary", json={"mode": "mock", "reviews": reviews, "basisReviewCount": 30})

    assert response.status_code == 200, response.text
    tally = response.json()["view"]["tally_text"]["PRODUCT"]
    assert "분석한 리뷰: 12건" in tally
    assert "아직 분석되지 않은 리뷰: 18건" in tally
