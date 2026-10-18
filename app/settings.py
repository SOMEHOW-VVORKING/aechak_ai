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
    # Haiku 4.5는 effort를 보내면 400이라 소형 등급으로 쓸 수 없다.
    screening_model: str = "claude-sonnet-5"
    screening_effort: str = "low"
    # 2단 에스컬레이션. 이미지를 함께 본다.
    escalation_model: str = "claude-opus-5"
    escalation_effort: str = "medium"
    # 수정 제안 생성
    notice_model: str = "claude-sonnet-5"
    notice_effort: str = "medium"

    request_timeout_seconds: float = 60.0
    max_tokens_screening: int = 2048
    max_tokens_escalation: int = 3072
    max_tokens_notice: int = 1024

    # 파이프라인 토글. 노드를 측정으로 남기고 빼기 위해 설정으로 갈라 둔다.
    enable_escalation: bool = True
    enable_notice: bool = True
    perspective_fanout: bool = True

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


settings = Settings()
