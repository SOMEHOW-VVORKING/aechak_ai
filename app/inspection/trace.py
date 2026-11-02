"""단계 추적. 판정 계약 **밖**이다.

왜 계약 밖인가. 콘솔이 "왜 이렇게 판정했는가"를 보여 주려면 아래 여덟이 필요한데
``ContentVerdict`` 에는 어느 것도 없다 — 선별이 적은 사유, 비평의 결정 열거,
호출 실패 상세(``LlmResult.detail``), 비평·수정문구의 단계 라벨, 인용 검증으로 떨어낸 개수,
수정 문구 후보 배열, 가린 본문, 규칙 적중의 사유와 차단 여부.

그런데 ``app/schemas/inspection.py`` 는 백엔드 ``com.aechak.message.inspection`` 패키지와
1:1이라 필드를 더할 수 없다. 더하면 백엔드 매퍼가 모르는 필드를 **에러 없이 버리고**,
두 레포가 서로 다른 것에 대해 녹색이 된다. 그래서 인프로세스 객체로 모은다.

규약 둘.

1. **추적기를 주지 않으면 아무 일도 일어나지 않는다.** 카프카 경로(``app/kafka/runner.py``)는
   추적기를 주지 않으므로 운영 동작이 바뀌지 않는다. ``tests/test_trace.py`` 가 추적 유무로
   판정 바이트가 같은 것을 단언한다.
2. **단계 라벨은 여기에만 둔다.** 원장(``CallUsage.perspective``)은 백엔드 열거에 있는 값만
   실어야 한다. 비평과 수정문구 호출이 둘 다 ``ALL`` 로 적히는 것을 고치려고 새 관점을
   만들면 백엔드 역직렬화가 깨진다. 구분은 추적 쪽에서 한다.

통과(``CLEAR``)와 미검사(``UNCHECKED``)를 뭉개지 않는 것이 이 프로젝트 전체의 불변식이라,
단계 레코드도 **돌았는지(status)** 와 **무엇으로 판정됐는지(outcome)** 를 따로 든다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.schemas.inspection import Perspective, PerspectiveOutcome, ViolationCategory


class Stage(StrEnum):
    """파이프라인 노드. 값은 콘솔이 그대로 표시하는 순서 표시다."""

    RULE = "N0_RULE"
    TRIAGE = "N0_5_TRIAGE"
    JUDGE = "N1_JUDGE"
    AGGREGATE = "N2_AGGREGATE"
    CRITIC = "N3_CRITIC"
    NOTICE = "N4_NOTICE"


class StageStatus(StrEnum):
    """단계가 어떻게 끝났는가.

    ``outcome`` 과 섞지 않는다. "돌았고 통과"와 "안 돌아서 미검사"는 축이 다른 사실이고,
    한 값으로 합치면 콘솔이 둘을 같은 색으로 그린다 — 이 프로젝트가 반복해 고친 결함이다.
    """

    RAN = "RAN"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class RewritePair:
    """수정 문구 후보 한 쌍. 판정에는 ``sellerNotice`` 한 줄만 실리고 이 배열은 버려진다."""

    original: str
    suggestion: str


@dataclass(frozen=True, slots=True)
class FindingRecord:
    """모델이 보고한 위반 한 건.

    ``reason`` 을 드는 것이 이 레코드의 이유다. ``Violation`` 은 ``suggestion`` 만 들고
    모델이 적은 사유를 버린다. ``verified`` 가 거짓인 건은 원문에 없는 문장을 인용해
    판정에서 떨어진 것이고, 판정 메시지에는 전부 떨어진 경우만 보인다.
    """

    category: str
    quote: str
    reason: str | None
    suggestion: str | None
    verified: bool


@dataclass(frozen=True, slots=True)
class RuleHitRecord:
    """규칙 적중 한 건. ``Violation`` 으로 옮길 때 사라지는 ``reason`` 과 ``blocking`` 을 든다."""

    category: ViolationCategory
    quote: str
    reason: str
    blocking: bool
    start: int | None = None
    end: int | None = None


@dataclass(slots=True)
class StageRecord:
    stage: Stage
    status: StageStatus
    # 사람이 읽는 단계 이름. 비평과 수정문구를 원장에서 가를 수 없는 것을 여기서 가른다
    label: str
    perspective: Perspective | None = None
    outcome: PerspectiveOutcome | None = None
    skip_reason: str | None = None
    # 모델이 돌려준 열거값과 사유. 선별의 CLEAR/NEEDS_REVIEW, 비평의 UPHOLD/OVERTURN/INSUFFICIENT
    decision: str | None = None
    reason: str | None = None
    # 실패 상세. CallOutcome 열거만으로는 max_tokens 초과와 스키마 오류 본문이 사라진다
    detail: str | None = None
    model_id: str | None = None
    latency_ms: int | None = None
    cost_micro_usd: int | None = None
    # 인용 검증으로 떨어낸 개수. 전부 떨어진 경우만 판정에 EVIDENCE_UNVERIFIED 로 보인다
    evidence_dropped: int = 0
    findings: list[FindingRecord] = field(default_factory=list)
    # 집계 단계의 셈. 쏜 관점 수와 그중 실제로 판정한 수
    dispatched: int | None = None
    checked: int | None = None
    rewrites: list[RewritePair] = field(default_factory=list)
    note: str | None = None


@dataclass(slots=True)
class Trace:
    """한 건의 추적. 콘솔이 이것을 그린다."""

    # 원문과 가린 본문을 함께 든다. ``Violation.spanStart/spanEnd`` 는 **가린 본문 기준**이라
    # 원문에 하이라이트를 찍으면 엉뚱한 구간을 가리킨다.
    text: str = ""
    redacted: str = ""
    rule_hits: list[RuleHitRecord] = field(default_factory=list)
    stages: list[StageRecord] = field(default_factory=list)


class TraceRecorder:
    """단계를 쌓는다. 파이프라인은 이 객체를 받았을 때만 기록한다."""

    def __init__(self) -> None:
        self.trace = Trace()

    def set_input(self, *, text: str, redacted: str) -> None:
        self.trace.text = text
        self.trace.redacted = redacted

    def set_rule_hits(self, hits: list[RuleHitRecord]) -> None:
        self.trace.rule_hits = hits

    def add(self, record: StageRecord) -> None:
        self.trace.stages.append(record)
