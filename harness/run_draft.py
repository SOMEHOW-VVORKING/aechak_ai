"""초안 측정 러너. **(가)와 (나)를 같은 입력 집합으로 비교한다.**

이 범위 산출물 가운데 값이 가장 크다. **입력 방식 결정이 여기 나오는 숫자에 달려 있다.**

    python -m harness.run_draft                        키 없이 도는 리포트 전부
    python -m harness.run_draft --count-tokens         접두부 토큰 실측 (거의 무료)
    python -m harness.run_draft --count-image-tokens   이미지 토큰 추정을 치환 (유료 호출 0)
    python -m harness.run_draft --mode a --repeat 3 --yes

키는 ``AECHAK_AI_ANTHROPIC_API_KEY`` 다(``ANTHROPIC_API_KEY`` 가 아니다 -- ``Settings`` 의
``env_prefix`` 가 ``AECHAK_AI_``). 없으면 **키 없이 뽑을 수 있는 리포트를 찍고 종료 코드 0**
으로 끝난다.

## 왜 두 방식의 비교가 공정한가

(가)와 (나)는 **별 코드 경로가 아니다.** 같은 프롬프트, 같은 툴, 같은 모델, 같은 접두부를
쓰고 **이미지 블록 하나의 유무만 다르다.** 이미지는 messages 에 들어가 tools+system 접두부를
깨지 않으므로 두 변형이 같은 캐시 엔트리를 공유한다. **변수가 하나다.**
즉 (가)는 별 기능이 아니라 (나)의 축퇴이고 ``imageUsability = NO_IMAGE_SENT`` 로 표현된다.

## 모드 다섯

    lexicon-only   모델 0회. 0단 어휘 게이트만.          ** 무료 기준선 **
    a              이름 + 카테고리(주어진 대로)           <- (가)
    a-blind        이름만. 카테고리를 숨긴다              <- 카테고리의 한계 이득 대조군
    b              a + 이미지 1장 (base64)                <- (나)
    gated          어휘 게이트가 확정하지 못했을 때만 이미지 <- 조건부 멀티모달 (제3안)

## 키 없이 뽑는 숫자 — 값비싼 것부터

1. **``lexicon-only`` 의 층별 리프 정확도.** 0원 기준선이고, 이것이 곧 (가)안의 핵심 주장이
   실제 상품명 분포에서 성립하는지다. 모델이 이것을 못 넘기면 호출 자체가 비용만 더한다.
2. **``OPAQUE`` 층 비중.** 이미지가 값을 낼 수 있는 구간의 **상한**이다. 그 층이 얇으면
   이미지의 최대 이득도 얇다 -- 키 없이 이미지 결정의 천장을 먼저 아는 길이다.
3. 게이트 등급 x 카테고리 교차표. 모델을 몇 건에서 아예 안 부르는지(= 0원 건수).
4. 사전 미적중 목록. **2층 동의어를 추측으로 채우지 말고 이 목록에서 받는다.**
5. 가드 겹별 작동 확인. ``claims`` 가 정책 위반 예시 전건을 잡는지.
6. 봉투 조립 검증. 적대적 상품명이 봉투를 닫지 못하는지.
7. 비용 추정. ``--yes`` 없이는 추정만 찍고 멈춘다.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic

from app.inspection import rules
from app.llm.client import cost_micro_usd
from app.schemas.draft import (
    Basis,
    FieldStatus,
    ImageMode,
    ImageUsability,
    SellerDraftRequest,
)
from app.seller_draft import catalog, claims, imagemeta
from app.seller_draft.catalog import ItemGrade
from app.seller_draft.prompt import DRAFT_PREFIX, draft_system_blocks, draft_version_string
from app.seller_draft.service import DraftService, DraftUnprocessable
from app.seller_draft.tool import DRAFT_TOOL
from app.settings import Settings
from harness.draft_cases import (
    CASES,
    DraftCase,
    ImageVariant,
    NameReveals,
    by_stratum,
    covered_leaves,
    scorable,
)
from harness.run import HAIKU_CACHE_MIN_TOKENS, KRW_PER_USD, SONNET_CACHE_MIN_TOKENS, CachePrefixTarget

OUT_ROOT = Path(__file__).parent / "out"
IMAGE_ROOT = Path(__file__).parent / "draft_images"

MODES = ("lexicon-only", "a", "a-blind", "b", "gated")
VARIANTS = (640, 768, 896, 1024, 1500)

# 오사진 안전율을 숫자로 찍기 위한 최소 케이스 수. 미달이면 "판정 불가"로 찍는다.
#
# 그 줄은 스스로를 출시 게이트로 선언하므로 분모가 작으면 게이트가 운에 걸린다.
# ``--repeat`` 는 같은 사진을 다시 보내는 것이라 분모를 늘리지 않는다 -- 필요한 것은
# **서로 다른 오사진**이고, 특히 어휘 게이트가 비어 모델이 사진에 가장 많이 의존하는
# OPAQUE 층에 있어야 한다.
WRONG_PHOTO_MIN_CASES = 6

# 건당 추정의 **토큰 가정값**. ``--count-tokens`` / ``--count-image-tokens`` 가 치환하는
# 지점이 여기 한 곳이다.
#
# 전제: 접두부 2,200 토큰(지시 + 리프 경로 실측 글자 + 툴 스키마), user content 140,
# 출력 450(설명 슬롯 4 + 옵션 2그룹 + 리프 + basis). 896 사방 이미지 1,024 토큰.
_DRAFT_TOKEN_ASSUMPTIONS = {
    "draft_text_haiku": {"input_tokens": 2_340, "output_tokens": 450},
    # 896 사방 이미지의 **증분**. 입력 토큰만 더한다.
    "draft_image_896_haiku": {"input_tokens": 1_024, "output_tokens": 0},
}


def _estimate_micro_usd() -> dict[str, int]:
    """건당 추정 microUSD. **단가 정본 함수에서 파생한다. 상수로 적지 않는다.**

    처음 판은 손계산한 microUSD 를 표에 적어 뒀다. 값은 맞았지만 ``cost_micro_usd()`` 와
    **연결이 없었다** -- 테스트는 이 표 아래쪽(비율·일 상한)의 파생만 고정하고 표 자체가
    ``_PRICING`` 을 따라가는지는 아무도 보지 않았다. ``client.py`` 의 ``_PRICING`` 주석이
    "모델 확정 시 공식 단가로 재확인한다"고 단가 변경을 예고하고 있어, 단가를 고치는 날
    두 하네스의 모든 비용 줄과 일 예산 상한이 **조용히 과거 값으로 남고 테스트는 전부
    초록**이었다. "상수로 적지 않고 비용표에서 파생한다"는 자랑이 표 아래쪽만 참이고
    표 자체가 상수였던 것이다.

    지금은 토큰 가정값만 상수이고 돈은 전부 여기서 계산된다.
    """
    model = Settings().draft_model
    return {
        key: cost_micro_usd(model, cache_read=0, cache_creation=0, **tokens)
        for key, tokens in _DRAFT_TOKEN_ASSUMPTIONS.items()
    }


_DRAFT_ESTIMATE_MICRO_USD = _estimate_micro_usd()


def _image_cost_ratio() -> float:
    """이미지 증분 비율. **상수로 적지 않고 비용표에서 파생한다.**

    적어 두면 비용표를 실측으로 갈아 끼울 때 이 숫자가 따라 움직이지 않고, 그래도 어느
    테스트도 깨지지 않는다 -- ``harness/run.py`` 의 ``_breakeven_promotion_rate()`` 가
    상수로 적히지 않은 이유와 같다. 테스트가 이 파생을 고정한다.
    """
    return _DRAFT_ESTIMATE_MICRO_USD["draft_image_896_haiku"] / _DRAFT_ESTIMATE_MICRO_USD["draft_text_haiku"]


IMAGE_COST_RATIO = _image_cost_ratio()

_DAILY_BUDGET_USD = Settings().draft_daily_budget_usd


def _daily_draft_ceiling() -> int:
    """일 예산으로 초안을 몇 건 돌릴 수 있는가. 비용표에서 파생한다."""
    return int(_DAILY_BUDGET_USD * 1_000_000 // _DRAFT_ESTIMATE_MICRO_USD["draft_text_haiku"])


DAILY_DRAFT_CEILING = _daily_draft_ceiling()


def model_earns_its_keep(acc_model: float, acc_lexicon: float) -> bool:
    """**기계적 중단 규칙.** 0단이 공짜인데 모델이 더 못 맞히면 그 호출은 비용만 더한다.

    선별 하네스의 손익분기와 같은 성질이고 비용표 없이도 성립한다. 테스트가 고정한다.
    """
    return acc_model > acc_lexicon


@dataclass(slots=True)
class Record:
    """케이스 한 건의 결과 한 줄."""

    mode: str
    repeat: int
    case_id: str
    name_reveals: str
    category_given: int | None
    image_variant: str | None
    leaf_truth: int | None
    gate_grade: str
    gate_leaf: int | None
    predicted_leaf: int | None
    leaf_correct: bool
    scorable: bool
    model_called: bool
    image_used: bool
    image_usability: str
    description_filled: bool
    option_groups_filled: bool
    option_group_names: list[str]
    option_group_hit: bool | None
    field_status: dict[str, str]
    guard_kinds: list[str]
    basis: dict[str, str]
    cost_micro_usd: int
    latency_ms: int
    cache_read_tokens: int
    note: str = ""
    # **옵션 그룹에 정답이 있는 케이스인가.** ``option_group_hit`` 과 별 축이어야 한다.
    #
    # ``option_group_hit`` 은 셋을 뭉개고 있었다 -- 맞혔다(True) / 틀렸다(False) /
    # **모델이 옵션을 아무것도 내지 않았다**(None). 집계가 None 을 분모에서 빼므로
    # 53건 중 10건만 옵션을 내고 그 10건이 맞으면 ``10/10(100%)`` 으로 찍히고 43건이
    # 아무것도 못 냈다는 사실이 어디에도 안 적혔다. 분모를 이 필드로 따로 들면
    # 미측정을 숫자로 적을 수 있다.
    option_group_scorable: bool = False

    def as_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


# --- 모델·이미지에 **실제로 닿는** 케이스 ------------------------------------------------
#
# 이 셋이 없어서 리포트가 거짓을 찍었다. "WRONG 6건이 오사진 안전율의 분모다 (최소 6건
# 충족)" 과 "이미지 이득 천장 OPAQUE 19/72" 가 둘 다 **모델에 닿지 않는 건을 분모로 셌다.**
# 실제 측정 분모는 각각 5건과 6건이고, 전자는 선언 최소에 미달해 게이트가 영구히 판정
# 불가였다. 분모를 세는 함수를 한 자리에 두고 리포트와 테스트가 같은 것을 본다.


def _category_for(case: DraftCase, mode: str) -> int | None:
    """그 모드에서 실제로 넘어가는 카테고리. ``a-blind`` 는 숨긴다."""
    return None if mode == "a-blind" else case.category_given


def model_called_cases(mode: str = "a") -> list[DraftCase]:
    """모델이 **불리는** 케이스. 0원 분기를 전부 뺀다.

    셋이 0원이다. 상품명 0단 룰 차단, 상품명 효능 단정 차단(``claims``), 품목 미식별.
    셋째는 모드에 따라 달라진다 -- ``a-blind`` 는 카테고리를 숨기므로 미식별이 늘어난다.
    """
    out: list[DraftCase] = []
    for case in CASES:
        if case.expect_rule_block or claims.screen(case.product_name):
            continue
        given = _category_for(case, mode)
        if catalog.resolve(case.product_name, given).grade is ItemGrade.OPAQUE and given is None:
            continue
        out.append(case)
    return out


def image_reachable_cases(mode: str) -> list[DraftCase]:
    """사진이 **모델에 실려 가는** 케이스.

    ``GATED`` 는 어휘 게이트가 리프를 확정하지 못했을 때만 이미지를 쓴다
    (``service._should_use_image``). 그 비대칭을 여기서 같은 조건으로 센다.
    """
    if mode not in ("b", "gated"):
        return []
    out: list[DraftCase] = []
    for case in model_called_cases(mode):
        if case.image_variant is None:
            continue
        if mode == "gated":
            signal = catalog.resolve(case.product_name, _category_for(case, mode))
            if signal.pinned_leaf_id is not None:
                continue
        out.append(case)
    return out


def wrong_photo_reachable_cases(mode: str = "b") -> list[DraftCase]:
    """오사진 안전율의 **실제 분모.**

    ``WRONG`` 등급 전수가 아니다. OPAQUE + 카테고리 없음인 건은 ``ITEM_UNIDENTIFIED`` 로
    0원 종료해 모델을 한 번도 부르지 않으므로 그 안전율을 측정할 수 없다. 그 건을 분모로
    세면 "최소 6건 충족" 이 거짓이 된다.
    """
    return [case for case in image_reachable_cases(mode) if case.image_variant is ImageVariant.WRONG]


# --- 키 없이 도는 자기검사 --------------------------------------------------------------


def _self_check() -> int:
    """케이스의 기대값이 실제 코드 결과와 맞는지 본다. **하나라도 어긋나면 종료 코드 1.**

    틀린 기대값으로 재면 그 숫자는 아무것도 말하지 않는다. ``harness/run.py`` 의
    ``_self_check()`` 와 같은 규약이다.
    """
    problems: list[str] = []
    grade_of_label = {
        NameReveals.EXPLICIT: ItemGrade.PINNED,
        NameReveals.AMBIGUOUS: ItemGrade.NARROWED,
        NameReveals.OPAQUE: ItemGrade.OPAQUE,
    }

    # (1) 리프 정답이 실제 시드 50개 안에 있는가 + 50 리프 전부가 최소 1건으로 덮이는가.
    known = set(catalog.LEAF_IDS)
    for case in CASES:
        if case.leaf_truth is not None and case.leaf_truth not in known:
            problems.append(f"{case.id}: 모르는 리프 id {case.leaf_truth}")
    missing = known - covered_leaves()
    if missing:
        problems.append(f"덮지 못한 리프 {sorted(missing)} -- 리프마다 최소 1건이 필요하다")

    # (2) ``name_reveals`` 라벨이 실제 ``catalog.resolve()`` 결과와 일치하는가.
    #     **라벨은 사람 직관이고 게이트는 코드다. 둘이 어긋나면 모든 층별 숫자가 거짓이다.**
    for case in CASES:
        actual = catalog.resolve(case.product_name, None).grade
        if actual is not grade_of_label[case.name_reveals]:
            problems.append(
                f"{case.id}: 층 라벨 {case.name_reveals.value} 인데 게이트는 {actual.value} 다"
                " (카테고리 없이 계산한다)"
            )

    # (3) ``expect_rule_block`` 이 실제 0단 결과와 맞는가. 분모 오염 방지.
    for case in CASES:
        blocked = bool(rules.screen(case.product_name).blocking)
        if blocked != case.expect_rule_block:
            problems.append(f"{case.id}: 0단 차단 기대 {case.expect_rule_block} / 실제 {blocked}")

    # (4) 툴 enum 이 ``LEAVES`` 를 **전부** 덮는가.
    #     하나라도 못 덮으면 모델이 고른 리프가 조용히 센티넬로 떨어진다.
    enum = set(DRAFT_TOOL["input_schema"]["properties"]["leafCategoryId"]["enum"])
    if not known <= enum:
        problems.append(f"툴 enum 이 리프 {sorted(known - enum)} 를 덮지 않는다")

    # (5) ``LEAVES`` 가 커밋된 시드 사본과 일치하는가.
    contract = Path(__file__).parent.parent / "tests" / "contract" / "category-leaves.json"
    if contract.is_file():
        stored = json.loads(contract.read_text(encoding="utf-8"))
        if stored.get("leaves") != catalog.contract_rows():
            problems.append(
                "리프 표가 tests/contract/category-leaves.json 과 어긋난다. "
                "백엔드 시드가 정본이므로 사본과 LEAVES 를 함께 갱신해야 한다"
            )
    else:
        problems.append(f"리프 계약 사본이 없다: {contract}")

    # (6) ``ITEM_TERMS`` 1층이 ``LEAVES`` 에서 파생된 것인가(손으로 적힌 항목이 없는지).
    derived: dict[str, set[int]] = {}
    for leaf in catalog.LEAVES:
        for surface in leaf.leaf_label.split("/"):
            from app.inspection.banned import fold

            key = fold(surface)
            if key:
                derived.setdefault(key, set()).add(leaf.category_id)
    expected = {
        key: frozenset(ids)
        for key, ids in derived.items()
        if key not in {k for k in derived if k in _stopwords()}
    }
    if catalog.ITEM_TERMS != expected:
        extra = sorted(set(catalog.ITEM_TERMS) - set(expected))
        gone = sorted(set(expected) - set(catalog.ITEM_TERMS))
        problems.append(f"1층 어휘가 파생값과 다르다. 추가={extra} 누락={gone}")

    # (7) ``claims`` 가 정책의 효능 위반 예시 전건을 잡는가(못 잡는다고 적어 둔 것은 제외).
    for example in claims.POLICY_CLAIM_EXAMPLES:
        hits = claims.screen(example)
        declared_uncovered = example in claims.UNCOVERED_POLICY_EXAMPLES
        if bool(hits) == declared_uncovered:
            problems.append(
                f"효능 사전 커버리지가 어긋난다: {example!r} "
                f"(적중={bool(hits)} / 못 잡는다고 적어 둠={declared_uncovered})"
            )

    # (8) 이미지 픽스처가 있으면 헤더 치수가 선언 등급과 맞는가.
    #     어긋나면 토큰 예산이 틀리고 비용 비교가 무의미해진다.
    #
    #     **치수만 보면 안 된다.** 그것이 이 검사의 결함이었다 -- 같은 합성 PNG 한 장을
    #     29건에 복사해 두면 "29/29건 준비됨 / 자기검사 통과" 로 찍히고, 키를 등재한 뒤
    #     (나)·gated 모드를 돌리면 **같은 무늬 한 장을 29번 보낸 결과가 "이미지 이득"으로**
    #     리포트에 올라간다. 가장 나쁜 자리는 WRONG 등급이다 -- "오사진 안전율"의 분모인데
    #     GOOD 과 완전히 같은 이미지를 받으므로 그 숫자가 무의미해진다. ``draft_images/`` 가
    #     gitignore 라 안을 감사할 사람도 없다. 그래서 **내용 해시의 유일성**을 함께 본다.
    digests: dict[str, list[str]] = defaultdict(list)
    for case in CASES:
        if case.image_variant is None:
            continue
        for variant in VARIANTS:
            path = _image_path(case, variant)
            if not path.is_file():
                continue
            data = path.read_bytes()
            read = imagemeta.read_dimensions(data)
            if read is None:
                problems.append(f"{case.id}/{variant}: 헤더를 읽을 수 없다")
                continue
            _media, width, height = read
            if max(width, height) != variant:
                problems.append(
                    f"{case.id}/{variant}: 긴 변이 {max(width, height)}px 다. "
                    "선언 등급과 달라 토큰 예산이 틀린다"
                )
            if variant == 896:
                digests[hashlib.sha256(data).hexdigest()].append(f"{case.id}({case.image_variant.value})")
    for digest, owners in sorted(digests.items()):
        if len(owners) > 1:
            problems.append(
                f"픽스처 {len(owners)}건이 **같은 파일**이다({digest[:12]}): {', '.join(sorted(owners))}. "
                "플레이스홀더를 깔아 두면 '이미지 이득'과 '오사진 안전율'이 같은 사진 한 장의 "
                "숫자가 된다 -- 치수만 맞으면 통과하던 자리다"
            )

    if problems:
        print("[치명] 케이스 기대값이 코드와 어긋난다. 측정 전에 멈춘다.")
        for problem in problems:
            print(f"  {problem}")
        return 1

    strata = by_stratum()
    print(
        f"자기검사 통과. 케이스 {len(CASES)}건 / 리프 {len(covered_leaves())}개 덮음 / "
        + " · ".join(f"{k.value} {len(v)}" for k, v in strata.items())
        + f" / 채점 분모 {len(scorable())}건"
    )
    return 0


def _stopwords() -> frozenset[str]:
    """``catalog`` 의 비공개 불용어 집합을 자기검사가 쓰기 위한 어댑터.

    사본을 만들지 않는다 -- 사본을 두면 1층 파생 검사가 자기 자신을 검사하는 셈이 된다.
    """
    return catalog._SURFACE_STOPWORDS


def _image_path(case: DraftCase, variant: int) -> Path:
    return IMAGE_ROOT / case.id / f"{variant}.jpg"


# --- 키 없이 뽑는 리포트 ----------------------------------------------------------------


def _report_lexicon() -> list[tuple[str, str]]:
    """어휘 사전의 모양과 **0원 기준선 정확도.** 키 없이 뽑는 가장 값비싼 숫자다."""
    stats = catalog.lexicon_stats()
    rows: list[tuple[str, str]] = [
        (
            "어휘 사전",
            f"리프 {stats['leaves']}개 / 표면형 {stats['surfaces']}개 "
            f"(리프 하나로 확정 {stats['singleLeafSurfaces']} / 모호 "
            f"{len(stats['multiLeafSurfaces'])}) / 수기 2층 {stats['manualTerms']}개",
        ),
        (
            "종이 모호성을 푼다",
            (
                "종을 알고도 후보가 둘 이상 남는 표면형: **없음** — 모호성의 전부가 종 한 축이고, "
                "그것이 '상품명에 품목이 드러나면 쓸 만하다'는 직관의 근거다"
                if not stats["speciesCannotResolve"]
                else f"종으로도 못 푸는 표면형이 있다: {stats['speciesCannotResolve']}"
            ),
        ),
        (
            "고유 표면형 없는 리프",
            f"{len(stats['leavesWithoutUniqueSurface'])}개 — {stats['leavesWithoutUniqueSurface']}. "
            "종을 모르면 가릴 근거가 없는 리프들이다",
        ),
    ]

    # 0원 기준선. **모델 없이 지금 계산된다.**
    rows.extend(_accuracy_rows("lexicon-only", _lexicon_only_records()))

    misses = [
        f"{case.id}({case.product_name!r})"
        for case in CASES
        if catalog.resolve(case.product_name, None).grade is ItemGrade.OPAQUE and case.leaf_truth is not None
    ]
    rows.append(
        (
            "사전 미적중",
            f"{len(misses)}건 — **2층 동의어를 추측으로 채우지 말고 이 목록에서 받는다.** "
            + ", ".join(misses[:6])
            + (" ..." if len(misses) > 6 else ""),
        )
    )
    return rows


def _lexicon_only_records() -> list[Record]:
    """모델을 0회 부르고 어휘 게이트만으로 예측한다. **비용 0, 키 불필요.**"""
    records: list[Record] = []
    for case in CASES:
        signal = catalog.resolve(case.product_name, case.category_given)
        predicted = signal.pinned_leaf_id
        if case.expect_rule_block:
            predicted = None
        records.append(
            Record(
                mode="lexicon-only",
                repeat=1,
                case_id=case.id,
                name_reveals=case.name_reveals.value,
                category_given=case.category_given,
                image_variant=case.image_variant.value if case.image_variant else None,
                leaf_truth=case.leaf_truth,
                gate_grade=signal.grade.value,
                gate_leaf=signal.pinned_leaf_id,
                predicted_leaf=predicted,
                leaf_correct=(predicted == case.leaf_truth),
                scorable=case in scorable(),
                model_called=False,
                image_used=False,
                image_usability=ImageUsability.NO_IMAGE_SENT.value,
                description_filled=False,
                option_groups_filled=False,
                option_group_names=[],
                option_group_hit=None,
                field_status={},
                guard_kinds=[],
                basis={},
                cost_micro_usd=0,
                latency_ms=0,
                cache_read_tokens=0,
                # 0단 어휘 게이트는 옵션을 제안하지 않는다. 정답이 있는 케이스인 것은
                # 사실이므로 분모 플래그는 세우고, 미측정으로 적힌다.
                option_group_scorable=bool(case.acceptable_option_group_names),
            )
        )
    return records


def _report_gate_matrix() -> list[tuple[str, str]]:
    """게이트 등급 x 카테고리 교차표. **모델을 몇 건에서 아예 안 부르는지가 여기 있다.**"""
    rows: list[tuple[str, str]] = []
    grid: Counter[tuple[str, str]] = Counter()
    zero_cost = 0
    for case in CASES:
        signal = catalog.resolve(case.product_name, case.category_given)
        given = (
            "리프"
            if case.category_given in catalog.LEAF_BY_ID
            else ("없음" if case.category_given is None else "상위")
        )
        grid[(signal.grade.value, given)] += 1
        if case.expect_rule_block or (signal.grade is ItemGrade.OPAQUE and case.category_given is None):
            zero_cost += 1
    rows.append(
        (
            "게이트 x 카테고리",
            ", ".join(f"{grade}/{given}={count}" for (grade, given), count in sorted(grid.items())),
        )
    )
    rows.append(
        (
            "모델 0회 건수",
            f"{zero_cost}/{len(CASES)}건 ({zero_cost / len(CASES):.0%}) — **0원이다.** "
            "0단 차단 또는 품목 미식별이고, 둘 다 일반론을 쓰지 않는 쪽으로 끝난다",
        )
    )

    opaque = len(by_stratum()[NameReveals.OPAQUE])
    reach_b = image_reachable_cases("b")
    reach_gated = image_reachable_cases("gated")
    scorable_ids = {case.id for case in scorable()}
    rows.append(
        (
            "이미지 이득 천장",
            f"OPAQUE 층이 {opaque}/{len(CASES)}건 ({opaque / len(CASES):.0%}) 이지만 **사진이 "
            f"모델에 실제로 닿는 건은 b={len(reach_b)} / gated={len(reach_gated)}** "
            f"(그 가운데 채점 분모 b={sum(1 for c in reach_b if c.id in scorable_ids)} / "
            f"gated={sum(1 for c in reach_gated if c.id in scorable_ids)}). "
            "앞 판은 OPAQUE 층 전수를 천장으로 적었는데 그 층의 상당수가 카테고리 없이 "
            "0원 종료해 **사진이 모델에 실리지 않는다** — 약 3배 과대였다",
        )
    )
    return rows


def _report_guards() -> list[tuple[str, str]]:
    """가드 네 겹이 실제로 무엇을 잡는지. **0단이 효능을 못 잡는 것을 숫자로 남긴다.**"""
    rows: list[tuple[str, str]] = []
    covered = [ex for ex in claims.POLICY_CLAIM_EXAMPLES if ex not in claims.UNCOVERED_POLICY_EXAMPLES]
    zero_stage_hits = sum(1 for ex in covered if rules.screen(ex).blocking)
    rows.append(
        (
            "0단과 효능",
            f"정책 효능 위반 예시 {len(covered)}건 중 rules.screen 이 잡는 것 "
            f"{zero_stage_hits}건 / claims 사전이 잡는 것 "
            f"{sum(1 for ex in covered if claims.screen(ex))}건 — "
            "**rules 가 0건인 것이 claims.py 를 새로 만든 이유다.** 그 사전은 이제 모델 "
            "출력만 보지 않고 **상품명 0단에도 걸린다**(NAME_CLAIM_BLOCKED) -- 걸려 있지 "
            "않던 동안 '관절염 치료 목줄' 에 완성 초안이 나갔다. 다만 claims 쪽 숫자는 "
            "**커버리지가 아니다** -- 분모가 사전의 출처와 같고 같은 목록이 프롬프트에도 "
            "들어가므로 순환이며 미탐을 측정하지 못한다. 그 축은 아래 보류 집합이 본다",
        )
    )
    holdout = claims.HOLDOUT_CLAIM_EXAMPLES
    holdout_hits = sum(1 for ex in holdout if claims.screen(ex))
    missed = [ex for ex in holdout if not claims.screen(ex)]
    rows.append(
        (
            "효능 사전 보류 집합",
            f"{holdout_hits}/{len(holdout)}건"
            + (
                " — **이 숫자는 이제 순환이다.** 이 집합이 0/8 이었고 그것을 보고 사전을 "
                "고쳤다(질병명·완치 동사·기간 단정·후기 인용 네 층을 더했다). 리터럴을 "
                "베끼지는 않았지만 목록을 보면서 썼으므로 미탐을 측정하지 못한다"
                if claims.HOLDOUT_IS_CIRCULAR
                else " — **사전을 쓸 때 참조하지 않은 표현이고 이것이 유일한 비순환 숫자다.**"
            )
            + (
                f" / 놓친 것: {', '.join(missed[:4])}" + (" ..." if len(missed) > 4 else "") if missed else ""
            ),
        )
    )
    rows.append(
        (
            "비순환 숫자가 지금 없다",
            "다음 사람이 미탐을 재려면 **실제 셀러 문구**에서 새 보류 집합을 받아야 한다. "
            "내가 지어낸 문장으로 만든 집합은 규칙을 쓴 사람이 같아서 증거가 약하다 -- "
            "그 사실을 숫자 대신 적어 둔다",
        )
    )
    rows.append(
        (
            "못 막는 것",
            f"{list(claims.UNCOVERED_POLICY_EXAMPLES)} — 원산지 허위는 어휘 문제가 아니다. "
            "적어 두지 않으면 커버리지 숫자가 거짓이 된다",
        )
    )
    false_positives = [leaf.leaf_label for leaf in catalog.LEAVES if claims.screen(leaf.leaf_label)]
    rows.append(
        (
            "리프 라벨 과탐",
            f"{len(false_positives)}건{' ' + str(false_positives) if false_positives else ''} — "
            "리프 라벨이 효능 사전에 걸리면 그 리프의 초안이 영구히 빈다",
        )
    )
    return rows


def _report_envelope() -> list[tuple[str, str]]:
    """봉투 조립 검증. **적대적 상품명이 봉투를 닫지 못하는지.**"""
    from app.seller_draft.prompt import draft_user_content

    rows: list[tuple[str, str]] = []
    leaks: list[str] = []
    for case in CASES:
        signal = catalog.resolve(case.product_name, case.category_given)
        content = draft_user_content(
            product_name=case.product_name,
            signal=signal,
            locked=None,
            image_block=None,
            low_fidelity=False,
        )
        text = next(part["text"] for part in content if part.get("type") == "text")
        body = text.split("<본문>", 1)[-1].split("</본문>", 1)[0]
        if any(token in body for token in rules.ENVELOPE_TOKENS):
            leaks.append(case.id)
    rows.append(
        (
            "봉투 중성화",
            (
                f"{len(CASES)}건 전부 본문에 중성화되지 않은 토큰이 없다 "
                "(기존 ENVELOPE_TOKENS 4종을 그대로 쓰고 rules.py 를 고치지 않았다)"
                if not leaks
                else f"**봉투가 닫힌다: {leaks}**"
            ),
        )
    )
    rows.append(
        (
            "접두부 길이",
            f"DRAFT_PREFIX {len(DRAFT_PREFIX)}자 / 리프 경로 블록 "
            f"{len(catalog.render_leaf_paths())}자 — **글자 수이고 토큰 수가 아니다.** "
            "--count-tokens 가 치환한다",
        )
    )
    return rows


def _report_images() -> list[tuple[str, str]]:
    """이미지 픽스처와 토큰 예산. 없으면 이유를 적고 (나) 모드를 건너뛴다."""
    settings = Settings()
    rows: list[tuple[str, str]] = []
    wanted = [case for case in CASES if case.image_variant is not None]
    present = sum(1 for case in wanted if _image_path(case, 896).is_file())
    rows.append(
        (
            "이미지 픽스처",
            f"{present}/{len(wanted)}건 준비됨 (896 기준). "
            + (
                "없으면 (나)·gated 모드를 건너뛰고 종료 코드 0 이다 — harness/make_draft_images.sh 가 만든다"
                if present < len(wanted)
                else "전부 있다"
            ),
        )
    )
    budget = settings.draft_image_max_tokens
    rows.append(
        (
            "해상도 예산",
            ", ".join(
                f"{v}={imagemeta.estimate_tokens(v, v)}tok"
                + ("(초과)" if imagemeta.estimate_tokens(v, v) > budget else "")
                for v in VARIANTS
            )
            + f" / 상한 {budget}tok, 긴 변 {imagemeta.max_long_edge_for(budget)}px. "
            "**가로x세로/784 추정이고 --count-image-tokens 가 치환한다**",
        )
    )
    variants = Counter(case.image_variant.value for case in CASES if case.image_variant is not None)
    wrong = variants.get(ImageVariant.WRONG.value, 0)
    # **분모는 WRONG 전수가 아니라 모델에 닿는 건이다.** 앞 판이 전수를 세어 "최소 6건
    # 충족" 이라고 찍었는데 실제 측정 분모는 5건이었다 -- OPAQUE + 카테고리 없음인 건은
    # ITEM_UNIDENTIFIED 로 0원 종료해 모델을 한 번도 부르지 않는다. 그 줄이 거짓이었고,
    # 거짓이 아니었어도 게이트는 영구히 판정 불가였다.
    reach = {mode: len(wrong_photo_reachable_cases(mode)) for mode in ("b", "gated")}
    enough = min(reach.values()) >= WRONG_PHOTO_MIN_CASES
    rows.append(
        (
            "사진 등급 분포",
            ", ".join(f"{k}={v}" for k, v in sorted(variants.items()))
            + f" / WRONG 전수 {wrong}건 가운데 **모델에 닿는 것** b={reach['b']} gated={reach['gated']} "
            + "— 닿는 건만 오사진 안전율의 분모다"
            + (
                f" (최소 {WRONG_PHOTO_MIN_CASES}건 충족)"
                if enough
                else f" / **최소 {WRONG_PHOTO_MIN_CASES}건에 미달해 그 줄은 판정 불가로 찍힌다**"
            ),
        )
    )
    kinds = Counter(case.wrong_photo_kind.value for case in CASES if case.wrong_photo_kind is not None)
    rows.append(
        (
            "오사진 종류",
            ", ".join(f"{k}={v}" for k, v in sorted(kinds.items()))
            + " — 한 가지로 뭉개면 어느 종류에서 무너지는지 알 수 없다. "
            "**OTHER_SPECIES(같은 품목 다른 종)가 반려동물 커머스에서 더 흔하고 더 위험하다**",
        )
    )
    layer_image = {
        layer.value: (
            sum(1 for case in group if case.image_variant is not None),
            len(group),
        )
        for layer, group in by_stratum().items()
    }
    rows.append(
        (
            "층별 사진 보유(케이스)",
            " / ".join(f"{k}={v[0]}/{v[1]}" for k, v in sorted(layer_image.items()))
            + " — **층별 한계 이득을 읽기 전에 이 줄을 봐야 한다.** 사진이 없는 건은 "
            "(가)와 (나)가 같은 입력을 받으므로 그 층의 delta 에 이미지가 들어 있지 않다",
        )
    )
    return rows


def _report_mode_plan(*, repeat: int, variant: int, selected: str | None = None) -> list[tuple[str, str]]:
    """**키 없이 뽑는 모드 비교표.** 이 티켓의 산출물이 그것인데 한 줄도 없었다.

    ``--mode a``/``a-blind``/``b``/``gated`` 네 리포트가 산출물 경로 한 줄만 빼고 **바이트
    동일**했다(diff 로 확인). 모드별로 다른 것은 "추정 전체" 한 줄뿐이었고 그마저 모드를
    주지 않아도 똑같이 찍혔다. 즉 **입력 방식을 고르는 데 쓸 표가 오늘 생산되지 않았다.**

    그런데 모델 품질 말고는 모드 결정 재료가 거의 다 키 없이 계산된다.

    - 모델을 **몇 건에서 부르는가**(0원 분기 셋이 모드마다 다르게 걸린다)
    - 사진이 **몇 건에서 모델에 닿는가**(gated 는 게이트가 확정하지 못한 건만 보낸다)
    - 그 가운데 **채점 분모**가 몇 건인가(층별로 나누면 층당 몇 건인지가 결론을 정한다)
    - **오사진 안전율의 분모**가 선언 최소를 넘는가
    - 추정 비용과 **사람이 모아야 하는 사진 장수**

    품질 숫자는 여기 없다. 키가 없으면 그것은 영구히 없고, 없는 것을 추정으로 메우지 않는다.
    """
    rows: list[tuple[str, str]] = []
    scorable_ids = {case.id for case in scorable()}
    parts: list[str] = []
    for mode in MODES:
        calls = len(model_called_cases(mode)) if mode != "lexicon-only" else 0
        images = image_reachable_cases(mode)
        scored = sum(1 for case in images if case.id in scorable_ids)
        cost = _mode_cost(mode, len(model_called_cases("a")), repeat)
        parts.append(
            f"{mode}: 호출 {calls}건 · 사진 {len(images)}건(채점 {scored}) · {cost / 1_000_000:.3f}USD"
        )
    rows.append(
        (
            "모드별 계획",
            " / ".join(parts) + f" (반복 {repeat}, {variant}px). "
            "**숫자가 키 없이 갈리는 축은 이것이 전부다** — 정확도는 키가 있어야 한다",
        )
    )

    layer_parts: list[str] = []
    for mode in ("b", "gated"):
        by_layer: Counter[str] = Counter(
            case.name_reveals.value for case in image_reachable_cases(mode) if case.id in scorable_ids
        )
        body = ", ".join(f"{k} {v}" for k, v in sorted(by_layer.items())) or "없음"
        layer_parts.append(f"{mode}={body}")
    rows.append(
        (
            "사진이 닿는 층별 분모",
            " / ".join(layer_parts) + " — **층당 건수가 결론을 정한다.** 층당 몇 건으로 "
            "(가) 대 (나) 를 고를 수 있는지는 사람이 이 줄을 보고 판단한다",
        )
    )

    wrong_parts = []
    for mode in ("b", "gated"):
        count = len(wrong_photo_reachable_cases(mode))
        wrong_parts.append(f"{mode}={count}" + ("" if count >= WRONG_PHOTO_MIN_CASES else " **미달**"))
    rows.append(
        (
            "모드별 오사진 분모",
            " / ".join(wrong_parts) + f" (최소 {WRONG_PHOTO_MIN_CASES}). "
            "미달이면 그 모드에서 오사진 안전율은 **판정 불가**이고, 정확도와 무관하게 "
            "이미지를 켜지 않는다",
        )
    )

    if selected is not None:
        need = {case.id for case in CASES if case.image_variant is not None}
        have = sum(
            1 for case in CASES if case.image_variant is not None and _image_path(case, variant).is_file()
        )
        if selected == "lexicon-only":
            note = "없음 — 키도 사진도 필요 없다. 0원 기준선이 이 모드의 산출물이다"
        elif selected in ("a", "a-blind"):
            note = "키 하나. 사진은 쓰지 않는 모드다"
        else:
            note = (
                f"키 하나 + {variant}px 사진 {len(need)}장({have}장 준비됨). "
                "WRONG 등급은 **상품명과 다른 상품** 사진이어야 하고, 같은 사진을 복사해 두면 "
                "자기검사가 막는다"
            )
        rows.append((f"선택한 모드 [{selected}] 선결", note))
    return rows


def _report_estimate(repeat: int) -> list[tuple[str, str]]:
    """비용 추정. **전부 추정이고 실측이 아니다.**"""
    text = _DRAFT_ESTIMATE_MICRO_USD["draft_text_haiku"]
    image = _DRAFT_ESTIMATE_MICRO_USD["draft_image_896_haiku"]
    # **0원 분기 셋을 한 함수가 센다.** 앞 판은 0단 룰 차단과 품목 미식별 둘만 세어서
    # 상품명 효능 차단(claims)이 더해진 뒤에도 호출 건수가 그대로였다.
    called = len(model_called_cases("a"))
    rows = [
        (
            "추정 건당",
            f"텍스트 {text}uUSD({text * KRW_PER_USD / 1_000_000:.2f}원) / "
            f"+896 이미지 {text + image}uUSD({(text + image) * KRW_PER_USD / 1_000_000:.2f}원, "
            f"+{IMAGE_COST_RATIO:.0%}) / 품목 미식별 **0원**",
        ),
        (
            "출력 토큰이 더 큰 레버",
            "출력 300->900 증분이 896 이미지 전체의 약 3배다(추정). "
            "'이미지를 넣을 것인가'는 비용 문제로서는 두 번째 레버다",
        ),
        (
            "추정 전체",
            " / ".join(
                f"{mode}={_mode_cost(mode, called, repeat) / 1_000_000:.3f}USD"
                f"({_mode_cost(mode, called, repeat) * KRW_PER_USD / 1_000_000:,.0f}원)"
                for mode in MODES
            )
            + f" (호출 대상 {called}건 x 반복 {repeat})",
        ),
        (
            "일 예산 상한",
            f"초안 몫 ${_DAILY_BUDGET_USD}/일이면 {DAILY_DRAFT_CEILING}건/일. "
            "**상수로 적지 않고 비용표에서 파생한다**",
        ),
        (
            "중단 규칙",
            "acc(a) <= acc(lexicon-only) 면 모델을 부르지 마라 — 0단이 공짜인데 모델이 더 못 "
            "맞히면 그 호출은 비용만 더한다. 기계적이고 테스트로 고정된다",
        ),
    ]
    return rows


def _mode_cost(mode: str, called: int, repeat: int) -> int:
    """모드별 추정 비용. **호출 건수와 이미지 건수를 모드별로 다시 센다.**

    ``called`` 인자는 (가) 기준이고 ``a-blind`` 는 카테고리를 숨겨 품목 미식별이 늘어
    호출 건수가 **줄어든다.** 같은 수를 쓰면 그 모드의 비용이 과대 계상된다.
    """
    text = _DRAFT_ESTIMATE_MICRO_USD["draft_text_haiku"]
    image = _DRAFT_ESTIMATE_MICRO_USD["draft_image_896_haiku"]
    if mode == "lexicon-only":
        return 0
    calls = len(model_called_cases(mode)) if mode == "a-blind" else called
    images = len(image_reachable_cases(mode))
    return (text * calls + image * images) * repeat


# --- 토큰 실측 -------------------------------------------------------------------------


def draft_cache_prefix_targets(settings: Settings) -> list[CachePrefixTarget]:
    """초안 접두부. 기본 모델이 Haiku 라 ``required=False`` 다.

    하한 미달이 **실패가 아니라 근거**다 -- 그것이 접두부를 패딩하지 않기로 한 이유이고,
    하네스가 그 사실을 숫자로 남긴다. **대상은 system + 고정 tool 둘 다**다. tools 가
    접두부에 함께 캐시되므로 system 만 세면 둘 중 어느 것도 재지 않는 셈이다.
    """
    haiku = settings.draft_model.startswith("claude-haiku")
    return [
        CachePrefixTarget(
            name="draft_prefix",
            model=settings.draft_model,
            system=draft_system_blocks(),
            tools=[DRAFT_TOOL],
            min_tokens=HAIKU_CACHE_MIN_TOKENS if haiku else SONNET_CACHE_MIN_TOKENS,
            required=not haiku,
        )
    ]


async def _count_tokens(settings: Settings) -> int:
    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key, timeout=settings.request_timeout_seconds
    )
    failed = False
    try:
        print()
        for target in draft_cache_prefix_targets(settings):
            result = await client.messages.count_tokens(
                model=target.model,
                system=target.system,
                tools=target.tools,
                messages=[{"role": "user", "content": [{"type": "text", "text": "x"}]}],
            )
            tokens = result.input_tokens
            margin = (tokens - target.min_tokens) / target.min_tokens
            print(f"{target.name:22} {tokens:>6} 토큰  (최소 {target.min_tokens}, 여유 {margin:+.0%})")
            if tokens < target.min_tokens:
                if target.required:
                    print(f"[치명] {target.name}이 하한에 못 미친다. 캐시가 에러 없이 죽는다.")
                    failed = True
                else:
                    print(
                        f"  {target.name}은 하한 미달이다. cache_control 을 붙여도 무시된다 "
                        "— 접두부를 짧게 쓰고 패딩하지 않기로 한 근거다."
                    )
    finally:
        await client.close()
    print()
    print(
        f"이 명령은 접두부 토큰만 치환한다. 출력 토큰 전제는 실호출이 필요하므로 "
        f"{_DRAFT_ESTIMATE_MICRO_USD} 는 **여전히 추정이다.**"
    )
    return 1 if failed else 0


async def _count_image_tokens(settings: Settings, variant: int) -> int:
    """이미지 토큰 추정(/784)을 실측으로 치환한다. **유료 호출 0회.**"""
    cases = [case for case in CASES if case.image_variant is not None]
    available = [(case, _image_path(case, variant)) for case in cases]
    available = [(case, path) for case, path in available if path.is_file()]
    if not available:
        print(f"\n{variant} 등급 이미지 픽스처가 없다. harness/make_draft_images.sh 를 먼저 돌려라.")
        return 0

    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key, timeout=settings.request_timeout_seconds
    )
    try:
        print()
        for case, path in available[:5]:
            data = path.read_bytes()
            read = imagemeta.read_dimensions(data)
            if read is None:
                continue
            media, width, height = read
            measured = await client.messages.count_tokens(
                model=settings.draft_model,
                system=draft_system_blocks(),
                tools=[DRAFT_TOOL],
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media,
                                    "data": base64.b64encode(data).decode("ascii"),
                                },
                            },
                            {"type": "text", "text": "x"},
                        ],
                    }
                ],
            )
            baseline = await client.messages.count_tokens(
                model=settings.draft_model,
                system=draft_system_blocks(),
                tools=[DRAFT_TOOL],
                messages=[{"role": "user", "content": [{"type": "text", "text": "x"}]}],
            )
            actual = measured.input_tokens - baseline.input_tokens
            guess = imagemeta.estimate_tokens(width, height)
            print(
                f"{case.id:24} {width}x{height} 실측 {actual:>5}tok / 추정 {guess:>5}tok "
                f"(오차 {(actual - guess) / max(guess, 1):+.0%})"
            )
    finally:
        await client.close()
    print()
    print("이 숫자로 imagemeta.TOKENS_PER_PIXEL_DIVISOR 와 비용표를 치환해야 한다.")
    return 0


# --- 실측 ----------------------------------------------------------------------------


def _settings_for(mode: str, model: str, variant: int) -> Settings:
    """모드를 설정으로 옮긴다. **리터럴에서 조립한다** -- 앰비언트 환경을 읽지 않는다.

    ``draft_cache_entries=0`` 이 중요하다. 재사용 캐시가 켜져 있으면 ``--repeat`` 가 같은
    응답을 돌려받아 **반복 불일치율이 항상 0 으로 측정된다** -- 결정성 레버가 API 에 하나도
    없는 상태에서 그 숫자가 가장 알고 싶은 것이므로, 측정에서는 캐시를 끈다.
    """
    image_mode = {
        "lexicon-only": ImageMode.OFF,
        "a": ImageMode.OFF,
        "a-blind": ImageMode.OFF,
        "b": ImageMode.ALWAYS,
        "gated": ImageMode.GATED,
    }[mode]
    return Settings(
        anthropic_api_key=Settings().anthropic_api_key,
        enable_draft=True,
        draft_model=("claude-sonnet-5" if model == "sonnet" else "claude-haiku-4-5"),
        draft_effort=("low" if model == "sonnet" else None),
        draft_image_mode=image_mode.value,
        draft_image_max_tokens=max(imagemeta.estimate_tokens(variant, variant), 1400),
        draft_cache_entries=0,
        draft_timeout_seconds=25.0,
    )


def _request_for(case: DraftCase, mode: str, variant: int) -> SellerDraftRequest:
    from app.schemas.draft import DraftImage

    category = None if mode == "a-blind" else case.category_given
    image = None
    if mode in ("b", "gated") and case.image_variant is not None:
        path = _image_path(case, variant)
        if path.is_file():
            data = path.read_bytes()
            read = imagemeta.read_dimensions(data)
            width, height = (read[1], read[2]) if read else (0, 0)
            image = DraftImage(
                mediaType=read[0] if read else "image/jpeg",
                base64=base64.b64encode(data).decode("ascii"),
                declaredWidthPx=width,
                declaredHeightPx=height,
            )
    return SellerDraftRequest(
        requestId=f"harness-{mode}-{case.id}",
        productName=case.product_name,
        categoryId=category,
        image=image,
    )


async def _measure(
    *, mode: str, model: str, variant: int, repeat: int, llm: Any, cases: list[DraftCase]
) -> list[Record]:
    settings = _settings_for(mode, model, variant)
    scorable_ids = {case.id for case in scorable()}
    records: list[Record] = []
    for round_no in range(1, repeat + 1):
        for case in cases:
            service = DraftService(llm=llm, settings=settings)
            request = _request_for(case, mode, variant)
            try:
                response = await service.run(request)
            except DraftUnprocessable as error:
                records.append(_unprocessable_record(case, mode, round_no, str(error), scorable_ids))
                continue
            signal = catalog.resolve(case.product_name, request.categoryId)
            predicted = response.draft.categoryId or signal.pinned_leaf_id
            if response.fieldStatus.get("categoryId") in (
                FieldStatus.EMPTY_CATEGORY_CONFLICT,
                FieldStatus.EMPTY_MODEL_DECLINED,
            ):
                # 비운 것은 **고르지 않은 것**이다. 게이트 값으로 메우면 거짓이 된다.
                predicted = None
            names = [group.name for group in (response.draft.optionGroups or [])]
            records.append(
                Record(
                    mode=mode,
                    repeat=round_no,
                    case_id=case.id,
                    name_reveals=case.name_reveals.value,
                    category_given=request.categoryId,
                    image_variant=case.image_variant.value if case.image_variant else None,
                    leaf_truth=case.leaf_truth,
                    gate_grade=signal.grade.value,
                    gate_leaf=signal.pinned_leaf_id,
                    predicted_leaf=predicted,
                    leaf_correct=(predicted == case.leaf_truth),
                    scorable=case.id in scorable_ids,
                    model_called=response.modelCalled,
                    image_used=response.imageUsability is ImageUsability.USED,
                    image_usability=response.imageUsability.value,
                    description_filled=response.fieldStatus.get("description") is FieldStatus.FILLED,
                    option_groups_filled=response.fieldStatus.get("optionGroups") is FieldStatus.FILLED,
                    option_group_names=names,
                    option_group_hit=(
                        bool(set(names) & case.acceptable_option_group_names)
                        if names and case.acceptable_option_group_names
                        else None
                    ),
                    option_group_scorable=bool(case.acceptable_option_group_names),
                    field_status={k: v.value for k, v in response.fieldStatus.items()},
                    guard_kinds=[drop.kind.value for drop in response.guardDrops],
                    basis={k: v.value for k, v in response.basis.items()},
                    cost_micro_usd=response.total_cost_micro_usd(),
                    latency_ms=sum(call.latencyMs for call in response.usage),
                    cache_read_tokens=sum(call.cacheReadInputTokens for call in response.usage),
                    note=response.skipReason.value if response.skipReason else "",
                )
            )
    return records


def _unprocessable_record(
    case: DraftCase, mode: str, round_no: int, detail: str, scorable_ids: set[str]
) -> Record:
    signal = catalog.resolve(case.product_name, case.category_given)
    return Record(
        mode=mode,
        repeat=round_no,
        case_id=case.id,
        name_reveals=case.name_reveals.value,
        category_given=case.category_given,
        image_variant=case.image_variant.value if case.image_variant else None,
        leaf_truth=case.leaf_truth,
        gate_grade=signal.grade.value,
        gate_leaf=signal.pinned_leaf_id,
        predicted_leaf=None,
        leaf_correct=(case.leaf_truth is None),
        scorable=case.id in scorable_ids,
        model_called=False,
        image_used=False,
        image_usability=ImageUsability.NO_IMAGE_SENT.value,
        description_filled=False,
        option_groups_filled=False,
        option_group_names=[],
        option_group_hit=None,
        field_status={},
        guard_kinds=[],
        basis={},
        cost_micro_usd=0,
        latency_ms=0,
        cache_read_tokens=0,
        note=f"UNPROCESSABLE: {detail}",
        option_group_scorable=bool(case.acceptable_option_group_names),
    )


# --- 집계 ----------------------------------------------------------------------------


def _accuracy_rows(mode: str, records: list[Record]) -> list[tuple[str, str]]:
    """**1순위 지표.** 리프 정확도를 층별로 찍는다. 평균만 보면 결론이 사라진다."""
    pool = [r for r in records if r.scorable]
    if not pool:
        return [(f"[{mode}] 정확도", "채점 가능한 건이 없다")]

    rows: list[tuple[str, str]] = []
    overall = sum(1 for r in pool if r.leaf_correct) / len(pool)
    rows.append(
        (
            f"[{mode}] 리프 정확도",
            f"{overall:.0%} ({sum(1 for r in pool if r.leaf_correct)}/{len(pool)}) "
            "— 0단 차단 건과 셀러가 리프를 고른 건은 분모에서 뺐다",
        )
    )
    by_layer: dict[str, list[Record]] = defaultdict(list)
    for record in pool:
        by_layer[record.name_reveals].append(record)
    rows.append(
        (
            f"[{mode}] 층별",
            " / ".join(
                f"{layer}={sum(1 for r in group if r.leaf_correct)}/{len(group)}"
                f"({sum(1 for r in group if r.leaf_correct) / len(group):.0%})"
                for layer, group in sorted(by_layer.items())
            )
            + " — **이 줄이 입력 방식 결정의 1순위 재료다**",
        )
    )
    rows.append(
        (
            f"[{mode}] 층별 사진 보유",
            " / ".join(
                f"{layer}={sum(1 for r in group if r.image_variant is not None)}/{len(group)}"
                for layer, group in sorted(by_layer.items())
            )
            + " — **이 줄 없이는 위 층별 숫자를 읽을 수 없다.** 사진이 없는 건은 (가)와 "
            "(나)가 완전히 같은 입력을 받으므로 그 층의 delta 는 반복 불일치 잡음이다",
        )
    )
    declines = [r for r in pool if r.leaf_truth is None]
    if declines:
        rows.append(
            (
                f"[{mode}] 정직한 포기",
                f"{sum(1 for r in declines if r.leaf_correct)}/{len(declines)} — "
                "사람도 품목을 알 수 없는 이름에서 **고르지 않는 것이 정답이다**. "
                "추측해서 맞히는 쪽이 유리하게 측정되지 않게 하는 분모다",
            )
        )
    # **분모는 "정답이 있는 케이스" 다. "모델이 옵션을 낸 케이스" 가 아니다.**
    #
    # 앞 판은 ``option_group_hit is not None`` 으로 걸렀다. 그 값은 모델이 옵션을 하나도
    # 내지 않으면 ``None`` 이라서 **분모에서 빠지고 행 자체가 사라졌다.** 전부 실패하면
    # "없음"으로 보이는 데 그치지만 **절반만 실패하면 거짓이 된다** -- 53건 중 10건만
    # 옵션을 내고 그 10건이 맞으면 ``10/10(100%)`` 으로 찍히고 43건이 아무것도 못 냈다는
    # 사실은 어디에도 안 적힌다. 레포 불변식("통과와 미검사를 절대 섞지 않는다")을 측정
    # 쪽에서 그대로 어기는 모양이고, 고친 결함(읽지 못한 리뷰를 "단점 없음"으로)과 같다.
    option_pool = [r for r in pool if r.option_group_scorable]
    if option_pool:
        scored = [r for r in option_pool if r.option_group_hit is not None]
        unattempted = len(option_pool) - len(scored)
        hit = sum(1 for r in scored if r.option_group_hit)
        rows.append(
            (
                f"[{mode}] 옵션 그룹 적중",
                f"{hit}/{len(option_pool)}({hit / len(option_pool):.0%}) — 분모는 **정답이 있는 "
                f"케이스 전수**다. 그 가운데 {unattempted}건은 모델이 옵션을 하나도 내지 않아 "
                "**미측정**이다(맞힌 것도 틀린 것도 아니다). 리프 말고 유일하게 정답이 있는 "
                "내용 지표다",
            )
        )
        by_layer_option: dict[str, list[Record]] = defaultdict(list)
        for record in option_pool:
            by_layer_option[record.name_reveals].append(record)
        rows.append(
            (
                f"[{mode}] 옵션 그룹 적중 층별",
                " / ".join(
                    f"{layer}={sum(1 for r in group if r.option_group_hit)}/{len(group)}"
                    + (
                        f"(미측정 {sum(1 for r in group if r.option_group_hit is None)})"
                        if any(r.option_group_hit is None for r in group)
                        else ""
                    )
                    for layer, group in sorted(by_layer_option.items())
                ),
            )
        )
    return rows


def _summarize(records: list[Record], mode: str) -> list[tuple[str, str]]:
    rows = _accuracy_rows(mode, records)
    called = [r for r in records if r.model_called]
    if not called:
        rows.append((f"[{mode}] 비용", "모델을 0회 불렀다. 0원이다"))
        return rows

    costs = [r.cost_micro_usd for r in called]
    latencies = sorted(r.latency_ms for r in called)
    rows.append(
        (
            f"[{mode}] 비용",
            f"합 {sum(costs)}uUSD ({sum(costs) * KRW_PER_USD / 1_000_000:,.1f}원) / "
            f"건당 중앙값 {int(statistics.median(costs))}uUSD "
            f"({statistics.median(costs) * KRW_PER_USD / 1_000_000:.2f}원)",
        )
    )
    rows.append(
        (
            f"[{mode}] 지연",
            f"p50 {latencies[len(latencies) // 2]}ms / "
            f"p95 {latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]}ms",
        )
    )
    guards: Counter[str] = Counter()
    for record in records:
        guards.update(record.guard_kinds)
    rows.append(
        (
            f"[{mode}] 가드 탈락",
            (", ".join(f"{k}={v}" for k, v in sorted(guards.items())) or "없음")
            + " — **claims 항목이 효능 사전을 새로 만들어야 했는지의 증거다**",
        )
    )
    statuses: Counter[str] = Counter()
    for record in records:
        for field, value in record.field_status.items():
            if field in ("categoryId", "description", "optionGroups"):
                statuses[f"{field}:{value}"] += 1
    rows.append((f"[{mode}] 필드 사유", ", ".join(f"{k}={v}" for k, v in sorted(statuses.items()))))

    with_image = [r for r in records if r.image_variant is not None and r.model_called]
    if with_image:
        wrong = [r for r in with_image if r.image_variant == ImageVariant.WRONG.value]
        rows.append(
            (
                f"[{mode}] 사진 자기보고",
                ", ".join(
                    f"{k}={v}" for k, v in sorted(Counter(r.image_usability for r in with_image).items())
                ),
            )
        )
        wrong_cases = {r.case_id for r in wrong if r.model_called}
        if len(wrong_cases) < WRONG_PHOTO_MIN_CASES:
            # **1건짜리 숫자로 출시 게이트를 통과·탈락시키지 않는다.**
            #
            # 이 줄은 스스로를 go/no-go 게이트로 선언한다("낮으면 정확도와 무관하게 켜지
            # 않는다"). 그런데 분모가 사진 1장이면 값이 0/3·1/3·2/3·3/3 네 눈금만 갖고
            # (``--repeat`` 는 같은 사진을 다시 보내는 것이라 분모를 늘리지 않는다),
            # "상품명과 다른 사진을 믿고 자신 있게 틀린 리프를 확정하는가"라는 이미지
            # 입장의 고유 위험이 **사진 한 장의 운**에 걸린다. 설계가 가장 피한다고 밝힌
            # 실패(조용히 틀린 PINNED)의 측정이 여기인데 통계적으로 아무것도 말하지 못한다.
            rows.append(
                (
                    f"[{mode}] 오사진 안전율",
                    f"**판정 불가** — WRONG 등급 케이스가 {len(wrong_cases)}건이고 "
                    f"최소 {WRONG_PHOTO_MIN_CASES}건이 필요하다. 1건짜리 숫자로 이미지를 "
                    "켜거나 끄지 않는다. 층을 걸쳐 케이스를 더해라",
                )
            )
        else:
            safe = sum(
                1
                for r in wrong
                if r.image_usability == ImageUsability.MISMATCHES_NAME.value
                or Basis.IMAGE.value not in r.basis.values()
            )
            rows.append(
                (
                    f"[{mode}] 오사진 안전율",
                    f"{safe}/{len(wrong)} (케이스 {len(wrong_cases)}건) — **이 숫자가 낮으면 "
                    "정확도와 무관하게 이미지를 켜지 않는다.** 비용 문제가 아니라 거짓 문제다",
                )
            )
        basis_image = sum(1 for r in with_image if Basis.IMAGE.value in r.basis.values())
        rows.append(
            (
                f"[{mode}] 이미지 근거 비율",
                f"{basis_image}/{len(with_image)} — 이미지 변형에서 이 비율이 안 오르면 "
                "**이미지가 일을 안 하고 있다**",
            )
        )

    rows.append(
        (
            f"[{mode}] 캐시 읽기",
            f"{sum(r.cache_read_tokens for r in records)} 토큰 — Haiku 기본에서는 "
            "**0 이 정상**이고 그 사실을 숫자로 남긴다",
        )
    )

    by_case: dict[str, list[Record]] = defaultdict(list)
    for record in records:
        by_case[record.case_id].append(record)
    unstable = [cid for cid, group in by_case.items() if len({r.predicted_leaf for r in group}) > 1]
    rows.append(
        (
            f"[{mode}] 반복 불일치",
            f"{len(unstable)}/{len(by_case)}건 — {', '.join(sorted(unstable)) or '없음'}. "
            "결정성 레버가 API 층에 하나도 없어서 이 숫자 없이 정확도 측정값을 믿을 수 없다",
        )
    )
    return rows


def _compare(baseline: list[Record], current: list[Record], *, label: str) -> list[tuple[str, str]]:
    """**층별 한계 이득.** 평균만 보면 이미지가 값을 내는 구간이 묻힌다.

    ## 분모를 사진 보유 건으로 좁힌 짝 비교가 함께 나간다

    처음 판은 층별 분모를 ``scorable and name_reveals`` 로만 걸렀다. 그런데 사진 픽스처가
    걸린 케이스는 층별로 치우쳐 있어(실측: AMBIGUOUS 1/19, EXPLICIT 2/31, OPAQUE 16/16)
    ``mode b`` 를 돌려도 AMBIGUOUS 층 19건 중 18건이 ``mode a`` 와 **완전히 같은 입력**을
    받는다. 그 층의 delta 는 이미지 효과가 아니라 **반복 불일치 잡음**이다(결정성 레버가
    API 층에 하나도 없다). 하필 그 줄에 "입력 방식 결정의 1순위 재료"가 붙어 있었다.

    그래서 두 줄을 나란히 찍는다. 희석된 전체 비교(운영에서 그 모드를 켠 평균 효과)와
    **사진 보유 건으로 좁힌 짝 비교**(이미지가 실제로 하는 일). 둘이 크게 다르면 읽는
    사람이 희석을 알아야 한다.
    """
    rows: list[tuple[str, str]] = []
    with_image = {r.case_id for r in current if r.image_variant is not None}

    def acc(records: list[Record], layer: str | None, *, only_image: bool) -> tuple[int, int]:
        pool = [
            r
            for r in records
            if r.scorable
            and (layer is None or r.name_reveals == layer)
            and (not only_image or r.case_id in with_image)
        ]
        return sum(1 for r in pool if r.leaf_correct), len(pool)

    def deltas(*, only_image: bool) -> str:
        parts: list[str] = []
        for layer in (None, *(reveal.value for reveal in NameReveals)):
            base_hit, base_total = acc(baseline, layer, only_image=only_image)
            cur_hit, cur_total = acc(current, layer, only_image=only_image)
            if not base_total or not cur_total:
                continue
            delta = cur_hit / cur_total - base_hit / base_total
            parts.append(f"{layer or '전체'}={delta:+.0%}(n={cur_total})")
        return " / ".join(parts)

    rows.append(
        (
            f"한계 이득 {label}",
            deltas(only_image=False) + " — 전체 분모다. **사진이 없는 건이 섞여 있어 희석된 값이고**, "
            "운영에서 그 모드를 켰을 때의 평균 효과에 가깝다",
        )
    )
    if with_image:
        rows.append(
            (
                f"한계 이득(사진 보유만) {label}",
                deltas(only_image=True) + " — **이미지가 실제로 하는 일은 이 줄이다.** 위 줄과 크게 다르면 "
                "그 차이가 희석의 크기다",
            )
        )
    else:
        rows.append(
            (
                f"한계 이득(사진 보유만) {label}",
                "사진 픽스처가 0건이라 짝 비교를 만들 수 없다. **위 줄만으로 이미지를 "
                "켜지 마라** — 그 숫자에 이미지가 들어 있지 않다",
            )
        )

    # **케이스 집합으로 정규화한다.** 레코드 단위로 쌓으면 ``--repeat`` 에서 같은 케이스가
    # 여러 번 들어가 "1건당 비용"의 분모가 반복 수만큼 부풀고, 바로 옆의 "n건"과 서로
    # 곱해지지 않는 두 숫자가 된다.
    baseline_correct = {r.case_id for r in baseline if r.leaf_correct}
    gained = {
        r.case_id for r in current if r.leaf_correct and r.scorable and r.case_id not in baseline_correct
    }
    extra_cost = sum(r.cost_micro_usd for r in current) - sum(r.cost_micro_usd for r in baseline)
    if gained:
        per_case = extra_cost / len(gained)
        noise = sorted(gained - with_image)
        rows.append(
            (
                f"추가로 맞힌 1건당 {label}",
                f"{per_case:.0f}uUSD ({per_case * KRW_PER_USD / 1_000_000:.2f}원) x "
                f"{len(gained)}건 — **이 숫자와 비용 증분율을 나란히 놓고 사람이 정한다.** "
                "추정을 손익분기로 포장하지 않는다"
                + (
                    f" / 그 가운데 {len(noise)}건은 **사진을 받지도 않았다**"
                    f"({', '.join(noise[:4])}) — 잡음 역전이므로 이미지 공로가 아니다"
                    if noise
                    else ""
                ),
            )
        )
    else:
        rows.append((f"추가로 맞힌 건 {label}", f"0건인데 비용은 {extra_cost}uUSD 더 썼다"))
    return rows


def load_records(path: str) -> list[Record]:
    """앞선 모드의 ``raw.jsonl`` 을 읽는다. ``--compare-from`` 이 층별 한계 이득에 쓴다."""
    return [
        Record(**json.loads(line))
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _write_out(
    records: list[Record], rows: list[tuple[str, str]], tag: str, *, out_root: Path | None = None
) -> Path:
    """산출물을 쓴다. **덮지 않고, 빈 raw 도 파일로 남긴다.**

    스탬프가 초 단위이고 ``mkdir(exist_ok=True)`` 라 같은 초에 두 번 돌면 조용히 덮었다.
    그리고 ``if records:`` 가드 때문에 키 없는 경로에서는 ``raw.jsonl`` 이 안 써져, 읽는
    사람이 raw 를 잃었는지 애초에 없었는지 구별할 수 없었다.
    """
    root = out_root if out_root is not None else OUT_ROOT
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = root / f"draft-{tag}-{stamp}"
    serial = 2
    while out.exists():
        out = root / f"draft-{tag}-{stamp}-{serial}"
        serial += 1
    out.mkdir(parents=True)
    (out / "raw.jsonl").write_text("".join(r.as_json() + "\n" for r in records), encoding="utf-8")
    (out / "summary.tsv").write_text("\n".join(f"{k}\t{v}" for k, v in rows) + "\n", encoding="utf-8")
    return out


def _print(rows: list[tuple[str, str]]) -> None:
    print()
    for key, value in rows:
        print(f"{key:26} {value}")


async def _main(args: argparse.Namespace) -> int:
    if _self_check():
        return 1

    settings = Settings()
    rows: list[tuple[str, str]] = [("초안 버전", draft_version_string())]
    rows.extend(_report_lexicon())
    rows.extend(_report_gate_matrix())
    rows.extend(_report_guards())
    rows.extend(_report_envelope())
    rows.extend(_report_images())
    rows.extend(_report_mode_plan(repeat=args.repeat, variant=args.variant, selected=args.mode))
    rows.extend(_report_estimate(args.repeat))
    _print(rows)

    # **선결 조건을 전부 모아서 한 번에 말한다.** 앞 판은 하나만 말했다.
    #
    # 순서가 (1) lexicon-only 면 return (2) 키 없으면 return (3) 그 다음에야 플래그 처리와
    # 픽스처 검사였다. 그래서 둘이 조용히 거짓이었다.
    #
    # - 기본 모드에서 ``--count-tokens`` 를 주면 1번이 먼저 return 해 **플래그가 아예
    #   읽히지 않고** 경고도 없고 종료 코드 0 이었다. 모듈 독스트링은 그 플래그를 "거의
    #   무료" 로 광고하는데 실제로는 키가 필수다. ``--compare-from /nope`` 도 같은 자리에서
    #   조용히 끝났다(존재하지 않는 경로인데 종료 코드 0).
    # - ``--mode b`` 는 픽스처와 키가 **둘 다** 없는데 사람은 "키가 없다" 만 들었다.
    #   픽스처 검사 메시지는 잘 쓰여 있었지만 키가 생기기 전까지 절대 보이지 않는다.
    blockers: list[str] = []
    cases = [case for case in CASES if not args.only or case.id in args.only]

    if args.compare_from is not None and not await asyncio.to_thread(Path(args.compare_from).is_file):
        blockers.append(f"--compare-from 경로가 없다: {args.compare_from}")

    if args.mode in ("b", "gated"):
        missing = [
            case.id
            for case in cases
            if case.image_variant is not None and not _image_path(case, args.variant).is_file()
        ]
        if missing:
            blockers.append(
                f"{args.variant} 등급 이미지 픽스처가 {len(missing)}건 없다 "
                "(harness/make_draft_images.sh 가 만든다. 같은 사진을 복사해 두면 자기검사가 막는다)"
            )

    # **모델을 쓰는 모드를 골랐으면 키가 선결 조건이다.** 기본 모드(``lexicon-only``)는
    # 아무것도 필요 없고 그 호출만 종료 코드 0 이다 -- 0원 기준선이 그 모드의 산출물이다.
    if args.mode != "lexicon-only" and not settings.anthropic_api_key:
        blockers.append(
            "AECHAK_AI_ANTHROPIC_API_KEY 가 없다(ANTHROPIC_API_KEY 가 아니라 이 이름이다 -- "
            "Settings 의 env_prefix 가 AECHAK_AI_). 사람이 SSM 에 등재해야 한다"
        )

    if blockers:
        print()
        requested = " ".join(
            name
            for name, on in (
                (f"--mode {args.mode}", True),
                ("--count-tokens", args.count_tokens),
                ("--count-image-tokens", args.count_image_tokens),
                ("--yes", args.yes),
                (f"--compare-from {args.compare_from}", args.compare_from is not None),
            )
            if on
        )
        print(f"[실패] 요청한 것을 하지 못했다: {requested}. 선결 조건:")
        for blocker in blockers:
            print(f"  - {blocker}")
        print("위 리포트는 키 없이 뽑은 것이고 비용 숫자는 **추정이다**. 종료 코드 2 로 알린다.")
        out = _write_out(_lexicon_only_records(), rows, f"blocked-{args.mode}")
        print(f"산출물 {out}")
        return 2

    if args.mode == "lexicon-only":
        out = _write_out(_lexicon_only_records(), rows, "lexicon-only")
        print(f"\n산출물 {out}")
        return 0

    if not settings.anthropic_api_key:
        print()
        print(
            "AECHAK_AI_ANTHROPIC_API_KEY 가 없어 실측을 건너뛴다. "
            "위 리포트는 키 없이 뽑은 것이고, 비용 숫자는 **추정이다**. "
            "모드 결정 재료 가운데 키 없이 나오는 것은 '모드별 계획' 줄이 전부다."
        )
        out = _write_out(_lexicon_only_records(), rows, f"nokey-{args.mode}")
        print(f"산출물 {out}")
        return 0

    if args.count_tokens:
        return await _count_tokens(settings)
    if args.count_image_tokens:
        return await _count_image_tokens(settings, args.variant)

    if not args.yes:
        print()
        print("실제 호출을 하려면 --yes 를 붙여라. 위 추정은 상한이다.")
        return 0

    from app.llm.client import LlmClient

    llm = LlmClient(api_key=settings.anthropic_api_key, timeout_seconds=settings.draft_timeout_seconds)
    try:
        records = await _measure(
            mode=args.mode,
            model=args.model,
            variant=args.variant,
            repeat=args.repeat,
            llm=llm,
            cases=cases,
        )
    finally:
        await llm.aclose()

    rows.extend(_summarize(records, args.mode))
    rows.extend(_compare(_lexicon_only_records(), records, label="(모델 - 0단)"))
    if args.compare_from:
        baseline = await asyncio.to_thread(load_records, args.compare_from)
        rows.extend(_compare(baseline, records, label=f"({args.mode} - {baseline[0].mode})"))

    out = _write_out(records, rows, f"{args.mode}-{args.model}-{args.variant}")
    _print(rows)
    print()
    print(f"산출물 {out}")
    print(
        "층별 한계 이득을 보려면 다른 모드의 raw.jsonl 을 --compare-from 으로 넘겨라. "
        "**(가)/(나)/gated 결정은 그 표를 사람이 읽고 한다.**"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="harness.run_draft", description="셀러 폼 초안 측정 러너")
    parser.add_argument("--mode", choices=MODES, default="lexicon-only")
    parser.add_argument("--model", choices=("haiku", "sonnet"), default="haiku")
    parser.add_argument("--variant", type=int, choices=VARIANTS, default=896)
    parser.add_argument("--repeat", type=int, default=3, help="같은 입력을 몇 번 돌릴지")
    parser.add_argument("--count-tokens", action="store_true", help="접두부 토큰 수만 실측한다")
    parser.add_argument(
        "--count-image-tokens", action="store_true", help="이미지 토큰 추정을 실측으로 치환한다"
    )
    parser.add_argument("--compare-from", default=None, help="앞선 모드의 raw.jsonl 경로")
    parser.add_argument("--only", nargs="*", default=None, help="케이스 ID 를 골라 돌린다")
    parser.add_argument("--yes", action="store_true", help="실제 호출을 승인한다")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DAILY_DRAFT_CEILING",
    "IMAGE_COST_RATIO",
    "MODES",
    "VARIANTS",
    "Record",
    "draft_cache_prefix_targets",
    "image_reachable_cases",
    "load_records",
    "model_called_cases",
    "model_earns_its_keep",
    "wrong_photo_reachable_cases",
]
