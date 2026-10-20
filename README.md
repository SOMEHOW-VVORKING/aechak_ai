# aechak-ai

애착 상품·리뷰 콘텐츠 검수 서버. 백엔드(`aechak_be`)와 **카프카로만** 붙고 DB에 접속하지 않는다.

```
aechak.inspection.events  →  이 서버  →  aechak.verdict.events
```

## 판정 흐름

```
N0 규칙            코드. 연락처·계좌·금지성분. 확정 위반이면 모델을 부르지 않는다
N0.5 선별          Haiku 4.5 / effort 없음. 깨끗이면 N1을 건너뛴다. 기본 꺼짐
                   규칙이 비차단으로라도 적중한 건은 묻지 않고 바로 승급한다
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

`effort`를 **빈 문자열로 주는 것도 미지정으로 접는다.** ECS taskdef·`.env`·SSM에서 "값을 비운다"가
`None`이 아니라 `""`로 들어오고, 그걸 그대로 실으면 `{"effort": ""}`가 전송돼 전건 400이다.
설정 계층이 접고(`Settings`의 `OptionalEffort`) 전송 계층도 한 번 더 접는다. 등급이 아닌 값
(`none`, `LOW`, 오타)은 부팅에서 거부한다 — 호출마다 400이 나는 쪽보다 기동이 멈추는 쪽이 낫다.
관점 호출의 등급은 비울 수 없다. 비우면 `output_config`가 빠져 모델 기본값(`high`)으로 조용히
올라가기 때문에, 400보다 더 조용한 사고가 된다.

`thinking`도 보내지 않는다. Sonnet 5와 Opus 5는 생략이 곧 adaptive이고, Haiku 4.5는 생략하면
사고하지 않는다(그 세대는 `budget_tokens`를 받아 adaptive가 400이다). 대신 `max_tokens`를
넉넉히 둔다 — 사고 토큰과 `tool_use`가 한 상한을 나눠 쓰고, 상한이 좁으면 `tool_use` 블록이
안 나와 `SCHEMA_INVALID`로 떨어진다.

### 재시도와 상한 초과

SDK 재시도를 **0으로 끈다.** 기본값이 2라서 그냥 두면 429 한 건이 HTTP 3회가 되고,
레이트 리밋 상황에서 리밋을 3배로 민다. 재시도는 파이프라인이 관점 호출 하나당 한 번만
직접 돌린다 — 예산이 한곳에서 보여야 카프카 폴 간격과 맞출 수 있다.
`Settings.worst_case_handle_seconds()`가 그 예산을 합산하고, `kafka_max_poll_interval_ms`가
그보다 넓은지 테스트가 지킨다. 상한만 올리고 폴 간격을 빠뜨리면 재시도 폭풍 한 번이
리밸런스를 유발해 같은 상품을 전액 재검수한다.

`stop_reason=max_tokens`로 끊긴 200 응답은 `SUCCESS`가 아니라 `TRUNCATED`로 적는다.
`tool_use.input`이 dict로 보여도 중간까지만 채워져 있고, 성공으로 적으면 끊긴 호출이
승급률 분자에서 "모델이 더 보라고 했다"와 같아진다. 스키마 오류로 적으면 상한을 올려야
하는 것을 알 수 없다. 끊김은 다시 쏴도 같은 결과라 재시도 대상에서도 뺀다.

### 프롬프트 캐시의 최소 길이

캐시 최소 접두부가 모델마다 다르고, 미달이면 **에러 없이 조용히 무시된다.**

- Haiku 4.5는 **4,096토큰**. `POLICY_PREFIX`는 2,617자이고 이 하한에 한참 못 미치므로 Haiku 쪽
  캐시는 먹지 않는다. 그래서 선별은 전용 프롬프트를 쓰고 `cache_control`을 붙이지 않는다.
- Sonnet 5는 **1,024토큰**. **접두부의 토큰 수는 아직 실측되지 않았다** — 글자 수만 셌다.
  미달이면 에러 없이 조용히 캐시가 죽어 관점 호출 비용이 두 배 가까이 뛴다. 접두부를 건드릴
  때는 `harness/run.py --count-tokens`로 하한을 확인할 것. 키가 필요해서 CI에는 없다.
- **캐시 접두부는 `POLICY_PREFIX` 문자열이 아니다.** 렌더 순서가 `tools → system → messages`이고
  `cache_control`이 마지막 system 블록에 있어서 **tools가 함께 캐시된다.** `findings_tool`은
  관점마다 enum 목록이 달라 tools가 다르고, 따라서 **캐시 엔트리가 관점별로 갈라진다.**
  관점 호출을 직렬로 돌리는 것은 접두부를 아끼기 위한 것이 아니다(애초에 공유되지 않는다) —
  레이트 리밋 압력을 한 요청 안에서 분산하는 것이 이유다.
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

## 측정 하네스

선별을 켤지 말지는 승급률로 정한다. 러너와 지표 정의, 손익분기표, 중단 규칙은
`harness/README.md`에 있다. `harness/`는 `testpaths` 밖이라 pytest가 수집하지 않고,
Dockerfile이 `app`과 `prompts`만 COPY하므로 이미지에도 들어가지 않는다.

## 계약

`tests/contract/`의 표본은 **백엔드가 정본을 소유한다.** 출처는
`aechak_be/infra/kafka/src/test/resources/contract/`이고 백엔드의 `messagingObjectMapper`
빈이 뽑은 바이트다. 여기 복사본은 스냅샷이며, 드리프트를 자동으로 잡는 CI 잡은 백엔드가 소유한다.

엔벨로프 필드는 여덟 개이고 `payload`는 객체가 아니라 **JSON 문자열**이다. 미러링에서
가장 자주 틀리는 자리다.

판정에는 관점 결과와 별개로 신호 둘이 더 실린다. 둘 다 백엔드의 어드민 검수 라우팅이 본다.

- `criticOverturned` — 비평이 모델 위반을 뒤집어 통과로 바꿨다는 사실. 이걸 빼면 백엔드가
  깨끗한 통과와 구분할 수 없어 "비평 뒤집음" 경로가 아예 발화하지 않는다. 필드가 없을 때
  Jackson이 기본값 `false`를 쓰기 때문에 **깨지지 않고 조용히 통과한다** — 그래서 테스트로 잡는다.
- `TRIAGE` 관점 행 — 선별이 무엇을 했는지. 건너뛰면 `CLEAR`, 승급하면 `UNCHECKED` +
  `skipReason`(`PROMOTED` 또는 규칙 적중으로 건너뛴 `RULE_HIT`)이다. 승급을 `CLEAR`로 적으면
  거짓이고, 행을 아예 빼면 누락률을 측정할 수 없다. 이 행은 집계에서 세지 않는다 —
  `countable_perspectives`가 이번 요청에서 **실제로 쏜** 관점만 센다.
