"""환경 설정. ECS task definition의 secrets로 주입받는다.

Spring Cloud AWS의 파라미터스토어 로딩은 자바 전용이라 파이썬은 쓸 수 없다.
그래서 백엔드 세 서비스와 달리 taskdef ``secrets``로 받고, execution role에
``ssm:GetParameters`` 권한이 필요하다.
"""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, model_validator
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
    max_tokens_notice: int = 4096
    # 선별도 좁게 두지 않는다. 툴의 ``reason``에 길이 제약을 걸 수 없어서(strict 모드가
    # maxLength 를 받지 않는다) 사유가 길어지면 응답이 끊기고, 끊긴 호출은 TRUNCATED 승급이
    # 되어 Haiku 비용을 쓰고도 Sonnet 2회를 그대로 태운다 -- 선별이 순수 손실인 건이다.
    # 한국어 사유는 글자당 토큰이 1을 넘나든다.
    max_tokens_triage: int = 1024
    # 선별이 전역 상한을 붙잡으면 깨끗한 건이 오히려 느려진다.
    triage_timeout_seconds: float = 15.0

    # 파이프라인 토글. 노드를 측정으로 남기고 빼기 위해 설정으로 갈라 둔다.
    #
    # ``enable_escalation`` 이 있었다. 비평 노드를 뺐으므로 토글도 뺀다 -- 끄는 레버만 남기면
    # 켜도 아무 일이 없는 설정이 되어 운영에서 "켰는데 왜 안 도나"가 된다.
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

    # --- 셀러 폼 초안(SCRUM-311). 동기 요청이고 **검수 컨슈머 경로가 아니다** ----------
    #
    # Haiku 4.5 를 기본으로 둔 이유가 요약과 같다. Sonnet 5 는 ``thinking`` 을 생략하면
    # adaptive 가 켜져 **사고 토큰이 출력 단가로 과금되는데 초안 글자 수에는 나타나지
    # 않는다.** 억제 레버는 effort 하나이고, thinking 을 끄려면 손대지 않기로 한
    # client.py 를 고쳐야 한다. Haiku 는 건당 비용이 고정이고 분산이 0이다.
    draft_model: str = "claude-haiku-4-5"
    # Haiku 4.5 는 effort 를 받으면 400 이고 그 400 이 SCHEMA_INVALID 로 숨는다.
    draft_effort: OptionalEffort = None
    # 설명 슬롯 4개 + 옵션 2그룹 + 리프 + basis. 좁으면 TRUNCATED 로 떨어져 비용을 쓰고도 못 쓴다.
    max_tokens_draft: int = 2048
    # **셀러가 화면에서 기다린다.** 전역 60초는 너무 길다.
    draft_timeout_seconds: float = 25.0
    # 설명 전체 길이 상한. 출력 토큰이 비용의 지배 항이라 이 수가 곧 비용 레버다.
    draft_description_chars: int = 400
    # 이미지 입력 방식. **기본 꺼짐이고 하네스 숫자가 켠다**(enable_triage 선례 --
    # 구현하고 측정하고 기본은 끈다). OFF | GATED | ALWAYS.
    #   OFF    이미지를 받아도 쓰지 않고 imageUsability = NO_IMAGE_SENT
    #   GATED  어휘 게이트가 품목을 확정하지 못했을 때만 쓴다  <- 조건부 멀티모달
    #   ALWAYS 항상 쓴다
    draft_image_mode: str = "OFF"
    # 해상도 예산의 정본. ~1,024 사방 상한이고 넘으면 거부하면서 필요 치수를 적어 돌려준다.
    # AI 서버는 리사이즈하지 않는다(의존성 금지) -- 축소는 셀러센터가 한다.
    draft_image_max_tokens: int = 1400
    # 초안 재사용 캐시. **프로세스 로컬 LRU 뿐이고 재배포에 사라진다** -- AI 서버가 DB 에
    # 붙지 않는 불변식 때문이다. 용도는 셀러가 같은 버튼을 연타하는 것을 막는 것까지이고,
    # 진짜 재사용은 백엔드의 초안 스냅샷 칼럼으로 민다. 0 이면 끈다(하네스가 그렇게 쓴다).
    draft_cache_entries: int = 64
    # 내부 호출용 공유 비밀. **비어 있으면 초안 앱이 전건을 거부한다**(fail closed) --
    # SSM 파라미터가 빠진 배포에서 인증 없는 경로가 열리는 쪽이 더 나쁜 실패다.
    draft_shared_secret: str = ""
    # 기본 꺼짐. 켜는 조건(하네스 숫자 몇을 넘으면)은 사람이 정한다.
    enable_draft: bool = False
    # 초안 몫의 일 예산. daily_cost_alert_usd 안의 몫이고 경보는 어느 기능이 태웠는지
    # 말해 주지 않는다 -- 기능별 경보 분리는 아직 없다.
    draft_daily_budget_usd: float = 1.0

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

    # 발행 본문 상한. aiokafka 기본값과 같은 수를 **설정으로 올려 둔다** -- 기본값에 기대면
    # 발행 전 크기 검사가 보는 상한과 프로듀서가 거부하는 상한이 갈라진다.
    #
    # 계약 상한을 꽉 채운 요약 결과가 이 안에 드는지를 테스트가 단언한다. 결과 쪽을 넘기는
    # 레버는 둘이고(``summary_scopes_per_message``·``summary_max_new_reviews``) 운영에서
    # 설정 한 줄을 올리면 발행이 터진다 -- 그 실패는 재배달로 풀리지 않는다.
    kafka_max_request_size: int = 1_048_576
    # 브로커의 ``max.message.bytes``. **이 값을 넘는 발행 상한을 기동에서 거부한다.**
    #
    # 앞 판은 ``SummaryBlocked`` 안내문이 사람에게 "상한도 같이 올려야 한다"고 **권했다.**
    # 권한 대로 5MiB 로 올리면 우리 쪽 사전 검사는 영구히 안 걸리고 브로커가 1MiB 에서
    # 거부한다. 그 실패는 ``MessageSizeTooLargeError`` 가 아니라 브로커 응답이라 **전건이**
    # 재시도 경로로 갔고, 요약 쪽 결함 하나가 같은 프로세스의 검수까지 교체로 멈췄다.
    # 두 수의 관계를 설정에 적어 두고 어긋나면 기동하지 않는다 -- 운영에서 브로커 쪽을
    # 먼저 올렸으면 이 값을 함께 올려야 한다.
    kafka_broker_max_message_bytes: int = 1_048_576
    # 수신 본문 상한. 요청을 발행하는 쪽은 백엔드이므로 **요청 크기는 이쪽 상한에 걸린다.**
    # ``summary_content_chars`` 가 그 레버다(리뷰 본문이 메시지에 그대로 실린다).
    kafka_max_partition_fetch_bytes: int = 1_048_576
    # 결과 발행을 몇 번까지 다시 쏘는가. **모델 호출을 다시 태우지 않고 발행만 다시 쏜다.**
    kafka_publish_attempts: int = 3
    kafka_publish_retry_seconds: float = 2.0

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

        **초안(SCRUM-311)은 이 식에 들어오지 않는다.** 카프카 컨슈머 경로가 아니라 별 ASGI
        앱의 동기 HTTP 요청이고, 같은 이벤트 루프에 두지 않는다. 그 사실을 여기 적어 두는
        이유는 이 함수가 폴 간격과 묶여 있어서 **모델 호출을 더하면서 폴 간격을 함께 올리는
        것을 빠뜨리는 함정을 이 레포가 이미 한 번 밟았기** 때문이다.
        """
        judge = _FANOUT_PERSPECTIVES * (2 * self.request_timeout_seconds + JUDGE_RETRY_DELAY_SECONDS)
        # 수정 문구 한 번. 비평을 뺐으므로 종전의 두 배가 아니다 -- 줄이는 쪽이라 폴 간격과의
        # 관계는 느슨해지고, 느슨해지는 방향은 테스트가 막지 않는다.
        tail = self.request_timeout_seconds
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
        # 발행 재시도도 wall clock 이다. 빠뜨리면 발행 재시도를 늘리는 변경이 폴 간격을
        # 조용히 먹는다 -- 이 함수가 존재하는 이유가 바로 그 종류의 누락이다.
        publish = max(0, self.kafka_publish_attempts - 1) * self.kafka_publish_retry_seconds
        return one_call * (batches + self.summary_scopes_per_message) + publish

    @model_validator(mode="after")
    def _new_review_limit_matches_contract(self) -> Settings:
        """계약의 ``newReviews`` 상한과 같아야 한다. **어긋나면 기동하지 않는다.**

        계약은 ``app.settings`` 를 import 하지 않는다(스키마가 설정에 의존하면 백엔드와
        공유하는 모양이 환경에 흔들린다). 그래서 사본을 두고 테스트가 표류를 막았는데, 그
        단언이 **기본값에서만** 성립했다 -- 환경 변수로 이 레버를 올리면 CI 는 통과하고
        운영에서는 요청마다 ``ValidationError`` 가 나 그 파티션이 영구히 막힌다. 요청마다
        터지므로 요청 토픽의 세 파티션이 차례로 다 막혀 전 상품 요약 정지가 되고, 막힌
        파티션으로는 설계상 헬스를 내리지 않으므로 아무도 깨지 않는다. 사본을 지우는 쪽이
        아니라 **어긋남을 기동에서 잡는 쪽**으로 닫는다.
        """
        from app.schemas.summary import _MAX_NEW_REVIEWS

        if self.summary_max_new_reviews != _MAX_NEW_REVIEWS:
            raise ValueError(
                f"summary_max_new_reviews={self.summary_max_new_reviews} 가 계약 상한 "
                f"{_MAX_NEW_REVIEWS} 와 다르다. 계약(app/schemas/summary.py)의 max_length 를 "
                "함께 올려야 하고, 그것은 백엔드와 맞출 변경이다"
            )
        return self

    @model_validator(mode="after")
    def _publish_limit_fits_broker(self) -> Settings:
        if self.kafka_max_request_size > self.kafka_broker_max_message_bytes:
            raise ValueError(
                f"kafka_max_request_size={self.kafka_max_request_size} 가 브로커 상한 "
                f"kafka_broker_max_message_bytes={self.kafka_broker_max_message_bytes} 보다 크다. "
                "우리 쪽 사전 검사가 통과하고 브로커가 거부하면 그 실패는 재배달로 풀리지 않는다"
            )
        return self

    def model_ids(self) -> tuple[str, ...]:
        """설정에 적힌 모델 ID 전부. 부팅에서 가격표와 맞춰 보는 데 쓴다.

        요약 모델과 초안 모델을 **반드시** 넣는다. 안 넣으면 ``validate_pricing`` 의 부팅
        검증을 받지 못하고, 모델 ID 를 오타 내도 ``_FALLBACK_PRICING (15.0, 75.0)`` 으로
        조용히 15배 비싸게 계상된다. 초안 앱은 별 프로세스지만 같은 함수로 검증한다.
        """
        return (
            self.screening_model,
            self.notice_model,
            self.triage_model,
            self.summary_extract_model,
            self.summary_narrate_model,
            self.draft_model,
        )


settings = Settings()
