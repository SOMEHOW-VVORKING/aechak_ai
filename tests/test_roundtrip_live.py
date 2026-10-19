"""실제 브로커로 왕복을 돌린다.

브로커가 없는 환경에서는 건너뛴다. 이 테스트가 증명하는 것은 판정 품질이 아니라
**요청 토픽에서 판정 토픽까지 끝까지 흐른다**는 사실이다.

    AECHAK_AI_LIVE_KAFKA=localhost:19092 uv run pytest tests/test_roundtrip_live.py
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

import pytest
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from app.inspection.pipeline import InspectionPipeline
from app.kafka.runner import InspectionRunner
from app.schemas.envelope import Envelope
from app.schemas.inspection import ContentVerdict, Verdict
from app.settings import Settings

BROKERS = os.environ.get("AECHAK_AI_LIVE_KAFKA")

pytestmark = pytest.mark.skipif(not BROKERS, reason="AECHAK_AI_LIVE_KAFKA가 없으면 실 브로커 왕복을 건너뛴다")


def envelope_for(payload: dict[str, Any], event_type: str) -> bytes:
    return json.dumps(
        {
            "eventId": f"{event_type}-{payload['inspectionId']}",
            "eventType": event_type,
            "occurredAt": "2026-10-19T03:00:00Z",
            "aggregateType": "inspection",
            "orderingKey": str(payload.get("productId", 0)),
            "traceId": "live-trace-1",
            "producer": "aechak-api",
            "payload": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        },
        ensure_ascii=False,
    ).encode()


async def test_요청_토픽에서_판정_토픽까지_흐른다(contract: Any) -> None:
    settings = Settings(
        anthropic_api_key="",  # stub 모드. 모델을 부르지 않는다
        kafka_bootstrap_servers=BROKERS or "",
        kafka_inspection_topic="live.inspection.events",
        kafka_verdict_topic="live.verdict.events",
        kafka_group_id="live-roundtrip",
    )
    runner = InspectionRunner(
        settings=settings,
        pipeline=InspectionPipeline(llm=None, settings=settings),
    )

    verdict_consumer = AIOKafkaConsumer(
        settings.kafka_verdict_topic,
        bootstrap_servers=BROKERS,
        group_id="live-roundtrip-assert",
        auto_offset_reset="earliest",
    )
    producer = AIOKafkaProducer(bootstrap_servers=BROKERS)

    await runner.start()
    await verdict_consumer.start()
    await producer.start()
    try:
        # 정상 상품은 통과, 연락처가 있는 상품은 규칙 단계에서 보류
        clean = contract("product-inspection-requested.json")
        dirty = clean | {
            "inspectionId": 1002,
            "contentHash": "c" * 64,
            "description": "문의는 010-1234-5678로 주세요",
        }
        await producer.send_and_wait(
            settings.kafka_inspection_topic, envelope_for(clean, "ProductInspectionRequestedMessage")
        )
        await producer.send_and_wait(
            settings.kafka_inspection_topic, envelope_for(dirty, "ProductInspectionRequestedMessage")
        )

        received: dict[int, ContentVerdict] = {}
        async with asyncio.timeout(60):
            while len(received) < 2:
                batch = await verdict_consumer.getmany(timeout_ms=1000)
                for records in batch.values():
                    for record in records:
                        envelope = Envelope.model_validate_json(record.value.decode())
                        assert envelope.eventType == "ContentVerdictMessage"
                        assert envelope.traceId == "live-trace-1", "상관키를 승계해야 한다"
                        parsed = ContentVerdict.model_validate(envelope.payload_obj())
                        received[parsed.inspectionId] = parsed

        # stub 모드라 모델 관점이 하나도 돌지 않는다. 그래서 깨끗한 상품도 통과가 아니라
        # 판정 실패다 — 검사한 관점이 없으면 통과로 적지 않는다는 규칙을 그대로 받는다.
        # 백엔드는 판정 실패를 받으면 노출을 건드리지 않으므로 상품은 그대로 보인다.
        assert received[1001].verdict is Verdict.FAILED
        assert received[1002].verdict is Verdict.FLAG
        assert received[1002].sellerNotice is not None
        assert received[1002].calls == [], "규칙이 확정한 건에 모델을 부르면 안 된다"
    finally:
        await producer.stop()
        await verdict_consumer.stop()
        await runner.stop()
