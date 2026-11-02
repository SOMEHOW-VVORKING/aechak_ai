"""이미지 헤더에서 치수와 바이트를 읽는다. **stdlib ``struct`` 만 쓴다. 새 의존성 0.**

## 왜 Pillow 를 들이지 않는가

``pyproject.toml`` 의 ``project.dependencies`` 는 테스트가 **집합 등호로** 고정하고 있고,
``Dockerfile`` 이 ``COPY app`` 을 하므로 여기 import 하는 것은 전부 운영 이미지에 실린다.
Pillow 를 들이면 1500 사방을 896 으로 줄여 건당 2.59원을 아낄 수 있지만(추정), 그 절감을
운영 이미지 무게와 바꾸지 않는다. **축소는 셀러센터가 하고, AI 서버는 토큰 예산 초과를
거부하면서 필요한 긴 변 길이를 응답에 적어 돌려준다** -- 의존성 0으로 같은 보호를 받는다.

## 왜 디코드하지 않고 헤더만 읽는가

필요한 것은 "토큰 예산을 넘기는가"와 "읽을 수 있을 만한 사진인가" 둘이고, 둘 다 치수와
바이트 수로 판단된다. 픽셀을 풀면 디코더가 필요하고 그것이 곧 Pillow 다. 그리고 셀러가
보낸 **선언 치수를 믿지 않는다** -- 선언만 믿으면 실제로는 2576 사방인 사진이 896 이라고
적혀 들어와 예산이 조용히 세 배가 된다.

## 못 하는 것

- 사진 안에 적힌 글자를 읽지 못한다. 그래서 **이미지는 ``rules.screen`` 이 보지 못하는
  주입 표면이고**, 막는 지점은 생성 결과가 텍스트 가드를 지날 때뿐이다. 그 한계를 지우려
  하지 않고 적어 둔다.
- 블러·노출을 측정하지 못한다. ``bytes/pixel`` 이 그 싼 대리 지표다 -- 1024 사방 JPEG 이
  8KB 면 거의 확실히 읽을 수 없다. 차단이 아니라 봉투에 적는 힌트로만 쓴다.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import StrEnum

# 받는 미디어 타입. Anthropic Vision 이 받는 것과 헤더를 stdlib 로 읽을 수 있는 것의 교집합이다.
SUPPORTED_MEDIA_TYPES: tuple[str, ...] = ("image/jpeg", "image/png", "image/webp")

# 이미지 토큰 추정식의 분모. 공식 문서의 패치 28x28 = 1 토큰에서 나온 수다.
# **추정이고 실측이 아니다.** ``harness/run_draft.py --count-image-tokens`` 가 키를 받아
# ``messages.count_tokens`` 로 치환한다(유료 호출 0회).
TOKENS_PER_PIXEL_DIVISOR = 784

# 이 아래로 작아지면 모델이 읽을 것이 남지 않는다. 차단이 아니라 이미지를 빼고 진행한다.
MIN_LONG_EDGE_PX = 256

# 압축·노출 불량의 싼 대리 지표. 차단하지 않고 봉투에 힌트로 적는다.
LOW_FIDELITY_BYTES_PER_PIXEL = 0.05


class ImageProblem(StrEnum):
    """헤더 선검사의 결과. **거부와 "빼고 진행"을 섞지 않는다.**

    ``UNSUPPORTED_MEDIA_TYPE`` 과 ``TOKEN_BUDGET_EXCEEDED`` 는 요청을 거부한다 -- 셀러센터가
    고쳐 보낼 수 있는 입력 오류이고, 조용히 이미지를 버리면 셀러는 자기 사진이 쓰인 줄 안다.
    ``TOO_SMALL`` 과 ``UNREADABLE_HEADER`` 는 이미지 없이 진행한다 -- 텍스트만으로도 초안이
    나오므로 전체를 실패시키는 것이 과한 반응이다.
    """

    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    UNREADABLE_HEADER = "UNREADABLE_HEADER"
    TOKEN_BUDGET_EXCEEDED = "TOKEN_BUDGET_EXCEEDED"
    TOO_SMALL = "TOO_SMALL"


@dataclass(frozen=True, slots=True)
class ImageMeta:
    media_type: str
    width_px: int
    height_px: int
    bytes_len: int
    estimated_tokens: int
    low_fidelity: bool
    problem: ImageProblem | None
    detail: str | None = None
    # 예산을 넘겼을 때 셀러센터가 줄여야 하는 긴 변. 거부 응답에 적어 돌려준다.
    max_long_edge_px: int | None = None

    @property
    def rejected(self) -> bool:
        return self.problem in (
            ImageProblem.UNSUPPORTED_MEDIA_TYPE,
            ImageProblem.TOKEN_BUDGET_EXCEEDED,
        )

    @property
    def usable(self) -> bool:
        return self.problem is None


def estimate_tokens(width: int, height: int) -> int:
    """치수에서 입력 토큰을 추정한다. **추정이다.**"""
    if width <= 0 or height <= 0:
        return 0
    return max(1, (width * height) // TOKENS_PER_PIXEL_DIVISOR)


def max_long_edge_for(max_tokens: int) -> int:
    """토큰 예산에서 정사방 기준 긴 변 상한을 역산한다. 거부 응답에 적어 돌려주는 수다."""
    if max_tokens <= 0:
        return 0
    return int((max_tokens * TOKENS_PER_PIXEL_DIVISOR) ** 0.5)


def read_dimensions(data: bytes) -> tuple[str, int, int] | None:
    """바이트 머리에서 ``(미디어 타입, 가로, 세로)`` 를 읽는다. 못 읽으면 ``None``.

    선언된 미디어 타입을 쓰지 않고 **매직 바이트로 다시 본다.** 확장자나 선언만 믿으면
    ``image/png`` 라고 적힌 JPEG 이 들어와 치수가 엉뚱하게 읽히고 예산이 틀어진다.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _png(data)
    if data.startswith(b"\xff\xd8"):
        return _jpeg(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp(data)
    return None


def _png(data: bytes) -> tuple[str, int, int] | None:
    # 서명 8 + 길이 4 + "IHDR" 4 뒤에 가로·세로가 빅엔디안 uint32 로 온다.
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return ("image/png", width, height)


# 치수를 들고 있지 않은 마커. SOF 로 착각하면 엉뚱한 바이트를 치수로 읽는다.
_JPEG_NON_SOF = {0xC4, 0xC8, 0xCC}


def _jpeg(data: bytes) -> tuple[str, int, int] | None:
    """SOFn 마커를 찾아 치수를 읽는다. 세그먼트 길이를 따라 건너뛴다."""
    index = 2
    size = len(data)
    while index + 3 < size:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xFF:
            index += 1
            continue
        (seg_len,) = struct.unpack(">H", data[index + 2 : index + 4])
        if 0xC0 <= marker <= 0xCF and marker not in _JPEG_NON_SOF:
            if index + 9 > size:
                return None
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return ("image/jpeg", width, height)
        if seg_len < 2:
            return None
        index += 2 + seg_len
    return None


def _webp(data: bytes) -> tuple[str, int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        # 24비트 리틀엔디안으로 (치수 - 1) 이 들어 있다.
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return ("image/webp", width, height)
    if chunk == b"VP8 " and len(data) >= 30:
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return ("image/webp", width, height)
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return ("image/webp", width, height)
    return None


def parse(
    data: bytes,
    *,
    declared_media_type: str,
    max_tokens: int,
) -> ImageMeta:
    """헤더 선검사 전부. **0원이고 네트워크를 타지 않는다.**

    **선언 치수를 보지 않는다.** 매직 바이트로 읽은 실측값으로만 예산을 계산하고, 그것이
    "896 이라고 적고 2576 을 보내는" 경로를 막는다.
    """
    read = read_dimensions(data)
    if read is None:
        return ImageMeta(
            media_type=declared_media_type,
            width_px=0,
            height_px=0,
            bytes_len=len(data),
            estimated_tokens=0,
            low_fidelity=False,
            problem=ImageProblem.UNREADABLE_HEADER,
            detail="헤더에서 치수를 읽을 수 없다. 이미지 없이 진행한다",
        )

    media_type, width, height = read
    if media_type not in SUPPORTED_MEDIA_TYPES:
        return ImageMeta(
            media_type=media_type,
            width_px=width,
            height_px=height,
            bytes_len=len(data),
            estimated_tokens=0,
            low_fidelity=False,
            problem=ImageProblem.UNSUPPORTED_MEDIA_TYPE,
            detail=f"받지 않는 미디어 타입이다: {media_type}",
        )

    tokens = estimate_tokens(width, height)
    pixels = width * height
    low = bool(pixels) and (len(data) / pixels) < LOW_FIDELITY_BYTES_PER_PIXEL

    if tokens > max_tokens:
        limit = max_long_edge_for(max_tokens)
        return ImageMeta(
            media_type=media_type,
            width_px=width,
            height_px=height,
            bytes_len=len(data),
            estimated_tokens=tokens,
            low_fidelity=low,
            problem=ImageProblem.TOKEN_BUDGET_EXCEEDED,
            detail=(
                f"{width}x{height} 은 약 {tokens} 토큰으로 예산 {max_tokens} 을 넘긴다. "
                f"긴 변을 {limit}px 이하로 줄여 보내라. AI 서버는 리사이즈하지 않는다"
            ),
            max_long_edge_px=limit,
        )

    if max(width, height) < MIN_LONG_EDGE_PX:
        return ImageMeta(
            media_type=media_type,
            width_px=width,
            height_px=height,
            bytes_len=len(data),
            estimated_tokens=tokens,
            low_fidelity=low,
            problem=ImageProblem.TOO_SMALL,
            detail=f"긴 변이 {MIN_LONG_EDGE_PX}px 에 못 미친다. 이미지 없이 진행한다",
        )

    return ImageMeta(
        media_type=media_type,
        width_px=width,
        height_px=height,
        bytes_len=len(data),
        estimated_tokens=tokens,
        low_fidelity=low,
        problem=None,
    )


__all__ = [
    "LOW_FIDELITY_BYTES_PER_PIXEL",
    "MIN_LONG_EDGE_PX",
    "SUPPORTED_MEDIA_TYPES",
    "TOKENS_PER_PIXEL_DIVISOR",
    "ImageMeta",
    "ImageProblem",
    "estimate_tokens",
    "max_long_edge_for",
    "parse",
    "read_dimensions",
]
