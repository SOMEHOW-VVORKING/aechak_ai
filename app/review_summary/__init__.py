"""리뷰 요약. **검수 판정이 아니다.**

``app/inspection/`` 안에 두지 않은 이유를 적어 둔다. 그 패키지의 모든 모듈 머리글이 "검수
판정"의 불변식을 선언하고 ``pipeline.py``가 ``rules -> triage -> perspectives -> policy ->
trace``를 한 상태머신으로 꿰고 있다. 요약은 판정이 아니고 ``Verdict``나
``PerspectiveOutcome``를 내지 않는다. 섞으면 ``countable_perspectives``·합성
``promptVersion`` 같은 판정 전용 술어가 요약까지 지배하고, ``Perspective`` 열거에 값을
더하는 압력이 생겨 백엔드 역직렬화를 깬다.

검수에서 가져오는 것은 **순수 함수 넷뿐**이다.

    rules.screen             0단 결정론 검사 + 레닥션
    rules.neutralize_envelope 봉투 구분자 중성화
    rules.normalize          판정 전용 정규화
    banned.fold              중복 리뷰 접기

어느 것도 ``Settings``도 ``LlmClient``도 받지 않아 그대로 호출된다. ``rules.py``는 **한 줄도
고치지 않는다** -- ``ENVELOPE_TOKENS``에 ``<리뷰>`` 를 더하는 대신 이미 등록된 ``<본문>``을
리뷰마다 반복한다(``envelope.py`` 머리 참조).
"""
