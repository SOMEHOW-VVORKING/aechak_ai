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


# --- 건너뛴 레코드를 다음 성공이 함께 커밋하지 않는다 -----------------------------------


class _Record:
    def __init__(self, topic: str, partition: int, offset: int, value: bytes) -> None:
        self.topic = topic
        self.partition = partition
        self.offset = offset
        self.value = value


class _RecordingConsumer:
    """``_loop`` 를 실제로 돌려 **커밋 인자**를 본다.

    기존 테스트는 ``handle()`` 이 던지는지와 ``producer.sent == []`` 만 봤다. 그래서
    ``commit()`` 을 인자 없이 부르는 것이 **건너뛴 레코드까지 넘어서** 오프셋을 올리는
    것을 아무도 보지 못했다 -- aiokafka 문서가 "Defaults to current consumed offsets for
    all subscribed partitions" 라고 적는다.
    """

    def __init__(self, records: list[_Record]) -> None:
        self._records = records
        self.commits: list[Any] = []

    def __aiter__(self) -> Any:
        self._iter = iter(self._records)
        return self

    async def __anext__(self) -> _Record:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration from None

    async def commit(self, offsets: Any = None) -> None:
        self.commits.append(offsets)

    async def stop(self) -> None:
        return None


async def test_모르는_이벤트를_건너뛴_뒤_다음_성공이_그_오프셋을_커밋하지_않는다(
    settings: Settings,
) -> None:
    """**"모르는 eventType 을 커밋하지 않는다"가 실제로는 거짓이었다.**

    인자 없는 ``commit()`` 은 소비 위치를 커밋하므로, 다음 레코드가 성공하면 그 커밋이
    건너뛴 레코드까지 넘어서 오프셋을 올린다. 활성 토픽에서는 그것이 정상 경로다.
    """
    from aiokafka import TopicPartition

    topic = settings.kafka_summary_request_topic
    unknown = _Record(topic, 0, 10, _envelope(_payload(), event_type="SomethingElseMessage").encode())
    good = _Record(topic, 0, 11, _envelope(_payload()).encode())
    consumer = _RecordingConsumer([unknown, good])

    runner = _runner(settings, FakeProducer())
    runner._consumer = consumer  # type: ignore[attr-defined]
    await runner._loop()  # type: ignore[attr-defined]

    # 같은 파티션에서 앞 레코드를 건너뛰었으므로 **그 뒤 오프셋을 커밋하지 않는다.**
    assert consumer.commits == [], consumer.commits
    assert TopicPartition(topic, 0) not in [k for c in consumer.commits if c for k in c]


async def test_성공한_레코드는_자기_오프셋만_커밋한다(settings: Settings) -> None:
    """레코드별 커밋이어야 건너뛴 레코드가 남는다."""
    from aiokafka import TopicPartition

    topic = settings.kafka_summary_request_topic
    consumer = _RecordingConsumer([_Record(topic, 3, 7, _envelope(_payload()).encode())])

    runner = _runner(settings, FakeProducer())
    runner._consumer = consumer  # type: ignore[attr-defined]
    await runner._loop()  # type: ignore[attr-defined]

    assert consumer.commits == [{TopicPartition(topic, 3): 8}]


async def test_건너뛴_파티션의_뒤_레코드도_커밋되지_않는다(settings: Settings) -> None:
    """다른 파티션은 멈추지 않는다. 손실은 파티션 단위로만 막는다."""
    from aiokafka import TopicPartition

    topic = settings.kafka_summary_request_topic
    consumer = _RecordingConsumer(
        [
            _Record(topic, 0, 10, _envelope(_payload(), event_type="Nope").encode()),
            _Record(topic, 0, 11, _envelope(_payload()).encode()),
            _Record(topic, 1, 4, _envelope(_payload()).encode()),
        ]
    )

    runner = _runner(settings, FakeProducer())
    runner._consumer = consumer  # type: ignore[attr-defined]
    await runner._loop()  # type: ignore[attr-defined]

    assert consumer.commits == [{TopicPartition(topic, 1): 5}]


async def test_폴_간격_식이_실제_배치_수를_쓴다(settings: Settings) -> None:
    """**식이 추출 배치를 1회로 가정했다.**

    ``service`` 는 리뷰 수에 비례해 배치를 만들고 ``newReviews`` 에 상한이 없었다.
    실제 최악은 ``한 호출 x (배치 수 + 스코프 수)`` 이고, 리뷰 60건이면 976초로
    폴 간격 900초를 넘긴다.
    """
    import math

    batches = math.ceil(settings.summary_max_new_reviews / settings.summary_extract_batch_max)
    one_call = 2 * settings.summary_timeout_seconds + 2.0
    # 발행 재시도도 wall clock 이다. 식에서 빼면 재시도를 늘리는 변경이 폴 간격을 조용히 먹는다.
    publish = (settings.kafka_publish_attempts - 1) * settings.kafka_publish_retry_seconds
    expected = one_call * (batches + settings.summary_scopes_per_message) + publish

    assert settings.summary_worst_case_handle_seconds() == expected
    assert settings.kafka_summary_max_poll_interval_ms / 1000 >= expected, (
        "폴 간격이 실제 최악 처리시간보다 좁다. 리뷰 상한이나 배치 상한을 올렸으면 폴 간격도 같이 올려야 한다"
    )


async def test_신규_리뷰_수에_상한이_있다() -> None:
    """상한이 없으면 최악 처리시간이 폴 간격을 넘겨 컨슈머 태스크가 죽는다.

    죽으면 다시 띄우는 코드가 없고 ``/health`` 는 200 UP 이라 ECS 가 교체하지 않는다.
    커밋도 안 됐으므로 리밸런스마다 같은 메시지를 다시 받아 추출 호출만 또 태운다.
    """
    from pydantic import ValidationError

    from app.schemas.summary import ReviewSummaryRequest

    limit = Settings().summary_max_new_reviews
    too_many = [
        {"reviewId": i, "rating": 5, "content": f"리뷰 {i}", "optionName": "기본"} for i in range(limit + 1)
    ]

    with pytest.raises(ValidationError):
        ReviewSummaryRequest(requestId="r", productId=1, newReviews=too_many)


async def test_커밋_실패가_루프를_조용히_죽이지_않는다(settings: Settings) -> None:
    """``commit()`` 이 ``handle`` 을 감싼 내부 try **밖**에 있어 바깥으로 빠져나갔다.

    바깥 ``except`` 가 ``_healthy=False`` 후 ``raise`` 해서 ``_loop`` 태스크가 죽고,
    다시 띄우는 코드가 없다. 커밋 실패는 리밸런스 신호이고 **그 메시지 하나의 실패가
    컨슈머 전체를 세울 이유가 아니다.**
    """
    from aiokafka.errors import CommitFailedError

    topic = settings.kafka_summary_request_topic

    class _FailingCommit(_RecordingConsumer):
        async def commit(self, offsets: Any = None) -> None:
            self.commits.append(offsets)
            raise CommitFailedError("membership changed")

    consumer = _FailingCommit(
        [
            _Record(topic, 0, 1, _envelope(_payload()).encode()),
            _Record(topic, 0, 2, _envelope(_payload()).encode()),
        ]
    )
    runner = _runner(settings, FakeProducer())
    runner._consumer = consumer  # type: ignore[attr-defined]

    # 루프가 예외로 죽지 않는다. 두 레코드를 다 지나고 정상 종료한다.
    await runner._loop()  # type: ignore[attr-defined]

    assert len(consumer.commits) == 2


async def test_리뷰_상한의_계약_사본이_설정과_같다() -> None:
    """두 수가 갈라지면 계약이 받아들인 메시지가 폴 간격 예산을 넘긴다.

    계약은 ``app.settings`` 를 import 하지 않는다(스키마가 설정에 의존하면 백엔드와
    공유하는 모양이 환경에 흔들린다). 그래서 사본을 두고 **그 사본의 표류를 테스트가 막는다.**
    """
    from app.schemas.summary import _MAX_NEW_REVIEWS

    assert _MAX_NEW_REVIEWS == Settings().summary_max_new_reviews


# --- 발행 실패를 계약 실패와 같은 등급으로 두지 않는다 -----------------------------------


class _FailingProducer(FakeProducer):
    """``send_and_wait`` 이 끝까지 실패하는 프로듀서."""

    def __init__(self, *, failures: int) -> None:
        super().__init__()
        self.attempts = 0
        self._failures = failures

    async def send_and_wait(self, topic: str, value: bytes, key: bytes | None = None) -> None:
        self.attempts += 1
        if self.attempts <= self._failures:
            from aiokafka.errors import KafkaTimeoutError

            raise KafkaTimeoutError("브로커가 응답하지 않는다")
        await super().send_and_wait(topic, value, key)


async def test_발행_실패가_그_파티션을_영구히_막지_않는다(settings: Settings) -> None:
    """**머리글이 "커밋하지 않는 경우는 둘뿐"이라 적었지만 사실이 아니었다.**

    ``send_and_wait`` 이 ``handle()`` 안이라 발행 실패가 계약 불일치와 같은 분기로 떨어져
    그 파티션을 **영구히** 막았다. 토픽이 1파티션이면 전 상품의 요약이 멈추고, ``/health``
    는 검수만 보므로 아무도 깨지 않는다. 발행 실패는 코드를 고쳐야 풀리는 상태가 아니라
    재배달로 풀리는 상태다 -- 루프를 세우고 커밋하지 않는 쪽이 사실에 맞다.
    """
    topic = settings.kafka_summary_request_topic
    consumer = _RecordingConsumer(
        [
            _Record(topic, 0, 10, _envelope(_payload()).encode()),
            _Record(topic, 0, 11, _envelope(_payload()).encode()),
        ]
    )
    producer = _FailingProducer(failures=99)
    runner = _runner(settings, producer)
    runner._consumer = consumer  # type: ignore[attr-defined]
    runner._healthy = True  # type: ignore[attr-defined]

    from app.kafka.summary_runner import SummaryPublishFailed

    with pytest.raises(SummaryPublishFailed):
        await runner._loop()  # type: ignore[attr-defined]

    # 커밋하지 않았으므로 **잃은 것이 없다.** 그리고 루프가 살아서 조용히 도는 것이 아니라
    # 죽었고, 그 사실이 헬스에 보인다 -- 그것이 재배달을 받는 유일한 길이다.
    # (``healthy`` 프로퍼티는 태스크 생존까지 보므로 직접 돌린 루프에서는 플래그를 본다.)
    assert consumer.commits == []
    assert runner._healthy is False  # type: ignore[attr-defined]
    assert runner.blocked_partitions == 0, "발행 실패로 파티션을 막으면 영구 정지다"


async def test_발행이_한_번_실패하면_모델을_다시_부르지_않고_발행만_다시_한다(
    settings: Settings,
) -> None:
    """유료 호출을 다시 태우지 않는다. 재시도 대상은 **발행뿐**이다."""
    topic = settings.kafka_summary_request_topic
    consumer = _RecordingConsumer([_Record(topic, 0, 5, _envelope(_payload()).encode())])
    producer = _FailingProducer(failures=1)
    runner = _runner(settings, producer)
    runner._consumer = consumer  # type: ignore[attr-defined]
    runner._healthy = True  # type: ignore[attr-defined]

    runs: list[int] = []
    original = runner._service.run

    async def counted(request: Any) -> Any:
        runs.append(1)
        return await original(request)

    runner._service.run = counted  # type: ignore[assignment]
    await runner._loop()  # type: ignore[attr-defined]

    from aiokafka import TopicPartition

    assert len(runs) == 1, "발행 재시도가 모델 호출까지 다시 태웠다"
    assert producer.attempts == 2
    assert len(producer.sent) == 1
    assert consumer.commits == [{TopicPartition(topic, 0): 6}]


async def test_발행_상한을_넘는_결과는_발행하지_않고_파티션을_막는다(settings: Settings) -> None:
    """**설정 하나를 올리면 터지는 자리를 코드가 먼저 본다.**

    ``summary_content_chars`` 나 ``summary_scopes_per_message`` 를 올리면 결과 엔벨로프가
    프로듀서의 ``max_request_size`` 를 넘고, 그 실패가 지금은 발행 실패로 떨어져 파티션을
    세운다. 크기 초과는 재배달로 풀리지 않는다 -- 코드나 설정을 고쳐야 하는 상태이므로
    **발행을 시도하지 않고** 그 파티션만 막는다.
    """
    small = settings.model_copy(update={"kafka_max_request_size": 200})
    topic = small.kafka_summary_request_topic
    consumer = _RecordingConsumer([_Record(topic, 0, 1, _envelope(_payload()).encode())])
    producer = FakeProducer()
    runner = _runner(small, producer)
    runner._consumer = consumer  # type: ignore[attr-defined]
    runner._healthy = True  # type: ignore[attr-defined]

    await runner._loop()  # type: ignore[attr-defined]

    assert producer.sent == [], "상한을 넘는 메시지를 브로커에 보냈다"
    assert consumer.commits == []
    assert runner._healthy is True, "크기 초과가 컨슈머 전체를 세울 이유는 아니다"  # type: ignore[attr-defined]
    assert runner.blocked_partitions == 1


async def test_계약_상한을_꽉_채운_결과가_발행_상한_안에_든다() -> None:
    """**세 설정과 ``max_request_size`` 의 관계를 테스트가 묶는다.**

    ``summary_content_chars`` 400->800 과 ``summary_scopes_per_message`` 2->5 가 각각
    상한을 넘긴다(실측). 그 관계가 테스트에 없으면 설정 한 줄을 올린 배포에서 요약이
    멈추고, 멈춘 것을 아무도 모른다.
    """
    from harness.run_summary import worst_case_result_bytes

    settings = Settings()
    worst = worst_case_result_bytes(settings)

    assert worst < settings.kafka_max_request_size, (
        f"계약 상한을 꽉 채운 결과가 {worst}B 로 발행 상한 "
        f"{settings.kafka_max_request_size}B 를 넘는다. 상한을 올렸으면 "
        "max_request_size 도 같이 올려야 한다"
    )


async def test_요약이_켜졌는데_러너가_죽으면_헬스가_내려간다(monkeypatch: pytest.MonkeyPatch) -> None:
    """**감시 공백이 코드에 적혀 있었다.** 이 필드를 보는 주체가 없다고.

    ``enable_review_summary`` 가 참인데 요약 러너가 죽으면 전용 토픽의 메시지가 영구히
    쌓이는데 ``/health`` 는 200 UP 이었다. 켜 둔 기능이 죽은 것은 DOWN 이다 -- 끈
    배포에서는 영구 503 이 되지 않게 **켜졌을 때만** 본다.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.main import settings as app_settings

    class _Dead:
        healthy = False
        blocked_partitions = 1

    class _Alive:
        healthy = True
        blocked_partitions = 0

    monkeypatch.setattr(app_settings, "enable_review_summary", True)
    # **lifespan 을 돌리지 않는다.** 돌리면 실제 브로커에 붙으려 한다. 라우트만 부르고
    # 러너 자리에는 생존 여부만 답하는 대역을 넣는다.
    app.state.runner = _Alive()
    app.state.summary_runner = _Dead()
    response = TestClient(app).get("/health")

    assert response.status_code == 503, response.json()
    body = response.json()
    assert body["reviewSummaryConsuming"] is False
    assert body["status"] == "DOWN"


# --- 분류 경계를 프로듀서와 같게 맞춘다 -----------------------------------------------------
#
# 사전 검사가 ``len(payload)`` 로 **value 바이트만** 셌다. aiokafka 는
# ``DefaultRecordBatchBuilder.estimate_size_in_bytes(key, value, headers)`` 로 재고 그 값이
# 레코드 오버헤드와 key 길이를 더한다. 그래서 그 차이만큼의 창에서는 사전 검사가 통과하고
# 브로커 직전에 ``MessageSizeTooLargeError`` 가 난다 -- 그것이 ``KafkaError`` 서브클래스라
# 재시도 집합에 들어가고, 3회 실패 뒤 ``SummaryPublishFailed`` 로 루프가 죽는다.
# ``/health`` 가 503 이 되어 ECS 가 태스크를 교체하면 **같은 프로세스의 검수 컨슈머가 함께
# 죽고**, 커밋하지 않은 레코드가 재배달되어 같은 결정적 실패를 다시 낸다 = 무한 교체 루프.


def _size_window_settings(settings: Settings, payload_bytes: int) -> Settings:
    return settings.model_copy(update={"kafka_max_request_size": payload_bytes})


async def test_레코드_오버헤드가_사전_검사와_프로듀서를_갈라놓지_않는다(settings: Settings) -> None:
    """**사전 검사가 통과하면 프로듀서도 통과해야 한다.** 그 사이가 크래시 루프 창이었다."""
    from aiokafka.record.default_records import DefaultRecordBatchBuilder

    from app.kafka.summary_runner import SummaryBlocked

    key = b"42"
    payload = b"x" * 4_000
    broker_size = DefaultRecordBatchBuilder.estimate_size_in_bytes(key, payload, [])
    assert broker_size > len(payload), "이 테스트의 전제(오버헤드가 있다)가 깨졌다"

    # 상한을 value 바이트와 정확히 같게 두면 앞 판의 사전 검사는 통과했고 프로듀서는 거부했다.
    runner = _runner(_size_window_settings(settings, len(payload)), FakeProducer())
    with pytest.raises(SummaryBlocked):
        await runner._publish(payload, key=key)  # type: ignore[attr-defined]


async def test_크기_초과를_발행_실패로_분류하지_않는다(settings: Settings) -> None:
    """``MessageSizeTooLargeError`` 는 재배달로 풀리지 않는다. 재시도 집합에서 뺀다."""
    from aiokafka.errors import MessageSizeTooLargeError

    from app.kafka.summary_runner import SummaryBlocked

    class _TooLarge(FakeProducer):
        async def send_and_wait(self, topic: str, value: bytes, key: bytes | None = None) -> None:
            self.sent.append((topic, value, key))
            raise MessageSizeTooLargeError("브로커 상한을 넘는다")

    producer = _TooLarge()
    # 사전 검사를 통과할 만큼 상한을 넉넉히 두고, 브로커가 거부하는 상황만 만든다.
    runner = _runner(settings, producer)
    with pytest.raises(SummaryBlocked):
        await runner._publish(b"y" * 100, key=b"42")  # type: ignore[attr-defined]

    assert len(producer.sent) == 1, "재시도로 브로커를 세 번 때렸다"


async def test_없는_토픽을_발행_실패로_분류하지_않는다(settings: Settings) -> None:
    """결과 토픽이 없고 auto-create 가 꺼진 클러스터는 **코드·운영을 고쳐야** 풀린다.

    머리글의 전제("브로커가 안 되는 상황이면 검수도 이미 못 돌고 있다")가 결과 토픽 한정
    실패에서 거짓이다. 그 경우 요약 쪽 결함 하나가 같은 프로세스의 검수를 영구히 멈춘다.
    """
    from aiokafka.errors import UnknownTopicOrPartitionError

    from app.kafka.summary_runner import SummaryBlocked

    class _NoTopic(FakeProducer):
        async def send_and_wait(self, topic: str, value: bytes, key: bytes | None = None) -> None:
            self.sent.append((topic, value, key))
            raise UnknownTopicOrPartitionError("결과 토픽이 없다")

    producer = _NoTopic()
    runner = _runner(settings, producer)
    with pytest.raises(SummaryBlocked):
        await runner._publish(b"z" * 100, key=b"42")  # type: ignore[attr-defined]

    assert len(producer.sent) == 1


async def test_발행_상한을_브로커_상한_위로_올리면_기동이_거부된다() -> None:
    """``SummaryBlocked`` 안내문이 사람에게 "상한도 같이 올려라"를 **권했다.**

    5MiB 로 올리면 사전 검사는 영구히 안 걸리고 브로커가 1MiB 에서 거부해 **전건이**
    크래시 루프 경로로 간다. 그래서 그 조합을 기동 시점에 거부하고 안내문에서도 뺀다.
    """
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        Settings(kafka_max_request_size=5 * 1_048_576, kafka_broker_max_message_bytes=1_048_576)


async def test_크기_초과_안내문이_상한을_올리라고_권하지_않는다(settings: Settings) -> None:
    from app.kafka.summary_runner import SummaryBlocked

    runner = _runner(_size_window_settings(settings, 10), FakeProducer())
    with pytest.raises(SummaryBlocked) as caught:
        await runner._publish(b"x" * 100, key=b"42")  # type: ignore[attr-defined]

    assert "올려야 한다" not in str(caught.value)


# --- 유료 호출을 태운 뒤 커밋 없이 떨어지지 않는다 -------------------------------------------


async def test_크기_초과가_모델을_부르기_전에_판정된다(settings: Settings) -> None:
    """**머리글이 금지한 지출 루프다.**

    ``handle()`` 순서가 ``service.run``(추출 배치 + 서술 N회 = 과금) -> ``Envelope.wrap``
    -> ``_publish`` 이고 크기 검사가 ``_publish`` **안**이었다. 그래서 결과가 발행 상한을
    넘는 요청은 전체 파이프라인 비용을 다 태운 뒤 ``SummaryBlocked`` 로 **커밋되지 않는다.**
    파티션 blocked 상태는 프로세스 로컬이라 배포·스케일·리밸런스·헬스 플랩으로 태스크가
    교체되면 그 레코드가 다시 와서 추출+서술을 처음부터 다시 과금한다.
    """
    small = settings.model_copy(update={"kafka_max_request_size": 2_000})
    topic = small.kafka_summary_request_topic
    consumer = _RecordingConsumer([_Record(topic, 0, 1, _envelope(_payload()).encode())])
    runner = _runner(small, FakeProducer())
    runner._consumer = consumer  # type: ignore[attr-defined]
    runner._healthy = True  # type: ignore[attr-defined]

    runs: list[int] = []
    original = runner._service.run

    async def counted(request: Any) -> Any:
        runs.append(1)
        return await original(request)

    runner._service.run = counted  # type: ignore[assignment]
    await runner._loop()  # type: ignore[attr-defined]

    assert runs == [], "크기 초과가 확실한 요청에 유료 호출을 태웠다"
    assert runner.blocked_partitions == 1
    assert runner._healthy is True  # type: ignore[attr-defined]


async def test_모델을_부르기_전_크기_상한이_최악_결과에서_나온다() -> None:
    """선판정의 분모가 **요청의 모양**(스코프 수·신규 리뷰 수)에서 조립돼야 한다."""
    from app.kafka.summary_runner import worst_case_result_bytes_for

    settings = Settings()
    one = worst_case_result_bytes_for(settings, scopes=1)
    two = worst_case_result_bytes_for(settings, scopes=2)

    assert one < two, "스코프를 늘렸는데 상한이 늘지 않는다"
    assert two <= settings.kafka_max_request_size


# --- 계약 상한과 설정이 어긋나면 기동에서 거부한다 ------------------------------------------


async def test_리뷰_상한을_계약_위로_올리면_기동이_거부된다() -> None:
    """**이 레버를 올리면 요청마다 ``ValidationError`` 다.**

    계약의 ``_MAX_NEW_REVIEWS`` 가 40 으로 하드코딩이고 ``newReviews`` 에 ``max_length`` 로
    박혀 있다. 설정만 100 으로 올리면 ``model_validate`` 가 요청마다 터지고, 그 예외는
    ``_loop`` 의 일반 ``except`` 로 떨어져 **그 파티션을 영구히 막는다.** 토픽이 1파티션이라
    전 상품 요약 정지이고 ``/health`` 는 설계상 UP 을 유지한다(막힌 파티션으로 상태를
    내리지 않는다). 같음을 단언하는 테스트가 **기본값에서만** 성립했으므로 CI 는 통과했다.
    """
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        Settings(summary_max_new_reviews=100)


async def test_최악_요청_바이트가_레버를_올려도_터지지_않는다() -> None:
    """리포트 함수가 숫자 대신 예외를 내던 자리다. 스키마 상한으로 자르고 그 사실을 적는다."""
    from harness.run_summary import worst_case_request_bytes

    settings = Settings()
    raised = settings.model_copy(update={"summary_max_new_reviews": 100})

    assert worst_case_request_bytes(raised) == worst_case_request_bytes(settings)


async def test_상한_여유_리포트가_계약과의_어긋남을_적는다() -> None:
    from harness.run_summary import _report_publish_budget

    settings = Settings().model_copy(update={"summary_max_new_reviews": 100})
    rows = dict(_report_publish_budget(settings))

    assert any("계약 상한" in key or "계약" in value for key, value in rows.items())
    assert any("40" in value for value in rows.values()), rows
