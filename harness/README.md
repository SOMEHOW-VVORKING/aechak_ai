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
| **승급률** (1순위) | `NEEDS_REVIEW / 선별을 실제로 탄 건`. PRODUCT와 REVIEW로 나눠 본다 |
| `triage_miss` | 라벨이 `VIOLATING`인데 선별이 `CLEAR`로 내려보낸 건 |
| `net_miss` | 그중 `baseline` 모드에서는 보류가 나왔던 건. **선별 때문에 생긴 누락은 이것뿐이다** |
| 건당 비용 | 선별만 / 선별+Sonnet / baseline, 그리고 본문 길이별 |
| 지연 | 모드별 p50과 p95 |
| 선별 캐시 적중 | Haiku 호출의 `cache_read_input_tokens`. 0이 정상이고 그 사실을 숫자로 남긴다 |
| 선별 실패율 | 승급 중 호출 실패가 차지하는 비중. 상한에 걸려 끊긴 호출(`TRUNCATED`)도 여기다 |
| 반복 불일치 | 같은 케이스의 판정이 반복 간에 갈린 비율 |
| 과탐 의심 | 일부러 남긴 과탐 건의 처리 결과 |

승급률 분모는 **선별을 실제로 탄 건**이다. 분모에서 빠지는 것이 둘이다.

1. 규칙이 즉시 차단한 건. 선별을 아예 타지 않는다. 케이스마다 `expect_rule_block`을 달아
   두었고, 러너가 그 플래그를 실제 규칙 결과와 맞춰 보고 어긋나면 측정 전에 멈춘다.
2. 규칙이 **비차단으로** 적중한 건. 파이프라인이 선별에 묻지 않고 바로 승급시킨다 —
   물으면 Haiku 1회로 `CLEAR`가 나올 수 있고, 그러면 결정론 계층이 의심한 건이 가장 약한
   심사를 받는다. 이 건들은 `triage_outcome`이 `OFF`로 남아 분모에서 빠진다.
   지금 케이스 세트에서는 40건 중 1건이다.

**승급 판정을 TRIAGE 관점 행의 유무로 역추론하지 않는다.** 승급 경로에도 행이 남기 때문이다
(`UNCHECKED` + `PROMOTED`). 행의 `outcome`이 근거다. 끊긴 호출은 `PROMOTED`가 아니라 `FAILED`로
센다 — 모델이 "더 보라"고 판단한 것과 아무 답도 못 한 것을 분자에서 뭉개면 1순위 지표를
믿을 수 없다. `tests/test_harness.py`가 이 분류를 전부 고정한다.

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

**위 표의 토큰 전제(1,200 / 600 / 300)는 가정이고 `--count-tokens`는 그것을 치환하지 않는다.**
치환하려면 출력 토큰 전제까지 같이 재야 하고 그건 실호출이다. `--count-tokens`가 하는 일은
접두부의 실제 토큰 수를 재서 캐시 하한을 단언하는 것뿐이고(미달이면 종료 코드 1), 측정값과
가정값을 나란히 찍는다. **어긋나면 이 표를 손으로 고쳐야 한다** — 손익분기 62.0%는 측정 뒤에도
가정값 위에 남아 있다(`1 - 1400/3680 = 0.6196`으로 역산된다).

## 캐시에 대해 알아 둘 것

- Haiku 4.5의 **최소 캐시 접두부는 4,096토큰**이다. `POLICY_PREFIX`는 2,617자로 거기에 한참
  못 미치고, Haiku에 `cache_control`을 붙이면 **에러 없이 조용히 무시된다.** 그래서 선별은
  전용 프롬프트를 쓰고 캐시 지점을 아예 붙이지 않는다.
- Sonnet 5의 최소치는 **1,024토큰**이다. **접두부의 토큰 수는 아직 실측되지 않았다** —
  2,617자는 글자 수다. 미달이면 에러 없이 캐시가 죽어 관점 호출 비용이 조용히 두 배 가까이
  뛰고, 위 손익분기표 전체가 그 숫자 위에 서 있다. `--count-tokens`가 하한을 단언하지만
  키가 필요해서 CI(`ruff check` + `pytest -q`)에는 없다. **표를 믿기 전에 한 번 돌릴 것.**
- **재는 대상이 `POLICY_PREFIX` 문자열이 아니다.** 캐시 렌더 순서가 `tools → system → messages`
  이고 `cache_control`이 마지막 system 블록에 있어서 **tools가 함께 캐시된다.** `findings_tool`은
  관점마다 enum이 달라 tools가 다르므로 **캐시 엔트리가 관점별로 갈라진다.** `--count-tokens`는
  관점별 접두부(tools+system)를 따로 재고, 선별 접두부도 같은 방식으로 잰다.
  즉 차가울 때 접두부 쓰기는 관점 수만큼 난다 — 직렬 실행이 그것을 아껴 주지 않는다.
- 캐시는 모델 범위다. Sonnet의 네임스페이스를 Haiku가 재활용할 수 없다.
