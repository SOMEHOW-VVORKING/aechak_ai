"""요약 서비스. 단계를 조립한다.

    N0 입력 게이트   (코드, 0원)  레닥션 + 주입 제외 + 중복 접기 + 절단 + 봉투 조립
    N1 추출          (모델 1회 x 배치)  리뷰 본문을 모델이 읽는 **유일한** 자리
    N2 검증 + 집계   (코드, 0원)  인용 리터럴 검증 + 극성 교차검사 + 임계 판정
    N3 서술          (모델 1회 x 스코프)  입력은 집계표. **리뷰 본문이 아니다**
    N4 출력 게이트   (코드, 0원)  산문·인용 재검사 + 집계표 소속 확인

불변식 셋을 ``pipeline.py`` 에서 그대로 가져왔다.

1. 모든 분기 조건이 결정론적 코드다. "반복된다"의 판정이 ``tally.py`` 에 있고 모델이
   돌려준 어떤 값도 승급 조건이 아니다.
2. 모델 출력 열거에 거짓을 만들 통로가 없다. 집계표에 없는 항목을 올리면 코드가 버리고,
   집계표에 없는 인용을 쓰면 코드가 버린다.
3. 모든 노드가 async 다. 동기 함수 하나가 이벤트 루프를 막고 그것이 카프카 하트비트
   고갈과 리밸런스로 이어진다.

**이음매는 덕 타이핑이다.** ``llm`` 에서 ``call_tool`` 만 부른다. 운영 코드에 테스트
주입구를 내지 않는 것이 이 레포의 관례이고, 그래서 가짜 클라이언트 셋이 그대로 끼워진다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from app.review_summary import envelope as env
from app.review_summary import evidence
from app.review_summary import tally as tally_mod
from app.review_summary.aspects import ASPECT_LABELS, parse_aspect_code
from app.review_summary.extract_prompt import EXTRACT_PROMPT, EXTRACT_VERSION
from app.review_summary.extract_tool import EXTRACT_TOOL
from app.review_summary.narrate_prompt import (
    NARRATE_PROMPT,
    NARRATE_VERSION,
    render_tally_block,
    render_tally_row,
)
from app.review_summary.narrate_tool import CONS_MAX, NARRATE_TOOL, PROS_MAX, QUOTES_PER_ASPECT_MAX
from app.review_summary.trace import (
    DroppedItem,
    SummaryStage,
    SummaryStageStatus,
    SummaryTraceRecorder,
)
from app.schemas.inspection import CallOutcome
from app.schemas.summary import (
    AspectCode,
    AspectTallyRow,
    DroppedCounts,
    DroppedScope,
    ExtractedReview,
    ExtractStatus,
    Polarity,
    QuoteSample,
    ReviewSummaryMessage,
    ReviewSummaryRequest,
    ScopeSummary,
    SummaryAspect,
    SummaryCallUsage,
    SummaryScope,
    SummaryState,
)
from app.settings import JUDGE_RETRY_DELAY_SECONDS, Settings

log = logging.getLogger(__name__)


def _headline_gate(text: str) -> str | None:
    """한 줄 요약이 떨어지는 사유. 통과하면 ``None``.

    인용에만 걸려 있던 겹을 여기에도 건다 -- 전에는 차단 적중 하나만 봤고 길이 상한도
    ``mask_heavy`` 도 없었다. ``headline`` 은 리터럴 검증이 불가능한 자유 텍스트라
    쓸 수 있는 겹이 이 셋뿐이다.

    **마스킹 복원 검사는 여기서 돌지 않는다.** 한 줄 요약에는 대조할 원본 리뷰가 없다
    (N3 모델은 리뷰 본문을 보지 않는다). 대신 그 모델의 입력인 집계표가 이미 게이트를
    지난 라벨과 인용뿐이라 복원할 재료가 거기 없다 -- 그것이 N2 라벨 게이트가 서는
    두 번째 이유다.
    """
    if len(text) > evidence.HEADLINE_MAX_CHARS:
        return "HEADLINE_TOO_LONG"
    if env.mask_heavy(text):
        return "HEADLINE_MASK_HEAVY"
    if evidence.blocking_hits(text):
        return "PII_OR_CONTACT"
    return None


class SummaryService:
    def __init__(
        self,
        *,
        llm: Any | None,
        settings: Settings,
        recorder: SummaryTraceRecorder | None = None,
    ) -> None:
        self._llm = llm
        self._settings = settings
        # 데모 콘솔만 추적기를 준다. 카프카 경로는 주지 않으므로 운영 동작이 바뀌지 않는다.
        self._recorder = recorder

    async def run(self, request: ReviewSummaryRequest) -> ReviewSummaryMessage:
        settings = self._settings
        counts = DroppedCounts()
        calls: list[SummaryCallUsage] = []
        attempted = 0
        succeeded = 0

        # --- N0 입력 게이트 -----------------------------------------------------------
        batch = env.prepare(request.newReviews, content_chars=settings.summary_content_chars)
        counts.injectionExcluded = batch.injection_excluded
        counts.duplicateFolded = batch.duplicate_folded
        counts.truncated = batch.truncated
        counts.piiRedacted = batch.pii_redacted

        extracted: list[ExtractedReview] = [
            ExtractedReview(
                extracted=ExtractStatus.UNEXTRACTED,
                reviewId=item.review_id,
                excludeReason=item.reason,
                note=item.note,
            )
            for item in batch.excluded
        ]
        self._record_gate(batch)

        # --- N1 추출 + N2 검증 --------------------------------------------------------
        new_rows: list[tuple[int, AspectCode, Polarity, str, str]] = []
        groups = env.batches(batch.prepared, settings.summary_extract_batch_max)
        for index, group in enumerate(groups):
            if self._llm is None:
                extracted.extend(_unextracted(group, "모델 클라이언트가 없다"))
                self._skip_stage(SummaryStage.EXTRACT, f"N1 추출 {index + 1}", "LLM_DISABLED", len(group))
                continue

            attempted += 1
            result = await self._extract(request, group)
            calls.append(_usage(request.requestId, "EXTRACT", result))
            if not result.ok:
                extracted.extend(_unextracted(group, f"추출 호출 실패: {result.outcome.value}"))
                self._record_extract(index, group, result, None, failed=True)
                continue

            succeeded += 1
            verified, verify_counts = evidence.verify_batch(result.tool_input, group)
            counts.evidenceUnverified += verify_counts.evidence_unverified
            counts.polarityConflict += verify_counts.polarity_conflict
            counts.polarityConflictNoted += verify_counts.polarity_conflict_noted
            counts.maskHeavyQuote += verify_counts.mask_heavy_quote
            counts.unknownAspectCode += verify_counts.unknown_aspect_code
            counts.labelRejected += verify_counts.label_rejected
            counts.maskRestored += verify_counts.mask_restored

            seen = {entry.review_id: entry for entry in verified}
            for item in group:
                entry = seen.get(item.review_id)
                if entry is None:
                    # 모델이 그 리뷰를 아예 언급하지 않았다. 항목 없음이 아니다.
                    extracted.append(
                        ExtractedReview(
                            extracted=ExtractStatus.UNEXTRACTED,
                            reviewId=item.review_id,
                            truncated=item.truncated,
                            note="모델이 이 리뷰를 돌려주지 않았다",
                        )
                    )
                    continue
                if entry.reported_anything and not entry.aspects:
                    # 보고는 했지만 하나도 검증되지 않았다. 통과로 적으면 못 본 것을 봤다고
                    # 적는 셈이다 -- ``pipeline._interpret`` 의 같은 판단이다.
                    extracted.append(
                        ExtractedReview(
                            extracted=ExtractStatus.UNEXTRACTED,
                            reviewId=item.review_id,
                            truncated=item.truncated,
                            petContext=entry.pet_context,
                            # **무엇 때문에 전부 떨어졌는지**를 적는다. "검증 실패"로
                            # 뭉개면 인용이 안 맞은 것과 극성이 어긋난 것이 같은 문장이 된다.
                            note=(
                                "보고된 항목이 전부 떨어졌다: " + ", ".join(dict.fromkeys(entry.drop_reasons))
                            ),
                        )
                    )
                    continue
                extracted.append(
                    ExtractedReview(
                        extracted=ExtractStatus.EXTRACTED,
                        reviewId=item.review_id,
                        aspects=list(entry.aspects),
                        petContext=entry.pet_context,
                        truncated=item.truncated,
                    )
                )
                for aspect in _dedup_per_review(entry.aspects):
                    new_rows.append(
                        (
                            item.review_id,
                            aspect.aspectCode,
                            aspect.polarity,
                            aspect.aspectLabel,
                            aspect.quote,
                        )
                    )

            self._record_extract(index, group, result, verify_counts, failed=False)

        # **이번 회차에 읽지 못한 리뷰 수.** 추출 실패·주입 제외·중복 접기·모델 무응답이
        # 전부 여기 들어간다. 이 수를 분모에서 빼지 않으면 못 읽은 부정 리뷰가 "단점
        # 언급 없음"이 된다 -- 이 프로젝트의 1번 불변식 위반이다.
        unextracted_now = sum(1 for row in extracted if row.extracted is ExtractStatus.UNEXTRACTED)

        # --- N3 서술 + N4 출력 게이트 -------------------------------------------------
        summaries: list[ScopeSummary] = []
        scopes = request.requestedScopes[: settings.summary_scopes_per_message]
        dropped_scopes = [
            DroppedScope(
                scope=scope.scope,
                segmentKey=scope.segmentKey,
                notProcessedReason=(
                    f"메시지당 스코프 상한 {settings.summary_scopes_per_message}개를 넘겨 "
                    "처리하지 않았다. 요약이 없다는 뜻이 아니다"
                ),
            )
            for scope in request.requestedScopes[settings.summary_scopes_per_message :]
        ]
        if dropped_scopes:
            # 로그만 남기면 백엔드가 "요약 없음"과 구분할 수 없어 그 세그먼트를 영구히
            # 빈 채로 캐시한다. 응답에도 싣는다.
            log.warning(
                "스코프 상한을 넘겨 뒤쪽을 버린다. requested=%d max=%d requestId=%s",
                len(request.requestedScopes),
                settings.summary_scopes_per_message,
                request.requestId,
            )
        for scope in scopes:
            summary, usage, did_attempt, did_succeed = await self._narrate_scope(
                request, scope, new_rows, counts, unextracted_now=unextracted_now
            )
            summaries.append(summary)
            if usage is not None:
                calls.append(usage)
            attempted += int(did_attempt)
            succeeded += int(did_succeed)

        state = SummaryState.FAILED if attempted > 0 and succeeded == 0 else SummaryState.OK
        self._alert_cost(calls, request.requestId)
        return ReviewSummaryMessage(
            requestId=request.requestId,
            productId=request.productId,
            state=state,
            extractVersion=EXTRACT_VERSION,
            narrateVersion=NARRATE_VERSION,
            extracted=extracted,
            summaries=summaries,
            droppedScopes=dropped_scopes,
            calls=calls,
            dropped=counts,
            failureDetail=("모델 호출이 전부 실패했다" if state is SummaryState.FAILED else None),
            generatedAt=datetime.now(UTC),
        )

    # --- 단계 ------------------------------------------------------------------------

    async def _extract(self, request: ReviewSummaryRequest, group: list[env.PreparedReview]) -> Any:
        text = env.render_envelope(request.categoryPath, group)
        if self._recorder is not None:
            self._recorder.envelope(text)
        return await self._call(
            stage="EXTRACT",
            model=self._settings.summary_extract_model,
            effort=self._settings.summary_extract_effort,
            max_tokens=self._settings.max_tokens_summary_extract,
            system_blocks=extract_system_blocks(),
            user_content=[{"type": "text", "text": text}],
            tool=EXTRACT_TOOL,
            request_id=request.requestId,
        )

    async def _narrate_scope(
        self,
        request: ReviewSummaryRequest,
        scope: Any,
        new_rows: list[tuple[int, AspectCode, Polarity, str, str]],
        counts: DroppedCounts,
        *,
        unextracted_now: int,
    ) -> tuple[ScopeSummary, SummaryCallUsage | None, bool, bool]:
        settings = self._settings
        merged = tally_mod.from_rows(scope.tally)

        # **이번 회차 실패를 더한다.** 요청값만 되읊으면 추출이 실패한 리뷰가 "분석한
        # 리뷰" 분모에 그대로 남는다. 상품 스코프에만 더하는 이유는 세그먼트 쪽에서 AI 가
        # 리뷰의 세그먼트 소속을 모르기 때문이다 -- 추측해서 더하면 그것이 또 다른 거짓이고,
        # 그 분모는 백엔드가 책임진다(계약에 적었다).
        unextracted = scope.unextractedReviewCount + (
            unextracted_now if scope.scope is SummaryScope.PRODUCT else 0
        )

        if scope.scope is SummaryScope.PRODUCT:
            # 새 항목은 **상품 스코프에만** 더한다. 세그먼트는 리뷰가 어느 세그먼트에
            # 속하는지 AI 가 모르므로(펫 스냅샷을 받지 않는다) 보낸 집계를 그대로 쓴다.
            for _review_id, code, polarity, label, quote_text in new_rows:
                tally_mod.add(
                    merged,
                    aspect_code=code,
                    polarity=polarity,
                    label=label,
                    quote=QuoteSample(reviewId=_review_id, text=quote_text),
                )

        thresholds = tally_mod.Thresholds(
            min_mentions=settings.summary_min_aspect_mentions,
            min_share=settings.summary_min_aspect_share,
            min_reviews=settings.summary_min_reviews,
            min_segment_reviews=settings.summary_min_segment_reviews,
        )
        classified = tally_mod.classify(
            merged,
            basis_review_count=scope.basisReviewCount,
            thresholds=thresholds,
            segment=scope.scope is SummaryScope.SEGMENT,
            unextracted_review_count=unextracted,
        )
        counts.tallyInconsistent += classified.inconsistent_rows
        fingerprint = tally_mod.fingerprint(merged)
        scope_label = scope.segmentKey or scope.scope.value

        if not classified.enough_reviews:
            # **모델을 부르지 않는다.** 그리고 "장단점 없음"으로 적지 않는다 -- 요약하지
            # 못한 것과 장단점이 없는 것은 다른 사실이다. 센 숫자를 그대로 보여 준다.
            minimum = (
                settings.summary_min_segment_reviews
                if scope.scope is SummaryScope.SEGMENT
                else settings.summary_min_reviews
            )
            self._skip_stage(
                SummaryStage.NARRATE,
                f"N3 서술 {scope_label}",
                "NOT_ENOUGH_REVIEWS",
                scope.basisReviewCount,
                scope=scope_label,
            )
            return (
                ScopeSummary(
                    scope=scope.scope,
                    segmentKey=scope.segmentKey,
                    basisReviewCount=scope.basisReviewCount,
                    unextractedReviewCount=unextracted,
                    analyzedReviewCount=classified.analyzed_review_count,
                    consAbsenceReason=classified.cons_absence_reason,
                    minorMentions=[row.model_copy(deep=True) for row in merged.as_list()],
                    # **전수가 아니라 분석된 건수를 적는다.** 못 읽은 리뷰를 분모에 세면
                    # 이 문장이 거짓이 되고, 화면이 그 문장을 그대로 그린다.
                    notEnoughReason=(
                        f"분석한 리뷰 {classified.analyzed_review_count}건이 최소 {minimum}건에 못 미친다"
                    ),
                    tallyFingerprint=fingerprint,
                ),
                None,
                False,
                False,
            )

        if self._llm is None:
            self._skip_stage(
                SummaryStage.NARRATE,
                f"N3 서술 {scope_label}",
                "LLM_DISABLED",
                scope.basisReviewCount,
                scope=scope_label,
            )
            return (
                _failed_scope(scope, unextracted, fingerprint, classified, "모델 클라이언트가 없다"),
                None,
                False,
                False,
            )

        rows = tally_mod.narrate_rows(classified)
        block = render_tally_block(
            category_path=request.categoryPath,
            scope_label=scope_label,
            # **분석된 건수를 보낸다.** 전수를 보내면 모델이 분모를 전수로 읽고 "대부분의
            # 보호자가" 같은 문장을 쓴다. 그 줄이 바로 그것을 막기 위해 있다.
            analyzed_review_count=classified.analyzed_review_count,
            unextracted_review_count=unextracted,
            rows=[_render_row(item) for item in rows],
        )
        if self._recorder is not None:
            self._recorder.tally_block(scope_label, block)

        result = await self._call(
            stage="NARRATE",
            model=settings.summary_narrate_model,
            effort=settings.summary_narrate_effort,
            max_tokens=settings.max_tokens_summary_narrate,
            system_blocks=narrate_system_blocks(),
            user_content=[{"type": "text", "text": block}],
            tool=NARRATE_TOOL,
            request_id=request.requestId,
        )
        usage = _usage(request.requestId, "NARRATE", result)
        if not result.ok:
            self._record_narrate(scope_label, result, None, failed=True)
            return (
                _failed_scope(
                    scope, unextracted, fingerprint, classified, f"서술 호출 실패: {result.outcome.value}"
                ),
                usage,
                True,
                False,
            )

        pros, cons, dropped = _apply_output_gate(result.tool_input, classified, counts)
        unnarrated = _unnarrated(classified, pros, cons)
        counts.narrationMissing += len(unnarrated)
        if unnarrated:
            dropped.extend(
                DroppedItem(
                    where="N4",
                    reason="NARRATION_MISSING",
                    detail=f"{row.aspectCode.value}/{row.polarity.value}: 반복으로 셌는데 한 줄이 없다",
                )
                for row in unnarrated
            )
        self._record_narrate(scope_label, result, dropped, failed=False)
        return (
            ScopeSummary(
                scope=scope.scope,
                segmentKey=scope.segmentKey,
                basisReviewCount=scope.basisReviewCount,
                unextractedReviewCount=unextracted,
                analyzedReviewCount=classified.analyzed_review_count,
                pros=pros,
                cons=cons,
                # 단점이 비면 사유가 **반드시** 붙고, 그 사유는 **코드의 것이다.**
                #
                # 모델이 보낸 값을 먼저 쓰던 것이 "반복된다"의 판정을 모델에게서 빼앗은
                # 설계에 뚫린 구멍이었다 -- 집계표에 CON 4건이 있는데 모델이
                # ``NO_NEGATIVE_MENTIONS_FOUND`` 를 보내면 그것이 그대로 출력됐다.
                # 이제 툴 스키마에 그 필드가 아예 없고 코드만 채운다.
                consAbsenceReason=_cons_absence_reason(classified, cons),
                minorMentions=[row.model_copy(deep=True) for row in classified.minor],
                unnarratedAspects=unnarrated,
                tallyFingerprint=fingerprint,
            ),
            usage,
            True,
            True,
        )

    async def _call(self, *, stage: str, **kwargs: Any) -> Any:
        """호출 한 번 + 조건부 재시도 1회. 예산을 한 자리에서 보이게 둔다.

        ``BUDGET_EXHAUSTED`` 와 ``TRUNCATED`` 는 다시 쏴도 같은 결과다 -- 재시도는 출력
        토큰만 한 번 더 태운다. ``pipeline._judge`` 의 조건이 선례다.
        """
        assert self._llm is not None
        result = await self._llm.call_tool(timeout_seconds=self._settings.summary_timeout_seconds, **kwargs)
        if result.ok or result.outcome in (CallOutcome.BUDGET_EXHAUSTED, CallOutcome.TRUNCATED):
            return result
        log.info("요약 %s 호출 실패. 한 번만 다시 쏜다. outcome=%s", stage, result.outcome.value)
        await asyncio.sleep(JUDGE_RETRY_DELAY_SECONDS)
        return await self._llm.call_tool(timeout_seconds=self._settings.summary_timeout_seconds, **kwargs)

    def _alert_cost(self, calls: list[SummaryCallUsage], request_id: str) -> None:
        """한 메시지가 일 예산의 눈에 띄는 몫을 태웠으면 남긴다.

        **프로세스 로컬이라 강제력이 없다.** 컨슈머는 상태가 없어서 일일 누적을 셀 수
        없고, 실제 상한은 백엔드의 쿨다운과 워크스페이스 spend limit 이 건다. 그래도
        한 건이 하루 예산의 5%를 태우면 그것은 설정이 틀어진 신호다.
        """
        total = sum(call.costMicroUsd for call in calls)
        ceiling = int(self._settings.summary_daily_budget_usd * 1_000_000 * 0.05)
        if ceiling > 0 and total > ceiling:
            log.warning(
                "요약 한 건이 일 예산의 5%%를 넘겼다. requestId=%s costMicroUsd=%d",
                request_id,
                total,
            )

    # --- 추적 ------------------------------------------------------------------------

    def _record_gate(self, batch: env.PreparedBatch) -> None:
        if self._recorder is None:
            return
        self._recorder.stage(
            SummaryStage.GATE,
            "N0 입력 게이트",
            SummaryStageStatus.RAN,
            note="모델을 부르지 않는다. 0원 단계다",
            counts={
                "prepared": len(batch.prepared),
                "injectionExcluded": batch.injection_excluded,
                "duplicateFolded": batch.duplicate_folded,
                "truncated": batch.truncated,
                "piiRedacted": batch.pii_redacted,
            },
            dropped=[
                DroppedItem(
                    where="N0",
                    reason=item.reason.value,
                    detail=f"리뷰 {item.review_id}: {item.note}",
                )
                for item in batch.excluded
            ],
        )

    def _skip_stage(
        self,
        stage: SummaryStage,
        label: str,
        reason: str,
        count: int,
        *,
        scope: str | None = None,
    ) -> None:
        if self._recorder is None:
            return
        self._recorder.stage(
            stage,
            label,
            SummaryStageStatus.SKIPPED,
            note=reason,
            scope=scope,
            counts={"count": count},
        )

    def _record_extract(
        self,
        index: int,
        group: list[env.PreparedReview],
        result: Any,
        verify: evidence.VerifyCounts | None,
        *,
        failed: bool,
    ) -> None:
        if self._recorder is None:
            return
        counts = {"reviews": len(group)}
        if verify is not None:
            counts.update(
                {
                    "evidenceUnverified": verify.evidence_unverified,
                    "polarityConflict": verify.polarity_conflict,
                    "polarityConflictNoted": verify.polarity_conflict_noted,
                    "maskHeavyQuote": verify.mask_heavy_quote,
                    "unknownAspectCode": verify.unknown_aspect_code,
                    "quoteTooLong": verify.quote_too_long,
                }
            )
        self._recorder.stage(
            SummaryStage.EXTRACT,
            f"N1 추출 {index + 1}",
            SummaryStageStatus.FAILED if failed else SummaryStageStatus.RAN,
            note=("호출이 실패해 이 배치 전건이 UNEXTRACTED 다" if failed else None),
            result=result,
            counts=counts,
        )

    def _record_narrate(
        self,
        scope_label: str,
        result: Any,
        dropped: list[DroppedItem] | None,
        *,
        failed: bool,
    ) -> None:
        if self._recorder is None:
            return
        self._recorder.stage(
            SummaryStage.NARRATE,
            f"N3 서술 {scope_label}",
            SummaryStageStatus.FAILED if failed else SummaryStageStatus.RAN,
            scope=scope_label,
            note=("호출이 실패했다. 빈 장단점이 아니라 서술 실패로 적는다" if failed else None),
            result=result,
            dropped=dropped or [],
        )


# --- 순수 함수 ------------------------------------------------------------------------


def extract_system_blocks() -> list[dict[str, Any]]:
    """``cache_control`` 을 붙이지 않는다.

    기본 모델 Haiku 4.5 의 최소 캐시 접두부가 4,096토큰이라 붙여도 **에러 없이 무시된다.**
    Sonnet 5 로 뒤집으면 붙이고 ``cache_prefix_targets()`` 에 ``required=True`` 로 등록해
    ``--count-tokens`` 가 하한 미달을 종료 코드로 알리게 한다.
    """
    return [{"type": "text", "text": EXTRACT_PROMPT}]


def narrate_system_blocks() -> list[dict[str, Any]]:
    return [{"type": "text", "text": NARRATE_PROMPT}]


def _usage(request_id: str, stage: str, result: Any) -> SummaryCallUsage:
    return SummaryCallUsage(
        requestId=request_id,
        callId=result.call_id,
        stage=stage,
        modelId=result.model_id,
        inputTokens=result.input_tokens,
        cacheReadInputTokens=result.cache_read_input_tokens,
        cacheCreationInputTokens=result.cache_creation_input_tokens,
        outputTokens=result.output_tokens,
        costMicroUsd=result.cost_micro_usd,
        latencyMs=result.latency_ms,
        outcome=result.outcome.value,
    )


def _unextracted(group: list[env.PreparedReview], note: str) -> list[ExtractedReview]:
    return [
        ExtractedReview(
            extracted=ExtractStatus.UNEXTRACTED,
            reviewId=item.review_id,
            truncated=item.truncated,
            note=note,
        )
        for item in group
    ]


def _dedup_per_review(aspects: list[Any]) -> list[Any]:
    """한 리뷰가 같은 ``(코드, 극성)`` 을 두 번 세지 않게 한다.

    모델이 같은 항목을 두 구간에서 뽑으면 리뷰 한 건이 언급 2건으로 세어지고, 그러면
    리뷰 2건으로 "3건 언급"이 만들어진다 -- 임계가 막으려던 것을 정확히 우회한다.
    """
    seen: set[tuple[AspectCode, Polarity]] = set()
    out = []
    for aspect in aspects:
        key = (aspect.aspectCode, aspect.polarity)
        if key in seen:
            continue
        seen.add(key)
        out.append(aspect)
    return out


def _render_row(item: tally_mod.Repeated) -> str:
    name = ASPECT_LABELS[item.row.aspectCode][0]
    return render_tally_row(
        aspect_code=item.row.aspectCode.value,
        aspect_name=name,
        polarity=item.row.polarity.value,
        mention_count=item.row.mentionCount,
        share=item.share,
        labels=list(item.row.labelSamples),
        quotes=[quote.text for quote in item.row.quoteSamples],
    )


def _failed_scope(
    scope: Any,
    unextracted: int,
    fingerprint: str,
    classified: tally_mod.Classified,
    detail: str,
) -> ScopeSummary:
    """서술이 못 돈 스코프. **빈 장단점을 "없음"으로 적지 않는다.**

    집계는 돌았으므로 ``minorMentions`` 와 지문은 그대로 싣는다 -- 그것이 "센 숫자는
    있지만 산문이 없다"는 사실을 정확히 표현한다.
    """
    return ScopeSummary(
        scope=scope.scope,
        segmentKey=scope.segmentKey,
        basisReviewCount=scope.basisReviewCount,
        unextractedReviewCount=unextracted,
        analyzedReviewCount=classified.analyzed_review_count,
        minorMentions=[row.model_copy(deep=True) for row in classified.minor],
        # 반복으로 센 항목을 그대로 싣는다. 산문만 없고 셈은 있다는 사실이 화면에 남아야 한다.
        unnarratedAspects=[item.row.model_copy(deep=True) for item in [*classified.pros, *classified.cons]],
        narrateFailure=detail,
        tallyFingerprint=fingerprint,
    )


def _cons_absence_reason(classified: tally_mod.Classified, cons: list[SummaryAspect]) -> Any:
    """단점이 빈 사유. **전부 코드가 정한다.**

    세 경우가 섞이지 않게 가른다.

    1. 단점이 실렸다 -> 사유가 없다.
    2. 집계가 반복 단점을 **셌는데** 출력에 하나도 없다 -> ``NARRATION_MISSING``.
       모델이 한 줄을 안 썼거나 출력 게이트가 전부 떨어뜨린 것이고, 어느 쪽이든
       "단점 언급이 없었다"가 아니다. 그 값이 여기 올 수 있던 경로를 코드에서 없앤다.
    3. 그 밖 -> 집계가 계산한 사유(미달·임계 미달·못 읽음·없음).
    """
    if cons:
        return None
    if classified.cons:
        from app.schemas.summary import ConsAbsenceReason

        return ConsAbsenceReason.NARRATION_MISSING
    return classified.cons_absence_reason


def _unnarrated(
    classified: tally_mod.Classified,
    pros: list[SummaryAspect],
    cons: list[SummaryAspect],
) -> list[AspectTallyRow]:
    """임계를 넘었는데 산문이 붙지 않은 항목. **버리지 않고 건수째로 돌려준다.**

    모델이 ``pros`` 를 비워 보내거나 출력 게이트가 그 항목을 떨어뜨리면, 코드가 이미
    "반복된다"로 센 항목이 결과에서 통째로 사라진다. 그러면 화면에 아무것도 안 뜨고
    구매자는 "반복되는 장점이 없다"로 읽는다 -- 센 숫자가 있는데 거짓을 말하는 것이다.

    ``minorMentions`` 에 넣지 않는다. 저쪽은 임계 미달이라는 **다른 사실**이다.
    """
    written = {(item.aspectCode, Polarity.PRO) for item in pros}
    written |= {(item.aspectCode, Polarity.CON) for item in cons}
    return [
        item.row.model_copy(deep=True)
        for item in [*classified.pros, *classified.cons]
        if (item.row.aspectCode, item.row.polarity) not in written
    ]


def _apply_output_gate(
    tool_input: dict[str, Any] | None,
    classified: tally_mod.Classified,
    counts: DroppedCounts,
) -> tuple[list[SummaryAspect], list[SummaryAspect], list[DroppedItem]]:
    """N4. **모델이 거짓을 통과시킬 통로를 닫는다.**

    셋을 본다.

    1. **임계를 넘은 항목인가.** 집계표에 없는 코드를 올리면 버린다. 프롬프트로 누르는
       것이 아니라 코드가 집합으로 확인한다.
    2. **인용이 집계표의 검증된 인용인가.** 모델이 고치거나 지어낸 인용은 버린다.
       원본은 이미 리뷰 본문에서 리터럴 검증을 통과한 문자열이다.
    3. **산문에 연락처·외부 유도·지시형이 섞이지 않았는가.** 적중하면 그 항목만 버리고
       요약 전체는 살린다. 금지 성분 적중은 보지 않는다 -- 리뷰가 성분을 걱정한다는
       사실이 구매자에게 가장 유용한 단점일 수 있다.

    ``mentionCount`` 와 ``mentionShare`` 는 **집계에서 채운다.** 툴 스키마에 그 필드가
    없으므로 모델은 적을 수 없다.
    """
    dropped: list[DroppedItem] = []
    allowed = {
        (item.row.aspectCode, item.row.polarity): item for item in [*classified.pros, *classified.cons]
    }
    raw = tool_input or {}

    def collect(key: str, polarity: Polarity, limit: int) -> list[SummaryAspect]:
        out: list[SummaryAspect] = []
        items = raw.get(key)
        items = items if isinstance(items, list) else []
        used: set[AspectCode] = set()
        for item in items:
            if len(out) >= limit:
                break
            if not isinstance(item, dict):
                continue
            code = parse_aspect_code(item.get("aspectCode"))
            if code is None:
                counts.unknownAspectCode += 1
                continue
            if code in used:
                continue
            repeated = allowed.get((code, polarity))
            if repeated is None:
                counts.belowThresholdRejected += 1
                dropped.append(
                    DroppedItem(
                        where=f"N4 {key}",
                        reason="BELOW_THRESHOLD",
                        detail=f"{code.value}: 집계표의 반복 항목이 아니다",
                    )
                )
                continue
            headline = item.get("headline")
            if not isinstance(headline, str) or not headline.strip():
                counts.evidenceUnverified += 1
                continue
            headline = headline.strip()
            gate = _headline_gate(headline)
            if gate is not None:
                # **``piiRedacted`` 에 섞지 않는다.** 저쪽은 입력 레닥션 건수이고 이쪽은
                # 출력 게이트 탈락이다. 한 필드로 세면 값이 올랐을 때 정상과 사고를
                # 구분할 수 없다.
                counts.outputGateBlocked += 1
                dropped.append(
                    DroppedItem(
                        where=f"N4 {key}",
                        reason=gate,
                        detail=f"{code.value}: 한 줄 요약이 출력 게이트에 걸렸다",
                    )
                )
                continue

            quotes = _gate_quotes(item.get("quotes"), repeated.row, counts, dropped, key)
            used.add(code)
            out.append(
                SummaryAspect(
                    aspectCode=code,
                    headline=headline,
                    mentionCount=repeated.row.mentionCount,
                    # ``classify`` 가 이미 1.0 으로 자른 값이다. 자르지 않으면 집계 손상
                    # 한 번으로 "240%의 후기가" 가 화면에 뜬다.
                    mentionShare=round(repeated.share, 4),
                    quotes=quotes,
                )
            )
        return out

    pros = collect("pros", Polarity.PRO, PROS_MAX)
    cons = collect("cons", Polarity.CON, CONS_MAX)
    # ``consAbsenceReason`` 을 **읽지 않는다.** 툴 스키마에 그 필드가 없고, 모델이 보내도
    # 코드가 센 숫자를 이길 수 없다. 사유는 ``_cons_absence_reason`` 이 정한다.
    return pros, cons, dropped


def _gate_quotes(
    raw: object,
    row: AspectTallyRow,
    counts: DroppedCounts,
    dropped: list[DroppedItem],
    where: str,
) -> list[QuoteSample]:
    verified = {(sample.reviewId, sample.text) for sample in row.quoteSamples}
    out: list[QuoteSample] = []
    items = raw if isinstance(raw, list) else []
    for item in items:
        if len(out) >= QUOTES_PER_ASPECT_MAX:
            break
        if not isinstance(item, dict):
            continue
        review_id = item.get("reviewId")
        text = item.get("text")
        if not isinstance(review_id, int) or not isinstance(text, str) or not text:
            counts.evidenceUnverified += 1
            continue
        if (review_id, text) not in verified:
            counts.evidenceUnverified += 1
            dropped.append(
                DroppedItem(
                    where=f"N4 {where}",
                    reason="QUOTE_NOT_IN_TALLY",
                    detail=f"리뷰 {review_id}: 집계표에 없는 인용이다",
                )
            )
            continue
        if env.mask_heavy(text):
            counts.maskHeavyQuote += 1
            continue
        if evidence.blocking_hits(text):
            counts.outputGateBlocked += 1
            continue
        out.append(QuoteSample(reviewId=review_id, text=text))
    return out
