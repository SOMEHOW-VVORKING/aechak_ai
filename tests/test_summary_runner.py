"""요약 컨슈머와 설정. **전용 토픽·전용 그룹·전용 폴 간격이 검수와 섞이지 않는 것을 고정한다.**"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.kafka.summary_runner import SummaryRunner
from app.llm.client import UnknownModelPricing, validate_pricing
from app.review_summary.service import SummaryService
from app.schemas.envelope import Envelope
from app.schemas.summary import (
    SUMMARY_AGGREGATE_TYPE,
    SUMMARY_REQUEST_EVENT_TYPE,
    SUMMARY_RESULT_EVENT_TYPE,
    SummaryState,
)
from app.settings import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="", kafka_bootstrap_servers="localhost:9092")


class FakeProducer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, bytes | None]] = []

    async def send_and_wait(self, topic: str, value: bytes, key: bytes | None = None) -> None:
        self.sent.append((topic, value, key))

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _runner(settings: Settings, producer: FakeProducer, llm: Any = None) -> SummaryRunner:
    runner = SummaryRunner(settings=settings, service=SummaryService(llm=llm, settings=settings))
    runner._producer = producer  # type: ignore[attr-defined]
    return runner


def _envelope(
    payload: dict[str, Any],
    *,
    event_type: str = SUMMARY_REQUEST_EVENT_TYPE,
) -> str:
    return json.dumps(
        {
            "eventId": "evt-1",
            "eventType": event_type,
            "occurredAt": "2026-10-27T00:00:00Z",
            "aggregateType": SUMMARY_AGGREGATE_TYPE,
            "orderingKey": "42",
            "traceId": "trace-1",
            "producer": "aechak-be",
            "payload": json.dumps(payload, ensure_ascii=False),
        }
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "requestId": "rs-1",
        "productId": 42,
        "categoryPath": ["강아지", "사료", "건식사료"],
        "totalVisibleReviews": 12,
        "newReviews": [],
        "requestedScopes": [{"scope": "PRODUCT", "segmentKey": None, "basisReviewCount": 12, "tally": []}],
        "schemaVersion": 1,
    }
    base.update(overrides)
    return base


async def test_결과를_전용_토픽에_발행한다(settings: Settings) -> None:
    """**기존 검수 토픽을 쓰지 않는다.**

    검수 러너의 ``_REQUEST_TYPES`` 는 모르는 eventType 을 경고 후 그대로 커밋해 영구히
    버린다. 구버전 AI 가 배포돼 있으면 배포 순서가 데이터 손실을 만든다.
    """
    producer = FakeProducer()
    await _runner(settings, producer).handle(_envelope(_payload()))

    topic, value, key = producer.sent[0]
    assert topic == settings.kafka_summary_result_topic == "aechak.summary.results"
    assert topic != settings.kafka_verdict_topic
    assert key == b"42"

    out = Envelope.model_validate_json(value.decode())
    assert out.eventType == SUMMARY_RESULT_EVENT_TYPE
    assert out.aggregateType == SUMMARY_AGGREGATE_TYPE
    # 상관키를 승계한다. 새로 만들면 백엔드 로그와 한 키로 꿰이지 않는다.
    assert out.traceId == "trace-1"
    # orderingKey 도 승계한다 -- 한 상품의 결과가 같은 파티션에서 순서대로 흐른다.
    assert out.orderingKey == "42"


async def test_payload가_객체가_아니라_문자열이다(settings: Settings) -> None:
    """백엔드가 ``payload`` 를 문자열로 읽는다. 객체를 넣으면 역직렬화가 깨진다."""
    producer = FakeProducer()
    await _runner(settings, producer).handle(_envelope(_payload()))

    raw = json.loads(producer.sent[0][1].decode())
    assert isinstance(raw["payload"], str)
    assert json.loads(raw["payload"])["requestId"] == "rs-1"


async def test_모르는_이벤트_종류를_커밋하지_않는다(settings: Settings) -> None:
    """**전용 토픽이라 오는 것이 전부 우리 것이다.**

    검수 토픽은 여러 종류가 흐르는 공용 토픽이라 건너뛰는 것이 맞았다. 여기서 건너뛰고
    커밋하면 영구히 버리는 것이고, 모르는 종류는 배포 순서가 어긋났다는 뜻이라
    기다리는 쪽이 사실에 맞다.
    """
    producer = FakeProducer()

    with pytest.raises(ValueError, match="모르는 이벤트 종류"):
        await _runner(settings, producer).handle(_envelope(_payload(), event_type="SomethingElseMessage"))

    assert producer.sent == []


async def test_계약_버전이_다르면_커밋하지_않는다(settings: Settings) -> None:
    producer = FakeProducer()

    with pytest.raises(ValueError, match="지원하지 않는 계약 버전"):
        await _runner(settings, producer).handle(_envelope(_payload(schemaVersion=99)))

    assert producer.sent == []


async def test_모델_실패는_결과를_발행하고_커밋한다(settings: Settings) -> None:
    """**유료 호출이 들어간 경로에서 커밋을 빼면 지출이 무한 루프가 된다.**

    여기서는 모델 클라이언트가 없는 경로로 같은 성질을 본다 -- 발행이 돌고 예외가
    나지 않으므로 바깥 루프가 커밋한다.
    """
    producer = FakeProducer()
    payload = _payload(
        newReviews=[{"reviewId": 1, "rating": 5, "content": "잘 먹어요", "optionName": "기본"}]
    )
    await _runner(settings, producer).handle(_envelope(payload))

    body = json.loads(json.loads(producer.sent[0][1].decode())["payload"])
    assert body["extracted"][0]["extracted"] == "UNEXTRACTED"
    assert body["state"] == SummaryState.OK.value


async def test_폴_간격이_최악_처리시간보다_넓다(settings: Settings, monkeypatch: Any) -> None:
    """**상한만 올리고 폴 간격을 빠뜨리는 것이 이 레포가 실제로 밟은 함정이다.**

    배치 상한이나 스코프 수를 올리면 ``summary_worst_case_handle_seconds()`` 가 커지고
    이 테스트가 떨어진다.
    """
    captured: dict[str, Any] = {}

    class FakeConsumer:
        def __init__(self, *topics: str, **kwargs: Any) -> None:
            captured.update(kwargs)
            captured["topics"] = topics

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        def __aiter__(self) -> Any:
            return self

        async def __anext__(self) -> Any:
            raise StopAsyncIteration

        async def commit(self) -> None:
            return None

    class FakeProducerClass:
        def __init__(self, **kwargs: Any) -> None:
            return None

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    monkeypatch.setattr("app.kafka.summary_runner.AIOKafkaConsumer", FakeConsumer)
    monkeypatch.setattr("app.kafka.summary_runner.AIOKafkaProducer", FakeProducerClass)
    runner = SummaryRunner(settings=settings, service=SummaryService(llm=None, settings=settings))

    await runner.start()
    await runner.stop()

    assert captured["enable_auto_commit"] is False
    assert captured["topics"] == (settings.kafka_summary_request_topic,)
    assert captured["group_id"] == settings.kafka_summary_group_id == "aechak-ai-summarizer"
    budget = settings.summary_worst_case_handle_seconds()
    assert captured["max_poll_interval_ms"] / 1000 >= budget, (
        f"요약 폴 간격 {captured['max_poll_interval_ms']}ms 가 최악 처리시간 {budget}초보다 좁다"
    )


def test_요약_예산이_검수_예산과_섞이지_않는다(settings: Settings) -> None:
    """검수 폴 간격 테스트가 ``worst_case_handle_seconds()`` 에 묶여 있다.

    요약을 거기 더하면 요약 상한을 올릴 때 검수 컨슈머의 리밸런스 여유가 조용히 줄어든다.
    """
    wide = settings.model_copy(update={"summary_scopes_per_message": 8})

    assert wide.summary_worst_case_handle_seconds() > settings.summary_worst_case_handle_seconds()
    # 검수 예산은 요약 설정에 흔들리지 않는다.
    assert wide.worst_case_handle_seconds() == settings.worst_case_handle_seconds()
    assert settings.kafka_summary_max_poll_interval_ms != settings.kafka_max_poll_interval_ms


def test_요약_그룹이_검수_그룹과_다르다(settings: Settings) -> None:
    """같은 그룹을 쓰면 요약 쪽 리밸런스가 검수 파티션 배정을 흔든다."""
    assert settings.kafka_summary_group_id != settings.kafka_group_id
    assert settings.kafka_summary_request_topic != settings.kafka_inspection_topic


def test_요약_모델이_부팅_가격표_검증에_들어온다() -> None:
    """안 넣으면 모델 ID 오타가 ``_FALLBACK_PRICING`` 으로 조용히 15배 비싸게 계상된다."""
    validate_pricing(Settings().model_ids())

    assert Settings().summary_extract_model in Settings().model_ids()
    assert Settings().summary_narrate_model in Settings().model_ids()

    with pytest.raises(UnknownModelPricing):
        validate_pricing(Settings(summary_extract_model="없는모델").model_ids())
    with pytest.raises(UnknownModelPricing):
        validate_pricing(Settings(summary_narrate_model="없는모델").model_ids())


def test_요약이_기본으로_꺼져_있다() -> None:
    """``enable_triage`` 선례다 -- 구현하고 측정하고 기본은 끈다."""
    assert Settings().enable_review_summary is False


def test_요약_모델에_effort를_붙이지_않는다() -> None:
    """Haiku 4.5 는 ``effort`` 를 받으면 400 이고 그 400 이 ``SCHEMA_INVALID`` 로 숨는다."""
    settings = Settings()

    assert settings.summary_extract_model.startswith("claude-haiku")
    assert settings.summary_extract_effort is None
    assert settings.summary_narrate_effort is None


def test_판정_계약을_건드리지_않았다() -> None:
    """``Perspective`` 열거에 값을 더하면 백엔드 ``com.aechak.message.inspection`` 이 깨진다.

    요약 원장은 ``perspective`` 대신 ``stage`` 를 든다.
    """
    from app.schemas.inspection import Perspective
    from app.schemas.summary import SummaryCallUsage

    assert {p.value for p in Perspective} == {
        "ALL",
        "DISPLAY_ADS",
        "CONDUCT_TRADE",
        "RULE",
        "TRIAGE",
    }
    assert "perspective" not in SummaryCallUsage.model_fields
    assert "stage" in SummaryCallUsage.model_fields


def test_요약_버전이_검수_promptVersion에_섞이지_않는다() -> None:
    """``PROMPT_VERSION_MAX_LENGTH = 40`` 이고 현재 합성값이 33자라 여유가 7자다."""
    from app.inspection.pipeline import PROMPT_VERSION_MAX_LENGTH
    from app.review_summary.extract_prompt import EXTRACT_VERSION
    from app.review_summary.narrate_prompt import NARRATE_VERSION

    composed = "inspection-v1+policy-v1+triage-v2"
    assert len(composed) <= PROMPT_VERSION_MAX_LENGTH
    assert EXTRACT_VERSION not in composed
    assert NARRATE_VERSION not in composed
    # 요약 버전은 자기 필드로 나간다.
    from app.schemas.summary import ReviewSummaryMessage

    assert "extractVersion" in ReviewSummaryMessage.model_fields
    assert "narrateVersion" in ReviewSummaryMessage.model_fields


def test_선별_사유_문자열을_건드리지_않았다() -> None:
    """백엔드와의 약속이다."""
    from app.inspection.triage import TRIAGE_SKIP_REASON

    assert TRIAGE_SKIP_REASON == "TRIAGE_CLEAR"
