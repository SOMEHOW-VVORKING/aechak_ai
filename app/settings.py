"""환경 설정. ECS task definition의 secrets로 주입받는다.

Spring Cloud AWS의 파라미터스토어 로딩은 자바 전용이라 파이썬은 쓸 수 없다.
그래서 백엔드 세 서비스와 달리 taskdef ``secrets``로 받고, execution role에
``ssm:GetParameters`` 권한이 필요하다.
"""

from __future__ import annotations

import math
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
    # 선별도 좁게 두지 않는다. 툴의 ``reason``에 길이 제약을 걸 수 없어서(strict 모드가
    # maxLength 를 받지 않는다) 사유가 길어지면 응답이 끊기고, 끊긴 호출은 TRUNCATED 승급이
    # 되어 Haiku 비용을 쓰고도 Sonnet 2회를 그대로 태운다 -- 선별이 순수 손실인 건이다.
    # 한국어 사유는 글자당 토큰이 1을 넘나든다.
    max_tokens_triage: int = 1024
    # 선별이 전역 상한을 붙잡으면 깨끗한 건이 오히려 느려진다.
    triage_timeout_seconds: float = 15.0

    # 파이프라인 토글. 노드를 측정으로 남기고 빼기 위해 설정으로 갈라 둔다.
    enable_escalation: bool = True
    enable_notice: bool = True
    perspective_fanout: bool = True
    # 기본 꺼짐. 백엔드 열거에 TRIAGE가 없는 동안 켜면 판정 전건이 격리 토픽으로 간다.
    enable_triage: bool = False

    # --- 리뷰 요약(SCRUM-310). 검수와 모델·토픽·폴 간격을 공유하지 않는다 ------------
    #
    # 기본 모델이 Haiku 4.5 인 이유가 둘이다. 같은 토큰에서 Sonnet 5 의 절반이고, 더
    # 중요하게 Sonnet 5 는 ``thinking`` 을 생략하면 adaptive 가 켜져 **사고 토큰이 출력
    # 단가로 과금되는데 산출물 글자 수에는 나타나지 않는다.** 억제 레버는 effort 하나이고
    # thinking 을 끄려면 손대지 않기로 한 client.py 를 고쳐야 한다.
    summary_extract_model: str = "claude-haiku-4-5"
    # Haiku 4.5 는 effort 를 받으면 400 이고 그 400 이 SCHEMA_INVALID 로 숨는다.
    # 미지정이면 client 가 output_config 를 아예 보내지 않는다.
    summary_extract_effort: OptionalEffort = None
    summary_narrate_model: str = "claude-haiku-4-5"
    summary_narrate_effort: OptionalEffort = None
    # 배치 10건 x 항목 2~4개 + 펫 맥락. 좁으면 TRUNCATED 로 떨어져 비용을 쓰고도 못 쓴다.
    max_tokens_summary_extract: int = 4096
    max_tokens_summary_narrate: int = 2048
    summary_timeout_seconds: float = 60.0
    # 메시지당 상한 둘. **폴 간격 예산이 이 두 수를 정한다**(아래 산수 참조).
    summary_extract_batch_max: int = 10
    # **메시지당 신규 리뷰 상한.** 이 수가 최악 처리시간의 배치 항을 정하고, 그것이 폴
    # 간격과 묶여 있다. 계약에서 ``ReviewSummaryRequest.newReviews`` 의 ``max_length``
    # 가 이 값을 쓴다 -- 상한이 없으면 인기 상품의 디바운스 배치 60건이 최악 976초가 되어
    # 폴 간격 900초를 넘기고, 넘기면 브로커가 멤버를 빼고 컨슈머 태스크가 죽는다.
    summary_max_new_reviews: int = 40
    summary_scopes_per_message: int = 2
    # 리뷰 본문 절단. 올리면 추출 입력 토큰이 그만큼 오른다.
    # 리뷰 최대 길이는 백엔드 ReviewConstraints.CONTENT_MAX = 1000 이다.
    summary_content_chars: int = 400
    # 임계 넷. 비용으로는 차이가 없고(미달이면 모델을 안 부른다) **거짓 일반화 위험으로만**
    # 정해야 한다. 리뷰 2건으로 "반복되는 장점"을 적는 것이 이 값들이 막는 것이다.
    summary_min_reviews: int = 10
    summary_min_segment_reviews: int = 8
    summary_min_aspect_mentions: int = 3
    summary_min_aspect_share: float = 0.10
    # 요약 몫의 일 예산. daily_cost_alert_usd 안의 몫이고 경보는 어느 기능이 태웠는지
    # 말해 주지 않는다 -- 기능별 경보 분리는 아직 없다.
    summary_daily_budget_usd: float = 2.0
    # 기본 꺼짐. enable_triage 선례 -- 구현하고 측정하고 기본은 끈다.
    enable_review_summary: bool = False

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

    # 요약은 **전용 토픽과 전용 컨슈머 그룹**이다. 기존 검수 토픽에 새 eventType 을 흘리면
    # runner._REQUEST_TYPES 가 모르는 종류를 경고 후 그대로 커밋해 **영구히 버린다** --
    # 구버전 AI 가 배포돼 있으면 배포 순서가 데이터 손실을 만든다. 신규 토픽은 컨슈머가
    # 없으면 메시지가 기다린다.
    kafka_summary_request_topic: str = "aechak.summary.requests"
    kafka_summary_result_topic: str = "aechak.summary.results"
    kafka_summary_group_id: str = "aechak-ai-summarizer"
    # 검수 폴 간격과 **따로** 둔다. 섞으면 요약 쪽 상한을 올릴 때 검수 리밸런스가 흔들린다.
    kafka_summary_max_poll_interval_ms: int = 900_000

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

    def summary_worst_case_handle_seconds(self) -> float:
        """요약 메시지 한 건의 최악 wall clock. **검수 예산과 섞지 않는다.**

        검수의 ``worst_case_handle_seconds()`` 는 ``tests/test_runner.py`` 가 검수 폴
        간격과의 관계를 단언한다. 요약을 거기 더하면 요약 상한을 올릴 때 검수 컨슈머의
        리밸런스 여유가 조용히 줄어든다. 그래서 식도 폴 간격도 따로 둔다.

        **상한만 올리고 폴 간격을 빠뜨리는 것이 이 레포가 실제로 밟은 함정이다.**
        추출 배치 수와 스코프 수를 올리면 이 값이 커지고, 폴 간격이 좁으면 테스트가 떨어진다.

        **배치를 1회로 가정했던 것이 틀렸다.** ``service`` 는 리뷰 수에 비례해 배치를
        만들고(``env.batches``) ``newReviews`` 에는 상한이 없었다. 실제 최악은
        ``한 호출 x (배치 수 + 스코프 수)`` 이고 리뷰 60건이면 976초로 폴 간격 900초를
        넘긴다. 인기 상품의 디바운스 배치가 60건이 되는 것은 평범하다. 그래서 식을
        실제 배치 수로 바꾸고 리뷰 수에 상한을 걸었다 -- 둘 다 해야 관계가 닫힌다.
        """
        one_call = 2 * self.summary_timeout_seconds + JUDGE_RETRY_DELAY_SECONDS
        batches = math.ceil(self.summary_max_new_reviews / max(1, self.summary_extract_batch_max))
        return one_call * (batches + self.summary_scopes_per_message)

    def model_ids(self) -> tuple[str, ...]:
        """설정에 적힌 모델 ID 전부. 부팅에서 가격표와 맞춰 보는 데 쓴다.

        요약 모델을 **반드시** 넣는다. 안 넣으면 ``validate_pricing`` 의 부팅 검증을 받지
        못하고, 모델 ID 를 오타 내도 ``_FALLBACK_PRICING (15.0, 75.0)`` 으로 조용히 15배
        비싸게 계상된다.
        """
        return (
            self.screening_model,
            self.escalation_model,
            self.notice_model,
            self.triage_model,
            self.summary_extract_model,
            self.summary_narrate_model,
        )


settings = Settings()
