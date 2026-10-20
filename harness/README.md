# 선별 계층 측정 하네스

선별(Haiku 4.5)을 켤지 말지를 **숫자로** 정하기 위한 러너다. 카프카를 타지 않고
`InspectionPipeline.run()`을 직접 부른다.

이 기능의 가치는 전부 **승급률** 하나에 걸려 있다. 승급률이 손익분기를 넘으면 선별은 비용을
늘리는 기능이다. 그래서 승급률이 1순위 지표이고 중단 규칙도 그 숫자로 적어 둔다.

## 키

환경변수는 `ANTHROPIC_API_KEY`가 **아니라** `AECHAK_AI_ANTHROPIC_API_KEY`다. `Settings`의
`env_prefix`가 `AECHAK_AI_`이기 때문이다. SSM 파라미터 이름도 같다 —
`/aechak/dev/ai/AECHAK_AI_ANTHROPIC_API_KEY`.

키가 없으면 러너가 이유를 적고 **종료 코드 0으로 건너뛴다.**

## 돌리는 순서

```bash
export AECHAK_AI_ANTHROPIC_API_KEY=sk-ant-...

# 1) 토큰 실측. 거의 무료라 맨 먼저 돈다
DOCKER_HOST="unix:///Users/woonkyung/.orbstack/run/docker.sock" \
  uv run python -m harness.run --count-tokens

# 2) 기준선. 선별 없이 Sonnet 2관점 전건
DOCKER_HOST="unix:///Users/woonkyung/.orbstack/run/docker.sock" \
  uv run python -m harness.run --mode baseline --repeat 3 --yes

# 3) 선별 판정만. Sonnet을 부르지 않아 승급률만 싸게 본다
DOCKER_HOST="unix:///Users/woonkyung/.orbstack/run/docker.sock" \
  uv run python -m harness.run --mode triage-only --repeat 3 --yes

# 4) 전체 흐름. net_miss 를 재려면 2)의 산출물 경로를 준다
DOCKER_HOST="unix:///Users/woonkyung/.orbstack/run/docker.sock" \
  uv run python -m harness.run --mode full --repeat 3 --yes \
  --baseline-from harness/out/<2단계의 타임스탬프>
```

`--yes`가 없으면 **예상 비용만 출력하고 멈춘다.** 케이스 50건에 반복 3회면 호출이 수백 건이 된다.
`--only <케이스ID> ...`로 몇 건만 골라 돌릴 수 있다.

산출물은 `harness/out/<타임스탬프>/`에 `raw.jsonl`과 `summary.tsv`로 떨어진다. 그 디렉터리는
`.gitignore`에 있다.

`--repeat` 기본값이 3인 이유는 결정성 레버가 하나도 없기 때문이다. `temperature`를 못 쓰고
Haiku는 `effort`도 못 받는다. 같은 입력의 판정이 흔들리는 폭을 재지 않으면 승급률 측정값
자체를 믿을 수 없다.

## 재는 지표

| 지표 | 정의 |
|---|---|
| **승급률** (1순위) | `NEEDS_REVIEW / 규칙을 통과한 건`. PRODUCT와 REVIEW로 나눠 본다 |
| `triage_miss` | 라벨이 `VIOLATING`인데 선별이 `CLEAR`로 내려보낸 건 |
| `net_miss` | 그중 `baseline` 모드에서는 보류가 나왔던 건. **선별 때문에 생긴 누락은 이것뿐이다** |
| 건당 비용 | 선별만 / 선별+Sonnet / baseline, 그리고 본문 길이별 |
| 지연 | 모드별 p50과 p95 |
| 선별 캐시 적중 | Haiku 호출의 `cache_read_input_tokens`. 0이 정상이고 그 사실을 숫자로 남긴다 |
| 선별 실패율 | 승급 중 호출 실패가 차지하는 비중 |
| 반복 불일치 | 같은 케이스의 판정이 반복 간에 갈린 비율 |
| 과탐 의심 | 일부러 남긴 과탐 건의 처리 결과 |

승급률 분모는 **규칙을 통과한 건**이다. 규칙이 즉시 차단한 건은 선별을 아예 타지 않으므로
분모에 넣으면 승급률이 희석되고 손익분기 비교가 틀어진다. 케이스마다 `expect_rule_block`을
달아 두었고, 러너가 그 플래그를 실제 규칙 결과와 맞춰 보고 어긋나면 측정 전에 멈춘다.

## 손익분기와 중단 규칙

전제: Sonnet5 $2/$10, Haiku4.5 $1/$5, 캐시읽기 0.1배와 쓰기 1.25배, 환율 1,400원,
접두부 약 1,200토큰, 본문 신규입력 300토큰, 출력 100토큰, 상시 트래픽에서 Sonnet 캐시는 따뜻하다고 본다.

| 구성 | 건당 | 손익분기 승급률 |
|---|---|---|
| 기준선 — Sonnet 2관점 | 5.15원 | — |
| 접두부 재사용(캐시가 무시된 현실) | 2.80원 | 45.7% |
| **채택안 — 전용 짧은 프롬프트, 캐시 없음** | **1.96원** | **62.0%** |
| 접두부를 4,200토큰으로 패딩, 캐시가 따뜻할 때 | 1.71원 | 66.8% |
| 같은 것, 캐시가 차가울 때 | 8.47원 | 기준선보다 비싸다 |

승급률 60%일 때 총비용은 채택안이 2% 절감으로 사실상 0이고, 30%면 32% 절감이다.

**중단 규칙: 측정 승급률이 62.0%를 넘으면 `enable_triage`를 켜지 않는다.** 그 위에서 선별은
비용을 늘리는 기능이다. 선별 실패율이 높을 때도 같다 — 실패는 전건 승급이라 선별 호출값이
그냥 더해진다.

위 표의 토큰 전제(1,200 / 600 / 300)는 가정이다. `--count-tokens`가 `messages.count_tokens`로
실측해 치환한다.

## 캐시에 대해 알아 둘 것

- Haiku 4.5의 **최소 캐시 접두부는 4,096토큰**이다. 지금 `POLICY_PREFIX`는 2,617자(약 1,200토큰)이고
  Haiku에 `cache_control`을 붙이면 **에러 없이 조용히 무시된다.** 그래서 선별은 전용 프롬프트를
  쓰고 캐시 지점을 아예 붙이지 않는다.
- Sonnet 5의 최소치는 **1,024토큰**이다. 지금 접두부의 여유가 15% 남짓이라 누가 정책 문구를
  줄이면 **Sonnet 캐시도 에러 없이 죽고** 관점 호출 비용이 조용히 두 배 가까이 뛴다.
  `--count-tokens`가 그 하한을 단언하고, 미달이면 종료 코드 1로 떨어진다.
- 캐시는 모델 범위다. Sonnet의 네임스페이스를 Haiku가 재활용할 수 없다.
