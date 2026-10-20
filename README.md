# aechak-ai

애착 상품·리뷰 콘텐츠 검수 서버. 백엔드(`aechak_be`)와 **카프카로만** 붙고 DB에 접속하지 않는다.

```
aechak.inspection.events  →  이 서버  →  aechak.verdict.events
```

## 판정 흐름

```
N0 규칙            코드. 연락처·계좌·금지성분. 확정 위반이면 모델을 부르지 않는다
N0.5 선별          Haiku 4.5 / effort 없음. 깨끗이면 N1을 건너뛴다. 기본 꺼짐
N1a 표시·광고      Sonnet 5 / effort low. 효능·허위·부당비교
N1b 행위·거래      Sonnet 5 / effort low. 외부거래·연락처·스팸·욕설·개인정보
N2 집계            코드. 3상태 OR + 인용 검증
N3 반박            Opus 5 / effort medium. 위반 판정일 때만. 실패하면 판정을 유지한다
N4 수정 문구       Sonnet 5. 확정 위반이고 상품일 때만
N5 확정·발행       코드
```

불변식 셋을 코드로 못박아 두었고 테스트가 지킨다.

1. 모든 분기 조건이 결정론적 코드다. 모델이 돌려준 확신도를 승급 조건으로 쓰지 않는다.
2. 모델 출력 열거에 **반려가 없다.** 인젝션이 성공해도 통과 방향으로만 실패한다.
3. 모든 노드가 `async`다. 동기 함수 하나가 이벤트 루프를 조용히 막고 그것이 카프카
   하트비트 고갈과 리밸런스로 이어진다.

오케스트레이션 프레임워크를 쓰지 않는 근거는 `docs/adr-001-no-orchestration-framework.md`에 있다.

## 모델 제약

`temperature`·`top_p`·`top_k`를 **보내지 않는다.** 현세대 모델에서 400이고 SDK 1.15의
`messages.create` 시그니처에 애초에 그 파라미터가 없다. 결정성 레버는 `output_config.effort`다.

**Haiku 4.5는 `effort`를 받지 않는다.** 그래서 선별 호출은 `output_config` 키를 **아예 보내지
않는다.** `effort=None`을 그냥 넘기면 SDK가 키를 생략하지 않고 `{"effort": null}`을 본문에 실어
400이 된다. `call_tool`이 조건부로 조립하고, `tests/test_client_params.py`가 요청 본문에서 그것을
단언한다.

`thinking`도 보내지 않는다. Sonnet 5와 Opus 5는 생략이 곧 adaptive이고, Haiku 4.5는 생략하면
사고하지 않는다(그 세대는 `budget_tokens`를 받아 adaptive가 400이다). 대신 `max_tokens`를
넉넉히 둔다 — 사고 토큰과 `tool_use`가 한 상한을 나눠 쓰고, 상한이 좁으면 `tool_use` 블록이
안 나와 `SCHEMA_INVALID`로 떨어진다.

### 프롬프트 캐시의 최소 길이

캐시 최소 접두부가 모델마다 다르고, 미달이면 **에러 없이 조용히 무시된다.**

- Haiku 4.5는 **4,096토큰**. 지금 `POLICY_PREFIX`는 약 1,200토큰이라 Haiku 쪽 캐시는 먹지 않는다.
  그래서 선별은 전용 프롬프트를 쓰고 `cache_control`을 붙이지 않는다.
- Sonnet 5는 **1,024토큰**. 지금 접두부의 여유가 15% 남짓이다. **정책 문구를 줄이면 Sonnet 캐시도
  같이 죽고** 관점 호출 비용이 조용히 두 배 가까이 뛴다. 접두부를 건드릴 때는
  `harness/run.py --count-tokens`로 하한을 확인할 것.
- 캐시는 모델 범위라 Sonnet 네임스페이스를 Haiku가 재활용할 수 없다.

### 비용 기록

`_PRICING`에 없는 모델은 비용 **0이 아니라 최고 단가로 과대 계상**하고 `ERROR`를 남긴다. 0으로
적으면 비용 경보가 영원히 침묵한다. 설정에 적힌 모델 ID는 부팅에서 `validate_pricing`이 전부
검사하고, 모르면 기동을 거부한다 — 돈을 쓰기 전에 설정 오타를 잡는 자리다.

## 개발

```bash
uv venv --python 3.12
uv sync
uv run pytest
uv run ruff check .
```

로컬 왕복은 백엔드 레포의 compose를 쓴다.

```bash
cd ../be && docker compose up -d                 # MySQL·Redis·Kafka
cd ../ai && docker build -t aechak-ai:local .
cd ../be && docker compose --profile ai up -d ai
```

## 계약

`tests/contract/`의 표본은 **백엔드가 정본을 소유한다.** 출처는
`aechak_be/infra/kafka/src/test/resources/contract/`이고 백엔드의 `messagingObjectMapper`
빈이 뽑은 바이트다. 여기 복사본은 스냅샷이며, 드리프트를 자동으로 잡는 CI 잡은 백엔드가 소유한다.

엔벨로프 필드는 여덟 개이고 `payload`는 객체가 아니라 **JSON 문자열**이다. 미러링에서
가장 자주 틀리는 자리다.
