"""요약 요청을 소비하고 요약을 발행한다. **전용 토픽, 전용 컨슈머 그룹.**

## 왜 기존 검수 토픽에 얹지 않는가

``app/kafka/runner.py`` 의 ``_REQUEST_TYPES`` 는 eventType 2종만 매핑하고 **모르는
eventType 은 경고 후 ``return`` 하고 바깥 루프가 그대로 커밋한다.** 구버전 AI 가 배포돼
있으면 새 eventType 이 담긴 이벤트를 **영구히 버린다** -- 배포 순서가 데이터 손실을 만드는
구조다. 신규 토픽은 컨슈머가 없으면 메시지가 **기다린다.**

같은 이유로 이 러너는 **모르는 eventType 을 커밋하지 않는다.** 검수 토픽은 여러 종류가
흐르는 공용 토픽이라 건너뛰는 것이 맞았지만, 이 토픽에 오는 것은 전부 우리 것이다.
모르는 종류가 보이면 그것은 배포 순서가 어긋났다는 뜻이고, 기다리는 것이 사실에 맞다.

## 커밋은 **레코드별 오프셋**이다. 인자 없는 ``commit()`` 으로는 위의 약속이 거짓이다

처음 판은 ``continue`` 로 커밋을 건너뛰고 다음 레코드로 갔다. 그런데 인자 없는
``consumer.commit()`` 은 **구독한 전 파티션의 현재 소비 위치**를 커밋한다(aiokafka 문서:
"Defaults to current consumed offsets for all subscribed partitions"). 그래서 다음
레코드가 성공하면 그 커밋이 **건너뛴 레코드까지 넘어서** 오프셋을 올렸다. 활성 토픽에서는
그것이 정상 경로이고, 결과적으로 이 모듈 머리글이 "영구히 버린다"고 비판한 바로 그 데이터
손실이 여기서도 일어났다.

그래서 셋을 바꿨다.

1. 성공한 레코드는 ``commit({TopicPartition(topic, partition): offset + 1})`` 로 **자기
   오프셋만** 커밋한다.
2. 어떤 레코드를 건너뛰면 **그 파티션을 막는다.** 뒤 레코드가 성공해도 커밋하지 않는다 --
   커밋하면 건너뛴 오프셋을 넘어선다. 다른 파티션은 멈추지 않는다.
3. 커밋 실패(``CommitFailedError``: 리밸런스로 멤버십이 바뀌었다)를 **따로 잡는다.**
   전에는 ``commit()`` 이 ``handle`` 을 감싼 내부 try 밖이라 바깥 ``except`` 로 빠져
   ``_healthy=False`` 후 ``raise`` 했고, ``_loop`` 태스크가 죽었다. 다시 띄우는 코드가
   없고 ``/health`` 는 검수만 보므로 200 UP 이라 ECS 가 태스크를 교체하지 않는다 --
   요약이 영구히 멈추고 아무도 깨지 않는다. 커밋 실패는 리밸런스 신호이고 그 메시지
   하나의 실패가 컨슈머 전체를 세울 이유가 아니다.

## 왜 HTTP 를 열지 않는가

``app/main.py`` 머리글의 판단을 뒤집지 않는다 -- "공개 ALB 뒤에 있으므로 관리용 경로를
열어 두면 이름이 internal 이어도 외부에서 닿는다". 요약은 배치성이라 동기 경로가 필요
없고, 사전계산 + DB 서빙으로 **조회 비용 0원, 조회 지연 0ms** 를 얻는다.

## 실패 처리 — 유료 재배달 루프를 만들지 않는다

모델 호출 실패는 ``state = FAILED`` 결과를 발행하고 **오프셋을 커밋한다.** 유료 호출이
들어간 경로에서 커밋을 빼면 지출이 무한 루프가 된다. 커밋하지 않는 경우는 계약 버전
불일치와 모르는 eventType 둘뿐이고, 둘 다 코드를 고쳐야 풀리는 상태다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.errors import CommitFailedError

from app.review_summary.service import SummaryService
from app.schemas.envelope import Envelope
from app.schemas.summary import (
    SUMMARY_AGGREGATE_TYPE,
    SUMMARY_REQUEST_EVENT_TYPE,
    SUMMARY_RESULT_EVENT_TYPE,
    ReviewSummaryRequest,
)
from app.settings import Settings

log = logging.getLogger(__name__)


class SummaryRunner:
    def __init__(self, *, settings: Settings, service: SummaryService) -> None:
        self._settings = settings
        self._service = service
        self._consumer: AIOKafkaConsumer | None = None
        self._producer: AIOKafkaProducer | None = None
        self._task: asyncio.Task[None] | None = None
        self._healthy = False

    @property
    def healthy(self) -> bool:
        return self._healthy and self._task is not None and not self._task.done()

    async def start(self) -> None:
        self._consumer = AIOKafkaConsumer(
            self._settings.kafka_summary_request_topic,
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            group_id=self._settings.kafka_summary_group_id,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            # **검수 폴 간격과 따로 둔다.** 한 메시지가 추출 1배치 + 서술 N회를 태우므로
            # ``summary_worst_case_handle_seconds()`` 가 이 값보다 좁아야 하고,
            # 테스트가 그 관계를 단언한다. 상한만 올리고 폴 간격을 빠뜨리는 것이 이 레포가
            # 실제로 밟은 함정이다.
            max_poll_interval_ms=self._settings.kafka_summary_max_poll_interval_ms,
        )
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            enable_idempotence=True,
            acks="all",
        )
        await self._consumer.start()
        await self._producer.start()
        self._healthy = True
        self._task = asyncio.create_task(self._loop(), name="summary-consumer")
        log.info(
            "요약 소비 시작. topic=%s group=%s",
            self._settings.kafka_summary_request_topic,
            self._settings.kafka_summary_group_id,
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
        # 건너뛴 레코드가 있는 파티션. 그 파티션은 **더 커밋하지 않는다** -- 커밋하면
        # 건너뛴 오프셋을 넘어서 영구히 버린다. 다른 파티션은 계속 흐른다.
        blocked: set[tuple[str, int]] = set()
        try:
            async for record in self._consumer:
                partition = (record.topic, record.partition)
                try:
                    await self.handle(record.value)
                except Exception:
                    blocked.add(partition)
                    log.exception(
                        "요약 처리 실패. 이 파티션의 커밋을 멈춘다. topic=%s partition=%s offset=%s",
                        record.topic,
                        record.partition,
                        record.offset,
                    )
                    continue
                if partition in blocked:
                    log.warning(
                        "앞 레코드를 건너뛴 파티션이라 커밋하지 않는다. topic=%s partition=%s offset=%s",
                        record.topic,
                        record.partition,
                        record.offset,
                    )
                    continue
                await self._commit_one(record)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._healthy = False
            log.exception("요약 소비 루프가 죽었다")
            raise

    async def _commit_one(self, record: Any) -> None:
        """그 레코드의 오프셋만 커밋한다. **실패가 루프를 죽이지 않는다.**

        ``CommitFailedError`` 는 리밸런스로 멤버십이 바뀐 신호다. 그때 예외를 밖으로
        내보내면 소비 태스크가 죽고, 다시 띄우는 코드가 없다. 커밋하지 못한 메시지는
        재배달되므로 **기다리는 쪽이 사실에 맞다.**
        """
        assert self._consumer is not None
        try:
            await self._consumer.commit({TopicPartition(record.topic, record.partition): record.offset + 1})
        except CommitFailedError:
            log.warning(
                "커밋이 리밸런스에 밀렸다. 재배달을 기다린다. topic=%s partition=%s offset=%s",
                record.topic,
                record.partition,
                record.offset,
            )

    async def handle(self, raw: bytes | str) -> None:
        payload = raw.decode() if isinstance(raw, bytes) else raw
        envelope = Envelope.model_validate_json(payload)

        if envelope.eventType != SUMMARY_REQUEST_EVENT_TYPE:
            # 전용 토픽이다. 모르는 종류를 커밋하면 영구히 버린다 -- 기다리는 쪽이 맞다.
            raise ValueError(
                f"요약 토픽에 모르는 이벤트 종류가 왔다. eventType={envelope.eventType} "
                f"eventId={envelope.eventId}. 배포 순서가 어긋났을 수 있다"
            )

        body = envelope.payload_obj()
        received = int(body.get("schemaVersion", 0))
        if received != self._settings.supported_schema_version:
            raise ValueError(
                f"지원하지 않는 계약 버전이다. received={received} "
                f"supported={self._settings.supported_schema_version} eventId={envelope.eventId}"
            )

        request = ReviewSummaryRequest.model_validate(body)
        log.info(
            "요약 시작. traceId=%s requestId=%s productId=%s newReviews=%d scopes=%d",
            envelope.traceId,
            request.requestId,
            request.productId,
            len(request.newReviews),
            len(request.requestedScopes),
        )

        message = await self._service.run(request)

        out = Envelope.wrap(
            event_id=message.event_id(),
            event_type=SUMMARY_RESULT_EVENT_TYPE,
            aggregate_type=SUMMARY_AGGREGATE_TYPE,
            # orderingKey 를 승계한다. 백엔드가 ``orderingKey = productId`` 로 보내므로
            # 한 상품의 요약 결과가 같은 파티션에서 순서대로 흐른다 -- 그 순서가 백엔드의
            # "K건마다 재생성" 카운터를 락 없이 유지할 수 있게 하는 선물이다.
            ordering_key=envelope.orderingKey,
            payload_obj=message,
            trace_id=envelope.traceId,
        )
        assert self._producer is not None
        # 발행이 브로커에게 확인된 뒤에만 커밋한다. 호출부가 그 순서를 지킨다.
        await self._producer.send_and_wait(
            self._settings.kafka_summary_result_topic,
            out.to_json().encode(),
            key=envelope.orderingKey.encode(),
        )
        cost = sum(call.costMicroUsd for call in message.calls)
        log.info(
            "요약 발행. traceId=%s requestId=%s state=%s calls=%d costMicroUsd=%d unextracted=%d",
            envelope.traceId,
            message.requestId,
            message.state.value,
            len(message.calls),
            cost,
            sum(1 for row in message.extracted if row.extracted.value == "UNEXTRACTED"),
        )
