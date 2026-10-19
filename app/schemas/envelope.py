"""백엔드 Envelope의 미러.

필드가 여덟 개이고 ``payload``는 객체가 아니라 **JSON 문자열**이다. 이 두 가지가
미러링에서 가장 자주 틀리는 자리이고, 틀리면 백엔드가 결과를 역직렬화하지 못한다.
표본은 백엔드 레포의 ``infra/kafka/src/test/resources/contract/``에 있고
``tests/contract/`` 에 같은 파일을 두어 왕복 테스트로 고정한다.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

PRODUCER = "aechak-ai"


class Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    eventId: str
    eventType: str
    occurredAt: datetime
    aggregateType: str
    orderingKey: str
    traceId: str
    producer: str
    payload: str

    def payload_obj(self) -> dict[str, Any]:
        return json.loads(self.payload)

    @classmethod
    def wrap(
        cls,
        *,
        event_id: str,
        event_type: str,
        aggregate_type: str,
        ordering_key: str,
        payload_obj: BaseModel,
        trace_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> Envelope:
        return cls(
            eventId=event_id,
            eventType=event_type,
            occurredAt=occurred_at or datetime.now(UTC),
            aggregateType=aggregate_type,
            orderingKey=ordering_key,
            traceId=trace_id or str(uuid4()),
            producer=PRODUCER,
            # 백엔드가 payload를 문자열로 읽으므로 객체를 넣으면 안 된다
            payload=payload_obj.model_dump_json(),
        )

    def to_json(self) -> str:
        return self.model_dump_json()
