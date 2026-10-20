"""환경 설정. ECS task definition의 secrets로 주입받는다.

Spring Cloud AWS의 파라미터스토어 로딩은 자바 전용이라 파이썬은 쓸 수 없다.
그래서 백엔드 세 서비스와 달리 taskdef ``secrets``로 받고, execution role에
``ssm:GetParameters`` 권한이 필요하다.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# output_config.effort가 받는 등급. 기본값은 high라서 안 보내면 조용히 올라간다.
EffortGrade = Literal["low", "medium", "high", "xhigh", "max"]


def _blank_to_none(value: object) -> object:
    """빈 문자열을 미지정으로 접는다.

    ECS taskdef·.env·SSM에서 "값을 비운다"가 None이 아니라 ""로 들어온다. 그대로 두면
    ``{"effort": ""}``가 본문에 실려 전건 400이고, SCHEMA_INVALID로 분류돼 스키마 오류로 숨는다.
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


# 미지정을 허용하는 등급. Haiku 4.5처럼 effort를 받지 않는 모델에 쓴다.
OptionalEffort = Annotated[EffortGrade | None, BeforeValidator(_blank_to_none)]

# 관점 호출이 실패했을 때 다시 쏘기 전에 기다리는 시간.
# 파이프라인과 폴 간격 계산이 같은 값을 봐야 해서 설정 모듈에 둔다.
JUDGE_RETRY_DELAY_SECONDS = 2.0
# 팬아웃을 켰을 때의 관점 수. 최악 처리시간 계산에 쓴다.
_FANOUT_PERSPECTIVES = 2


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AECHAK_AI_", extra="ignore")

    # Anthropic
    anthropic_api_key: str = ""
    # 1단 관점 판정. temperature를 못 쓰므로 effort가 결정성 레버다.
    # Haiku 4.5는 effort를 보내면 400이다. 선별 계층은 output_config를 아예 보내지 않는 경로로 쓴다.
    screening_model: str = "claude-sonnet-5"
    # 등급을 비우면 거부한다. 빈 값을 미지정으로 접으면 모델 기본값(high)으로 조용히 올라간다.
    screening_effort: EffortGrade = "low"
    # 2단 에스컬레이션
    escalation_model: str = "claude-opus-5"
    escalation_effort: EffortGrade = "medium"
    # 수정 제안 생성
    notice_model: str = "claude-sonnet-5"
    notice_effort: EffortGrade = "medium"
    # 0.5단 선별. 날짜 접미가 없는 정식 ID다.
    triage_model: str = "claude-haiku-4-5"
    # Haiku 4.5는 effort를 받지 않는다. 미지정이면 output_config를 보내지 않는다.
    # 빈 문자열도 미지정이고, "none"처럼 등급이 아닌 값은 부팅에서 떨어뜨린다.
    triage_effort: OptionalEffort = None

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
    # aiokafka 기본값은 300,000ms다. 한 건의 최악 처리시간이 그걸 넘기면 리밸런스가 일어나
    # 커밋 안 된 오프셋이 재배달되고 같은 상품을 전액 재검수한다.
    # worst_case_handle_seconds()보다 넓게 두고, 테스트가 그 관계를 지킨다.
    kafka_max_poll_interval_ms: int = 600_000

    prompt_version: str = "inspection-v1"

    # 계약 버전. 모르는 major를 받으면 떨어뜨리지 않고 거절한다.
    supported_schema_version: int = 1

    log_level: str = Field(default="INFO")

    def worst_case_handle_seconds(self) -> float:
        """한 건을 처리하는 최악 wall clock.

        타임아웃·재시도 예산과 카프카 폴 간격을 한 자리에서 잇는다. ``max_tokens``나
        타임아웃을 올리면 이 값이 커지고, 폴 간격이 좁으면 테스트가 떨어진다 —
        상한만 올리고 폴 간격을 빠뜨리는 것이 실제로 밟은 함정이다.

        SDK 재시도는 0으로 꺼 두었으므로 한 ``call_tool``은 타임아웃 한 번이 상한이고,
        관점 호출만 파이프라인이 한 번 더 쏜다.
        """
        judge = _FANOUT_PERSPECTIVES * (2 * self.request_timeout_seconds + JUDGE_RETRY_DELAY_SECONDS)
        tail = self.request_timeout_seconds * 2  # 비평 + 수정 문구
        triage = self.triage_timeout_seconds if self.enable_triage else 0.0
        return triage + judge + tail

    def model_ids(self) -> tuple[str, ...]:
        """설정에 적힌 모델 ID 전부. 부팅에서 가격표와 맞춰 보는 데 쓴다."""
        return (
            self.screening_model,
            self.escalation_model,
            self.notice_model,
            self.triage_model,
        )


settings = Settings()
