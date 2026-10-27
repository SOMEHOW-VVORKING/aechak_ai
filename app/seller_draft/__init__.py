"""셀러 상품 등록 폼 초안 자동완성.

**``app/inspection/`` 안에 넣지 않았다.** 그 패키지의 모든 모듈 머리글이 "검수 판정"의
불변식을 선언하고 ``pipeline.py`` 가 ``rules -> triage -> perspectives -> policy -> trace``
를 한 상태머신으로 꿰고 있다. 초안은 판정이 아니고 ``Verdict``/``PerspectiveOutcome`` 를
내지 않는다. 섞으면 ``countable_perspectives``·합성 ``promptVersion`` 같은 판정 전용 술어가
초안까지 지배한다.

**초안은 판정하지 않는다.** 초안 단계에서 "이 문구는 위반"이라고 적으면 판정 계약
(``app/schemas/inspection.py``)과 뜻이 겹치고 혼동이 난다. 초안이 하는 일은 **그 필드를
채우지 않는 것**까지이고, 판정은 등록 시점의 기존 파이프라인이 한다.

``app/inspection/`` 에서 가져다 쓰는 것은 순수 함수 둘뿐이다 -- ``rules.screen``/
``rules.neutralize_envelope`` 와 ``banned.fold``. 둘 다 ``Settings`` 도 ``LlmClient`` 도
받지 않아 그대로 호출되고, 그 파일들을 한 줄도 고치지 않는다.
"""
