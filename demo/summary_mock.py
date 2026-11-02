"""모의 추출·서술기. **모델이 아니다.**

카세트는 적어 둔 입력에만 답한다. 그런데 발표 중에 사람이 **자기 리뷰를 넣어 보는 것**이
이 콘솔의 절반이고 실 API 키는 아직 없다. 검수 탭에는 그 대역(``demo/mock.py``)이 있었고
요약 탭에는 없었다 -- 그래서 요약 기능에서는 적어 둔 시나리오 9개만 돌 수 있었다.
그 비대칭을 닫는다.

무엇이 아닌지를 분명히 해 둔다. **키워드 표로 답하는 결정론적 대역**이고 모델의 판단이
아니다. 비용과 토큰도 글자 수에서 뽑은 추정값이다. 콘솔이 이 모드에서 그렇게 표시한다.
이 대역이 보여 주는 것은 요약 품질이 아니라 **파이프라인의 분기와 단계**다.

## 이 대역이 거짓을 그리지 못하게 막는 것

1. **인용은 반드시 본문에서 떼어 온다.** 꾸며 쓰면 ``evidence.verify_batch`` 가 그 항목을
   버리고 ``QUOTE_NOT_IN_BODY`` 로 적힌다 -- 대역이 거짓 단계를 그리게 되는 자리다.
2. **서술은 집계표에 적힌 것만 쓴다.** 집계표 밖의 항목을 올리면 ``_apply_output_gate`` 가
   ``BELOW_THRESHOLD`` 로 버린다. 대역은 셈을 하지 않는다 -- "반복된다"의 판정은 코드 것이다.
3. **펫 정보를 추측하지 않는다.** 본문에 적힌 글자만 옮긴다. 품종으로 체중을 어림하면
   ``_verify_pet`` 이 떨어뜨린다.

표에 안 걸린다는 것은 "그 리뷰가 아무 말도 하지 않았다"가 아니라 **"표에 없다"**는 뜻이다.
대역은 그 둘을 구별할 수 없고, 그래서 ``aspects`` 가 비는 것이 흔하다. 화면이 그것을
"항목 없음"으로 읽지 않게 하는 것은 서비스 쪽 ``UNEXTRACTED`` 규약이 이미 하고 있다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.llm.client import LlmResult, cost_micro_usd
from app.review_summary.envelope import QUOTE_MAX_CHARS
from app.review_summary.extract_tool import EXTRACT_TOOL_NAME
from app.review_summary.narrate_tool import NARRATE_TOOL_NAME
from app.schemas.inspection import CallOutcome
from app.schemas.summary import AspectCode, Polarity

# (키워드, 항목 코드, 극성). 위에서부터 보고 걸린 것을 전부 쓴다.
#
# 항목 코드 열거 18종 전부에 줄을 두지 않는다 -- 표에 없는 축은 대역이 못 찾고, 그것을
# "없다"로 읽지 않는 것이 이 모듈 머리글의 약속이다. 콘솔이 그 한계를 함께 띄운다.
KEYWORD_TABLE: tuple[tuple[str, AspectCode, Polarity], ...] = (
    ("잘 먹", AspectCode.PALATABILITY, Polarity.PRO),
    ("맛있", AspectCode.PALATABILITY, Polarity.PRO),
    ("안 먹", AspectCode.PALATABILITY, Polarity.CON),
    ("거부", AspectCode.PALATABILITY, Polarity.CON),
    ("가성비", AspectCode.PRICE, Polarity.PRO),
    ("저렴", AspectCode.PRICE, Polarity.PRO),
    ("비싸", AspectCode.PRICE, Polarity.CON),
    ("배송 빠", AspectCode.DELIVERY, Polarity.PRO),
    ("배송빠", AspectCode.DELIVERY, Polarity.PRO),
    ("빨리 왔", AspectCode.DELIVERY, Polarity.PRO),
    ("늦게 왔", AspectCode.DELIVERY, Polarity.CON),
    ("포장이 꼼꼼", AspectCode.PACKAGING, Polarity.PRO),
    ("포장이 터", AspectCode.PACKAGING, Polarity.CON),
    ("냄새", AspectCode.SMELL, Polarity.CON),
    ("비린", AspectCode.SMELL, Polarity.CON),
    ("튼튼", AspectCode.DURABILITY, Polarity.PRO),
    ("금방 망가", AspectCode.DURABILITY, Polarity.CON),
    ("찢어", AspectCode.DURABILITY, Polarity.CON),
    ("작아", AspectCode.SIZE_FIT, Polarity.CON),
    ("딱 맞", AspectCode.SIZE_FIT, Polarity.PRO),
    ("쓰기 편", AspectCode.EASE_OF_USE, Polarity.PRO),
    ("재구매", AspectCode.REPURCHASE, Polarity.PRO),
    ("설사", AspectCode.ADVERSE_REACTION, Polarity.CON),
    ("토했", AspectCode.ADVERSE_REACTION, Polarity.CON),
    ("가려워", AspectCode.ADVERSE_REACTION, Polarity.CON),
    ("조용", AspectCode.NOISE, Polarity.PRO),
    ("시끄", AspectCode.NOISE, Polarity.CON),
    ("세척이 쉬", AspectCode.CLEANING, Polarity.PRO),
    ("닦기 힘", AspectCode.CLEANING, Polarity.CON),
    ("예쁘", AspectCode.DESIGN, Polarity.PRO),
    ("새어", AspectCode.LEAKAGE, Polarity.CON),
    ("가루가 날", AspectCode.LEAKAGE, Polarity.CON),
    ("양이 많", AspectCode.QUANTITY, Polarity.PRO),
    ("양이 적", AspectCode.QUANTITY, Polarity.CON),
)

# 봉투에서 리뷰 한 건을 떼어 온다. 머리줄의 번호가 **인용 귀속 키**다.
_REVIEW = re.compile(r"\[리뷰 (\d+) / (\d)점[^\]]*\]\n<본문>\n(.*?)\n</본문>", re.S)
# 집계표 한 줄과 그 아래 인용 줄. ``narrate_prompt.render_tally_row`` 가 쓰는 양식이다.
_TALLY = re.compile(r"^- \[([A-Z_]+)\] (.+?) / (PRO|CON) / (\d+)건")
_TALLY_QUOTE = re.compile(r"^ {4}인용\(리뷰 (\d+)\): (.*)$")
_TALLY_LABEL = re.compile(r"^ {4}언급 표본: (.*)$")
_SENTENCE = re.compile(r"[^.!?\n]+")

_WEIGHT = re.compile(r"(\d+(?:\.\d+)?)\s*kg", re.I)
_AGE = re.compile(r"(\d+)\s*살")
_DOG = ("강아지", "멍멍", "멍뭉", "댕댕", "우리 개", "소형견", "대형견")
_CAT = ("고양이", "냥이", "냐옹", "아깽이")

# 글자 수로 뽑는 토큰 추정. 실측이 아니다 -- 실측은 harness 가 키를 받아 한다.
_CHARS_PER_TOKEN = 1.0


@dataclass(slots=True)
class MockSummaryLlmClient:
    """``call_tool`` 과 ``aclose`` 만 구현한다. 잠들지 않고 네트워크를 타지 않는다."""

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
        name = str(tool.get("name"))
        prompt = "\n".join(str(block.get("text") or "") for block in user_content)
        self.calls.append({"tool": name, "model": model})

        if name == EXTRACT_TOOL_NAME:
            tool_input: dict[str, Any] = {"reviews": extract_reviews(prompt)}
        elif name == NARRATE_TOOL_NAME:
            tool_input = narrate(prompt)
        else:
            # 모르는 툴에 빈 입력을 돌려주면 그 단계가 "아무것도 찾지 못했다"로 그려진다.
            raise AssertionError(f"요약 대역이 모르는 툴이다: {name}")

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


def extract_reviews(prompt: str) -> list[dict[str, Any]]:
    """봉투에서 리뷰를 떼어 키워드 표로 항목을 뽑는다. **받은 리뷰 전건에 한 줄씩 돌려준다.**

    빠뜨리면 서비스가 그 리뷰를 ``UNEXTRACTED`` 로 적는다 -- 그 경로는 재생 시나리오가
    보여 주는 것이고, 대역이 조용히 만들 것은 아니다.
    """
    out: list[dict[str, Any]] = []
    for raw_id, _rating, body in _REVIEW.findall(prompt):
        aspects = []
        for keyword, code, polarity in KEYWORD_TABLE:
            if keyword not in body:
                continue
            quote = _sentence_with(body, keyword)[:QUOTE_MAX_CHARS]
            aspects.append(
                {
                    "aspectCode": code.value,
                    "aspectLabel": f"{keyword}는 말을 했다",
                    "polarity": polarity.value,
                    "quote": quote,
                }
            )
        out.append({"reviewId": int(raw_id), "aspects": aspects, "petMention": _pet(body)})
    return out


def narrate(prompt: str) -> dict[str, Any]:
    """집계표의 각 줄에 한 줄을 붙인다. **건수를 적지 않고 집계표 밖을 올리지 않는다.**

    인용은 집계표가 보여 준 ``인용(리뷰 N): ...`` 을 글자 그대로, 번호까지 그대로 옮긴다.
    번호를 주지 않던 양식에서는 이 대역도 인용을 하나도 돌려줄 수 없었다 -- 그것이
    ``narrate-v3`` 에서 양식을 고친 이유다.
    """
    pros: list[dict[str, Any]] = []
    cons: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    bucket: list[dict[str, Any]] | None = None

    for line in prompt.splitlines():
        row = _TALLY.match(line)
        if row is not None:
            code, name, polarity, count = row.groups()
            current = {
                "aspectCode": code,
                "headline": _headline(name.strip(), polarity, int(count)),
                "quotes": [],
            }
            bucket = pros if polarity == "PRO" else cons
            bucket.append(current)
            continue
        if current is None:
            continue
        quote = _TALLY_QUOTE.match(line)
        if quote is not None:
            current["quotes"].append({"reviewId": int(quote.group(1)), "text": quote.group(2)})
    return {"pros": pros, "cons": cons}


def _subject_particle(word: str) -> str:
    """받침이 있으면 "이", 없으면 "가". 화면에 나가는 문장이라 조사를 틀리지 않는다."""
    if not word:
        return "가"
    last = word[-1]
    if "\uac00" <= last <= "\ud7a3":
        return "이" if (ord(last) - 0xAC00) % 28 else "가"
    return "가"


def _headline(name: str, polarity: str, count: int) -> str:
    """60자 이내. **효능·의료 단정을 쓰지 않는다** -- 쓰면 출력 게이트가 그 항목을 버린다."""
    tail = "좋았다는" if polarity == "PRO" else "아쉬웠다는"
    return f"{name}{_subject_particle(name)} {tail} 후기가 {count}건 있었다"[:60]


def _pet(body: str) -> dict[str, Any]:
    """본문에 적힌 펫 정보만 옮긴다. **추측하지 않는다.**"""
    weight = _WEIGHT.search(body)
    age = _AGE.search(body)
    species = ""
    if any(word in body for word in _CAT):
        species = "CAT"
    elif any(word in body for word in _DOG):
        species = "DOG"
    anchor = weight or age
    if not species and anchor is None:
        return {
            "present": False,
            "species": "",
            "breedLabel": "",
            "weightKg": 0,
            "ageYears": 0,
            "quote": "",
        }
    keyword = anchor.group(0) if anchor is not None else next(w for w in (*_CAT, *_DOG) if w in body)
    return {
        "present": True,
        "species": species,
        # 품종 표기는 사전이 없어 비운다. 비우는 것이 추측보다 정직하다.
        "breedLabel": "",
        "weightKg": float(weight.group(1)) if weight else 0,
        "ageYears": int(age.group(1)) if age else 0,
        "quote": _sentence_with(body, keyword)[:QUOTE_MAX_CHARS],
    }


def _sentence_with(body: str, keyword: str) -> str:
    """키워드를 품은 문장을 본문에서 **글자 그대로** 떼어 온다."""
    for match in _SENTENCE.finditer(body):
        sentence = match.group(0).strip()
        if keyword in sentence:
            return sentence
    return keyword


__all__ = ["KEYWORD_TABLE", "MockSummaryLlmClient", "extract_reviews", "narrate"]
