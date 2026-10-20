"""환경 설정. ECS task definition의 secrets로 주입받는다.

Spring Cloud AWS의 파라미터스토어 로딩은 자바 전용이라 파이썬은 쓸 수 없다.
그래서 백엔드 세 서비스와 달리 taskdef ``secrets``로 받고, execution role에
``ssm:GetParameters`` 권한이 필요하다.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AECHAK_AI_", extra="ignore")

    # Anthropic
    anthropic_api_key: str = ""
    # 1단 관점 판정. temperature를 못 쓰므로 effort가 결정성 레버다.
    # Haiku 4.5는 effort를 보내면 400이다. 선별 계층은 output_config를 아예 보내지 않는 경로로 쓴다.
    screening_model: str = "claude-sonnet-5"
    screening_effort: str = "low"
    # 2단 에스컬레이션. 이미지를 함께 본다.
    escalation_model: str = "claude-opus-5"
    escalation_effort: str = "medium"
    # 수정 제안 생성
    notice_model: str = "claude-sonnet-5"
    notice_effort: str = "medium"
    # 0.5단 선별. 날짜 접미가 없는 정식 ID다.
    triage_model: str = "claude-haiku-4-5"
    # Haiku 4.5는 effort를 받지 않는다. None이면 output_config를 보내지 않는다.
    triage_effort: str | None = None

    request_timeout_seconds: float = 60.0
    # 사고 토큰과 tool_use가 한 상한을 나눠 쓴다. 상한이 좁으면 tool_use 블록이 안 나와
    # SCHEMA_INVALID로 떨어진다. max_tokens는 지출이 아니라 상한이라 넉넉히 둔다.
    max_tokens_screening: int = 8192
    max_tokens_escalation: int = 8192
    max_tokens_notice: int = 4096
    max_tokens_triage: int = 512
    # 선별이 전역 상한을 붙잡으면 깨끗한 건이 오히려 느려진다.
    triage_timeout_seconds: float = 15.0

    # 파이프라인 토글. 노드를 측정으로 남기고 빼기 위해 설정으로 갈라 둔다.
    enable_escalation: bool = True
    enable_notice: bool = True
    perspective_fanout: bool = True
    # 기본 꺼짐. 백엔드 열거에 TRIAGE가 없는 동안 켜면 판정 전건이 격리 토픽으로 간다.
    enable_triage: bool = False

    # 예산 경보. 프로세스 로컬이라 강제력이 없다.
    # 실제 상한은 워크스페이스 spend limit이 걸고, 이 값은 경보용이다.
    daily_cost_alert_usd: float = 5.0

    # Kafka
    kafka_bootstrap_servers: str = "localhost:9092"
    kafka_inspection_topic: str = "aechak.inspection.events"
    kafka_verdict_topic: str = "aechak.verdict.events"
    kafka_group_id: str = "aechak-ai-inspector"

    prompt_version: str = "inspection-v1"

    # 계약 버전. 모르는 major를 받으면 떨어뜨리지 않고 거절한다.
    supported_schema_version: int = 1

    log_level: str = Field(default="INFO")

    def model_ids(self) -> tuple[str, ...]:
        """설정에 적힌 모델 ID 전부. 부팅에서 가격표와 맞춰 보는 데 쓴다."""
        return (
            self.screening_model,
            self.escalation_model,
            self.notice_model,
            self.triage_model,
        )


settings = Settings()
