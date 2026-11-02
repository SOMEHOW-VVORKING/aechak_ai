"""인용 검증과 극성 교차검사. **모델 0회, 0원.**

## 인용 검증 — 레포에 이미 그 기계가 있다

``app/inspection/pipeline.py``의 ``_interpret``이 같은 일을 한다. 모델이 돌려준 ``quote``를
``redacted.find(quote)``로 검증하고 못 찾으면 그 판정을 버리며, 전부 떨어지면
``UNCHECKED``로 적는다 -- "통과로 적으면 못 본 것을 봤다고 적는 셈"이라는 주석이 그 자리에
있다. 그 기계를 그대로 가져온다.

차이는 하나다. **검증 기준이 리뷰별이다.** 모델이 지목한 ``reviewId``의 본문에서 찾아져야
하므로, 본문에 가짜 머리줄을 끼워 귀속을 속이는 공격이 여기서 떨어진다.

정직한 한계: ``headline`` 산문은 인용으로 검증되지 않는다. 검증되는 것은 **인용의 존재와
언급 건수**다. 모델이 인용 3개를 정확히 달고도 그 위에 과장된 한 줄을 쓸 수 있고 코드는
막지 못한다. 하네스가 사람 라벨로 재는 축으로 남겼다.

## 자유 텍스트 게이트 — 인용에만 걸려 있던 것을 라벨에도 건다

게이트 넷(길이 상한 / 리터럴 검증 / ``mask_heavy`` / 차단 적중)이 ``quote`` 에만 걸려
있었다. ``aspectLabel`` 은 ``isinstance(str)`` 만 보고 통과했는데 그것은 **모델이 쓴
자유 텍스트**이고, ``tally.add(label=...)`` 로 ``labelSamples`` 에 들어가 백엔드가
**영구 보관**한다. 한 번 들어간 실명·주소는 이후 모든 요약의 집계표와 화면에 계속
재등장한다. 실제로 재현했다 -- 라벨에 실명과 주소를 넣은 응답이 ``dropped`` 전 필드 0 으로
통과했고, 하네스가 "1순위 안전 지표"라 부르는 ``piiRedacted`` 가 유출이 일어난 실행에서
0 을 찍었다.

리터럴 검증은 라벨에 쓸 수 없다 -- 라벨은 "비린 냄새"처럼 정규화된 명사구가 정상이고
본문에 그 글자가 없다. 그래서 **쓸 수 있는 겹을 전부 쓴다.**

1. 짧은 길이 상한. **유출량의 상한이 이것이다.**
2. ``mask_heavy`` -- 인용과 같은 기준.
3. 차단 적중(연락처·외부 유도·지시형). 금지 성분 적중은 보지 않는다 -- 인용·산문과 같은
   이유다(리뷰가 성분을 걱정한다는 사실이 가장 유용한 단점일 수 있다).
4. **마스킹 복원 검사.** 원문의 ``*`` 구간 앞뒤 맥락이 라벨에 그대로 있고 ``*`` 는
   없으면, 모델이 가린 글자를 채운 모양이다.

4번의 한계를 적어 둔다: ``*`` 구간이 본문 맨 앞이나 맨 끝이면 앞뒤 맥락 한쪽이 없어
판정하지 않는다. **제거가 아니라 완화다.**

## 극성 교차검사 — 코드가 할 수 있는 유일한 극성 방어

극성 오분류(단점 언급을 장점으로 적음)가 "부정 리뷰 누락"을 **직접** 만든다. 코드가 의미를
검증할 수는 없지만 독립 신호 하나가 있다 -- ``rating``은 NOT NULL 1..5다.

**설계 문서의 경계를 좁혔다.** 그대로 쓰면 이 검사가 스스로를 배반하는 것을 구현 중
실측으로 확인했다 -- 근거와 대안은 ``_PRO_CONFLICT_RATINGS`` 위의 주석에 적었다. 요약하면
1점의 PRO 만 버리고 5점의 CON 은 세기만 한다. 버리는 쪽이 부정 리뷰 누락을 만들면 그것은
이 검사의 목적과 반대다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.inspection import rules
from app.review_summary.aspects import parse_aspect_code, parse_polarity
from app.review_summary.envelope import QUOTE_MAX_CHARS, PreparedReview, mask_heavy
from app.schemas.inspection import ViolationCategory
from app.schemas.summary import AspectCode, ExtractedAspect, PetContext, PetSource, Polarity

# 라벨 길이 상한. 라벨은 상품 속성을 가리키는 짧은 명사구이고, 이 수가 유출량의 상한이다.
LABEL_MAX_CHARS = 40
# 한 줄 요약 길이 상한. 툴 설명의 "60자 이내"와 같은 수다 -- 설명만으로 누르지 않고 코드가 센다.
HEADLINE_MAX_CHARS = 60

# 마스킹 복원 검사의 맥락 창. 짧게 두면 과탐이 늘고 길게 두면 미탐이 는다.
_MASK_CONTEXT_CHARS = 2
_MASK_RUN = re.compile(r"\*+")

# 출력 게이트가 **보지 않는** 적중. 금지 성분 하나다.
#
# 리뷰에 "자일리톨이 들었다고 적혀 있어 걱정된다"가 있고 그것이 반복되는 단점이면 그 요약은
# **구매자에게 가장 유용한 문장**이다. 성분 적중으로 떨어뜨리면 그 축이 영구히 사라진다.
# 막아야 하는 것은 연락처·외부 유도·지시형이고, 그것들만 차단 적중으로 센다.
_OUTPUT_GATE_IGNORED = (ViolationCategory.BANNED_INGREDIENT,)


def blocking_hits(text: str) -> list[Any]:
    """출력에 남으면 안 되는 차단 적중. **산문·인용·라벨이 같은 함수를 쓴다.**

    사본을 두면 한쪽만 고쳐져 라벨은 통과하고 인용은 떨어지는 상태가 된다.
    """
    return [hit for hit in rules.screen(text).blocking if hit.category not in _OUTPUT_GATE_IGNORED]


def restores_mask(text: str, source: str) -> bool:
    """모델이 마스킹된 구간을 **채워 넣었는가.**

    백엔드 금칙어 마스킹이 ``사장 **아`` 로 가린 것을 모델이 ``사장 병신아`` 로 쓰면
    앞뒤 맥락은 그대로이고 가린 글자만 채워진 모양이다. 그 모양은 코드가 알아볼 수 있다.

    ``*`` 가 그대로 있으면 복원이 아니다 -- 가린 채로 옮긴 것이고 그것은 정상이다.
    """
    if "*" in text or "*" not in source:
        return False
    for run in _MASK_RUN.finditer(source):
        pre = source[max(0, run.start() - _MASK_CONTEXT_CHARS) : run.start()]
        post = source[run.end() : run.end() + _MASK_CONTEXT_CHARS]
        # 구간이 본문 끝에 붙어 있으면 맥락 한쪽이 없다. 판정하지 않는다 -- 모듈 머리글의 한계다.
        if len(pre) < _MASK_CONTEXT_CHARS or len(post) < _MASK_CONTEXT_CHARS:
            continue
        at = text.find(pre)
        if at >= 0 and text.find(post, at + len(pre)) >= 0:
            return True
    return False


# 극성 충돌을 판정하는 평점. **비대칭이고, 그 비대칭이 의도다.**
#
# 설계 문서는 "1~2점의 PRO, 4~5점의 CON 을 충돌로 보고 셈에서 제외한다"였다. 구현하고
# 실제 리뷰 모양으로 돌려 보니 그 규칙이 **스스로를 배반한다.**
#
# 4점 리뷰가 "잘 먹는데 다만 비린 냄새가 좀 납니다"라고 쓰는 것은 이상한 일이 아니다 --
# 그것이 5점이 아니라 4점인 **이유**다. 5점에 "냄새는 조금 나요"도 흔하다. 그 CON 들을
# 버리면 가장 흔한 형태의 비판이 체계적으로 사라지고, 그것이 바로 이 교차검사가 막으려던
# **부정 리뷰 누락**이다. 실측으로 확인했다 -- 냄새 단점을 3건이 말한 묶음에서 2건이
# 4·5점이어서, 넓은 경계로는 언급 1건이 되어 임계 미달로 요약에서 빠졌다.
#
# 방향으로 가른다. 손해의 방향이 양쪽에서 다르다.
#
# - **1점 리뷰의 PRO 는 버린다.** 한 점을 준 사람이 그 항목을 칭찬했다는 것은 극성 뒤집힘의
#   서명이고, PRO 를 버리는 것으로는 부정 리뷰 누락이 생기지 않는다. 손해 방향이 안전하다.
# - **5점 리뷰의 CON 은 세지만 버리지 않는다.** 뒤집힘 탐지에 필요한 신호는 **셈**이고
#   (하네스의 1순위 안전 지표가 그 비율이다), 버리는 것은 목표와 반대로 작동한다.
#
# 2~4점은 양쪽이 정당하게 섞이므로 판정하지 않는다.
_PRO_CONFLICT_RATINGS = (1,)
_CON_NOTED_RATINGS = (5,)


@dataclass(slots=True)
class VerifyCounts:
    evidence_unverified: int = 0
    # 버린 충돌. 1점 리뷰의 PRO 뿐이다.
    polarity_conflict: int = 0
    # 버리지 않고 센 충돌. 5점 리뷰의 CON. **뒤집힘 탐지는 이 숫자가 한다.**
    polarity_conflict_noted: int = 0
    mask_heavy_quote: int = 0
    unknown_aspect_code: int = 0
    quote_too_long: int = 0
    # 자유 텍스트 게이트에 걸려 떨어진 라벨. 길이·차단 적중·마스킹 과다를 합친 수다.
    label_rejected: int = 0
    # **마스킹을 되돌린 라벨.** 따로 센다 -- 다른 탈락과 성질이 다르고, 오르면 사고다.
    mask_restored: int = 0


@dataclass(slots=True)
class VerifiedReview:
    review_id: int
    aspects: list[ExtractedAspect] = field(default_factory=list)
    pet_context: PetContext | None = None
    # 모델이 뭔가 보고했는데 전부 떨어졌는가. 그러면 그 리뷰는 UNEXTRACTED 다 --
    # "항목이 없다"로 적으면 못 본 것을 봤다고 적는 셈이다.
    reported_anything: bool = False
    # 떨어진 사유를 모아 둔다. 전부 떨어졌을 때 **무엇 때문인지**를 결과에 적어야
    # "인용이 안 맞았다"와 "극성이 평점과 어긋났다"가 같은 문장으로 읽히지 않는다.
    drop_reasons: list[str] = field(default_factory=list)


def polarity_conflict_drops(rating: int, polarity: Polarity) -> bool:
    """셈에서 **빼야 하는** 충돌인가. 1점 리뷰의 PRO 하나다."""
    return rating in _PRO_CONFLICT_RATINGS and polarity is Polarity.PRO


def polarity_conflict_noted(rating: int, polarity: Polarity) -> bool:
    """세지만 빼지는 않는 충돌인가. 5점 리뷰의 CON 이다.

    이 비율이 뛰면 추출 모델이 극성을 뒤집고 있다는 신호다. 그런데 평범한 5점 리뷰도
    사소한 불만을 적으므로 기준선이 0 이 아니다 -- 하네스가 라벨과 함께 재야 뜻이 생긴다.
    """
    return rating in _CON_NOTED_RATINGS and polarity is Polarity.CON


def verify_batch(
    tool_input: dict[str, Any] | None,
    prepared: list[PreparedReview],
) -> tuple[list[VerifiedReview], VerifyCounts]:
    """추출 응답을 검증된 항목으로 접는다.

    모델이 보고하지 않은 리뷰는 **결과에 빠진다.** 호출부가 그것을 ``UNEXTRACTED``로
    적는다 -- 여기서 빈 ``VerifiedReview``를 만들어 돌려주면 "모델이 봤고 항목이 없었다"와
    "모델이 그 리뷰를 아예 언급하지 않았다"가 같은 모양이 된다.
    """
    counts = VerifyCounts()
    by_id = {item.review_id: item for item in prepared}
    rows = (tool_input or {}).get("reviews")
    if not isinstance(rows, list):
        return [], counts

    verified: list[VerifiedReview] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        review_id = row.get("reviewId")
        if not isinstance(review_id, int):
            continue
        source = by_id.get(review_id)
        if source is None:
            # 봉투에 없던 리뷰를 모델이 지어냈다. 귀속을 속이는 공격의 가장 단순한 꼴이다.
            counts.evidence_unverified += 1
            continue

        entry = VerifiedReview(review_id=review_id)
        raw_aspects = row.get("aspects")
        raw_aspects = raw_aspects if isinstance(raw_aspects, list) else []
        entry.reported_anything = bool(raw_aspects)

        for item in raw_aspects:
            aspect = _verify_aspect(item, source, counts, entry.drop_reasons)
            if aspect is not None:
                entry.aspects.append(aspect)

        entry.pet_context = _verify_pet(row.get("petMention"), source, counts)
        verified.append(entry)

    return verified, counts


def _verify_aspect(
    item: object,
    source: PreparedReview,
    counts: VerifyCounts,
    reasons: list[str],
) -> ExtractedAspect | None:
    if not isinstance(item, dict):
        counts.evidence_unverified += 1
        reasons.append("SHAPE_INVALID")
        return None

    code = parse_aspect_code(item.get("aspectCode"))
    if code is None:
        # 열거 밖의 코드를 ``OTHER``로 구제하지 않는다. 툴 enum 이 지켜지지 않은 사실이
        # 집계에 섞여 보이지 않게 된다.
        counts.unknown_aspect_code += 1
        reasons.append("UNKNOWN_ASPECT_CODE")
        return None

    polarity = parse_polarity(item.get("polarity"))
    if polarity is None:
        counts.evidence_unverified += 1
        reasons.append("POLARITY_INVALID")
        return None

    quote = item.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        counts.evidence_unverified += 1
        reasons.append("QUOTE_MISSING")
        return None
    if len(quote) > QUOTE_MAX_CHARS:
        counts.quote_too_long += 1
        reasons.append("QUOTE_TOO_LONG")
        return None
    if mask_heavy(quote):
        counts.mask_heavy_quote += 1
        reasons.append("MASK_HEAVY_QUOTE")
        return None

    # **모델이 실제로 본 텍스트** 기준이다. 원문 기준으로 검증하면 레닥션 때문에 전건 탈락한다.
    start = source.model_text.find(quote)
    if start < 0:
        counts.evidence_unverified += 1
        reasons.append("QUOTE_NOT_IN_BODY")
        return None

    if polarity_conflict_drops(source.rating, polarity):
        counts.polarity_conflict += 1
        reasons.append("POLARITY_CONFLICT")
        return None
    if polarity_conflict_noted(source.rating, polarity):
        # 세고 **남긴다.** 버리면 5점 리뷰의 사소한 불만이 통째로 사라지고, 그것이
        # 이 교차검사가 막으려던 부정 리뷰 누락이다.
        counts.polarity_conflict_noted += 1

    raw_label = item.get("aspectLabel")
    label = raw_label.strip() if isinstance(raw_label, str) and raw_label.strip() else None
    if label is not None:
        # **인용과 같은 게이트를 라벨에도 건다.** 떨어지면 항목째로 버리지 않고 열거
        # 라벨로 되돌린다 -- 셈은 코드의 것이고, 읽는 축이 더러워서 세는 축을 버릴 이유가 없다.
        gate = _label_gate(label, source.model_text)
        if gate is not None:
            reasons.append(gate)
            counts.label_rejected += 1
            if gate == "LABEL_RESTORES_MASK":
                counts.mask_restored += 1
            label = None

    return ExtractedAspect(
        aspectCode=code,
        aspectLabel=label or _fallback_label(code),
        polarity=polarity,
        quote=quote,
        quoteStart=start,
        quoteEnd=start + len(quote),
    )


def _label_gate(label: str, source_text: str) -> str | None:
    """라벨이 떨어지는 사유. 통과하면 ``None``.

    사유를 문자열로 돌려주는 것은 ``drop_reasons`` 에 **무엇 때문인지** 적기 위한 것이다.
    "검증 실패"로 뭉개면 길이가 넘친 것과 마스킹을 되돌린 것이 같은 문장으로 읽힌다.
    """
    if len(label) > LABEL_MAX_CHARS:
        return "LABEL_TOO_LONG"
    if mask_heavy(label):
        return "LABEL_MASK_HEAVY"
    if blocking_hits(label):
        return "LABEL_BLOCKED"
    if restores_mask(label, source_text):
        return "LABEL_RESTORES_MASK"
    return None


def _fallback_label(code: AspectCode) -> str:
    from app.review_summary.aspects import ASPECT_LABELS

    return ASPECT_LABELS[code][0]


def _verify_pet(raw: object, source: PreparedReview, counts: VerifyCounts) -> PetContext | None:
    """본문 내 펫 언급을 구조화한다. **인용으로 검증된 것만** 돌려준다.

    ``present``가 참이어도 인용이 본문에 없으면 버린다. 이 값은 세그먼트 분모에 들어가지
    않고 정렬 가중치로만 쓰이지만, 검증 없이 통과시키면 "본문: '우리 15kg 코카'"가 화면에
    뜨면서 본문에 그 문장이 없는 일이 생긴다.
    """
    if not isinstance(raw, dict) or not raw.get("present"):
        return None

    quote = raw.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        counts.evidence_unverified += 1
        return None
    if len(quote) > QUOTE_MAX_CHARS or source.model_text.find(quote) < 0:
        counts.evidence_unverified += 1
        return None

    species = raw.get("species")
    breed = raw.get("breedLabel")
    weight = raw.get("weightKg")
    age = raw.get("ageYears")
    return PetContext(
        source=PetSource.BODY_INFERRED,
        species=species if species in ("DOG", "CAT") else None,
        breedLabel=breed if isinstance(breed, str) and breed.strip() else None,
        # 0 을 "0kg"으로 받지 않는다. 툴이 nullable 을 못 받아 미기재를 0 으로 싣기 때문이다.
        weightKg=float(weight) if isinstance(weight, int | float) and weight > 0 else None,
        ageYears=int(age) if isinstance(age, int) and age > 0 else None,
        quote=quote,
    )
