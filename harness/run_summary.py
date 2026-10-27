"""요약 측정 러너.

카프카를 타지 않고 ``SummaryService.run()`` 을 직접 부른다. 재는 것은 메시징 왕복이
아니라 요약 품질과 비용이고, 브로커를 끼우면 실패 지점만 늘어난다.

    python -m harness.run_summary                      키 없이 도는 리포트 전부
    python -m harness.run_summary --count-tokens       접두부 토큰 실측 (거의 무료)
    python -m harness.run_summary --repeat 3 --yes     실측

키는 ``AECHAK_AI_ANTHROPIC_API_KEY`` 다. 없으면 **키 없이 뽑을 수 있는 리포트를 찍고**
종료 코드 0으로 끝난다 -- 그 리포트가 지금 이 범위에서 가장 값비싼 산출물이다(§아래).
``--yes`` 가 없으면 예상 비용만 출력하고 멈춘다.

## 키 없이 뽑는 숫자 — 값비싼 것부터

1. **티어·세그먼트 분포.** 합성 펫 모집단에 대해 T0~T4·TU 비율과 세그먼트별 리뷰 수.
   **``NO_PET_INFO`` 가 압도적이거나 어느 세그먼트도 임계 8건에 못 닿으면 백엔드 펫
   스냅샷 티켓은 쓸 가치가 없고, 그 사실을 티켓을 쓰기 전에 알 수 있다.**
2. **봉투 조립 검증.** 적대적 본문으로 중성화 후 봉투가 닫히지 않는지, 가짜 머리줄
   귀속 인용이 검증에서 떨어지는지.
3. **게이트 통계.** 중복 접기가 몇 건을 줄였고 절단이 몇 건에 걸렸는지.
4. **임계 로직 전수.** 경계 직전/직후, ``consAbsenceReason`` 세 갈래.
5. **극성 경계 민감도.** 설계안의 넓은 경계를 쓰면 단점이 몇 건 사라지는지.
   경계를 되돌리려는 다음 사람이 이 표를 먼저 보게 된다.
6. **비용 추정.** ``--yes`` 없이는 추정만 찍고 멈춘다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic

from app.review_summary import envelope as env
from app.review_summary import selection
from app.review_summary import tally as tally_mod
from app.review_summary.evidence import polarity_conflict_drops, polarity_conflict_noted
from app.review_summary.extract_tool import EXTRACT_TOOL
from app.review_summary.narrate_tool import NARRATE_TOOL
from app.review_summary.service import (
    SummaryService,
    extract_system_blocks,
    narrate_system_blocks,
)
from app.review_summary.trace import SummaryTraceRecorder
from app.schemas.summary import (
    ConsAbsenceReason,
    ExtractStatus,
    Polarity,
    RequestedScope,
    ReviewSummaryRequest,
    SummaryScope,
)
from app.settings import Settings
from harness.run import HAIKU_CACHE_MIN_TOKENS, KRW_PER_USD, SONNET_CACHE_MIN_TOKENS, CachePrefixTarget
from harness.summary_cases import PRODUCTS, VIEWERS, ProductCase, reviewer_population

OUT_ROOT = Path(__file__).parent / "out"

# 건당 추정 microUSD. **추정이고 실측이 아니다.** ``--count-tokens`` 는 접두부 토큰만
# 치환하고 이 표를 치환하지 않는다 -- 출력 토큰 전제까지 재려면 실호출이 필요하다.
#
# 전제: 한국어 1토큰/글자, 추출 접두부 ~1,800 / 서술 접두부 ~1,500, 리뷰 1건 입력 425,
# 추출 출력 리뷰당 200, 서술 입력(집계표) 2,300, 서술 출력 700. Haiku 4.5 $1/$5.
_SUMMARY_ESTIMATE_MICRO_USD = {
    # 배치 10건 한 번.
    "extract_batch_10": 16_050,
    # 리뷰 수에 **무관하다.** 입력이 상한 고정 집계표이기 때문이다.
    "narrate_once": 7_300,
}

# 요약 몫의 일 예산. 설정 정본은 ``Settings.summary_daily_budget_usd`` 다.
_DAILY_BUDGET_USD = Settings().summary_daily_budget_usd


def _daily_narrate_ceiling() -> int:
    """일 예산으로 서술을 몇 번 돌릴 수 있는가. **상수로 적지 않고 비용표에서 파생한다.**

    적어 두면 비용표를 실측으로 갈아 끼울 때 이 숫자가 따라 움직이지 않고, 그래도 어느
    테스트도 깨지지 않는다 -- ``harness/run.py`` 의 ``_breakeven_promotion_rate()`` 가
    상수로 적히지 않은 이유와 같다. 테스트가 이 파생을 고정한다.
    """
    return int(_DAILY_BUDGET_USD * 1_000_000 // _SUMMARY_ESTIMATE_MICRO_USD["narrate_once"])


DAILY_NARRATE_CEILING = _daily_narrate_ceiling()


def _extract_per_review_micro_usd() -> int:
    """추출 건당 비용. 배치 비용에서 파생한다."""
    return _SUMMARY_ESTIMATE_MICRO_USD["extract_batch_10"] // 10


@dataclass(slots=True)
class Record:
    """상품 한 건의 결과 한 줄."""

    repeat: int
    case_id: str
    reviews: int
    prepared: int
    extracted: int
    unextracted: int
    narrated: int
    pros: int
    cons: int
    cons_absence_reason: str | None
    not_enough: bool
    dropped: dict[str, int]
    cost_micro_usd: int
    latency_ms: int
    cache_read_tokens: int
    repeated_aspects: list[str]

    def as_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


# --- 키 없이 도는 자기검사 --------------------------------------------------------------


def _self_check() -> int:
    """케이스의 기대값이 실제 코드 결과와 맞는지 본다. **하나라도 어긋나면 종료 코드 1.**

    틀린 기대값으로 재면 그 숫자는 아무것도 말하지 않는다. ``harness/run.py`` 의
    ``_self_check()`` 와 같은 규약이다.
    """
    settings = Settings()
    problems: list[str] = []

    # (1) 항목 코드 정답이 고정 열거 안에 있는가. 열거를 좁히면 여기서 깨진다.
    from app.schemas.summary import AspectCode

    known = set(AspectCode)
    for case in PRODUCTS:
        for review in case.reviews:
            for code, _polarity in review.expect_aspects:
                if code not in known:
                    problems.append(f"{case.id}/{review.review_id}: 모르는 항목 코드 {code}")

    # (2) 제외·레닥션·절단 기대가 실제 게이트 결과와 맞는가.
    for case in PRODUCTS:
        batch = env.prepare(case.inputs(), content_chars=settings.summary_content_chars)
        excluded = {item.review_id: item.reason for item in batch.excluded}
        prepared = {item.review_id: item for item in batch.prepared}
        for review in case.reviews:
            actual = excluded.get(review.review_id)
            if actual is not review.expect_excluded:
                problems.append(
                    f"{case.id}/{review.review_id}: 제외 기대 {review.expect_excluded} / 실제 {actual}"
                )
            item = prepared.get(review.review_id)
            if item is None:
                continue
            if item.pii_redacted != review.expect_redacted:
                problems.append(
                    f"{case.id}/{review.review_id}: 레닥션 기대 {review.expect_redacted} "
                    f"/ 실제 {item.pii_redacted}"
                )
            if item.truncated != review.expect_truncated:
                problems.append(
                    f"{case.id}/{review.review_id}: 절단 기대 {review.expect_truncated} "
                    f"/ 실제 {item.truncated}"
                )

        # (3) 봉투에 중성화되지 않은 토큰이 남아 있는가. **적대적 본문이 있는 이유다.**
        text = env.render_envelope(list(case.category_path), batch.prepared)
        bodies = text.split("<본문>")
        for index, chunk in enumerate(bodies[1:], start=1):
            body = chunk.split("</본문>")[0]
            if env.envelope_escaped(body):
                problems.append(f"{case.id}: {index}번째 본문에 중성화되지 않은 봉투 토큰이 남았다")

        # (4) 산문 생성 가능 여부 기대가 임계와 맞는가.
        enough = case.total_visible() >= settings.summary_min_reviews
        if enough != case.expect_narrated:
            problems.append(
                f"{case.id}: 산문 기대 {case.expect_narrated} / 실제 분모 {case.total_visible()}"
                f" (최소 {settings.summary_min_reviews})"
            )

        # (5) 정답 라벨만으로 집계했을 때 반복 항목 기대가 맞는가.
        #
        # **이것이 임계 로직의 전수 검사다.** 정답표에서 집계를 만들어 코드의 임계를
        # 그대로 태우므로, 임계 네 값을 바꾸면 여기서 깨진다.
        repeated = _repeated_from_labels(case, settings)
        expected = {(code, polarity) for code, polarity in case.expect_repeated}
        if repeated != expected:
            problems.append(
                f"{case.id}: 반복 항목 기대 {sorted(f'{c.value}/{p.value}' for c, p in expected)} "
                f"/ 실제 {sorted(f'{c.value}/{p.value}' for c, p in repeated)}"
            )

    # (6) 골든 벡터 파일이 실제 티어 함수와 같은가.
    vectors_path = Path(__file__).parent.parent / "tests" / "contract" / "review-selection-vectors.json"
    if vectors_path.is_file():
        stored = json.loads(vectors_path.read_text(encoding="utf-8"))
        if stored != selection.golden_vectors():
            problems.append(
                "골든 벡터 파일이 selection.py 와 어긋난다. "
                "백엔드 미러가 그 파일을 읽으므로 양 레포가 다른 답을 낸다"
            )
    else:
        problems.append(f"골든 벡터 파일이 없다: {vectors_path}")

    if problems:
        print("[치명] 케이스 기대값이 코드와 어긋난다. 측정 전에 멈춘다.")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(
        f"자기검사 통과. 상품 {len(PRODUCTS)}건 / 리뷰 "
        f"{sum(len(c.reviews) for c in PRODUCTS)}건 / 임계 "
        f"{settings.summary_min_reviews}·{settings.summary_min_segment_reviews}·"
        f"{settings.summary_min_aspect_mentions}·{settings.summary_min_aspect_share:.0%}"
    )
    return 0


def _repeated_from_labels(
    case: ProductCase,
    settings: Settings,
    *,
    wide_polarity_band: bool = False,
) -> set[tuple[Any, Polarity]]:
    """정답 라벨만으로 집계해 반복 항목을 낸다. 모델을 부르지 않는다.

    ``wide_polarity_band`` 는 설계안의 원래 경계(1~2점 PRO / 4~5점 CON 을 전부 버림)를
    흉내 낸다. 두 경계의 차이를 숫자로 보여 주는 데만 쓴다.
    """
    from app.schemas.summary import AspectTallyRow, QuoteSample

    tally = tally_mod.Tally()
    excluded = {review.review_id for review in case.reviews if review.expect_excluded is not None}
    for review in case.reviews:
        if review.review_id in excluded:
            continue
        for code, polarity in dict.fromkeys(review.expect_aspects):
            if wide_polarity_band:
                conflict = (review.rating in (1, 2) and polarity is Polarity.PRO) or (
                    review.rating in (4, 5) and polarity is Polarity.CON
                )
            else:
                conflict = polarity_conflict_drops(review.rating, polarity)
            if conflict:
                continue
            tally_mod.add(
                tally,
                aspect_code=code,
                polarity=polarity,
                label=code.value,
                quote=QuoteSample(reviewId=review.review_id, text=code.value),
            )
    _ = AspectTallyRow  # 형 참조만. 집계는 위에서 끝났다
    classified = tally_mod.classify(
        tally,
        basis_review_count=case.total_visible(),
        thresholds=_thresholds(settings),
    )
    return {(item.row.aspectCode, item.row.polarity) for item in [*classified.pros, *classified.cons]}


def _thresholds(settings: Settings) -> tally_mod.Thresholds:
    return tally_mod.Thresholds(
        min_mentions=settings.summary_min_aspect_mentions,
        min_share=settings.summary_min_aspect_share,
        min_reviews=settings.summary_min_reviews,
        min_segment_reviews=settings.summary_min_segment_reviews,
    )


# --- 키 없이 뽑는 리포트 ----------------------------------------------------------------


def _report_tiers() -> list[tuple[str, str]]:
    """티어·세그먼트 분포. **이 범위에서 키 없이 뽑을 수 있는 가장 값비싼 숫자다.**

    백엔드 펫 스냅샷 티켓(리뷰에 작성 시점 펫을 기록하는 마이그레이션)을 쓸 가치가
    있는지가 여기서 먼저 드러난다. 합성 모집단이라 실제 분포는 아니지만, **세그먼트
    임계가 달성 가능한 구조인지**는 이것으로 판단할 수 있다.
    """
    rows: list[tuple[str, str]] = []
    reviewers = reviewer_population()
    today = selection.GOLDEN_TODAY

    no_info = sum(1 for _review_id, facts in reviewers if facts is None)
    rows.append(
        (
            "펫 정보 없는 리뷰",
            f"{no_info}/{len(reviewers)}건 ({no_info / len(reviewers):.0%}) — "
            "지금 백엔드 리뷰에는 펫 참조가 **아예 없다**. 이 비중은 합성 모집단의 것이고, "
            "실제로는 스냅샷 티켓이 들어와도 기존 리뷰 전량이 영구히 NO_PET_INFO 다",
        )
    )

    for name, viewer in VIEWERS:
        tally: Counter[str] = Counter()
        for _review_id, reviewer in reviewers:
            tally[selection.match_tier(viewer, reviewer, today=today).value] += 1
        ordered = sorted(tally.items(), key=lambda kv: selection.tier_rank(selection.MatchTier(kv[0])))
        rows.append((f"티어 분포 [{name}]", ", ".join(f"{k}={v}" for k, v in ordered)))

    segments: Counter[str] = Counter()
    unsegmented = 0
    for _review_id, reviewer in reviewers:
        key = selection.segment_key(reviewer)
        if key is None:
            unsegmented += 1
            continue
        segments[key] += 1
    minimum = Settings().summary_min_segment_reviews
    reachable = [key for key, count in segments.items() if count >= minimum]
    rows.append(
        (
            "세그먼트 분포",
            (
                ", ".join(f"{k}={v}" for k, v in sorted(segments.items()))
                + f" / 세그먼트 없음 {unsegmented}건"
            ),
        )
    )
    rows.append(
        (
            "세그먼트 임계 달성",
            f"{len(reachable)}/{len(selection.all_segment_keys())}개가 최소 {minimum}건을 넘겼다"
            + (
                " — **하나도 못 넘겼다. 이 모집단에서는 세그먼트 요약이 성립하지 않는다**"
                if not reachable
                else f" ({', '.join(sorted(reachable))})"
            ),
        )
    )
    return rows


def _report_gate() -> list[tuple[str, str]]:
    """게이트 통계. 중복 접기·절단·주입 제외가 실제로 몇 건을 건드리는가."""
    settings = Settings()
    rows: list[tuple[str, str]] = []
    totals: Counter[str] = Counter()
    for case in PRODUCTS:
        batch = env.prepare(case.inputs(), content_chars=settings.summary_content_chars)
        totals["reviews"] += len(case.reviews)
        totals["prepared"] += len(batch.prepared)
        totals["injection"] += batch.injection_excluded
        totals["duplicate"] += batch.duplicate_folded
        totals["truncated"] += batch.truncated
        totals["redacted"] += batch.pii_redacted
    rows.append(
        (
            "입력 게이트",
            f"리뷰 {totals['reviews']}건 중 추출 입력 {totals['prepared']}건 "
            f"(주입 제외 {totals['injection']} / 중복 접기 {totals['duplicate']})",
        )
    )
    rows.append(
        (
            "절단과 레닥션",
            f"{settings.summary_content_chars}자 절단 {totals['truncated']}건 / "
            f"레닥션 {totals['redacted']}건 — 절단은 손실이고 플래그로 결과에 나간다",
        )
    )
    return rows


def _report_polarity_band() -> list[tuple[str, str]]:
    """극성 경계 민감도. **설계안의 넓은 경계가 단점을 몇 개 지우는가.**

    이 표가 있는 이유: 경계를 설계 문서대로 되돌리려는 다음 사람이 숫자를 먼저 보게
    하려는 것이다. 코드 주석만으로는 "4점 리뷰의 단점이 흔하다"가 추측처럼 읽힌다.
    """
    settings = Settings()
    rows: list[tuple[str, str]] = []
    lost: list[str] = []
    for case in PRODUCTS:
        narrow = _repeated_from_labels(case, settings)
        wide = _repeated_from_labels(case, settings, wide_polarity_band=True)
        gone = narrow - wide
        if gone:
            lost.extend(f"{case.id}:{code.value}/{polarity.value}" for code, polarity in sorted(gone))
    rows.append(
        (
            "극성 경계 손실",
            (
                f"넓은 경계(설계안)로 두면 반복 항목 {len(lost)}개가 사라진다: {', '.join(lost)}"
                if lost
                else "두 경계가 같은 반복 항목을 낸다 — 이 케이스 세트가 경계를 구분하지 못한다"
            ),
        )
    )

    noted = sum(
        1
        for case in PRODUCTS
        for review in case.reviews
        for _code, polarity in review.expect_aspects
        if polarity_conflict_noted(review.rating, polarity)
    )
    dropped = sum(
        1
        for case in PRODUCTS
        for review in case.reviews
        for _code, polarity in review.expect_aspects
        if polarity_conflict_drops(review.rating, polarity)
    )
    rows.append(
        (
            "극성 충돌",
            f"버린 것(1점 PRO) {dropped}건 / 세기만 한 것(5점 CON) {noted}건 — "
            "뒤집힘 탐지는 둘째 숫자가 하고, 기준선이 0 이 아니라서 라벨과 함께 봐야 뜻이 생긴다",
        )
    )
    return rows


def _report_thresholds() -> list[tuple[str, str]]:
    """임계 세 갈래가 각각 나오는 케이스가 있는가."""
    settings = Settings()
    seen: dict[str, list[str]] = defaultdict(list)
    for case in PRODUCTS:
        from app.schemas.summary import QuoteSample

        tally = tally_mod.Tally()
        excluded = {r.review_id for r in case.reviews if r.expect_excluded is not None}
        for review in case.reviews:
            if review.review_id in excluded:
                continue
            for code, polarity in dict.fromkeys(review.expect_aspects):
                if polarity_conflict_drops(review.rating, polarity):
                    continue
                tally_mod.add(
                    tally,
                    aspect_code=code,
                    polarity=polarity,
                    label=code.value,
                    quote=QuoteSample(reviewId=review.review_id, text=code.value),
                )
        classified = tally_mod.classify(
            tally,
            basis_review_count=case.total_visible(),
            thresholds=_thresholds(settings),
        )
        key = classified.cons_absence_reason.value if classified.cons_absence_reason else "CONS_PRESENT"
        seen[key].append(case.id)

    rows = [
        (
            "단점 부재 사유",
            ", ".join(f"{k}={','.join(v)}" for k, v in sorted(seen.items())),
        )
    ]
    missing = [r.value for r in ConsAbsenceReason if r.value not in seen]
    if missing:
        rows.append(
            (
                "덮지 못한 사유",
                f"{', '.join(missing)} — 케이스 세트에 그 갈래가 없다. "
                "'단점 없음' 한 가지로 접히지 않는 것을 보여 주려면 케이스를 더해야 한다",
            )
        )
    return rows


def _report_estimate(repeat: int) -> list[tuple[str, str]]:
    """비용 추정. **전부 추정이고 실측이 아니다.**"""
    settings = Settings()
    per_review = _extract_per_review_micro_usd()
    narrate = _SUMMARY_ESTIMATE_MICRO_USD["narrate_once"]
    reviews = sum(len(case.reviews) for case in PRODUCTS)
    narrations = sum(1 for case in PRODUCTS if case.expect_narrated)
    total = (reviews * per_review + narrations * narrate) * repeat
    return [
        (
            "추정 건당",
            f"추출 {per_review}uUSD({per_review * KRW_PER_USD / 1_000_000:.2f}원)/리뷰 · "
            f"서술 {narrate}uUSD({narrate * KRW_PER_USD / 1_000_000:.2f}원)/회 "
            "— **서술은 리뷰 수에 무관하다**",
        ),
        (
            "추정 전체",
            f"리뷰 {reviews}건 + 서술 {narrations}회 x 반복 {repeat} = "
            f"{total / 1_000_000:.3f} USD ({total * KRW_PER_USD / 1_000_000:,.0f}원)",
        ),
        (
            "일 예산 상한",
            f"요약 몫 ${_DAILY_BUDGET_USD}/일이면 서술 {DAILY_NARRATE_CEILING}회/일. "
            "**상수로 적지 않고 비용표에서 파생한다** — 표를 실측으로 갈면 따라 움직인다",
        ),
        (
            "경보 공유",
            f"daily_cost_alert_usd=${settings.daily_cost_alert_usd}를 검수와 나눠 쓰고, "
            "경보는 어느 기능이 태웠는지 말해 주지 않는다",
        ),
    ]


# --- 토큰 실측 -------------------------------------------------------------------------


def summary_cache_prefix_targets(settings: Settings) -> list[CachePrefixTarget]:
    """요약 접두부. 기본 모델이 Haiku 라 ``required=False`` 다.

    하한 미달이 **실패가 아니라 근거**다 -- 그것이 접두부를 짧게 쓰고 패딩하지 않기로
    한 이유이고, 하네스가 그 사실을 숫자로 남긴다. Sonnet 으로 뒤집으면
    ``required=True`` 로 바꿔 ``--count-tokens`` 가 종료 코드로 알리게 한다.
    """
    haiku = settings.summary_extract_model.startswith("claude-haiku")
    return [
        CachePrefixTarget(
            name="summary_extract_prefix",
            model=settings.summary_extract_model,
            system=extract_system_blocks(),
            tools=[EXTRACT_TOOL],
            min_tokens=HAIKU_CACHE_MIN_TOKENS if haiku else SONNET_CACHE_MIN_TOKENS,
            required=not haiku,
        ),
        CachePrefixTarget(
            name="summary_narrate_prefix",
            model=settings.summary_narrate_model,
            system=narrate_system_blocks(),
            tools=[NARRATE_TOOL],
            min_tokens=(
                HAIKU_CACHE_MIN_TOKENS
                if settings.summary_narrate_model.startswith("claude-haiku")
                else SONNET_CACHE_MIN_TOKENS
            ),
            required=not settings.summary_narrate_model.startswith("claude-haiku"),
        ),
    ]


async def _count_tokens(settings: Settings) -> int:
    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key, timeout=settings.request_timeout_seconds
    )
    failed = False
    try:
        print()
        for target in summary_cache_prefix_targets(settings):
            result = await client.messages.count_tokens(
                model=target.model,
                system=target.system,
                tools=target.tools,
                messages=[{"role": "user", "content": [{"type": "text", "text": "x"}]}],
            )
            tokens = result.input_tokens
            margin = (tokens - target.min_tokens) / target.min_tokens
            print(f"{target.name:28} {tokens:>6} 토큰  (최소 {target.min_tokens}, 여유 {margin:+.0%})")
            if tokens < target.min_tokens:
                if target.required:
                    print(
                        f"[치명] {target.name}이 하한에 못 미친다. 캐시가 에러 없이 죽어 "
                        "비용이 조용히 올라간다."
                    )
                    failed = True
                else:
                    print(
                        f"  {target.name}은 하한 미달이다. cache_control 을 붙여도 무시된다 "
                        "— 접두부를 짧게 쓰기로 한 근거다."
                    )
    finally:
        await client.close()
    print()
    print(
        "이 명령은 접두부 토큰만 치환한다. 출력 토큰 전제는 실호출이 필요하므로 "
        f"{_SUMMARY_ESTIMATE_MICRO_USD} 는 **여전히 추정이다.**"
    )
    print(
        "한국어 글자당 토큰 전제가 레포 안에서 어긋나 있다(1.0 대 0.46). "
        "위 측정값으로 그 모순을 먼저 닫을 것."
    )
    return 1 if failed else 0


# --- 실측 ----------------------------------------------------------------------------


def _repeated_labels(summary: Any) -> list[str]:
    """요약에 실린 항목을 극성과 함께 적는다. 반복 간 불일치 비교의 키다."""
    if summary is None:
        return []
    return sorted(
        [f"{item.aspectCode.value}/PRO" for item in summary.pros]
        + [f"{item.aspectCode.value}/CON" for item in summary.cons]
    )


async def _measure(settings: Settings, llm: Any, repeat: int, cases: list[ProductCase]) -> list[Record]:
    records: list[Record] = []
    for round_no in range(1, repeat + 1):
        for case in cases:
            recorder = SummaryTraceRecorder()
            service = SummaryService(llm=llm, settings=settings, recorder=recorder)
            request = ReviewSummaryRequest(
                requestId=f"harness-{case.id}-{round_no}",
                productId=abs(hash(case.id)) % 100_000,
                categoryPath=list(case.category_path),
                totalVisibleReviews=case.total_visible(),
                newReviews=case.inputs(),
                requestedScopes=[
                    RequestedScope(
                        scope=SummaryScope.PRODUCT,
                        basisReviewCount=case.total_visible(),
                        unextractedReviewCount=0,
                        tally=[],
                    )
                ],
            )
            message = await service.run(request)
            summary = message.summaries[0] if message.summaries else None
            records.append(
                Record(
                    repeat=round_no,
                    case_id=case.id,
                    reviews=len(case.reviews),
                    prepared=sum(1 for row in message.extracted if row.excludeReason is None),
                    extracted=sum(1 for row in message.extracted if row.extracted is ExtractStatus.EXTRACTED),
                    unextracted=sum(
                        1 for row in message.extracted if row.extracted is ExtractStatus.UNEXTRACTED
                    ),
                    narrated=int(bool(summary and (summary.pros or summary.cons))),
                    pros=len(summary.pros) if summary else 0,
                    cons=len(summary.cons) if summary else 0,
                    cons_absence_reason=(
                        summary.consAbsenceReason.value if summary and summary.consAbsenceReason else None
                    ),
                    not_enough=bool(summary and summary.notEnoughReason),
                    dropped=json.loads(message.dropped.model_dump_json()),
                    cost_micro_usd=sum(call.costMicroUsd for call in message.calls),
                    latency_ms=sum(call.latencyMs for call in message.calls),
                    cache_read_tokens=sum(call.cacheReadInputTokens for call in message.calls),
                    repeated_aspects=_repeated_labels(summary),
                )
            )
    return records


def _summarize_measured(records: list[Record]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    by_case: dict[str, list[Record]] = defaultdict(list)
    for record in records:
        by_case[record.case_id].append(record)

    costs = [record.cost_micro_usd for record in records]
    rows.append(
        (
            "실측 비용",
            f"합 {sum(costs)}uUSD ({sum(costs) * KRW_PER_USD / 1_000_000:,.1f}원) / "
            f"상품당 중앙값 {int(statistics.median(costs)) if costs else 0}uUSD",
        )
    )
    rows.append(
        (
            "서술 비용 평탄성",
            " / ".join(
                f"{case_id}(리뷰{group[0].reviews}건)="
                f"{int(statistics.median(r.cost_micro_usd for r in group))}uUSD"
                for case_id, group in sorted(by_case.items())
            )
            + " — **이 설계의 핵심 주장**이 리뷰 수와 서술 비용의 무관함이다",
        )
    )
    totals: Counter[str] = Counter()
    for record in records:
        for key, value in record.dropped.items():
            totals[key] += value
    rows.append(("버린 것", ", ".join(f"{k}={v}" for k, v in sorted(totals.items()) if v)))
    rows.append(
        (
            "캐시 읽기",
            f"{sum(r.cache_read_tokens for r in records)} 토큰 — "
            "Haiku 기본에서는 **0 이 정상**이고 그 사실을 숫자로 남긴다",
        )
    )

    unstable = [
        case_id for case_id, group in by_case.items() if len({tuple(r.repeated_aspects) for r in group}) > 1
    ]
    rows.append(
        (
            "반복 간 불일치",
            f"{len(unstable)}/{len(by_case)}건 — {', '.join(sorted(unstable)) or '없음'}. "
            "결정성 레버가 API 층에 하나도 없어서 이 숫자 없이 '요약이 안정적이다'라고 쓸 수 없다",
        )
    )
    return rows


def _write_out(records: list[Record], rows: list[tuple[str, str]]) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_ROOT / f"summary-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    if records:
        (out / "raw.jsonl").write_text("\n".join(r.as_json() for r in records) + "\n")
    (out / "summary.tsv").write_text("\n".join(f"{k}\t{v}" for k, v in rows) + "\n")
    return out


def _print(rows: list[tuple[str, str]]) -> None:
    print()
    for key, value in rows:
        print(f"{key:20} {value}")


async def _main(args: argparse.Namespace) -> int:
    if _self_check():
        return 1

    settings = Settings()
    rows: list[tuple[str, str]] = []
    rows.extend(_report_tiers())
    rows.extend(_report_gate())
    rows.extend(_report_polarity_band())
    rows.extend(_report_thresholds())
    rows.extend(_report_estimate(args.repeat))
    _print(rows)

    if not settings.anthropic_api_key:
        print()
        print(
            "AECHAK_AI_ANTHROPIC_API_KEY 가 없어 실측을 건너뛴다. "
            "ANTHROPIC_API_KEY 가 아니라 이 이름이다(Settings 의 env_prefix 가 AECHAK_AI_). "
            "위 리포트는 키 없이 뽑은 것이고, 비용 숫자는 **추정이다**."
        )
        out = _write_out([], rows)
        print(f"산출물 {out}")
        return 0

    if args.count_tokens:
        return await _count_tokens(settings)

    cases = [case for case in PRODUCTS if not args.only or case.id in args.only]
    if not args.yes:
        print()
        print("실제 호출을 하려면 --yes 를 붙여라. 위 추정은 상한이다.")
        return 0

    from app.llm.client import LlmClient

    llm = LlmClient(api_key=settings.anthropic_api_key, timeout_seconds=settings.summary_timeout_seconds)
    try:
        records = await _measure(settings, llm, args.repeat, cases)
    finally:
        await llm.aclose()

    rows.extend(_summarize_measured(records))
    out = _write_out(records, rows)
    _print(rows)
    print()
    print(f"산출물 {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="harness.run_summary", description="리뷰 요약 측정 러너")
    parser.add_argument("--count-tokens", action="store_true", help="접두부 토큰 수만 실측한다")
    parser.add_argument("--repeat", type=int, default=3, help="같은 입력을 몇 번 돌릴지")
    parser.add_argument("--yes", action="store_true", help="실제 호출을 승인한다")
    parser.add_argument("--only", nargs="*", default=None, help="상품 케이스 ID 를 골라 돌린다")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "DAILY_NARRATE_CEILING",
    "Record",
    "summary_cache_prefix_targets",
]
