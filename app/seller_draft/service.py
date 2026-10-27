"""초안 서비스. 단계를 조립한다.

    N0 0원 결정론 선검사  입력 검증 + 상품명 0단 룰 + 이미지 헤더 + 어휘 게이트 + 봉투
    N1 모델 1회           strict 툴 강제. 실패 분기는 조건부 재시도 1회
    N2 0원 생성후 가드     슬롯·옵션·근거·길이·리프 대조. **재생성 0회**
    N3 초안 조립          ProductDraftSaveRequest 모양 + 필드마다 fieldStatus

**모델을 0회 부르는 길이 둘 있고 그것이 비용 설계의 핵심이다.** 상품명이 0단 룰에 걸렸거나
(셀러를 함정에 빠뜨리는 초안을 만들지 않는다) 품목을 전혀 확정하지 못했을 때
(일반론을 쓰지 않고 비운다). 둘 다 0원이다.

**이음매는 덕 타이핑이다.** ``llm`` 에서 ``call_tool`` 만 부른다. 운영 코드에 테스트
주입구를 내지 않는 것이 이 레포의 관례이고, 그래서 가짜 클라이언트 셋이 그대로 끼워진다.
호출이 1회라 재생 카세트의 키 충돌도 없다.

## 결정론에 대해 정직하게

**API 층에서 결정론을 얻을 수 없다.** ``temperature``·``top_p``·``top_k`` 는 클라이언트가
보내지 않고(계약으로 못박혀 있고 전송 본문 테스트가 단언한다), Haiku 4.5 는 ``effort`` 를
받으면 400 이며 ``seed`` 는 Messages API 에 없다. 그래서 같은 입력에 같은 초안이 **나오게
만드는** 대신 같은 초안을 **돌려준다** -- 내용 해시 재사용이다. 다만 AI 서버는 DB 에 붙지
않으므로 그 캐시는 프로세스 로컬이고 재배포에 사라진다. 진짜 재사용은 백엔드의 초안 스냅샷
칼럼으로 민다. 흔들림 폭은 하네스의 ``--repeat`` 가 잰다.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import unicodedata
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from app.inspection import rules
from app.schemas.draft import (
    DRAFT_FIELDS,
    Basis,
    DraftCallUsage,
    DraftState,
    FieldStatus,
    GuardDrop,
    GuardKind,
    ImageMode,
    ImageReport,
    ImageUsability,
    ItemSignalOut,
    ProductDraftSaveRequest,
    SellerDraftRequest,
    SellerDraftResponse,
    SkipReason,
)
from app.schemas.inspection import CallOutcome
from app.seller_draft import guard, imagemeta
from app.seller_draft.catalog import LEAF_BY_ID, ItemGrade, ItemSignal, leaf_path, resolve
from app.seller_draft.prompt import CATALOG_VERSION as _CATALOG_VERSION
from app.seller_draft.prompt import (
    DRAFT_VERSION,
    draft_system_blocks,
    draft_user_content,
    draft_version_string,
    image_block,
)
from app.seller_draft.tool import DRAFT_TOOL, LEAF_DECLINED
from app.seller_draft.trace import (
    DraftDroppedItem,
    DraftStage,
    DraftStageStatus,
    DraftTraceRecorder,
)
from app.settings import JUDGE_RETRY_DELAY_SECONDS, Settings

log = logging.getLogger(__name__)

# 설명 슬롯을 이어 붙이는 순서. 사실이 거친 것부터 세부로 간다.
_SLOT_ORDER = ("itemSummary", "materialOrSpec", "usage", "careAndStorage")

# 초안이 **설계상 채우지 않는** 필드. 돈·재고·스토리지 실체가 있는 식별자다.
# 모델이 만들어 낼 수 없는 값이고, 비어 있는 이유가 "못 했다"가 아니라 "안 한다"다.
_NEVER_ATTEMPTED: tuple[str, ...] = (
    "productName",
    "regularPrice",
    "discountPrice",
    "discountStartAt",
    "discountEndAt",
    "thumbnailImageKey",
    "additionalImageKeys",
    "detailImageKeys",
)


def _identity_key(text: str) -> str:
    """재사용 키의 상품명 조각. **NFKC + 공백 정규화뿐이다.**

    반복 축약을 하지 않는 이유는 ``DraftService._cache_key`` 에 적었다 -- 요약 쪽
    ``envelope.dedupe_key`` 와 같은 함수이고 같은 이유다.
    """
    return " ".join(unicodedata.normalize("NFKC", text).split())


class DraftUnprocessable(ValueError):
    """셀러센터가 고쳐 보낼 수 있는 입력 오류. 조용히 이미지를 버리지 않는다.

    ``imagemeta`` 의 거부(미지원 타입·토큰 예산 초과)와 상품명 누락이 여기 온다. 이미지를
    말없이 빼고 돌려주면 셀러는 자기 사진이 쓰인 줄 알고, 그 오해가 "자동완성이 사진을
    무시한다"는 평가가 된다.
    """

    def __init__(self, message: str, *, max_long_edge_px: int | None = None) -> None:
        super().__init__(message)
        self.max_long_edge_px = max_long_edge_px


class DraftService:
    def __init__(
        self,
        *,
        llm: Any | None,
        settings: Settings,
        recorder: DraftTraceRecorder | None = None,
    ) -> None:
        self._llm = llm
        self._settings = settings
        # 데모 콘솔과 하네스만 추적기를 준다. 운영 경로는 주지 않으므로 동작이 바뀌지 않는다.
        self._recorder = recorder
        self._cache: OrderedDict[str, SellerDraftResponse] = OrderedDict()

    # --- 공개 ------------------------------------------------------------------------

    async def run(self, request: SellerDraftRequest) -> SellerDraftResponse:
        settings = self._settings
        name = (request.productName or "").strip()
        if not name:
            raise DraftUnprocessable("상품명이 비어 있다. 초안을 만들 입력이 없다")

        mode = _image_mode(settings.draft_image_mode)
        signal = resolve(name, request.categoryId)
        image_meta, image_bytes = self._read_image(request, mode)

        cache_key = self._cache_key(request, mode=mode, image_bytes=image_bytes)
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            # **비용을 두 번 세지 않는다.** 원장은 처음 호출의 것이고 그 사실을 플래그로
            # 알린다 -- 재사용을 새 호출처럼 적으면 일 예산 계산이 조용히 부풀려진다.
            self._record_gate(signal, image_meta, skip=SkipReason.REUSED_FROM_CACHE)
            return cached.model_copy(update={"requestId": request.requestId, "reusedFromCache": True})

        if not settings.enable_draft:
            # 기본 꺼짐이다. 끈 상태에서 돌면 **0원이고 전 필드가 비며 사유가 그렇게 적힌다.**
            return self._skipped(request, signal, image_meta, mode, SkipReason.DRAFT_DISABLED)

        screened = rules.screen(name)
        if screened.blocking:
            reasons = ", ".join(dict.fromkeys(hit.reason for hit in screened.blocking))
            self._record_gate(signal, image_meta, skip=SkipReason.NAME_RULE_BLOCKED, detail=reasons)
            return self._skipped(
                request,
                signal,
                image_meta,
                mode,
                SkipReason.NAME_RULE_BLOCKED,
                notice=(
                    f"상품명이 플랫폼 정책에 걸립니다({reasons}). 그대로 등록하면 검수에서 "
                    "막히므로 초안을 만들지 않았습니다. 상품명을 고친 뒤 다시 시도해 주세요."
                ),
            )

        if signal.grade is ItemGrade.OPAQUE and request.categoryId is None:
            # **일반론을 쓰지 않는다.** 상품명도 카테고리도 품목을 집지 못하면 초안을
            # 만들지 않고 그 사실을 그대로 적는다. 0원짜리 분기다.
            self._record_gate(signal, image_meta, skip=SkipReason.ITEM_UNIDENTIFIED)
            return self._skipped(
                request,
                signal,
                image_meta,
                mode,
                SkipReason.ITEM_UNIDENTIFIED,
                notice=(
                    "상품명에서 어떤 품목인지 읽히지 않았습니다. 카테고리를 먼저 골라 주시거나 "
                    "상품명에 품목어(예: 목줄, 하네스, 건식사료)를 넣어 주시면 초안을 채웁니다 "
                    "— 검색에도 그 편이 잡힙니다."
                ),
            )

        if self._llm is None:
            self._record_gate(signal, image_meta, skip=SkipReason.LLM_UNAVAILABLE)
            return self._skipped(request, signal, image_meta, mode, SkipReason.LLM_UNAVAILABLE)

        use_image = _should_use_image(mode, signal, image_meta)
        self._record_gate(signal, image_meta, use_image=use_image)
        response = await self._generate(
            request,
            signal=signal,
            image_meta=image_meta,
            image_bytes=image_bytes,
            mode=mode,
            use_image=use_image,
        )
        if response.state is DraftState.OK:
            self._remember(cache_key, response)
        self._alert_cost(response)
        return response

    # --- N0 ---------------------------------------------------------------------------

    def _read_image(
        self, request: SellerDraftRequest, mode: ImageMode
    ) -> tuple[imagemeta.ImageMeta | None, bytes | None]:
        """헤더 선검사. **거부와 "빼고 진행"을 섞지 않는다.**

        모드가 ``OFF`` 면 바이트를 디코드조차 하지 않는다 -- 쓰지 않을 데이터를 푸는 일이
        없어야 base64 팽창분이 메모리에 남지 않는다.
        """
        if request.image is None or not request.image.base64:
            return None, None
        if mode is ImageMode.OFF:
            return None, None

        try:
            data = base64.b64decode(request.image.base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise DraftUnprocessable(f"이미지 base64 를 읽을 수 없다: {error}") from error

        meta = imagemeta.parse(
            data,
            declared_media_type=request.image.mediaType,
            declared_width_px=request.image.declaredWidthPx,
            declared_height_px=request.image.declaredHeightPx,
            max_tokens=self._settings.draft_image_max_tokens,
        )
        if meta.rejected:
            raise DraftUnprocessable(
                meta.detail or "이미지를 받을 수 없다", max_long_edge_px=meta.max_long_edge_px
            )
        return meta, data

    def _cache_key(self, request: SellerDraftRequest, *, mode: ImageMode, image_bytes: bytes | None) -> str:
        """내용 해시 재사용 키. ``requestId`` 를 넣지 않는다 -- 넣으면 절대 적중하지 않는다.

        판정 재사용을 ``contentHash + promptVersion`` 으로 잡은 관례와 같다. 버전을 올리면
        재사용이 끊기는 것도 같다.

        **상품명은 ``fold()`` 로 접지 않는다.** 처음 판이 그 함수를 썼고 그것이 결함이었다.
        ``banned.fold()`` 는 금지 성분 우회 표기를 잡으려고 **같은 글자 반복을 한 번으로
        줄이므로** 상품명의 수량·중량·사이즈가 접힌다. 실측이다.

            fold('고양이 모래 2kg')  == fold('고양이 모래 22kg')  -> '고양이모래2kg'
            fold('츄르 20개')        == fold('츄르 200개')       -> '츄르2o개'
            fold('강아지 목줄 S')     == fold('강아지 목줄 SS')     -> '강아지목줄s'

        그래서 "고양이 모래 2kg" 로 받은 초안이 "22kg" 요청에 ``reusedFromCache=True`` 로
        그대로 돌아갔고, 설명이 "2kg 포장"인데 ``fieldStatus=FILLED`` · ``basis=NAME`` 이라
        셀러 화면은 그것을 "상품명 근거로 채운 사실"로 읽었다. **다른 상품의 초안이다.**

        ``fold()`` 는 금지어 매칭 전용이고 **동일성 판정에 쓰면 안 된다** -- 요약 쪽
        ``envelope.dedupe_key`` 가 같은 이유로 그 함수를 거부한 선례가 이미 있었다.
        여기서는 NFKC + 공백 정규화만 한다. 공백·전각 차이는 같은 상품이고 숫자 차이는
        다른 상품이다.
        """
        locked = request.lockedFields
        parts = [
            DRAFT_VERSION,
            _CATALOG_VERSION,
            mode.value,
            _identity_key(request.productName),
            str(request.categoryId),
            hashlib.sha256(image_bytes).hexdigest() if image_bytes else "-",
            (locked.model_dump_json() if locked is not None else "-"),
        ]
        return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()

    def _remember(self, key: str, response: SellerDraftResponse) -> None:
        limit = self._settings.draft_cache_entries
        if limit <= 0:
            return
        self._cache[key] = response
        self._cache.move_to_end(key)
        while len(self._cache) > limit:
            self._cache.popitem(last=False)

    # --- N1 ~ N3 ----------------------------------------------------------------------

    async def _generate(
        self,
        request: SellerDraftRequest,
        *,
        signal: ItemSignal,
        image_meta: imagemeta.ImageMeta | None,
        image_bytes: bytes | None,
        mode: ImageMode,
        use_image: bool,
    ) -> SellerDraftResponse:
        block = (
            image_block(
                media_type=image_meta.media_type,
                data_base64=base64.b64encode(image_bytes).decode("ascii"),
            )
            if use_image and image_meta is not None and image_bytes is not None
            else None
        )
        content = draft_user_content(
            product_name=request.productName,
            signal=signal,
            locked=request.lockedFields,
            image_block=block,
            low_fidelity=bool(image_meta and image_meta.low_fidelity),
        )
        if self._recorder is not None:
            text = next(part["text"] for part in content if part.get("type") == "text")
            self._recorder.envelope(text, image_attached=block is not None)

        result = await self._call(request.requestId, content)
        usage = _usage(request.requestId, result)

        if not result.ok:
            self._record_generate(result, failed=True)
            return self._failed(request, signal, image_meta, mode, usage, result)

        self._record_generate(result, failed=False)
        return self._assemble(
            request,
            signal=signal,
            image_meta=image_meta,
            mode=mode,
            use_image=use_image,
            tool_input=result.tool_input or {},
            usage=usage,
        )

    async def _call(self, request_id: str, user_content: list[dict[str, Any]]) -> Any:
        """호출 한 번 + 조건부 재시도 1회. 예산을 한 자리에서 보이게 둔다.

        ``BUDGET_EXHAUSTED`` 와 ``TRUNCATED`` 는 다시 쏴도 같은 결과다 -- 재시도는 출력
        토큰만 한 번 더 태운다. ``pipeline._judge`` 의 조건이 선례다.
        """
        assert self._llm is not None
        settings = self._settings
        kwargs: dict[str, Any] = {
            "model": settings.draft_model,
            "effort": settings.draft_effort,
            "max_tokens": settings.max_tokens_draft,
            "system_blocks": draft_system_blocks(),
            "user_content": user_content,
            "tool": DRAFT_TOOL,
            "request_id": request_id,
            "timeout_seconds": settings.draft_timeout_seconds,
        }
        result = await self._llm.call_tool(**kwargs)
        if result.ok or result.outcome in (CallOutcome.BUDGET_EXHAUSTED, CallOutcome.TRUNCATED):
            return result
        log.info("초안 호출 실패. 한 번만 다시 쏜다. outcome=%s", result.outcome.value)
        await asyncio.sleep(JUDGE_RETRY_DELAY_SECONDS)
        return await self._llm.call_tool(**kwargs)

    def _assemble(
        self,
        request: SellerDraftRequest,
        *,
        signal: ItemSignal,
        image_meta: imagemeta.ImageMeta | None,
        mode: ImageMode,
        use_image: bool,
        tool_input: dict[str, Any],
        usage: DraftCallUsage,
    ) -> SellerDraftResponse:
        """N2 가드 + N3 조립. **모델이 돌려준 어떤 값도 그냥 통과하지 않는다.**"""
        settings = self._settings
        locked = request.lockedFields
        status: dict[str, FieldStatus] = {name: FieldStatus.EMPTY_NOT_ATTEMPTED for name in _NEVER_ATTEMPTED}
        basis: dict[str, Basis] = {}
        drops: list[GuardDrop] = []
        draft = ProductDraftSaveRequest()

        reported = _basis_map(tool_input.get("basis"))
        usability = _usability(tool_input.get("imageUsability"), use_image=use_image, meta=image_meta)

        # (ㄱ) 사진을 못 봤다면서 사진을 근거로 적은 필드는 **폐기한다.**
        # "못 봤는데 봤다고 적은" 상태가 응답에 남지 않는 것이 이 블록의 전부다.
        mismatched = {
            field
            for field, value in reported.items()
            if value is Basis.IMAGE and usability is not ImageUsability.USED
        }
        for field in sorted(mismatched):
            drops.append(
                GuardDrop(
                    field=field,
                    kind=GuardKind.BASIS_MISMATCH,
                    detail=(
                        f"사진 사용 가능 여부가 {usability.value} 인데 근거를 IMAGE 로 적어 "
                        "그 필드를 폐기했다"
                    ),
                )
            )

        # (ㄴ) 리프 분류. 어휘 게이트와 모델을 교차검증한다.
        leaf_status, leaf_value, leaf_basis, leaf_drop = self._resolve_leaf(
            signal, tool_input.get("leafCategoryId"), reported.get("categoryId", Basis.NONE)
        )
        if leaf_drop is not None:
            drops.append(leaf_drop)
        if "categoryId" in mismatched:
            leaf_status, leaf_value = FieldStatus.EMPTY_GUARD_BASIS_MISMATCH, None
            leaf_basis = Basis.NONE
        status["categoryId"] = leaf_status
        draft.categoryId = leaf_value
        basis["categoryId"] = leaf_basis

        # 품목을 끝까지 확정하지 못했으면 **설명과 옵션도 비운다.** 일반론 회피의 두 번째
        # 관문이다 -- 어느 품목인지 모르는 채로 쓴 설명은 어느 상품에도 맞지 않는다.
        item_unknown = leaf_status is FieldStatus.EMPTY_MODEL_DECLINED and signal.pinned_leaf_id is None

        # (ㄷ) 설명 슬롯.
        if locked is not None and locked.description:
            status["description"] = FieldStatus.KEPT_LOCKED
            draft.description = locked.description
            basis["description"] = Basis.NONE
        elif item_unknown:
            status["description"] = FieldStatus.EMPTY_ITEM_UNIDENTIFIED
            basis["description"] = Basis.NONE
        elif "description" in mismatched:
            status["description"] = FieldStatus.EMPTY_GUARD_BASIS_MISMATCH
            basis["description"] = Basis.NONE
        else:
            built = guard.build_description(
                _slots(tool_input.get("descriptionParts")),
                order=_SLOT_ORDER,
                max_chars=settings.draft_description_chars,
            )
            drops.extend(built.drops)
            status["description"] = built.status
            draft.description = built.text
            basis["description"] = reported.get("description", Basis.NONE) if built.text else Basis.NONE

        # (ㄹ) 옵션. 그룹·값만 받고 조합은 서버가 만든다.
        if locked is not None and locked.optionGroups:
            # **잠근 값도 ``check_options()`` 를 지난다.** 처음 판은 건너뛰었고 그것이
            # 결함이었다 -- 값 이름 전역 유일 같은 ``ProductOptions`` 불변식을 어긴 옵션을
            # ``KEPT_LOCKED``(= "셀러가 쓴 값이니 괜찮다")로, **사유 없이** 돌려줬다.
            # 셀러가 그대로 저장하면 등록이 거부되고, 왜 거부되는지 알 길이 없다.
            #
            # 잠근 값을 버리는 것이 목적이 아니다. 불변식을 지키면 그대로 ``KEPT_LOCKED``
            # 이고, 어기면 사유와 함께 비워 셀러가 고칠 수 있게 한다.
            verdict = guard.check_locked_options(list(locked.optionGroups))
            drops.extend(verdict.drops)
            kept = verdict.status is FieldStatus.FILLED
            locked_status = FieldStatus.KEPT_LOCKED if kept else verdict.status
            status["optionGroups"] = locked_status
            status["optionCombinations"] = locked_status
            draft.optionGroups = verdict.groups or None
            draft.optionCombinations = verdict.combinations or None
            basis["optionGroups"] = Basis.NONE
        elif item_unknown:
            status["optionGroups"] = FieldStatus.EMPTY_ITEM_UNIDENTIFIED
            status["optionCombinations"] = FieldStatus.EMPTY_ITEM_UNIDENTIFIED
            basis["optionGroups"] = Basis.NONE
        elif "optionGroups" in mismatched:
            status["optionGroups"] = FieldStatus.EMPTY_GUARD_BASIS_MISMATCH
            status["optionCombinations"] = FieldStatus.EMPTY_GUARD_BASIS_MISMATCH
            basis["optionGroups"] = Basis.NONE
        else:
            verdict = guard.check_options(_option_groups(tool_input.get("optionGroups")))
            drops.extend(verdict.drops)
            status["optionGroups"] = verdict.status
            # 조합은 그룹이 살았을 때만 있다. 그룹이 비었는데 조합이 남으면 등록이 거부된다.
            status["optionCombinations"] = verdict.status
            draft.optionGroups = verdict.groups or None
            draft.optionCombinations = verdict.combinations or None
            basis["optionGroups"] = reported.get("optionGroups", Basis.NONE) if verdict.groups else Basis.NONE

        notice = _notice(tool_input.get("sellerNotice"), drops)
        self._record_guard(status, drops)
        return SellerDraftResponse(
            requestId=request.requestId,
            draftVersion=draft_version_string(),
            state=DraftState.OK,
            itemSignal=_signal_out(signal),
            image=_image_report(image_meta, usability, mode=mode, use_image=use_image),
            imageUsability=usability,
            imageMode=mode,
            draft=draft,
            basis=basis,
            fieldStatus=_complete(status),
            guardDrops=drops,
            sellerNotice=notice,
            modelCalled=True,
            usage=[usage],
            generatedAt=datetime.now(UTC),
        )

    def _resolve_leaf(
        self, signal: ItemSignal, raw: Any, reported_basis: Basis
    ) -> tuple[FieldStatus, int | None, Basis, GuardDrop | None]:
        """리프를 확정한다. **어느 쪽이 맞는지 모르는 상태를 "맞혔음"으로 적지 않는다.**

        어휘 게이트가 리프 하나를 지목했는데 모델이 다른 리프를 냈으면 탐지 가능한 불일치다.
        둘 중 하나를 조용히 고르지 않고 비우고 사유를 적어 셀러가 고르게 한다.
        """
        model_leaf = raw if isinstance(raw, int) and raw != LEAF_DECLINED else None
        if model_leaf is not None and model_leaf not in LEAF_BY_ID:
            # ``strict`` 가 막아야 하는 자리다. 백스톱으로 둔다.
            return (
                FieldStatus.EMPTY_SCHEMA_INVALID,
                None,
                Basis.NONE,
                GuardDrop(
                    field="categoryId",
                    kind=GuardKind.UNKNOWN_LEAF,
                    detail=f"열거 밖의 리프 id {model_leaf} 를 냈다",
                ),
            )

        if signal.given_category_id is not None and signal.given_category_id in LEAF_BY_ID:
            # 셀러가 이미 리프를 골랐다. 채울 것이 없다 -- 모델이 다르게 봤으면 기록만 한다.
            drop = None
            if model_leaf is not None and model_leaf != signal.given_category_id:
                drop = GuardDrop(
                    field="categoryId",
                    kind=GuardKind.CATEGORY_CONFLICT,
                    detail=(
                        f"셀러가 고른 {signal.given_category_id}({leaf_path(signal.given_category_id)}) "
                        f"와 모델이 지목한 {model_leaf}({leaf_path(model_leaf)}) 가 다르다. "
                        "셀러의 선택을 그대로 둔다"
                    ),
                )
            return FieldStatus.EMPTY_NOT_ATTEMPTED, None, Basis.NONE, drop

        pinned = signal.pinned_leaf_id
        if model_leaf is None:
            if pinned is not None:
                # 모델이 못 골라도 **0원 게이트가 확정한 값이 있다.** 그것을 쓴다.
                return FieldStatus.FILLED, pinned, Basis.NAME, None
            return (
                FieldStatus.EMPTY_MODEL_DECLINED,
                None,
                Basis.NONE,
                GuardDrop(
                    field="categoryId",
                    kind=GuardKind.CATEGORY_CONFLICT,
                    detail="모델이 소분류를 고르지 못했다고 답했다. 추측으로 채우지 않는다",
                ),
            )

        if pinned is not None and model_leaf != pinned:
            return (
                FieldStatus.EMPTY_CATEGORY_CONFLICT,
                None,
                Basis.NONE,
                GuardDrop(
                    field="categoryId",
                    kind=GuardKind.CATEGORY_CONFLICT,
                    detail=(
                        f"어휘 게이트는 {pinned}({leaf_path(pinned)}), 모델은 "
                        f"{model_leaf}({leaf_path(model_leaf)}) 를 지목했다. "
                        "어느 쪽이 맞는지 모르므로 비우고 셀러가 고르게 한다"
                    ),
                ),
            )

        if signal.candidate_leaf_ids and model_leaf not in signal.candidate_leaf_ids:
            return (
                FieldStatus.EMPTY_CATEGORY_CONFLICT,
                None,
                Basis.NONE,
                GuardDrop(
                    field="categoryId",
                    kind=GuardKind.CATEGORY_CONFLICT,
                    detail=(
                        f"모델이 지목한 {model_leaf} 가 게이트 후보 {list(signal.candidate_leaf_ids)} 밖이다"
                    ),
                ),
            )

        return FieldStatus.FILLED, model_leaf, (reported_basis or Basis.NAME), None

    # --- 0원으로 끝나는 응답 ----------------------------------------------------------

    def _skipped(
        self,
        request: SellerDraftRequest,
        signal: ItemSignal,
        image_meta: imagemeta.ImageMeta | None,
        mode: ImageMode,
        reason: SkipReason,
        *,
        notice: str | None = None,
    ) -> SellerDraftResponse:
        status = _field_status_for_skip(reason)
        return SellerDraftResponse(
            requestId=request.requestId,
            draftVersion=draft_version_string(),
            state=DraftState.OK,
            itemSignal=_signal_out(signal),
            image=_image_report(image_meta, ImageUsability.NO_IMAGE_SENT, mode=mode, use_image=False),
            imageUsability=ImageUsability.NO_IMAGE_SENT,
            imageMode=mode,
            basis=dict.fromkeys(("categoryId", "description", "optionGroups"), Basis.NONE),
            fieldStatus=_complete({}, default=status),
            sellerNotice=notice,
            modelCalled=False,
            skipReason=reason,
            generatedAt=datetime.now(UTC),
        )

    def _failed(
        self,
        request: SellerDraftRequest,
        signal: ItemSignal,
        image_meta: imagemeta.ImageMeta | None,
        mode: ImageMode,
        usage: DraftCallUsage,
        result: Any,
    ) -> SellerDraftResponse:
        """호출이 죽었다. **"제안할 것이 없었다"로 적지 않는다.**

        호출이 실패해 못 본 것과 모델이 제안하지 않은 것은 둘 다 빈 필드지만 뜻이 정반대다.
        셀러 화면도 둘을 다르게 보여 줘야 한다 -- "다시 시도" 대 "직접 작성해 주세요".
        """
        status = _complete({}, default=FieldStatus.EMPTY_CALL_FAILED)
        return SellerDraftResponse(
            requestId=request.requestId,
            draftVersion=draft_version_string(),
            state=DraftState.FAILED,
            itemSignal=_signal_out(signal),
            image=_image_report(image_meta, ImageUsability.NO_IMAGE_SENT, mode=mode, use_image=False),
            imageUsability=ImageUsability.NO_IMAGE_SENT,
            imageMode=mode,
            basis=dict.fromkeys(("categoryId", "description", "optionGroups"), Basis.NONE),
            fieldStatus=status,
            modelCalled=True,
            usage=[usage],
            failureDetail=f"초안 호출이 실패했다: {result.outcome.value}"
            + (f" ({result.detail})" if result.detail else ""),
            generatedAt=datetime.now(UTC),
        )

    def _alert_cost(self, response: SellerDraftResponse) -> None:
        """한 건이 일 예산의 눈에 띄는 몫을 태웠으면 남긴다.

        **프로세스 로컬이라 강제력이 없다.** 실제 상한은 셀러센터의 레이트 리밋과 워크스페이스
        spend limit 이 건다. 그래도 한 건이 하루 예산의 5%를 태우면 설정이 틀어진 신호다.
        """
        total = response.total_cost_micro_usd()
        ceiling = int(self._settings.draft_daily_budget_usd * 1_000_000 * 0.05)
        if ceiling > 0 and total > ceiling:
            log.warning(
                "초안 한 건이 일 예산의 5%%를 넘겼다. requestId=%s costMicroUsd=%d",
                response.requestId,
                total,
            )

    # --- 추적 ------------------------------------------------------------------------

    def _record_gate(
        self,
        signal: ItemSignal,
        image_meta: imagemeta.ImageMeta | None,
        *,
        skip: SkipReason | None = None,
        use_image: bool = False,
        detail: str | None = None,
    ) -> None:
        if self._recorder is None:
            return
        self._recorder.stage(
            DraftStage.GATE,
            "N0 어휘 게이트",
            DraftStageStatus.SKIPPED if skip is not None else DraftStageStatus.RAN,
            note=(
                f"모델을 부르지 않는다: {skip.value}" + (f" ({detail})" if detail else "")
                if skip is not None
                else f"{signal.grade.value} / {signal.resolved_by.value}. 0원 단계다"
            ),
            counts={
                "candidates": len(signal.candidate_leaf_ids),
                "matchedTerms": len(signal.matched_terms),
            },
        )
        if image_meta is not None:
            self._recorder.stage(
                DraftStage.IMAGE,
                "N0 이미지 헤더",
                DraftStageStatus.RAN if use_image else DraftStageStatus.SKIPPED,
                note=image_meta.detail
                or (
                    f"{image_meta.width_px}x{image_meta.height_px} 약 "
                    f"{image_meta.estimated_tokens} 토큰(추정)"
                    + (" / 해상도 대비 용량이 작다" if image_meta.low_fidelity else "")
                ),
                counts={
                    "estimatedTokens": image_meta.estimated_tokens,
                    "bytes": image_meta.bytes_len,
                },
            )

    def _record_generate(self, result: Any, *, failed: bool) -> None:
        if self._recorder is None:
            return
        self._recorder.stage(
            DraftStage.GENERATE,
            "N1 초안 생성",
            DraftStageStatus.FAILED if failed else DraftStageStatus.RAN,
            note=("호출이 실패했다. 빈 필드가 아니라 **못 봤다**로 적는다" if failed else None),
            result=result,
        )

    def _record_guard(self, status: dict[str, FieldStatus], drops: list[GuardDrop]) -> None:
        if self._recorder is None:
            return
        filled = sum(1 for value in status.values() if value is FieldStatus.FILLED)
        self._recorder.stage(
            DraftStage.OUTPUT_GUARD,
            "N2 생성후 가드",
            DraftStageStatus.RAN,
            note="재생성 0회. 걸린 필드만 비우고 사유를 적는다",
            counts={"filled": filled, "drops": len(drops)},
            dropped=[
                DraftDroppedItem(where="N2", reason=drop.kind.value, detail=f"{drop.field}: {drop.detail}")
                for drop in drops
            ],
        )


# --- 순수 함수 ------------------------------------------------------------------------


def _image_mode(raw: str) -> ImageMode:
    """모르는 값은 **가장 안전한 쪽으로** 접는다. 오타가 이미지를 켜지 않아야 한다."""
    try:
        return ImageMode(str(raw).strip().upper())
    except ValueError:
        log.warning("모르는 draft_image_mode 다. OFF 로 접는다. value=%s", raw)
        return ImageMode.OFF


def _should_use_image(mode: ImageMode, signal: ItemSignal, meta: imagemeta.ImageMeta | None) -> bool:
    """``GATED`` 의 비대칭. **게이트가 실패하는 방향이 안전한 쪽이다.**

    게이트가 틀리는 두 방향의 값이 다르다. 잘못 확정해 이미지를 뺐는데 필요했으면 **초안이
    틀리고** 셀러 신뢰를 잃는다. 잘못 보내 불필요했으면 1.43원이다(추정). 그래서 ``GATED``
    는 게이트가 리프를 **확정했을 때만** 이미지를 생략한다 -- 어휘 사전이 쓸모없다고 판명되면
    전건이 확정되지 않아 ``ALWAYS`` 와 같아진다.
    """
    if meta is None or not meta.usable:
        return False
    if mode is ImageMode.ALWAYS:
        return True
    if mode is ImageMode.GATED:
        return signal.pinned_leaf_id is None
    return False


def _basis_map(raw: Any) -> dict[str, Basis]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Basis] = {}
    for key, value in raw.items():
        try:
            out[str(key)] = Basis(str(value))
        except ValueError:
            out[str(key)] = Basis.NONE
    return out


def _usability(raw: Any, *, use_image: bool, meta: imagemeta.ImageMeta | None) -> ImageUsability:
    """사진 사용 가능 여부. **이미지를 안 보냈으면 모델 말을 믿지 않는다.**

    이미지를 싣지 않았는데 모델이 ``USED`` 라고 적는 것은 환각이다. 그 경우 코드가
    ``NO_IMAGE_SENT`` 로 바로잡고, 그러면 ``basis == IMAGE`` 인 필드가 정합성 검사에서
    전부 폐기된다 -- 환각이 초안에 스며들 통로가 닫힌다.
    """
    if not use_image:
        if meta is not None and meta.problem is imagemeta.ImageProblem.TOO_SMALL:
            return ImageUsability.TOO_SMALL
        return ImageUsability.NO_IMAGE_SENT
    try:
        value = ImageUsability(str(raw))
    except ValueError:
        return ImageUsability.UNREADABLE
    if value is ImageUsability.NO_IMAGE_SENT:
        # 보냈는데 안 받았다고 적었다. 사진을 못 읽은 것으로 본다.
        return ImageUsability.UNREADABLE
    return value


def _slots(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value or "") for key, value in raw.items()}


def _option_groups(raw: Any) -> list[Any]:
    from app.schemas.draft import OptionGroupDraft

    if not isinstance(raw, list):
        return []
    groups: list[OptionGroupDraft] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        values = item.get("values")
        groups.append(
            OptionGroupDraft(
                name=str(item.get("name") or ""),
                values=[str(value) for value in values] if isinstance(values, list) else [],
            )
        )
    return groups


def _notice(raw: Any, drops: list[GuardDrop]) -> str | None:
    """셀러 안내 문구도 가드를 지난다. 폼 필드가 아니지만 셀러가 읽는 글자다."""
    text = str(raw or "").strip()
    if not text:
        return None
    verdict = guard.screen_text(text)
    if not verdict.ok:
        assert verdict.kind is not None
        drops.append(GuardDrop(field="sellerNotice", kind=verdict.kind, detail=verdict.detail))
        return None
    return text


def _signal_out(signal: ItemSignal) -> ItemSignalOut:
    return ItemSignalOut(
        grade=signal.grade,
        resolvedBy=signal.resolved_by,
        matchedTerms=list(signal.matched_terms),
        candidateLeafIds=list(signal.candidate_leaf_ids),
        species=signal.species.value if signal.species else None,
    )


def _image_report(
    meta: imagemeta.ImageMeta | None,
    usability: ImageUsability,
    *,
    mode: ImageMode,
    use_image: bool,
) -> ImageReport:
    if meta is None:
        return ImageReport(usability=usability, skippedByMode=mode is ImageMode.OFF)
    return ImageReport(
        usability=usability,
        mediaType=meta.media_type,
        widthPx=meta.width_px,
        heightPx=meta.height_px,
        bytes=meta.bytes_len,
        estimatedTokens=meta.estimated_tokens,
        declaredMismatch=meta.declared_mismatch,
        lowFidelity=meta.low_fidelity,
        rejected=meta.rejected,
        problem=meta.problem.value if meta.problem else None,
        detail=meta.detail,
        maxLongEdgePx=meta.max_long_edge_px,
        skippedByMode=not use_image and meta.usable,
    )


def _field_status_for_skip(reason: SkipReason) -> FieldStatus:
    """0원 분기의 사유를 필드 상태로 옮긴다. **네 갈래를 뭉개지 않는다.**"""
    return {
        SkipReason.NAME_RULE_BLOCKED: FieldStatus.EMPTY_NAME_RULE_BLOCKED,
        SkipReason.ITEM_UNIDENTIFIED: FieldStatus.EMPTY_ITEM_UNIDENTIFIED,
        SkipReason.DRAFT_DISABLED: FieldStatus.EMPTY_NOT_ATTEMPTED,
        SkipReason.LLM_UNAVAILABLE: FieldStatus.EMPTY_CALL_FAILED,
        SkipReason.REUSED_FROM_CACHE: FieldStatus.EMPTY_NOT_ATTEMPTED,
    }[reason]


def _complete(
    status: dict[str, FieldStatus], *, default: FieldStatus = FieldStatus.EMPTY_NOT_ATTEMPTED
) -> dict[str, FieldStatus]:
    """**모든 초안 필드에 상태를 붙인다.** 사유 없이 빈 필드는 응답에 남지 않는다.

    설계가 적어 둔 불변식이고, 테스트가 ``DRAFT_FIELDS`` 전건을 덮는지 단언한다. 필드를
    더하면서 상태를 빠뜨리면 그 필드가 화면에서 "제안할 것이 없었다"로 읽힌다.

    **채우지 않기로 한 필드를 먼저 박는다.** 순서가 뒤집히면 호출이 실패했을 때 가격·재고가
    ``EMPTY_CALL_FAILED`` 로 적혀 "다시 시도하면 채워질 값"으로 읽힌다 -- 그 필드들은 호출이
    성공해도 영원히 비는 값이고, 그래서 사유가 실패에 물들어서는 안 된다.
    """
    out = dict(status)
    for name in _NEVER_ATTEMPTED:
        out.setdefault(name, FieldStatus.EMPTY_NOT_ATTEMPTED)
    for name in DRAFT_FIELDS:
        out.setdefault(name, default)
    return out


def _usage(request_id: str, result: Any) -> DraftCallUsage:
    return DraftCallUsage(
        requestId=request_id,
        callId=result.call_id,
        stage="DRAFT",
        modelId=result.model_id,
        inputTokens=result.input_tokens,
        cacheReadInputTokens=result.cache_read_input_tokens,
        cacheCreationInputTokens=result.cache_creation_input_tokens,
        outputTokens=result.output_tokens,
        costMicroUsd=result.cost_micro_usd,
        latencyMs=result.latency_ms,
        outcome=result.outcome.value,
    )


__all__ = ["DraftService", "DraftUnprocessable"]
