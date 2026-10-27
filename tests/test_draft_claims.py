"""효능 단정 사전. **0단 룰이 못 잡는 축이고 그 사실 자체를 테스트가 고정한다.**

초안 생성기가 자기 플랫폼의 검수에 걸리는 문구를 셀러에게 권하면, 셀러가 그대로 저장했을 때
**자기 상품이 차단된다.** 그래서 이 사전의 커버리지와 과탐을 둘 다 고정한다.
"""

from __future__ import annotations

import re

from app.inspection import rules
from app.inspection.policy import POLICY_PREFIX
from app.schemas.inspection import ViolationCategory
from app.seller_draft import catalog, claims
from app.seller_draft.prompt import DRAFT_PREFIX

# 사전이 소유하는 분류 셋. ``POLICY_PREFIX`` 의 절 제목과 같은 이름이다.
_OWNED_SECTIONS = ("MEDICAL_CLAIM", "EFFICACY_OVERCLAIM", "FALSE_CLAIM")


def _policy_violation_examples() -> list[str]:
    """``POLICY_PREFIX`` 에서 세 분류의 위반 예시를 뽑는다.

    정책 문서를 고쳐 예시가 늘면 이 목록이 늘고 드리프트 테스트가 깨진다. **깨지는 쪽이
    조용히 어긋나는 쪽보다 낫다.**
    """
    found: list[str] = []
    for block in POLICY_PREFIX.split("## ")[1:]:
        name = block.split(" ", 1)[0].strip()
        if name not in _OWNED_SECTIONS:
            continue
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith("- 위반:"):
                found.extend(re.findall(r'"([^"]+)"', stripped))
    return found


def test_정책_위반_예시_목록이_드리프트하지_않았다() -> None:
    """``claims.POLICY_CLAIM_EXAMPLES`` 가 정책 문서의 사본이다. 어긋나면 깨져야 한다."""
    assert _policy_violation_examples() == list(claims.POLICY_CLAIM_EXAMPLES)


def test_0단_룰은_효능을_하나도_잡지_못한다() -> None:
    """**이 모듈이 존재하는 이유다.** 실행해 확인한 사실을 테스트로 고정한다.

    ``MEDICAL_CLAIM`` 과 ``EFFICACY_OVERCLAIM`` 은 모델 계층(표시·광고 관점)이 소유하는
    분류이고 0단 정규식에 그 패턴이 없다. "생성물을 기존 0단 룰에 태우면 검수를 통과한다"는
    설계가 **틀렸다는 증거**다.
    """
    for example in claims.POLICY_CLAIM_EXAMPLES:
        hits = [
            hit
            for hit in rules.screen(example).hits
            # 금지 성분은 0단이 잡는 별 축이다. 효능 축만 본다.
            if hit.category is not ViolationCategory.BANNED_INGREDIENT
        ]
        assert hits == [], f"0단이 효능 예시를 잡았다: {example!r} -> {hits}"


def test_효능_사전이_정책_예시를_덮는다() -> None:
    """못 잡는 것을 **적어 두지 않으면 커버리지 숫자가 거짓이 된다.**"""
    for example in claims.POLICY_CLAIM_EXAMPLES:
        hits = claims.screen(example)
        if example in claims.UNCOVERED_POLICY_EXAMPLES:
            assert hits == [], f"못 잡는다고 적어 둔 것을 잡았다. 목록을 고쳐라: {example!r}"
        else:
            assert hits, f"정책 위반 예시를 놓쳤다: {example!r}"


def test_못_잡는_것이_원산지_허위_하나다() -> None:
    """원산지 허위는 어휘 문제가 아니다 -- "국내산"이 거짓인지는 문자열 계층에 없는 정보다.

    초안이 원산지를 쓰지 않게 하는 것이 맞는 방어이고, 그래서 사실 슬롯에 원산지 자리를
    두지 않았다.
    """
    assert claims.UNCOVERED_POLICY_EXAMPLES == ("국내산(실제 수입품)",)


def test_정책이_통과로_든_문구를_잡지_않는다() -> None:
    """과탐은 필드를 비우게 하고(셀러가 직접 쓴다) 과소탐은 셀러 상품이 차단되게 한다.

    그래도 **정책이 명시적으로 통과로 든 문구**를 잡으면 그 리프의 초안이 영구히 빈다.
    """
    for passing in (
        "관절 건강에 도움을 줄 수 있는 글루코사민 함유",
        "피부 보습에 도움",
        "급여 후 변화를 느낀 보호자가 많았습니다",
        "국내 공장에서 소분 포장",
        "자체 품질 기준을 따릅니다",
    ):
        assert claims.screen(passing) == [], passing


def test_부정_맥락과_제외_접미를_거른다() -> None:
    """``banned.py`` 의 ``deny_suffix`` 설계를 그대로 빌려왔다.

    ``처방식사료`` 는 리프 라벨(114·213)이다. 그 상품의 설명이 "처방식"을 쓰는 것은
    정상이고, 막으면 **그 리프의 초안이 영구히 빈다.**
    """
    for passing in (
        "처방식 사료입니다",
        "치료 보조 목적으로 쓰입니다",
        "동물용 의약품이 아닙니다",
        "수의사 처방 없이 급여할 수 있습니다",
        "자체 인증 기준에 따라 검사했습니다",
        "주문 후 즉시 발송합니다",
        "교환·반품 보장기간은 7일입니다",
    ):
        assert claims.screen(passing) == [], passing


def test_리프_라벨_50개가_전부_통과한다() -> None:
    """리프 라벨이 효능 사전에 걸리면 그 리프의 초안이 영구히 빈다.

    접두부에 50개 라벨이 그대로 실리므로, 걸리면 **프롬프트가 자기 금지 규칙을 어기는 셈**이다.
    """
    for leaf in catalog.LEAVES:
        assert claims.screen(leaf.leaf_label) == [], leaf.leaf_label
    assert claims.screen(catalog.render_leaf_paths()) == []


def test_모의_판정기_표와의_교집합을_고정한다() -> None:
    """후보 표가 ``demo/mock.py`` 에 이미 있지만 **import 하지 않는다.**

    ``app/`` 어느 파일도 ``demo`` 를 import 하지 않는 것을 다른 테스트가 단언하고,
    Dockerfile 이 ``demo`` 를 COPY 하지 않아 import 하면 **운영에서 ImportError** 다.
    표를 새로 쓰고 여기서 교집합을 고정해 **한쪽만 늘어나는 것**을 잡는다.
    """
    from demo.mock import KEYWORD_TABLE

    owned = {
        ViolationCategory.MEDICAL_CLAIM,
        ViolationCategory.EFFICACY_OVERCLAIM,
        ViolationCategory.FALSE_CLAIM,
    }
    keywords = [keyword for keyword, category, _r, _s in KEYWORD_TABLE if category in owned]
    caught = {keyword for keyword in keywords if claims.screen(keyword)}

    assert caught == set(keywords), (
        f"모의 표에만 있는 효능 표현이 생겼다: {sorted(set(keywords) - caught)}. "
        "둘 중 하나를 고치면 다른 쪽도 고쳐야 한다"
    )


def test_접두부가_금지_예시를_인용한다() -> None:
    """``POLICY_PREFIX`` **전문을 재사용하지 않는다.**

    2,617자가 접두부에 더해지면 모델 선택의 비용 교차점이 밀리고, 판정 정책과 생성 지침이
    한 문자열에 섞이면 ``POLICY_VERSION`` 을 올릴 때 초안 재사용 키까지 끊긴다.
    금지 예시만 발췌하고 **이 테스트가 드리프트를 잡는다.**
    """
    covered = [ex for ex in claims.POLICY_CLAIM_EXAMPLES if ex not in claims.UNCOVERED_POLICY_EXAMPLES]

    for example in covered:
        assert example in DRAFT_PREFIX, f"금지 예시가 접두부에 없다: {example!r}"
    # 정책 전문이 섞여 들어가지 않았는지. 분류 해설 절 제목이 있으면 전문을 붙인 것이다.
    assert "# 위반 분류 11종" not in DRAFT_PREFIX


def test_표현_하나당_한_번만_보고한다() -> None:
    """셀러에게 전할 말이 "치료가 세 번 나왔다"가 아니라 "그 필드를 비웠다"다."""
    hits = claims.screen("치료 치료 치료에 효과가 있습니다")

    assert [hit.canonical for hit in hits] == ["치료"]


def test_빈_문자열과_공백은_적중이_없다() -> None:
    assert claims.screen("") == []
    assert claims.screen("   \n ") == []


# --- 커버리지 숫자가 순환이 아니어야 한다 -------------------------------------------------


def test_보류_집합이_정책_예시와_겹치지_않는다() -> None:
    """**겹치면 보류 집합이 순환이 되어 측정 도구가 하나 사라진다.**

    ``POLICY_CLAIM_EXAMPLES`` 를 분모로 쓴 "8건 중 8건"은 커버리지가 아니다 -- 사전이
    그 목록에서 나왔고 같은 목록이 프롬프트에도 들어간다. 미지의 표현에 대해 아무것도
    말하지 않는데 리포트 한 줄로는 커버리지처럼 읽혔다.
    """
    assert set(claims.HOLDOUT_CLAIM_EXAMPLES).isdisjoint(set(claims.POLICY_CLAIM_EXAMPLES))
    assert len(claims.HOLDOUT_CLAIM_EXAMPLES) >= 6


def test_보류_집합이_프롬프트에_들어가지_않는다() -> None:
    """들어가면 모델이 그 표현만 피해 보류 집합이 측정 도구로서 죽는다."""
    block = claims.forbidden_examples_block()

    for example in claims.HOLDOUT_CLAIM_EXAMPLES:
        assert example not in block, example


def test_보류_집합의_미탐을_숨기지_않는다() -> None:
    """**네거티브 리스트는 못 적어 둔 표현을 놓친다. 낮은 것이 사실이다.**

    이 테스트는 특정 적중 수를 고정하지 않는다 -- 고정하면 사전을 늘릴 때마다 깨지고,
    보류 집합을 보고 사전을 고치는 쪽으로 압력이 생긴다. 고정하는 것은 **그 수를 리포트가
    실제로 찍는다는 사실**이다.
    """
    from harness import run_draft

    rows = dict(run_draft._report_guards())

    assert "효능 사전 보류 집합" in rows
    assert "비순환" in rows["효능 사전 보류 집합"]
    # 지금은 0/8 이다. 0 이어도 찍혀야 한다 -- 숨기면 커버리지 숫자가 거짓이 된다.
    hits = sum(1 for ex in claims.HOLDOUT_CLAIM_EXAMPLES if claims.screen(ex))
    assert f"{hits}/{len(claims.HOLDOUT_CLAIM_EXAMPLES)}건" in rows["효능 사전 보류 집합"]
