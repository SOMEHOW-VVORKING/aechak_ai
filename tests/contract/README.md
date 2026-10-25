# 계약 표본

이 파일들은 **백엔드 레포가 정본을 소유한다.** 출처는
`aechak_be/infra/kafka/src/test/resources/contract/` 이고, 백엔드의 `messagingObjectMapper`
빈이 뽑은 바이트다.

여기 복사본은 스냅샷이고 드리프트 탐지기가 아니다. 백엔드 계약이 바뀌면 이 파일도 같은
변경 묶음에서 갱신해야 하고, 그 동기화를 자동으로 잡는 CI 잡은 백엔드 레포가 소유한다.

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
