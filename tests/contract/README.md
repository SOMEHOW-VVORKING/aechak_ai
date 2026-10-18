# 계약 표본

이 파일들은 **백엔드 레포가 정본을 소유한다.** 출처는
`aechak_be/infra/kafka/src/test/resources/contract/` 이고, 백엔드의 `messagingObjectMapper`
빈이 뽑은 바이트다.

여기 복사본은 스냅샷이고 드리프트 탐지기가 아니다. 백엔드 계약이 바뀌면 이 파일도 같은
변경 묶음에서 갱신해야 하고, 그 동기화를 자동으로 잡는 CI 잡은 백엔드 레포가 소유한다.
