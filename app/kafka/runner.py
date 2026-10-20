"""검수 요청을 소비하고 판정을 발행한다.

순서 규약 하나가 이 모듈의 핵심이다. **결과 발행이 브로커에게 확인된 뒤에 오프셋을 커밋한다.**
반대로 하면 판정이 사라지고, 결과를 두 번 발행하는 쪽이 판정을 잃는 쪽보다 안전하다
(백엔드가 ``processed_message``로 중복을 걸러낸다).

계약 버전 게이트도 여기 있다. 백엔드 매퍼가 모르는 필드를 조용히 버리도록 설정돼 있어서,
버전을 명시적으로 막지 않으면 드리프트가 에러 없이 흘러간다.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer

from app.inspection.pipeline import InspectionPipeline
from app.schemas.envelope import Envelope
from app.schemas.inspection import (
    SCHEMA_VERSION,
    VERDICT_AGGREGATE_TYPE,
    ProductInspectionRequest,
    ReviewInspectionRequest,
)
from app.settings import Settings

log = logging.getLogger(__name__)

_REQUEST_TYPES = {
    "ProductInspectionRequestedMessage": ProductInspectionRequest,
    "ReviewInspectionRequestedMessage": ReviewInspectionRequest,
}

_VERDICT_EVENT_TYPE = "ContentVerdictMessage"


class InspectionRunner:
    def __init__(self, *, settings: Settings, pipeline: InspectionPipeline) -> None:
        self._settings = settings
        self._pipeline = pipeline
        self._consumer: AIOKafkaConsumer | None = None
        self._producer: AIOKafkaProducer | None = None
        self._task: asyncio.Task[None] | None = None
        self._healthy = False

    @property
    def healthy(self) -> bool:
        """소비 루프가 살아 있는지. 헬스체크가 프로세스 생존만 보면 컨슈머가 죽은 것을 놓친다."""
        return self._healthy and self._task is not None and not self._task.done()

    async def start(self) -> None:
        self._consumer = AIOKafkaConsumer(
            self._settings.kafka_inspection_topic,
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            group_id=self._settings.kafka_group_id,
            # 수동 커밋이어야 발행 확인 후 커밋이라는 순서를 지킬 수 있다
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            # 한 건의 처리는 모델 호출 다섯 번까지 간다. 기본 300,000ms로는 재시도 폭풍 한 번이
            # 리밸런스를 유발하고, 커밋 안 된 오프셋이 재배달돼 같은 상품을 전액 재검수한다.
            max_poll_interval_ms=self._settings.kafka_max_poll_interval_ms,
        )
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            enable_idempotence=True,
            acks="all",
        )
        await self._consumer.start()
        await self._producer.start()
        self._healthy = True
        self._task = asyncio.create_task(self._loop(), name="inspection-consumer")
        log.info(
            "소비 시작. topic=%s group=%s",
            self._settings.kafka_inspection_topic,
            self._settings.kafka_group_id,
        )

    async def stop(self) -> None:
        self._healthy = False
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._consumer is not None:
            await self._consumer.stop()
        if self._producer is not None:
            await self._producer.stop()

    async def _loop(self) -> None:
        assert self._consumer is not None
        try:
            async for record in self._consumer:
                try:
                    await self.handle(record.value)
                except Exception:
                    # 처리 실패를 커밋하지 않는다. 다시 받아 재처리하거나 사람이 본다.
                    log.exception(
                        "검수 처리 실패. topic=%s partition=%s offset=%s",
                        record.topic,
                        record.partition,
                        record.offset,
                    )
                    continue
                await self._consumer.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            self._healthy = False
            log.exception("소비 루프가 죽었다")
            raise

    async def handle(self, raw: bytes | str) -> None:
        """엔벨로프 하나를 처리한다. 발행이 확인되면 반환하고, 그 뒤에 커밋이 일어난다."""
        payload = raw.decode() if isinstance(raw, bytes) else raw
        envelope = Envelope.model_validate_json(payload)

        request_type = _REQUEST_TYPES.get(envelope.eventType)
        if request_type is None:
            log.warning("모르는 이벤트 종류를 건너뛴다. eventType=%s", envelope.eventType)
            return

        body = envelope.payload_obj()
        received = int(body.get("schemaVersion", 0))
        if received != self._settings.supported_schema_version:
            # 조용히 흘려보내면 계약 드리프트가 운영 중에 발견된다
            raise ValueError(
                f"지원하지 않는 계약 버전이다. received={received} "
                f"supported={self._settings.supported_schema_version} eventId={envelope.eventId}"
            )

        request = request_type.model_validate(body)
        log.info(
            "검수 시작. traceId=%s inspectionId=%s target=%s",
            envelope.traceId,
            request.inspectionId,
            request.target_type.value,
        )

        verdict = await self._pipeline.run(request)

        out = Envelope.wrap(
            event_id=verdict.event_id(),
            event_type=_VERDICT_EVENT_TYPE,
            aggregate_type=VERDICT_AGGREGATE_TYPE,
            ordering_key=envelope.orderingKey,
            payload_obj=verdict,
            # 새 상관키를 만들지 않고 승계한다. 그래야 백엔드 로그와 한 키로 꿰인다.
            trace_id=envelope.traceId,
        )
        assert self._producer is not None
        # send_and_wait 이 브로커 확인까지 기다린다. 이 줄이 반환된 뒤에만 커밋한다.
        await self._producer.send_and_wait(
            self._settings.kafka_verdict_topic,
            out.to_json().encode(),
            key=envelope.orderingKey.encode(),
        )
        cost = sum(c.costMicroUsd for c in verdict.calls)
        log.info(
            "판정 발행. traceId=%s inspectionId=%s verdict=%s calls=%d costMicroUsd=%d unchecked=%d",
            envelope.traceId,
            verdict.inspectionId,
            verdict.verdict.value,
            len(verdict.calls),
            cost,
            sum(1 for p in verdict.perspectives if p.outcome.value == "UNCHECKED"),
        )


def verdict_schema_version() -> int:
    return SCHEMA_VERSION


def dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
