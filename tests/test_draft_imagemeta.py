"""이미지 헤더 선검사. **stdlib ``struct`` 만 쓰고 새 의존성이 0이다.**

선언 치수를 믿지 않는 것이 이 모듈의 핵심이다 -- 선언만 믿으면 실제로는 2576 사방인 사진이
896 이라고 적혀 들어와 **예산이 조용히 세 배가 된다.**
"""

from __future__ import annotations

import struct
import zlib

from app.seller_draft import imagemeta
from app.seller_draft.imagemeta import ImageProblem
from demo.draft_cassettes import synthetic_png


def _jpeg(width: int, height: int, *, pad: int = 0) -> bytes:
    """SOF0 마커 하나를 든 최소 JPEG. 앞에 APP0 세그먼트를 둬 건너뛰기를 태운다."""
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof0 = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x03"
        + b"\x00" * 9
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xda" + struct.pack(">H", 2) + b"d" * pad


def _webp_vp8x(width: int, height: int) -> bytes:
    body = b"WEBP" + b"VP8X" + struct.pack("<I", 10) + b"\x00" * 4
    body += (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", len(body)) + body


def _png_bad_signature() -> bytes:
    ihdr = struct.pack(">IIBBBBB", 10, 10, 8, 2, 0, 0, 0)
    return b"\x00NOT\r\n\x1a\n" + struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", 0)


def test_png_치수를_읽는다() -> None:
    assert imagemeta.read_dimensions(synthetic_png(896, 512)) == ("image/png", 896, 512)


def test_jpeg_치수를_읽는다() -> None:
    """세그먼트 길이를 따라 건너뛰어야 SOF 를 찾는다."""
    assert imagemeta.read_dimensions(_jpeg(1024, 768)) == ("image/jpeg", 1024, 768)


def test_webp_치수를_읽는다() -> None:
    assert imagemeta.read_dimensions(_webp_vp8x(640, 480)) == ("image/webp", 640, 480)


def test_매직_바이트가_아니면_못_읽는다() -> None:
    """선언된 미디어 타입을 쓰지 않고 **매직 바이트로 다시 본다.**"""
    assert imagemeta.read_dimensions(_png_bad_signature()) is None
    assert imagemeta.read_dimensions(b"") is None
    assert imagemeta.read_dimensions(b"not an image at all") is None


def test_선언_치수가_어긋나면_실측값으로_예산을_계산한다() -> None:
    """**선언만 믿으면 예산이 조용히 세 배가 된다.**"""
    meta = imagemeta.parse(
        synthetic_png(1024, 1024),
        declared_media_type="image/png",
        declared_width_px=512,
        declared_height_px=512,
        max_tokens=4000,
    )

    assert meta.declared_mismatch is True
    assert (meta.width_px, meta.height_px) == (1024, 1024)
    assert meta.estimated_tokens == imagemeta.estimate_tokens(1024, 1024)


def test_토큰_예산을_넘기면_거부하고_필요_치수를_적어_준다() -> None:
    """**AI 서버는 리사이즈하지 않는다**(의존성 금지). 셀러센터가 줄여 보내야 한다."""
    meta = imagemeta.parse(
        synthetic_png(1500, 1500),
        declared_media_type="image/png",
        max_tokens=1400,
    )

    assert meta.problem is ImageProblem.TOKEN_BUDGET_EXCEEDED
    assert meta.rejected is True
    assert meta.max_long_edge_px == imagemeta.max_long_edge_for(1400)
    assert "리사이즈하지 않는다" in (meta.detail or "")
    # 역산한 긴 변이 실제로 예산 안에 들어와야 한다. 아니면 셀러가 줄여도 또 거부된다.
    limit = meta.max_long_edge_px or 0
    assert imagemeta.estimate_tokens(limit, limit) <= 1400


def test_너무_작으면_거부하지_않고_이미지_없이_진행한다() -> None:
    """**거부와 "빼고 진행"을 섞지 않는다.** 텍스트만으로도 초안이 나온다."""
    meta = imagemeta.parse(
        synthetic_png(128, 128),
        declared_media_type="image/png",
        max_tokens=4000,
    )

    assert meta.problem is ImageProblem.TOO_SMALL
    assert meta.rejected is False
    assert meta.usable is False


def test_미지원_타입은_거부한다() -> None:
    """셀러센터가 고쳐 보낼 수 있는 입력 오류다."""
    gif = b"GIF89a" + b"\x00" * 20
    meta = imagemeta.parse(gif, declared_media_type="image/gif", max_tokens=4000)

    # 매직 바이트를 못 읽으므로 헤더 불가로 떨어진다 -- 이미지 없이 진행한다.
    assert meta.problem is ImageProblem.UNREADABLE_HEADER
    assert meta.rejected is False


def test_해상도_대비_용량이_작으면_힌트만_올린다() -> None:
    """``bytes/pixel`` 은 블러·과압축의 **싼 대리 지표**이고 차단이 아니다."""
    thin = imagemeta.parse(
        _jpeg(1024, 1024, pad=1_000),
        declared_media_type="image/jpeg",
        max_tokens=4000,
    )

    assert thin.low_fidelity is True
    assert thin.problem is None, "저품질은 차단이 아니다"
    assert thin.bytes_per_pixel < imagemeta.LOW_FIDELITY_BYTES_PER_PIXEL


def test_충분한_용량은_저품질로_읽지_않는다() -> None:
    fat = imagemeta.parse(synthetic_png(896, 896), declared_media_type="image/png", max_tokens=4000)

    assert fat.low_fidelity is False
    assert fat.usable is True
    assert fat.problem is None


def test_토큰_추정이_면적_비례다() -> None:
    """**추정이고 실측이 아니다.** ``--count-image-tokens`` 가 치환한다."""
    assert imagemeta.estimate_tokens(896, 896) == (896 * 896) // imagemeta.TOKENS_PER_PIXEL_DIVISOR
    assert imagemeta.estimate_tokens(0, 100) == 0
    assert imagemeta.estimate_tokens(-1, -1) == 0
    # 면적이 네 배면 토큰도 네 배 근처다.
    assert abs(imagemeta.estimate_tokens(1024, 1024) / imagemeta.estimate_tokens(512, 512) - 4) < 0.01


def test_긴_변_역산이_추정식의_역함수다() -> None:
    for budget in (500, 1400, 4784):
        edge = imagemeta.max_long_edge_for(budget)
        assert imagemeta.estimate_tokens(edge, edge) <= budget
        assert imagemeta.estimate_tokens(edge + 2, edge + 2) > budget
    assert imagemeta.max_long_edge_for(0) == 0


def test_지원_타입이_셋이다() -> None:
    """Vision 이 받는 것과 헤더를 stdlib 로 읽을 수 있는 것의 교집합이다."""
    assert imagemeta.SUPPORTED_MEDIA_TYPES == ("image/jpeg", "image/png", "image/webp")


def test_합성_png_가_구조적으로_유효하다() -> None:
    """데모 카세트가 쓰는 합성 헤더가 실제 파싱 경로를 태우는지.

    유효하지 않으면 재생 시나리오가 전건 ``UNREADABLE_HEADER`` 로 떨어지고, 콘솔이
    "사진을 못 읽었다"를 **설계 판단이 아니라 픽스처 고장 때문에** 그린다.
    """
    data = synthetic_png(896, 896)

    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert zlib.crc32(b"IHDR" + data[16:29]) & 0xFFFFFFFF == struct.unpack(">I", data[29:33])[0]
    assert imagemeta.read_dimensions(data) == ("image/png", 896, 896)
