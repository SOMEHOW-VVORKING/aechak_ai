"""소비 규약을 고정한다.

여기서 잡는 것은 두 가지다. 계약 버전을 조용히 넘기지 않는지, 그리고 결과 발행이
확인된 뒤에 오프셋이 커밋되는지다. 후자가 뒤바뀌면 판정이 영구히 사라진다.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import FakeLlmClient, ok

from app.inspection.pipeline import InspectionPipeline
from app.kafka.runner import InspectionRunner
from app.schemas.envelope import Envelope
from app.schemas.inspection import ContentVerdict
from app.settings import Settings


class FakeProducer:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, bytes]] = []
        self._fail = fail

    async def send_and_wait(self, topic: str, value: bytes, key: bytes | None = None) -> None:
        if self._fail:
            raise RuntimeError("브로커 거절")
        self.sent.append((topic, value))


class RecordingConsumer:
    """커밋 호출을 기록한다. 발행보다 먼저 커밋되면 그 순서가 드러난다."""

    def __init__(self, producer: FakeProducer) -> None:
        self.commits = 0
        self._producer = producer
        self.commit_order: list[str] = []

    async def commit(self) -> None:
        self.commits += 1
        self.commit_order.append(f"commit-after-{len(self._producer.sent)}-sends")


def envelope_for(payload: dict[str, Any], event_type: str) -> str:
    return json.dumps(
        {
            "eventId": "product-inspection-1001:requested",
            "eventType": event_type,
            "occurredAt": "2026-10-19T03:00:00Z",
            "aggregateType": "inspection",
            "orderingKey": "42",
            "traceId": "trace-xyz",
            "producer": "aechak-api",
            "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
        ensure_ascii=False,
    )


def runner_with(settings: Settings, producer: FakeProducer, scripted: list[Any]) -> InspectionRunner:
    pipeline = InspectionPipeline(llm=FakeLlmClient(scripted) if scripted else None, settings=settings)
    runner = InspectionRunner(settings=settings, pipeline=pipeline)
    runner._producer = producer
    return runner


async def test_판정이_판정_토픽으로_나간다(settings: Settings, contract: Any) -> None:
    producer = FakeProducer()
    runner = runner_with(settings, producer, [ok({"findings": []}), ok({"findings": []})])

    await runner.handle(
        envelope_for(contract("product-inspection-requested.json"), "ProductInspectionRequestedMessage")
    )

    assert len(producer.sent) == 1
    topic, raw = producer.sent[0]
    assert topic == settings.kafka_verdict_topic
    out = Envelope.model_validate_json(raw.decode())
    assert out.eventType == "ContentVerdictMessage"
    assert out.traceId == "trace-xyz", "수신 상관키를 승계해야 백엔드 로그와 꿰인다"
    verdict = ContentVerdict.model_validate(out.payload_obj())
    assert verdict.inspectionId == 1001


async def test_모르는_계약_버전은_거절한다(settings: Settings, contract: Any) -> None:
    payload = contract("product-inspection-requested.json") | {"schemaVersion": 99}
    producer = FakeProducer()
    runner = runner_with(settings, producer, [])

    with pytest.raises(ValueError, match="지원하지 않는 계약 버전"):
        await runner.handle(envelope_for(payload, "ProductInspectionRequestedMessage"))

    assert producer.sent == [], "모르는 버전을 통과시키면 드리프트가 운영 중에 발견된다"


async def test_모르는_이벤트_종류는_건너뛴다(settings: Settings, contract: Any) -> None:
    producer = FakeProducer()
    runner = runner_with(settings, producer, [])

    await runner.handle(envelope_for(contract("product-inspection-requested.json"), "SomethingElse"))

    assert producer.sent == []


async def test_발행이_실패하면_예외가_올라가_커밋되지_않는다(settings: Settings, contract: Any) -> None:
    producer = FakeProducer(fail=True)
    runner = runner_with(settings, producer, [ok({"findings": []}), ok({"findings": []})])

    with pytest.raises(RuntimeError, match="브로커 거절"):
        await runner.handle(
            envelope_for(contract("product-inspection-requested.json"), "ProductInspectionRequestedMessage")
        )

    assert producer.sent == []


async def test_커밋은_발행이_확인된_뒤에_일어난다(settings: Settings, contract: Any) -> None:
    producer = FakeProducer()
    consumer = RecordingConsumer(producer)
    runner = runner_with(settings, producer, [ok({"findings": []}), ok({"findings": []})])

    # 소비 루프가 하는 순서를 그대로 흉내 낸다
    await runner.handle(
        envelope_for(contract("product-inspection-requested.json"), "ProductInspectionRequestedMessage")
    )
    await consumer.commit()

    assert consumer.commit_order == ["commit-after-1-sends"], (
        "발행 전에 커밋하면 판정이 사라지고 다시 만들 방법이 없다"
    )


async def test_리뷰_요청도_같은_토픽으로_들어온다(settings: Settings, contract: Any) -> None:
    producer = FakeProducer()
    runner = runner_with(settings, producer, [ok({"findings": []}), ok({"findings": []})])

    await runner.handle(
        envelope_for(contract("review-inspection-requested.json"), "ReviewInspectionRequestedMessage")
    )

    out = Envelope.model_validate_json(producer.sent[0][1].decode())
    verdict = ContentVerdict.model_validate(out.payload_obj())
    assert verdict.targetRef == 77
    assert verdict.sellerNotice is None
