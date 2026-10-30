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

## 실패 처리 — 유료 재배달 루프를 만들지 않는다. **그리고 실패를 등급으로 가른다**

모델 호출 실패는 ``state = FAILED`` 결과를 발행하고 **오프셋을 커밋한다.** 유료 호출이
들어간 경로에서 커밋을 빼면 지출이 무한 루프가 된다.

그 뒤의 실패를 둘로 가른다. **가르지 않았던 것이 결함이었다.**

- ``SummaryBlocked`` -- **코드나 설정을 고쳐야 풀리는 상태.** 계약 버전 불일치, 모르는
  eventType, 결과가 발행 상한을 넘는 것. 커밋하지 않고 **그 파티션만 막는다.** 재배달로
  풀리지 않으므로 다시 받아도 같은 자리에서 멈춘다.
- ``SummaryPublishFailed`` -- **재배달로 풀리는 상태.** 발행을 ``kafka_publish_attempts``
  번까지 다시 쏘고도 실패한 경우다. 커밋하지 않고 **소비 루프를 세운다.**

머리글의 앞 판이 "커밋하지 않는 경우는 계약 버전 불일치와 모르는 eventType 둘뿐"이라고
적었지만 **사실이 아니었다.** ``send_and_wait`` 가 ``handle()`` 안이라 발행 실패가 같은
분기로 떨어져 그 파티션을 **영구히** 막았다. 그리고 ``/health`` 가 검수만 보았으므로
아무도 깨지 않았다.

**요청 토픽은 1파티션이 아니다.** 앞 판 머리글이 "자동 생성이라 1파티션"을 전제로 "한 번의
브로커 단절이 전 상품을 세운다"고 적었는데, 백엔드가 요청 토픽을 **3파티션**으로 만든다
(``ReviewSummaryTopicConfig``. 그 KDoc 이 "AI 서버는 처리할 수 없는 요청이 오면 그 파티션을
멈추므로, 요청 토픽을 나눠 한 상품이 전체를 세우지 않게 함"이라고 적었다 -- 이 머리글을 읽고
그렇게 설계했다). 결과·DLT 는 1파티션이다. 발행 키가 상품(``orderingKey``)이라 한 상품은
늘 같은 파티션으로 가므로, **막힌 파티션 하나는 전 상품이 아니라 그 파티션에 해시되는
상품들을 세운다.** 그 설정 빈에는 ``spring.kafka.bootstrap-servers`` 조건이 걸려 있어
카프카를 끈 환경에서는 토픽을 만들지 않고, 그때는 ``KAFKA_AUTO_CREATE_TOPICS_ENABLE`` 의
1파티션 자동 생성이 그대로다.

그래서 셋을 함께 고친다. (1) 발행 실패를 위 등급으로 내려 재배달에 맡기고, (2) 발행을
유료 호출을 다시 태우지 않고 **발행만** 다시 쏘고, (3) ``app/main.py`` 의 ``/health`` 가
켜 둔 요약 러너의 생존을 상태에 반영한다. 막힌 파티션은 ``blocked_partitions`` 로 세어
헬스 응답에 함께 싣는다 -- 요청 파티션이 셋이므로 그 숫자는 "셋 중 몇이 막혔는가"이고
3이면 요약이 전 상품에서 멈춘 것이다. 경보 주체는 아직 백엔드 선결 목록에 있지만, 숫자가
응답에 없으면 그 경보를 붙일 재료조차 없다.

**막힌 파티션으로 헬스를 내리지는 않는다.** 요약 러너는 검수와 같은 프로세스이고, 내리면
ECS 가 태스크를 교체해 **검수까지** 선다. 결정적 실패라면 그 교체가 반복되므로 요약 쪽
버그 하나가 검수를 영구히 멈추게 만든다. 커밋하지 않은 레코드는 잃지 않으므로, 막아 두고
숫자로 보이는 쪽을 고른다. 발행 실패는 반대다 -- 브로커가 안 되는 상황이면 검수도 이미
못 돌고 있으므로 태스크 교체가 맞는 처방이다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition
from aiokafka.errors import (
    CommitFailedError,
    KafkaError,
    MessageSizeTooLargeError,
    RecordTooLargeError,
    UnknownTopicOrPartitionError,
)
from aiokafka.record.default_records import DefaultRecordBatchBuilder

from app.review_summary.budget import worst_case_result_bytes_for
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


class SummaryBlocked(ValueError):
    """코드나 설정을 고쳐야 풀리는 실패. **그 파티션을 막고 커밋하지 않는다.**

    재배달로 풀리지 않으므로 다시 받아도 같은 자리에서 멈춘다. 기다리는 쪽이 사실에 맞다.

    ``ValueError`` 를 상속한다 -- 앞 판이 계약 실패를 ``ValueError`` 로 던졌고 호출부와
    테스트가 그 약속을 보고 있다. 등급을 가르면서 그 약속을 깨지 않는다.
    """


class SummaryPublishFailed(RuntimeError):
    """결과 발행이 재시도까지 쓰고도 실패했다. **재배달로 풀린다.**

    파티션을 막지 않는다 -- 막으면 브로커 단절 한 번이 영구 정지가 된다. 요청 토픽이
    3파티션이라 막히는 것은 그 파티션의 상품들이지만, 브로커가 안 되는 상황은 세 파티션에
    똑같이 일어나므로 결과는 전 상품 정지와 같다.
    """


class SummaryRunner:
    def __init__(self, *, settings: Settings, service: SummaryService) -> None:
        self._settings = settings
        self._service = service
        self._consumer: AIOKafkaConsumer | None = None
        self._producer: AIOKafkaProducer | None = None
        self._task: asyncio.Task[None] | None = None
        self._healthy = False
        self._blocked: frozenset[tuple[str, int]] = frozenset()

    @property
    def healthy(self) -> bool:
        return self._healthy and self._task is not None and not self._task.done()

    @property
    def blocked_partitions(self) -> int:
        """커밋을 멈춘 파티션 수. **헬스 응답에 숫자로 싣는다.**

        0이 아니면 그 파티션의 메시지가 쌓이고 있다는 뜻이고, 코드나 설정을 고쳐야 풀린다.
        요청 토픽이 3파티션이므로(``ReviewSummaryTopicConfig``) 1이면 그 파티션에 해시되는
        상품들만, 3이면 전 상품의 요약이 멈춘 것이다.
        경보 주체는 아직 없지만 숫자가 응답에 없으면 경보를 붙일 재료조차 없다.
        """
        return len(self._blocked)

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
            # **요청 크기 상한을 설정으로 올려 둔다.** 백엔드가 발행하는 쪽이라 우리가 쓸
            # 수 있는 레버는 이것뿐이고, 기본값에 기대면 ``summary_content_chars`` 를 올린
            # 날 요청이 흐르지 않는다. 하네스가 그 여유를 숫자로 찍는다.
            max_partition_fetch_bytes=self._settings.kafka_max_partition_fetch_bytes,
        )
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            enable_idempotence=True,
            acks="all",
            # **설정 정본을 넘긴다.** 기본값에 기대면 발행 전 크기 검사가 보는 상한과
            # 프로듀서가 거부하는 상한이 갈라지고, 그때 검사는 통과하고 발행이 터진다.
            max_request_size=self._settings.kafka_max_request_size,
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
                except SummaryPublishFailed:
                    # **재배달로 풀리는 실패다.** 파티션을 막으면 영구 정지이고, 브로커가
                    # 안 되는 상황은 세 파티션에 똑같이 온다. 커밋하지 않고 루프를 세운다 --
                    # 그래야 ``/health`` 가 내려가고 태스크가 교체되며 레코드가 다시 온다.
                    self._healthy = False
                    log.exception(
                        "요약 결과 발행이 실패했다. 커밋하지 않고 소비를 세운다. "
                        "topic=%s partition=%s offset=%s",
                        record.topic,
                        record.partition,
                        record.offset,
                    )
                    raise
                except Exception:
                    blocked.add(partition)
                    self._blocked = frozenset(blocked)
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

    # 재배달로 풀리지 않는 발행 실패. **재시도 집합에서 뺀다.**
    #
    # 셋 다 ``KafkaError`` 서브클래스라 앞 판의 ``except KafkaError`` 가 전부 재시도했고,
    # 3회 실패 뒤 ``SummaryPublishFailed`` 로 루프가 죽었다. 그러면 ``/health`` 가 503 이
    # 되고 ECS 가 태스크를 교체하는데 **같은 프로세스의 검수 컨슈머가 함께 죽는다.** 커밋하지
    # 않은 레코드가 재배달되어 같은 결정적 실패를 다시 내므로 무한 교체 루프다. 결과 토픽이
    # 없는 클러스터가 그 모양의 대표 사례이고, 머리글의 전제("브로커가 안 되는 상황이면 검수도
    # 이미 못 돌고 있다")가 **결과 토픽 한정 실패에서 거짓이다.**
    _NOT_RETRYABLE = (MessageSizeTooLargeError, RecordTooLargeError, UnknownTopicOrPartitionError)

    async def _publish(self, payload: bytes, *, key: bytes) -> None:
        """결과를 발행한다. **유료 호출을 다시 태우지 않고 발행만 다시 쏜다.**

        크기 초과는 재시도로 풀리지 않으므로 **쏘기 전에** 보고 ``SummaryBlocked`` 로 가른다.
        그 상한을 넘기는 레버가 설정에 둘이고(``summary_scopes_per_message``,
        ``summary_max_new_reviews``) 그 관계는 ``app.review_summary.budget`` 이 조립해
        숫자로 찍고 테스트가 묶는다.

        **사전 검사의 경계를 프로듀서와 같게 맞춘다.** 앞 판은 ``len(payload)`` 로 value
        바이트만 셌는데 aiokafka 는 ``estimate_size_in_bytes(key, value, headers)`` 로 재고
        그 값이 레코드 오버헤드(~88B)와 key 길이를 더한다. 그 차이만큼의 창에서는 사전 검사가
        통과하고 브로커 직전에 ``MessageSizeTooLargeError`` 가 나 **분류가 뒤집혔다.**
        """
        assert self._producer is not None
        settings = self._settings
        estimated = DefaultRecordBatchBuilder.estimate_size_in_bytes(key, payload, [])
        if estimated > settings.kafka_max_request_size:
            raise SummaryBlocked(
                f"요약 결과가 발행 상한을 넘는다. bytes={len(payload)} "
                f"recordBytes={estimated} max_request_size={settings.kafka_max_request_size}. "
                "결과 크기를 키우는 설정은 summary_scopes_per_message 와 summary_max_new_reviews "
                "둘이다 -- 그 둘을 내려야 풀린다"
            )

        attempts = max(1, settings.kafka_publish_attempts)
        for attempt in range(1, attempts + 1):
            try:
                await self._producer.send_and_wait(settings.kafka_summary_result_topic, payload, key=key)
                return
            except self._NOT_RETRYABLE as error:
                raise SummaryBlocked(
                    f"재배달로 풀리지 않는 발행 실패다: {type(error).__name__}: {error}. "
                    "이 파티션만 막는다 -- 재시도하면 같은 실패가 반복되고 루프가 죽으면 "
                    "같은 프로세스의 검수까지 교체로 멈춘다"
                ) from error
            except KafkaError as error:
                if attempt >= attempts:
                    raise SummaryPublishFailed(
                        f"요약 결과 발행이 {attempts}회 모두 실패했다: {error}"
                    ) from error
                log.warning(
                    "요약 결과 발행 실패. 발행만 다시 쏜다. attempt=%d/%d error=%s",
                    attempt,
                    attempts,
                    error,
                )
                await asyncio.sleep(settings.kafka_publish_retry_seconds)

    async def handle(self, raw: bytes | str) -> None:
        payload = raw.decode() if isinstance(raw, bytes) else raw
        envelope = Envelope.model_validate_json(payload)

        if envelope.eventType != SUMMARY_REQUEST_EVENT_TYPE:
            # 전용 토픽이다. 모르는 종류를 커밋하면 영구히 버린다 -- 기다리는 쪽이 맞다.
            raise SummaryBlocked(
                f"요약 토픽에 모르는 이벤트 종류가 왔다. eventType={envelope.eventType} "
                f"eventId={envelope.eventId}. 배포 순서가 어긋났을 수 있다"
            )

        body = envelope.payload_obj()
        received = int(body.get("schemaVersion", 0))
        if received != self._settings.supported_schema_version:
            raise SummaryBlocked(
                f"지원하지 않는 계약 버전이다. received={received} "
                f"supported={self._settings.supported_schema_version} eventId={envelope.eventId}"
            )

        request = ReviewSummaryRequest.model_validate(body)

        # **크기 초과를 모델을 부르기 전에 판정한다.** 앞 판은 유료 호출을 다 태운 뒤 발행
        # 직전에 봤고, 그래서 ``SummaryBlocked`` 가 커밋 없이 떨어지면 태스크 교체마다 그
        # 상품의 추출+서술 비용이 처음부터 다시 나갔다(파티션 blocked 는 프로세스 로컬이다).
        # 모듈 머리글이 "유료 호출이 들어간 경로에서 커밋을 빼면 지출이 무한 루프가 된다"를
        # 원칙으로 적어 뒀고 크기 하나가 그 원칙을 어기고 있었다. 이제 요청의 모양만으로
        # 상한을 조립해 넘을 수 있으면 0원에 가른다.
        worst = worst_case_result_bytes_for(
            self._settings,
            scopes=len(request.requestedScopes),
            new_reviews=len(request.newReviews),
        )
        if worst > self._settings.kafka_max_request_size:
            raise SummaryBlocked(
                f"이 요청의 결과는 최악에서 발행 상한을 넘는다. worstCaseBytes={worst} "
                f"max_request_size={self._settings.kafka_max_request_size} "
                f"scopes={len(request.requestedScopes)} newReviews={len(request.newReviews)}. "
                "모델을 부르지 않았다 -- 불렀다면 그 비용이 교체마다 다시 나간다"
            )

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
            # orderingKey 를 승계한다. 그래야 한 상품의 결과가 **같은 파티션**으로 간다.
            #
            # **그것이 순서를 보장한다고 적지 않는다.** 백엔드 계약이 스스로 부인한다 --
            # ``IntegrationMessage`` 의 KDoc 이 "같은 키는 같은 파티션으로 감. 발행 순서는
            # 보장 안 함"이라고 적어 뒀고, 아웃박스가 ``afterCommit`` 비동기 즉시 발행 +
            # 실패 시 스위퍼 재발행이라 **같은 상품의 두 요청이 뒤집혀 도착할 수 있다.**
            # 앞 판 주석은 그 순서를 "백엔드가 K건 카운터를 락 없이 유지할 수 있게 하는
            # 선물"이라고 적었는데, 백엔드가 그 말을 믿고 멱등과 단조 가드를 생략하면
            # ``mentionCount`` 가 절대값이라 **늦게 온 옛 결과가 최신 집계를 더 작은 수로
            # 덮는다.** 되돌릴 방법이 없다(원본 추출 결과를 들고 있지 않으면 전 코퍼스
            # 재추출이다). 그래서 백엔드 선결 목록에 셋을 적었다 -- 리뷰별 행으로 저장하고
            # 집계는 SQL 로 다시 세기, ``analyzedReviewCount`` 단조 가드, 인박스 멱등.
            ordering_key=envelope.orderingKey,
            payload_obj=message,
            trace_id=envelope.traceId,
        )
        # 발행이 브로커에게 확인된 뒤에만 커밋한다. 호출부가 그 순서를 지킨다.
        await self._publish(out.to_json().encode(), key=envelope.orderingKey.encode())
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
