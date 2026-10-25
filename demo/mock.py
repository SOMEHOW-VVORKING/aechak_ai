"""모의 판정기. **모델이 아니다.**

카세트는 적어 둔 입력에만 답한다. 그런데 발표 중에 사람이 자기 문장을 넣어 보는 것이
이 콘솔의 절반이고, 실 API 키는 아직 없다. 그래서 키 없이 아무 본문에나 답하는 대역을 둔다.

무엇이 아닌지를 분명히 해 둔다 -- **키워드 표로 답하는 결정론적 대역**이고 모델의 판단이
아니다. 비용과 토큰도 글자 수에서 뽑은 추정값이다. 콘솔이 이 모드에서 그렇게 표시한다.
이 대역이 보여 주는 것은 판정 품질이 아니라 **파이프라인의 분기와 단계**다.

인용은 반드시 본문에서 떼어 온다. 꾸며 쓰면 ``_interpret`` 의 인용 검증이 그 판정을 버려
전건이 ``EVIDENCE_UNVERIFIED`` 로 떨어진다 -- 대역이 거짓 단계를 그리게 되는 자리다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.inspection.perspectives import specs_for
from app.llm.client import LlmResult, cost_micro_usd
from app.schemas.inspection import CallOutcome, Perspective, TargetType, ViolationCategory
from demo.replay import cassette_key

# (키워드, 분류, 사유, 제안). 위에서부터 보고 먼저 걸린 것을 쓴다.
# 규칙 단계가 결정론적으로 보는 것(연락처·계좌·금지 성분)은 여기에 넣지 않는다 -- 그것들은
# 모델에 닿기 전에 판정되거나 가려진다.
KEYWORD_TABLE: tuple[tuple[str, ViolationCategory, str, str | None], ...] = (
    ("완치", ViolationCategory.MEDICAL_CLAIM, "완치를 단정한다", "꾸준한 관리를 권장합니다"),
    ("치료", ViolationCategory.MEDICAL_CLAIM, "질병 치료를 단정한다", "건강 관리에 도움을 줄 수 있습니다"),
    ("나았", ViolationCategory.MEDICAL_CLAIM, "질병이 나았다고 서술한다", None),
    ("100%", ViolationCategory.EFFICACY_OVERCLAIM, "효과를 수치로 보장한다", "개체에 따라 다를 수 있습니다"),
    ("보장", ViolationCategory.EFFICACY_OVERCLAIM, "효과를 보장한다", "개체에 따라 다를 수 있습니다"),
    ("즉시", ViolationCategory.EFFICACY_OVERCLAIM, "즉각적인 효과를 단정한다", None),
    ("인증", ViolationCategory.FALSE_CLAIM, "확인되지 않은 인증을 주장한다", "자체 품질 기준을 따릅니다"),
    ("처방", ViolationCategory.FALSE_CLAIM, "의약품으로 오인될 수 있다", "수의사와 상담해 주세요"),
    ("쓰레기", ViolationCategory.DISPARAGEMENT, "다른 대상을 깎아내린다", None),
    ("싸구려", ViolationCategory.DISPARAGEMENT, "다른 상품을 깎아내린다", None),
    ("직거래", ViolationCategory.OFF_PLATFORM, "플랫폼 밖 거래를 권유한다", "플랫폼 주문으로 진행해 주세요"),
    ("계좌", ViolationCategory.OFF_PLATFORM, "플랫폼 밖 결제를 권유한다", "플랫폼 결제를 이용해 주세요"),
    ("최저가 보장", ViolationCategory.SPAM, "같은 문구를 반복한다", None),
)

_BODY = re.compile(r"<본문>\n(.*)\n</본문>", re.S)
_SENTENCE = re.compile(r"[^.!?\n]+")

# 글자 수로 뽑는 토큰 추정. 한국어는 대략 글자 하나가 토큰 하나를 넘나든다.
# 실측이 아니다 -- 실측은 harness/run.py --count-tokens 가 키를 받아 한다.
_CHARS_PER_TOKEN = 1.0


@dataclass(slots=True)
class MockLlmClient:
    """키워드 표로 답하는 대역. 잠들지 않고 네트워크를 타지 않는다."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    async def call_tool(
        self,
        *,
        model: str,
        effort: str | None,
        max_tokens: int,
        system_blocks: list[dict[str, Any]],
        user_content: list[dict[str, Any]],
        tool: dict[str, Any],
        request_id: str,
        timeout_seconds: float | None = None,
    ) -> LlmResult:
        name, perspective = cassette_key(tool)
        prompt = "\n".join(str(block.get("text") or "") for block in user_content)
        body = _body_of(prompt)
        hits = _hits(body)
        self.calls.append({"tool": name, "perspective": perspective, "model": model})

        if name == "triage_content":
            tool_input: dict[str, Any] = (
                {"decision": "NEEDS_REVIEW", "reason": f"걸릴 구석이 있다: {hits[0][1].value}"}
                if hits
                else {"decision": "CLEAR", "reason": "키워드 표에 걸리는 구석이 없다"}
            )
        elif name == "report_findings":
            allowed = _categories_for(perspective)
            tool_input = {
                "findings": [
                    {
                        "category": category.value,
                        "quote": quote,
                        "reason": f"{reason} (모의 판정기의 키워드 표)",
                        "suggestion": suggestion,
                    }
                    for quote, category, reason, suggestion in hits
                    if category in allowed
                ]
            }
        elif name == "review_finding":
            # 대역은 뒤집지 않는다. 뒤집힘 경로는 카세트 시나리오(critic-overturn)가 보여 준다.
            tool_input = {"decision": "UPHOLD", "reason": "모의 판정기는 1차 판정을 유지한다"}
        else:
            quoted = _quoted_violations(prompt)
            tool_input = {
                "sellerNotice": "정책에 맞지 않는 표현이 있습니다. 아래 문구로 바꿔 주세요.",
                "rewrites": [{"original": quote, "suggestion": _suggestion_for(quote)} for quote in quoted],
            }

        system_chars = sum(len(str(block.get("text") or "")) for block in system_blocks)
        input_tokens = int((system_chars + len(prompt)) / _CHARS_PER_TOKEN)
        output_tokens = max(20, len(str(tool_input)) // 2)
        return LlmResult(
            call_id=f"{request_id}-{len(self.calls):02d}-{name}",
            outcome=CallOutcome.SUCCESS,
            model_id=model,
            tool_input=tool_input,
            input_tokens=input_tokens,
            # 캐시 적중을 꾸미지 않는다. 대역은 캐시를 타지 않으므로 0이 사실이다.
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            output_tokens=output_tokens,
            cost_micro_usd=cost_micro_usd(
                model,
                input_tokens=input_tokens,
                cache_read=0,
                cache_creation=0,
                output_tokens=output_tokens,
            ),
            latency_ms=0,
        )

    async def aclose(self) -> None:
        return None


def _body_of(prompt: str) -> str:
    found = _BODY.search(prompt)
    return found.group(1) if found else prompt


def _hits(body: str) -> list[tuple[str, ViolationCategory, str, str | None]]:
    """(인용, 분류, 사유, 제안). 인용은 키워드를 품은 문장을 본문에서 그대로 떼어 온다."""
    found: list[tuple[str, ViolationCategory, str, str | None]] = []
    seen: set[str] = set()
    for keyword, category, reason, suggestion in KEYWORD_TABLE:
        if keyword not in body:
            continue
        quote = _sentence_with(body, keyword)
        if quote in seen:
            continue
        seen.add(quote)
        found.append((quote, category, reason, suggestion))
    return found


def _sentence_with(body: str, keyword: str) -> str:
    for match in _SENTENCE.finditer(body):
        sentence = match.group(0).strip()
        if keyword in sentence:
            return sentence
    return keyword


def _categories_for(perspective: Perspective | None) -> frozenset[ViolationCategory]:
    for fanout in (True, False):
        for target in TargetType:
            for spec in specs_for(target, fanout=fanout):
                if spec.perspective is perspective:
                    return frozenset(spec.categories)
    return frozenset()


def _quoted_violations(prompt: str) -> list[str]:
    """``_rewrite`` 가 만든 ``- [분류] 인용`` 줄에서 인용만 떼어 온다."""
    quotes: list[str] = []
    for line in prompt.splitlines():
        stripped = line.strip()
        if stripped.startswith("- [") and "] " in stripped:
            quotes.append(stripped.split("] ", 1)[1].strip())
    return quotes


def _suggestion_for(quote: str) -> str:
    for keyword, _category, _reason, suggestion in KEYWORD_TABLE:
        if keyword in quote and suggestion:
            return suggestion
    return "같은 뜻을 정책에 맞게 다시 써 주세요"
