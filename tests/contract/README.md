# 계약 표본

이 파일들은 **백엔드 레포가 정본을 소유한다.** 출처는
`aechak_be/infra/kafka/src/test/resources/contract/` 이고, 백엔드의 `messagingObjectMapper`
빈이 뽑은 바이트다.

여기 복사본은 스냅샷이고 드리프트 탐지기가 아니다. 백엔드 계약이 바뀌면 이 파일도 같은
변경 묶음에서 갱신해야 하고, 그 동기화를 자동으로 잡는 CI 잡은 백엔드 레포가 소유한다.

## 리뷰 요약 표본 — `review-summary-requested.json` / `review-summary-result.json`

출처는 팀원 브랜치 `feature/SCRUM-327-review-summary-result-api` 의 같은 폴더이고 바이트가
같다. 앞은 **백엔드가 보내는 모양**이고, 뒤는 백엔드가 "AI 서버가 직렬화한 모양"으로 들고
테스트를 박아 둔 표본이다. `tests/test_contract.py` 가 앞을 `ReviewSummaryRequest` 로 읽어
우리가 요구하는 키가 빠짐없이 오는지 보고, 뒤는 경로마다 키 집합을 우리 출력과 맞춘다.

요청 표본에는 **요청 계약에서 뺀 키 넷이 아직 들어 있다** —
`totalVisibleReviews`·`ratingBuckets` 와 `newReviews` 의 `hasPhoto`·`createdAt`. 모든 요약
모델이 `extra="ignore"` 라서 조용히 버려지고 런타임은 깨지지 않는다(그 사실 자체를
`tests/test_summary_runner.py` 가 따로 묶어 뒀다). 백엔드가 그 넷을 빼는 날 이 사본만
갱신하면 되고, 그때까지 어느 키를 왜 버리는지는 테스트의 `_SUMMARY_REQUEST_IGNORED` 가
들고 있다 — 집합에 없는 새 키가 오면 테스트가 터져 읽을지 말지를 사람이 정하게 한다.

## 알려진 값 어긋남 — `review-summary-result.json` 의 `calls[0].stage`

표본은 `"extract"` 소문자이고 우리는 `"EXTRACT"` 를 보낸다
(`app/review_summary/service.py`). 백엔드 `ReviewSummaryMessage` 에 `calls` 필드가 없어 그
값을 읽는 자리가 아직 없으므로 무해하다. 정본이 백엔드이므로 여기서 바이트를 고치지 않고,
백엔드가 원장을 받기로 하는 날 같은 묶음에서 대문자로 맞춘다. 키 집합을 보는 테스트는
값을 보지 않으므로 이 어긋남으로는 터지지 않는다.

## 알려진 어긋남 — `content-verdict-triage.json`

이 표본은 **선별이 건너뛴 통과 경로를 생산자보다 뒤처진 모양으로 적고 있다.** 관점을
`[RULE/CLEAR, TRIAGE/CLEAR]` 둘로만 두고 모델 관점 행이 없다.

백엔드 `InspectionFacade.modelVerified()` 는 그 통과를 받아들이는 조건으로 모델 관점 행이
비어 있지 않은 것을 요구한다. 그래서 이 모양을 그대로 보내면 선별이 통과시킨 전건이 관리자
검토 큐로 간다 — 선별이 Sonnet 호출만 아끼고 사람 손은 그만큼 더 쓰는 상태다. 파이프라인은
이미 건너뛴 관점마다 `UNCHECKED` + `skipReason=TRIAGE_CLEAR` 행을 남기므로 **생산자와
소비자 코드는 서로 맞고, 어긋난 것은 이 표본과 표본을 물고 있는 테스트뿐이다.**

정본이 백엔드 레포에 있으므로 여기서 바이트를 고치지 않는다. 사본만 고치면 정본과 어긋난
파일이 둘 생기고, 동기화를 잡는 CI 잡은 백엔드 소관이다. 대신
`test_건너뛴_표본이_백엔드_게이트_조건에_아직_못_미친다` 가 이 사실을 단언해 둬서, 표본이
갱신되면 그 테스트가 실패해 후속 조치를 가리킨다.

필요한 변경은 백엔드 쪽 한 묶음이다.

1. 표본에 `DISPLAY_ADS/UNCHECKED/TRIAGE_CLEAR` 와 `CONDUCT_TRADE/UNCHECKED/TRIAGE_CLEAR`
   두 행을 더한다.
2. `InspectionContractTest` 의 관점 집합 단언과 "모델 판정 관점 없이 통과하는 것이 선별의
   정상 경로다"라는 주석을 고친다. 같은 레포의 `modelVerified()` 가 그 모양을 증거 없는
   통과로 다루므로 지금은 백엔드 안에서 모순이다.
3. 그 뒤 이 사본을 갱신하고 `test_contract.py` 의 위 테스트를 지운다.
