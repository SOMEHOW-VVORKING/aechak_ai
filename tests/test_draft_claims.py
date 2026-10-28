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


def test_보류_집합의_수를_숨기지_않고_순환임을_함께_적는다() -> None:
    """**네거티브 리스트는 못 적어 둔 표현을 놓친다. 낮은 것이 사실이다.**

    이 테스트는 특정 적중 수를 고정하지 않는다 -- 고정하면 사전을 늘릴 때마다 깨진다.
    고정하는 것은 둘이다. **그 수를 리포트가 실제로 찍는가**, 그리고 사전을 그 목록을 보고
    고친 뒤에는 **그 수가 순환이라고 적는가.**

    앞 판은 그 줄에 "이것이 유일한 비순환 숫자다" 라고 적어 뒀다. 0/8 을 보고 사전을 고친
    뒤에도 그 문장이 남으면 리포트가 **거짓 커버리지**를 주장한다. 고친 사실과 그 대가를
    같은 줄에 적는다.
    """
    from harness import run_draft

    rows = dict(run_draft._report_guards())
    line = rows["효능 사전 보류 집합"]

    hits = sum(1 for ex in claims.HOLDOUT_CLAIM_EXAMPLES if claims.screen(ex))
    assert f"{hits}/{len(claims.HOLDOUT_CLAIM_EXAMPLES)}건" in line
    assert "순환이다" in line, line
    assert "이미 보고 고친 회귀 사례 수" in line, line
    assert "유일한 비순환" not in line, line
    assert "비순환 숫자가 지금 없다" in rows
    assert claims.HOLDOUT_CLAIM_EXAMPLES, "목록을 지우지 않는다 -- 무엇을 보고 고쳤는지가 근거다"


# --- 비순환 재현율이 0/8 이었다 ----------------------------------------------------------


def test_질병명을_쓴_문구를_잡는다() -> None:
    """**초안은 질병명을 쓰지 않는다.** 사실 슬롯에 질병 자리가 없다.

    앞 판의 사전은 "치료"·"완치"처럼 **동작 어휘**만 들고 있었고 보류 집합 8건을 전건
    미탐했다. 주입 테스트로 확인했다 -- '염증을 가라앉혀 줍니다' 가 ``guardDrops=[]`` 로
    description 에 그대로 실려 나갔다.

    그 구멍을 리터럴 한 줄로 메우면 보류 집합이 순환이 된다. 그래서 **축이 다른 규칙**을
    둔다. 질병·증상 명사는 사실 슬롯(품목·소재·사용법·보관)의 어느 자리에도 정당하게
    들어가지 않으므로, 그 명사가 보이면 비우는 것이 과탐 쪽으로 기울이는 이 계층의
    방향과 맞는다.
    """
    for text in (
        "염증을 가라앉혀 줍니다",
        "피부병이 사라집니다",
        "관절염에 좋습니다",
        "알러지를 잡아 줍니다",
        "장 건강을 되찾아 줍니다",
        "면역력을 높여 질병을 예방합니다",
        "사용 2주 후 통증이 줄어듭니다",
    ):
        assert claims.screen(text), f"{text!r} 을 잡지 못했다"


def test_기간_단정과_후기_인용을_잡는다() -> None:
    """'3일이면 달라집니다' 와 '후기가 많습니다' 는 질병명 없이 효과를 단정한다.

    기간 + 변화 서술은 효과를 수치로 약속하는 모양이고, 후기 인용은 초안이 가질 수 없는
    근거다(초안은 리뷰를 받지 않는다 -- 툴 스키마에 그 입력이 없다).
    """
    for text in (
        "3일이면 달라집니다",
        "2주면 효과를 보실 수 있습니다",
        "눈에 띄게 좋아졌다는 후기가 많습니다",
    ):
        assert claims.screen(text), f"{text!r} 을 잡지 못했다"


def test_정상_문구를_막지_않는다() -> None:
    """**과탐 방향으로 기울이더라도 리프의 초안이 영구히 비면 안 된다.**

    이 목록은 사실 슬롯에 정당하게 들어갈 문장들이다. 하나라도 걸리면 그 리프의 설명이
    항상 비고, 셀러는 이유를 모른다.
    """
    for text in (
        "나일론 소재의 산책용 목줄입니다",
        "실내 배변 패드로 쓰는 제품입니다",
        "건조한 곳에 보관하고 물로 세척해 주세요",
        "처방식사료를 급여하는 아이에게 맞춘 구성입니다",
        "수의사와 상담해 급여량을 정해 주세요",
        "관절 건강에 도움을 줄 수 있는 글루코사민이 들어 있습니다",
        "2kg 포장이고 하루 두 번 급여합니다",
    ):
        assert not claims.screen(text), f"{text!r} 이 과탐으로 걸렸다: {claims.screen(text)}"


# --- 이름 0단과 출력 가드를 같은 사전에 걸지 않는다 -----------------------------------------
#
# **머리글의 비용 비대칭 논증이 입력 경로에서는 거꾸로다.**
#
# 모듈이 과탐 방향으로 기울인 근거를 "과탐은 필드를 비우게 하고(셀러가 직접 쓴다) 과소탐은
# 셀러 상품이 차단되게 한다" 로 적어 뒀다. 그 논증은 **출력 가드**용이다. 같은 사전을 0단
# 입력 차단(``NAME_CLAIM_BLOCKED``)에 걸면서 과탐의 비용이 '필드 하나 비움'에서 '초안을
# 아예 안 줌 + 셀러를 꾸짖는 안내문'으로 바뀌었는데 사전은 그대로였다.

# 사전을 보지 않고 쓴 정상 상품명. **한국 커머스에서 흔하고 효능과 무관하다.**
_ORDINARY_NAMES: tuple[str, ...] = (
    "강아지 목줄 가죽 M",
    "3일 배송 보장 고양이 모래",
    "후기 많은 인기 하네스",
    "치료실 전용 매트",
    "염증 걱정되는 아이용 순한 패드",
    "완치 기원 반려동물 쾌유 카드",
    "보장된 내구성 하네스",
    "저알러지 사료 2kg",
    "노견용 관절 영양제",
    "피부과 추천 샴푸",
    "수술 후 회복용 넥카라",
    "효과 좋은 치석 제거 껌",
    "100% 순면 반려동물 타월",
    "즉시 발송 강아지 간식",
)

# 이름만 보고 **모호하지 않게** 효능을 단정하는 것. 0단에서 막아야 한다.
_CLAIMING_NAMES: tuple[str, ...] = (
    "관절염 치료 목줄",
    "아토피 완치 샴푸",
    "슬개골 탈구가 낫는 보조제",
    "만병통치 반려동물 영양제",
    "부작용이 전혀 없는 피부 연고",
    "모든 강아지에게 효과 있는 사료",
    "식약처 인증 완료 관절 영양제",
    "털빠짐 완전해결 브러시",
)


def test_이름_0단이_정상_상품명을_막지_않는다() -> None:
    """**내가 쓴 14건 중 6건이 차단됐다.** 전건 ``modelCalled=false`` + 빈 초안이다.

    '배송 보장'·'후기 많은'·'치료실'·'염증 걱정되는'·'완치 기원' 은 효능과 무관하다. 안내문이
    오진을 하면서 '질병·치료·기간 약속 표현을 빼라'고 지시하므로 셀러는 무엇을 고쳐야 하는지
    알 수 없다.
    """
    blocked = {name: claims.screen_name(name) for name in _ORDINARY_NAMES}
    hits = {name: [hit.canonical for hit in found] for name, found in blocked.items() if found}
    assert not hits, f"정상 상품명이 0단에서 막혔다: {hits}"


def test_이름_0단이_모호하지_않은_효능_단정은_막는다() -> None:
    """느슨하게 풀어 주는 쪽으로 기울이지 않는다. 공기(共起)를 요구하되 전건 잡아야 한다."""
    missed = [name for name in _CLAIMING_NAMES if not claims.screen_name(name)]
    assert not missed, f"이름에 효능 단정이 있는데 0단을 통과했다: {missed}"


def test_이름_0단이_출력_가드보다_좁다() -> None:
    """같은 함수를 두 경로에 걸면 한쪽의 비용 논증이 다른 쪽에서 거꾸로 선다.

    느슨한 층은 출력 가드에 남긴다 -- 거기서는 슬롯 하나를 비우는 비용이다.
    """
    for loose in ("염증이 걱정되는 아이에게", "3일 배송을 보장합니다", "후기가 아주 많습니다"):
        assert claims.screen(loose), f"{loose!r} 이 출력 가드에서도 빠졌다"
        assert not claims.screen_name(loose), f"{loose!r} 이 이름 0단에서 막혔다"


# --- 출력 가드의 효능 미탐 -----------------------------------------------------------------
#
# **같은 사전이 입력에서는 과탐으로 정상 상품명을 하드 차단하고 출력에서는 과소탐으로 효능
# 문구를 통과시켰다.** 아래 목록은 사전을 보지 않고 쓴 것이고, 전건이 ``guardDrops=[]`` 로
# ``description`` 에 실려 나갔다(재현율 5/18). 셀러는 그것을 저장하고 등록 검수가 막는다 --
# 설계가 피하겠다고 선언한 함정이다.
#
# **이 목록도 순환이다.** 보고 고쳤으므로 그렇게 적는다. 비순환 숫자는 실제 셀러 문구에서
# 받아야 하고, 내가 지어낸 문장으로 만든 집합은 규칙을 쓴 사람이 같아서 증거가 약하다.


def test_출력_가드가_효능_문구를_통과시키지_않는다() -> None:
    missed = [text for text in claims.SECOND_HOLDOUT_CLAIM_EXAMPLES if not claims.screen(text)]
    assert not missed, f"효능 문구가 출력 가드를 통과한다: {missed}"


def test_없이가_뒤_표현의_부정으로_읽히지_않는다() -> None:
    """``_NEG_BEFORE_WORDS`` 에 '없이'·'없는' 이 있어 **그 뒤의 적중이 통째로 꺼졌다.**

    '항생제 없이' 는 항생제를 부정하고 그 뒤의 '염증 잡는' 을 부정하지 않는다. 한국어에서
    '없이'·'없는' 은 **앞** 명사의 수식어라 접두 부정 집합에 있을 수 없다. 뒤에 붙는
    경우(``'알러지 없는 사료'``)는 접미 부정이 이미 본다.
    """
    assert claims.screen("항생제 없이 염증 잡는 연고"), "'없이' 가 뒤 적중을 껐다"
    assert not claims.screen("알러지 없는 원료로 만들었습니다"), "접미 부정이 깨졌다"
    assert not claims.screen("치료 목적이 아닙니다")
    assert not claims.screen("의약품이 아닙니다")


def test_출력_가드_과탐이_정상_슬롯_문장을_막지_않는다() -> None:
    """출력 층을 넓혔으므로 **정상 슬롯 문장의 분모를 함께 넓혀** 과탐을 다시 잰다."""
    for text in (
        "나일론 소재의 산책용 목줄입니다",
        "급여량을 급히 늘리면 묽은 변을 볼 수 있습니다",
        "털이 많이 빠지는 시기에는 자주 빗어 주세요",
        "피부가 민감한 아이를 위해 무향으로 만들었습니다",
        "그늘에 말려 보관하고 물로 세척해 주세요",
        "관절 건강에 도움을 줄 수 있는 글루코사민이 들어 있습니다",
        "장 건강을 생각한 유산균 배합입니다",
        "사이즈 차이가 있을 수 있으니 치수를 확인해 주세요",
        "배변 후 냄새가 퍼지지 않도록 덮개를 덮어 주세요",
        "7일 이내 교환이 가능합니다",
        "30일 보관 후 폐기해 주세요",
        "실내 배변 패드로 쓰는 제품입니다",
    ):
        assert not claims.screen(text), f"{text!r} 이 과탐으로 걸렸다: {claims.screen(text)}"


def test_안내문이_층_이름_대신_고칠_글자를_보여_준다() -> None:
    """``"'질병 치료 단정' 이 있습니다"`` 는 무엇을 고쳐야 하는지 알려 주지 않는다."""
    hits = claims.screen_name("관절염 치료 목줄")

    assert hits
    shown = [hit.display for hit in hits]
    assert any("관절염" in text and "치료" in text for text in shown), shown
    # 어휘 층은 그대로 표준형을 보여 준다 -- 그쪽은 글자가 곧 표준형이다.
    lexical = claims.screen_name("만병통치 반려동물 영양제")
    assert [hit.display for hit in lexical] == ["특효"]
