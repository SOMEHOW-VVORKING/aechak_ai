"""측정 러너.

카프카를 타지 않고 ``InspectionPipeline.run()``을 직접 부른다. 재는 것은 메시징 왕복이 아니라
판정 품질과 비용이고, 브로커를 끼우면 실패 지점만 늘어난다. 왕복은 ``tests/test_roundtrip_live.py``가 본다.

이 기능의 가치는 전부 **승급률** 하나에 걸려 있다. 승급률이 손익분기를 넘으면 선별은 비용을
늘리는 기능이므로 끈다. 그래서 승급률이 1순위 지표다.

    python -m harness.run --count-tokens
    python -m harness.run --mode baseline --repeat 3 --yes
    python -m harness.run --mode full --repeat 3 --yes --baseline-from harness/out/<타임스탬프>

키는 ``AECHAK_AI_ANTHROPIC_API_KEY``다. 없으면 이유를 적고 종료 코드 0으로 건너뛴다.
``--yes``가 없으면 예상 비용만 출력하고 멈춘다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic

from app.inspection import rules, triage
from app.inspection.perspectives import findings_tool, specs_for
from app.inspection.pipeline import InspectionPipeline
from app.inspection.policy import POLICY_PREFIX
from app.llm.client import LlmClient
from app.schemas.inspection import CallOutcome, Perspective, PerspectiveOutcome, TargetType
from app.settings import Settings
from harness.cases import CASES, OVERBLOCK_SUSPECT, VIOLATING, Case, promotable

OUT_ROOT = Path(__file__).parent / "out"

# 원화 환산. 비용표와 같은 전제를 쓴다.
KRW_PER_USD = 1_400

# 채택안의 손익분기 승급률. 측정값이 이걸 넘으면 선별을 켜지 않는다.
BREAKEVEN_PROMOTION_RATE = 0.620

# 캐시 최소 접두부. Sonnet은 하한이고 Haiku는 넘지 못하는 벽이다.
SONNET_CACHE_MIN_TOKENS = 1_024
HAIKU_CACHE_MIN_TOKENS = 4_096

# 예상 비용 계산에 쓰는 건당 microUSD. 토큰 전제는 --count-tokens가 실측으로 치환한다.
_ESTIMATE_MICRO_USD = {"triage": 1_400, "sonnet_pair": 3_680, "critic_and_notice": 9_000}

MODES = ("baseline", "triage-only", "full")


@dataclass(slots=True)
class Record:
    """케이스 한 건의 결과 한 줄."""

    mode: str
    repeat: int
    case_id: str
    label: str
    target: str
    text_len: int
    expect_rule_block: bool
    verdict: str
    triage_outcome: str
    perspectives: dict[str, str]
    cost_micro_usd: int
    triage_cost_micro_usd: int
    latency_ms: int
    haiku_cache_read: int
    triage_call_outcome: str | None

    def as_json(self) -> str:
        # slots 데이터클래스는 __dict__가 없다. asdict를 써야 한다.
        return json.dumps(asdict(self), ensure_ascii=False)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, round((len(ordered) - 1) * pct))
    return ordered[index]


def _triage_outcome(verdict: Any) -> tuple[str, str | None]:
    """판정 메시지만 보고 선별 결과를 되살린다.

    선별 관점 행의 **유무**로 가르지 않는다. 승급 경로에도 행이 남기 때문이다
    (``UNCHECKED`` + ``PROMOTED``). 유무로 가르면 승급 전건이 CLEAR로 분류되어 1순위 지표가
    0%로 뒤집힌다. 행의 outcome이 판단 근거다.

    호출을 모델 ID가 아니라 관점으로 고른다. 원장의 전용 관점이 선별을 식별하는 신호이고,
    모델 ID는 설정으로 바뀐다.
    """
    calls = [c for c in verdict.calls if c.perspective is Perspective.TRIAGE]
    if not calls:
        return "OFF", None
    outcome = calls[0].outcome.value
    if calls[0].outcome is not CallOutcome.SUCCESS:
        # 끊긴 응답(TRUNCATED)도 여기로 떨어진다. 모델이 "더 보라"고 판단한 것과
        # 아무 답도 못 한 것을 분자에서 뭉개면 승급률을 믿을 수 없다.
        return "FAILED", outcome
    row = next((p for p in verdict.perspectives if p.perspective is Perspective.TRIAGE), None)
    if row is not None and row.outcome is PerspectiveOutcome.CLEAR:
        return "CLEAR", outcome
    return "PROMOTED", outcome


async def _run_pipeline_mode(
    mode: str, settings: Settings, llm: LlmClient, repeat: int, cases: list[Case]
) -> list[Record]:
    tuned = settings.model_copy(update={"enable_triage": mode == "full"})
    pipeline = InspectionPipeline(llm=llm, settings=tuned)
    records: list[Record] = []

    for round_no in range(1, repeat + 1):
        for index, case in enumerate(cases, start=1):
            request = case.to_request(round_no * 10_000 + index)
            started = time.monotonic()
            verdict = await pipeline.run(request)
            elapsed = int((time.monotonic() - started) * 1000)

            triage_outcome, call_outcome = _triage_outcome(verdict)
            triage_calls = [c for c in verdict.calls if c.perspective is Perspective.TRIAGE]
            records.append(
                Record(
                    mode=mode,
                    repeat=round_no,
                    case_id=case.id,
                    label=case.label,
                    target=case.target.value,
                    text_len=len(case.text),
                    expect_rule_block=case.expect_rule_block,
                    verdict=verdict.verdict.value,
                    triage_outcome=triage_outcome,
                    perspectives={p.perspective.value: p.outcome.value for p in verdict.perspectives},
                    cost_micro_usd=sum(c.costMicroUsd for c in verdict.calls),
                    triage_cost_micro_usd=sum(c.costMicroUsd for c in triage_calls),
                    latency_ms=elapsed,
                    haiku_cache_read=sum(c.cacheReadInputTokens for c in triage_calls),
                    triage_call_outcome=call_outcome,
                )
            )
            print(f"  {mode} r{round_no} {case.id:24} {verdict.verdict.value:7} {triage_outcome}")
    return records


async def _run_triage_only(
    settings: Settings, llm: LlmClient, repeat: int, cases: list[Case]
) -> list[Record]:
    """선별 판정만 재고 Sonnet을 부르지 않는다. 파이프라인에는 이 모드가 없어 직접 조립한다."""
    records: list[Record] = []
    for round_no in range(1, repeat + 1):
        for index, case in enumerate(cases, start=1):
            request = case.to_request(round_no * 10_000 + index)
            rule_result = rules.screen(request.inspect_text())
            if rule_result.blocking:
                continue

            started = time.monotonic()
            result = await llm.call_tool(
                model=settings.triage_model,
                effort=settings.triage_effort,
                max_tokens=settings.max_tokens_triage,
                system_blocks=triage.triage_system_blocks(),
                user_content=triage.triage_user_content(request, rule_result.redacted),
                tool=triage.TRIAGE_TOOL,
                request_id=f"harness-{case.id}-{round_no}",
                timeout_seconds=settings.triage_timeout_seconds,
            )
            elapsed = int((time.monotonic() - started) * 1000)
            decision = triage.decide(result)
            if not result.ok:
                outcome = "FAILED"
            else:
                outcome = "CLEAR" if decision.skip else "PROMOTED"

            records.append(
                Record(
                    mode="triage-only",
                    repeat=round_no,
                    case_id=case.id,
                    label=case.label,
                    target=case.target.value,
                    text_len=len(case.text),
                    expect_rule_block=case.expect_rule_block,
                    verdict="N/A",
                    triage_outcome=outcome,
                    perspectives={},
                    cost_micro_usd=result.cost_micro_usd,
                    triage_cost_micro_usd=result.cost_micro_usd,
                    latency_ms=elapsed,
                    haiku_cache_read=result.cache_read_input_tokens,
                    triage_call_outcome=result.outcome.value,
                )
            )
            print(f"  triage-only r{round_no} {case.id:24} {outcome} ({decision.decision})")
    return records


@dataclass(frozen=True, slots=True)
class CachePrefixTarget:
    """토큰을 재야 하는 캐시 접두부 하나.

    접두부는 ``POLICY_PREFIX`` 문자열이 아니다. 캐시 렌더 순서가
    ``tools -> system -> messages``이고 ``cache_control``이 마지막 system 블록에 있어서
    **tools가 함께 캐시된다.** ``findings_tool``은 관점마다 enum 목록이 달라 tools가 다르고,
    따라서 캐시 엔트리도 관점별로 갈라진다. system만 세면 둘 중 어느 것도 재지 않는 셈이다.
    """

    name: str
    model: str
    system: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    min_tokens: int
    required: bool


def cache_prefix_targets(settings: Settings) -> list[CachePrefixTarget]:
    """실제로 캐시되는 접두부들을 조립한다. 키 없이 도는 부분이라 테스트가 소유한다."""
    targets = [
        CachePrefixTarget(
            name="triage_prompt_on_haiku",
            model=settings.triage_model,
            system=triage.triage_system_blocks(),
            tools=[triage.TRIAGE_TOOL],
            min_tokens=HAIKU_CACHE_MIN_TOKENS,
            # 선별은 캐시를 안 쓰는 것이 채택안이다. 하한 미달이 실패가 아니라 근거다.
            required=False,
        )
    ]
    for spec in specs_for(TargetType.PRODUCT, fanout=settings.perspective_fanout):
        targets.append(
            CachePrefixTarget(
                name=f"perspective_{spec.perspective.value.lower()}_on_sonnet",
                model=settings.screening_model,
                system=[{"type": "text", "text": POLICY_PREFIX}],
                tools=[findings_tool(spec)],
                min_tokens=SONNET_CACHE_MIN_TOKENS,
                required=True,
            )
        )
    return targets


async def _count_tokens(settings: Settings) -> list[tuple[CachePrefixTarget, int]]:
    """접두부들의 실제 토큰 수를 잰다. 거의 무료라 맨 먼저 돈다."""
    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key, timeout=settings.request_timeout_seconds
    )
    counted: list[tuple[CachePrefixTarget, int]] = []
    try:
        for target in cache_prefix_targets(settings):
            result = await client.messages.count_tokens(
                model=target.model,
                system=target.system,
                tools=target.tools,
                messages=[{"role": "user", "content": [{"type": "text", "text": "x"}]}],
            )
            counted.append((target, result.input_tokens))
    finally:
        await client.close()
    return counted


def _report_tokens(counted: list[tuple[CachePrefixTarget, int]]) -> int:
    """하한을 단언한다. 표의 토큰 전제를 **치환하지는 않는다.**

    치환하려면 출력 토큰 전제까지 같이 재야 하고 그건 실호출이다. 여기서는 측정값과 가정값을
    나란히 찍어 사람이 표를 고칠지 정한다 — 가정값 위에 선 숫자를 측정값처럼 쓰지 않는 것이
    이 출력의 목적이다.
    """
    print()
    for target, tokens in counted:
        margin = (tokens - target.min_tokens) / target.min_tokens
        print(f"{target.name:40} {tokens:>6} 토큰  (최소 {target.min_tokens}, 여유 {margin:+.0%})")
    print()

    failed = False
    for target, tokens in counted:
        if tokens >= target.min_tokens:
            continue
        if target.required:
            print(
                f"[치명] {target.name}이 {tokens} 토큰으로 최소치 {target.min_tokens}에 못 미친다. "
                "캐시가 에러 없이 죽어 관점 호출 비용이 조용히 두 배 가까이 뛴다."
            )
            failed = True
        else:
            print(
                f"{target.name}은 최소치 {target.min_tokens}에 못 미친다. "
                "cache_control을 붙여도 에러 없이 무시된다 — 선별이 캐시를 안 쓰는 근거다."
            )

    print()
    print(
        f"손익분기표의 토큰 전제는 {_ESTIMATE_MICRO_USD}를 만든 가정이고 이 명령은 그것을 "
        "치환하지 않는다. 위 측정값과 어긋나면 harness/README.md의 표를 손으로 고칠 것."
    )
    return 1 if failed else 0


def _estimate(mode: str, repeat: int, cases: list[Case]) -> None:
    rule_passed = len(promotable(cases))
    per_case = {
        "baseline": _ESTIMATE_MICRO_USD["sonnet_pair"] + _ESTIMATE_MICRO_USD["critic_and_notice"],
        "triage-only": _ESTIMATE_MICRO_USD["triage"],
        "full": _ESTIMATE_MICRO_USD["triage"]
        + _ESTIMATE_MICRO_USD["sonnet_pair"]
        + _ESTIMATE_MICRO_USD["critic_and_notice"],
    }[mode]
    micro = per_case * rule_passed * repeat
    print()
    print(f"모드 {mode} / 케이스 {len(cases)}건(규칙 통과 {rule_passed}건) / 반복 {repeat}회")
    print(f"예상 상한 비용  {micro / 1_000_000:.3f} USD  = {micro * KRW_PER_USD / 1_000_000:,.0f}원")
    print("위반이 전부 비평과 수정문구를 태우는 최악을 가정한 상한이다. 실제는 이보다 적다.")
    print("실행하려면 --yes 를 붙여라.")


def _load_baseline(path: Path) -> dict[tuple[str, int], str]:
    """baseline 모드의 판정을 (케이스, 반복) 키로 읽어 둔다. net_miss 계산에 쓴다."""
    loaded: dict[tuple[str, int], str] = {}
    raw = path / "raw.jsonl" if path.is_dir() else path
    for line in raw.read_text().splitlines():
        row = json.loads(line)
        if row["mode"] == "baseline":
            loaded[(row["case_id"], row["repeat"])] = row["verdict"]
    return loaded


def _summarize(records: list[Record], baseline: dict[tuple[str, int], str]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    scored = [r for r in records if not r.expect_rule_block]

    # (1순위) 승급률. 분모는 규칙을 통과한 건이다.
    for label, subset in (
        ("전체", scored),
        (TargetType.PRODUCT.value, [r for r in scored if r.target == TargetType.PRODUCT.value]),
        (TargetType.REVIEW.value, [r for r in scored if r.target == TargetType.REVIEW.value]),
    ):
        triaged = [r for r in subset if r.triage_outcome != "OFF"]
        if not triaged:
            continue
        promoted = [r for r in triaged if r.triage_outcome in ("PROMOTED", "FAILED")]
        rate = len(promoted) / len(triaged)
        over = rate > BREAKEVEN_PROMOTION_RATE
        note = "손익분기 초과. 선별을 켜지 마라" if over else "손익분기 아래"
        rows.append((f"승급률 {label}", f"{rate:.1%} ({len(promoted)}/{len(triaged)}) — {note}"))

    # (2) 누락률 둘. 선별 때문에 생긴 누락은 net_miss 뿐이다.
    violating = [r for r in scored if r.label == VIOLATING and r.triage_outcome == "CLEAR"]
    rows.append(("triage_miss", f"{len(violating)}건 — 라벨이 위반인데 선별이 건너뛴 건"))
    net = [r for r in violating if baseline.get((r.case_id, r.repeat)) == "FLAG"]
    if baseline:
        rows.append(("net_miss", f"{len(net)}건 — 그중 baseline에서는 보류가 나왔던 건"))
    else:
        rows.append(("net_miss", "측정 불가 — --baseline-from 으로 baseline 산출물을 줘야 한다"))

    # (3) 건당 비용. 본문 길이로 얼마나 움직이는지 함께 본다.
    if scored:
        total = statistics.mean(r.cost_micro_usd for r in scored)
        rows.append(("건당 비용", f"{total:,.0f} microUSD = {total * KRW_PER_USD / 1_000_000:.2f}원"))
        triage_only_cost = [r.triage_cost_micro_usd for r in scored if r.triage_cost_micro_usd]
        if triage_only_cost:
            mean_triage = statistics.mean(triage_only_cost)
            krw = mean_triage * KRW_PER_USD / 1_000_000
            rows.append(("선별만 비용", f"{mean_triage:,.0f} microUSD = {krw:.2f}원"))
        for name, subset in (
            ("짧은 본문(100자 미만)", [r for r in scored if r.text_len < 100]),
            ("긴 본문(100자 이상)", [r for r in scored if r.text_len >= 100]),
        ):
            if subset:
                mean = statistics.mean(r.cost_micro_usd for r in subset)
                rows.append((f"건당 비용 {name}", f"{mean:,.0f} microUSD"))

    # (4) 지연
    latencies = [float(r.latency_ms) for r in scored]
    if latencies:
        rows.append(("지연 p50", f"{_percentile(latencies, 0.50):,.0f} ms"))
        rows.append(("지연 p95", f"{_percentile(latencies, 0.95):,.0f} ms"))

    # (5) 캐시 적중. Haiku 호출에서 0임을 숫자로 남긴다.
    haiku = [r for r in scored if r.triage_cost_micro_usd]
    if haiku:
        hit = sum(1 for r in haiku if r.haiku_cache_read > 0)
        rows.append(("선별 캐시 적중", f"{hit}/{len(haiku)}건 — 0이면 최소 접두부 미달이 확인된 것이다"))

    # (6) 선별 실패율
    triaged = [r for r in scored if r.triage_outcome != "OFF"]
    if triaged:
        failures = sum(1 for r in triaged if r.triage_outcome == "FAILED")
        rows.append(("선별 실패율", f"{failures / len(triaged):.1%} ({failures}/{len(triaged)})"))

    # (7) 반복 간 불일치율
    by_case: dict[str, set[str]] = defaultdict(set)
    for record in scored:
        by_case[record.case_id].add(f"{record.verdict}/{record.triage_outcome}")
    unstable = [case_id for case_id, seen in by_case.items() if len(seen) > 1]
    rows.append(
        ("반복 불일치", f"{len(unstable)}/{len(by_case)}건 — {', '.join(sorted(unstable)[:5]) or '없음'}")
    )

    # (8) 과탐 의심 건의 처리 결과
    suspects = [r for r in records if r.label == OVERBLOCK_SUSPECT]
    if suspects:
        tally = Counter(f"{r.verdict}/{r.triage_outcome}" for r in suspects)
        rows.append(("과탐 의심", ", ".join(f"{k}={v}" for k, v in sorted(tally.items()))))
    return rows


def _write_out(records: list[Record], rows: list[tuple[str, str]]) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = OUT_ROOT / stamp
    out.mkdir(parents=True, exist_ok=True)
    (out / "raw.jsonl").write_text("\n".join(r.as_json() for r in records) + "\n")
    (out / "summary.tsv").write_text("\n".join(f"{k}\t{v}" for k, v in rows) + "\n")
    return out


def _self_check() -> int:
    """케이스의 ``expect_rule_block``이 실제 규칙과 맞는지 본다. 키 없이 도는 단계다."""
    mismatched = [
        (case.id, case.expect_rule_block, bool(rules.screen(case.text).blocking))
        for case in CASES
        if bool(rules.screen(case.text).blocking) != case.expect_rule_block
    ]
    if mismatched:
        print("[치명] 케이스의 expect_rule_block이 규칙 결과와 어긋난다. 승급률 분모가 틀어진다.")
        for case_id, expected, actual in mismatched:
            print(f"  {case_id}: 기대 {expected} / 실제 {actual}")
        return 1
    print(f"케이스 {len(CASES)}건 중 규칙 통과 {len(promotable(CASES))}건. 분모 검증 통과.")
    return 0


async def _main(args: argparse.Namespace) -> int:
    if _self_check():
        return 1

    settings = Settings()
    if not settings.anthropic_api_key:
        print(
            "AECHAK_AI_ANTHROPIC_API_KEY가 없어 측정을 건너뛴다. "
            "ANTHROPIC_API_KEY가 아니라 이 이름이다(Settings의 env_prefix가 AECHAK_AI_)."
        )
        return 0

    if args.count_tokens:
        return _report_tokens(await _count_tokens(settings))

    cases = [c for c in CASES if not args.only or c.id in args.only]
    if not args.yes:
        _estimate(args.mode, args.repeat, cases)
        return 0

    baseline = _load_baseline(Path(args.baseline_from)) if args.baseline_from else {}
    llm = LlmClient(api_key=settings.anthropic_api_key, timeout_seconds=settings.request_timeout_seconds)
    try:
        if args.mode == "triage-only":
            records = await _run_triage_only(settings, llm, args.repeat, cases)
        else:
            records = await _run_pipeline_mode(args.mode, settings, llm, args.repeat, cases)
    finally:
        await llm.aclose()

    rows = _summarize(records, baseline)
    out = _write_out(records, rows)
    print()
    for key, value in rows:
        print(f"{key:24} {value}")
    print()
    print(f"산출물 {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="harness.run", description="선별 계층 측정 러너")
    parser.add_argument("--count-tokens", action="store_true", help="접두부 토큰 수만 실측한다")
    parser.add_argument("--mode", choices=MODES, default="baseline")
    parser.add_argument("--repeat", type=int, default=3, help="같은 입력을 몇 번 돌릴지")
    parser.add_argument("--yes", action="store_true", help="실제 호출을 승인한다")
    parser.add_argument("--only", nargs="*", default=None, help="케이스 ID를 골라 돌린다")
    parser.add_argument("--baseline-from", default=None, help="net_miss 계산에 쓸 baseline 산출물 경로")
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
