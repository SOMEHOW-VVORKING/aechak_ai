"""픽스처가 **서로 다른 사진인지** 본다. 디코드하지 않는다.

## 왜 파일 전체 해시로는 안 되는가

자기검사가 ``sha256(파일 전체)`` 였고 그것이 한 청크로 우회됐다. ``demo.draft_cassettes.
synthetic_png`` 가 패딩을 ``tEXt`` 청크에 담으므로, 같은 치수의 합성 PNG 는 ``IHDR`` 과
``IDAT`` 이 **전부 동일**하고 주석만 다르다. 그런 파일 32장을 깔면 파일 전체 해시는 32종으로
갈려 "32/32건 준비됨 / 자기검사 통과" 가 찍히고, 리포트가 "같은 사진을 복사해 두면 자기검사가
막는다" 는 거짓을 말한다. 키를 등재하면 **같은 무늬 한 장을 32번 보낸 결과가 "이미지 이득"**
으로 올라가고, WRONG 등급이 GOOD 과 픽셀 동일한 사진을 받아 "오사진 안전율"이 무의미해진다.
``harness/draft_images/`` 가 gitignore 라 안을 감사할 사람도 없다.

## 무엇을 해시하는가

**메타데이터를 뺀 그림 내용**이다. 디코드하면 Pillow 가 필요하고 그것은
``app/seller_draft/imagemeta.py`` 머리글이 거부한 의존성이다(그 판단은 운영 이미지 무게
때문이고 여기는 하네스지만, 같은 사실을 두 번 적지 않는다 -- 청크만 걸러도 이 공격은 닫힌다).

- PNG: ``IHDR`` + 모든 ``IDAT`` 페이로드. ``tEXt``·``iTXt``·``zTXt``·``tIME``·``eXIf`` 는 뺀다.
  ``IEND`` 뒤의 꼬리 바이트도 무시한다(파일 뒤에 ``\\x00`` 을 붙이는 것이 같은 우회다).
- JPEG: ``SOFn`` 세그먼트 + ``SOS`` 뒤의 엔트로피 데이터. ``APPn``·``COM`` 은 뺀다(EXIF 가
  거기 있다).
- WebP: ``VP8``/``VP8L``/``VP8X`` 청크. ``EXIF``·``XMP``·``ICCP`` 는 뺀다.

## 못 막는 것

같은 사진을 **다시 인코딩한** 파일은 내용 해시가 갈린다. 그것까지 막으려면 픽셀을 풀어
지각 해시를 재야 하고 그것이 Pillow 다. 여기서 막는 것은 "한 장을 복사해 깔았다"이고,
그 사실을 리포트에 그대로 적는다 -- **"서로 다른 사진임을 보장했다"로 적지 않는다.**
"""

from __future__ import annotations

import hashlib
import struct

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# 그림 내용을 들고 있는 청크만 센다. 나머지는 메타데이터다.
_PNG_CONTENT_CHUNKS = frozenset({b"IHDR", b"PLTE", b"IDAT"})
# 치수를 들고 있는 JPEG 프레임 머리. ``imagemeta._JPEG_NON_SOF`` 와 같은 집합을 뺀다.
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
_WEBP_CONTENT_CHUNKS = frozenset({b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ANMF"})


def content_digest(data: bytes) -> str | None:
    """메타데이터를 뺀 그림 내용의 sha256. 모양을 못 읽으면 ``None``."""
    if data.startswith(_PNG_SIGNATURE):
        parts = _png_parts(data)
    elif data.startswith(b"\xff\xd8"):
        parts = _jpeg_parts(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        parts = _webp_parts(data)
    else:
        return None
    if parts is None:
        return None
    digest = hashlib.sha256()
    for part in parts:
        digest.update(struct.pack(">I", len(part)))
        digest.update(part)
    return digest.hexdigest()


def _png_parts(data: bytes) -> list[bytes] | None:
    parts: list[bytes] = []
    index = len(_PNG_SIGNATURE)
    size = len(data)
    while index + 8 <= size:
        (length,) = struct.unpack(">I", data[index : index + 4])
        kind = data[index + 4 : index + 8]
        start = index + 8
        end = start + length
        if end > size:
            return None
        if kind in _PNG_CONTENT_CHUNKS:
            parts.append(kind + data[start:end])
        if kind == b"IEND":
            # 꼬리 바이트는 보지 않는다. 파일 뒤에 붙이는 것이 같은 우회다.
            break
        index = end + 4
    return parts or None


def _jpeg_parts(data: bytes) -> list[bytes] | None:
    parts: list[bytes] = []
    index = 2
    size = len(data)
    while index + 4 <= size:
        if data[index] != 0xFF:
            return None
        marker = data[index + 1]
        if marker == 0xD9:  # EOI
            break
        if marker == 0xDA:  # SOS -- 뒤는 엔트로피 데이터다. 끝까지 센다.
            parts.append(b"SOS" + data[index:])
            break
        (length,) = struct.unpack(">H", data[index + 2 : index + 4])
        end = index + 2 + length
        if end > size:
            return None
        # ``APPn``(0xE0-0xEF)·``COM``(0xFE) 이 메타데이터다. 양자화·허프만 표는 내용이다.
        if marker in _JPEG_SOF or marker in {0xDB, 0xC4}:
            parts.append(bytes([marker]) + data[index + 4 : end])
        index = end
    return parts or None


def _webp_parts(data: bytes) -> list[bytes] | None:
    parts: list[bytes] = []
    index = 12
    size = len(data)
    while index + 8 <= size:
        kind = data[index : index + 4]
        (length,) = struct.unpack("<I", data[index + 4 : index + 8])
        start = index + 8
        end = start + length
        if end > size:
            return None
        if kind in _WEBP_CONTENT_CHUNKS:
            parts.append(kind + data[start:end])
        index = end + (length & 1)
    return parts or None


__all__ = ["content_digest"]
