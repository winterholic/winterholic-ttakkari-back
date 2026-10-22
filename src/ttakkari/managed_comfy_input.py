"""managed Comfy I2V의 기준 이미지 입력 검증.

임의 경로·외부 URL은 받지 않는다. 호출 쪽(API·creative 어댑터)이 소유한 자산의 바이트와 선언된 id·sha256을 넘기면,
이 모듈이 sha256 일치·크기·이미지 형식·애니메이션 여부·해상도·파일명 규칙을 확인해 `InputImage`로 봉인한다.
컨트롤러는 `InputImage`만 받으며, 검증을 통과하지 못한 바이트는 lease 획득 전에 거부된다.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import re
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

MAX_BYTES = 25 * 1024 * 1024
MIN_SIDE, MAX_SIDE = 64, 4096
MAX_PIXELS = 36_000_000
ASSET_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}\.(png|jpg|jpeg|webp)$")
_FORMATS = {"PNG": {"png"}, "JPEG": {"jpg", "jpeg"}, "WEBP": {"webp"}}
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class InputImageError(ValueError):
    """기준 이미지가 계약을 어겼다. 메시지는 사용자에게 그대로 보여도 된다."""


@dataclass(frozen=True)
class InputImage:
    asset_id: str
    sha256: str
    data: bytes
    format: str
    width: int
    height: int

    @property
    def upload_name(self) -> str:
        # 자산 id는 위 정규식을 통과한 단일 파일명이다. Comfy input/ttk/ 아래에 이 이름으로 올라간다.
        return self.asset_id

    def public(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "sha256": self.sha256,
            "bytes": len(self.data),
            "format": self.format,
            "width": self.width,
            "height": self.height,
            "upload_name": self.upload_name,
        }


def validate_input_image(*, asset_id: str, sha256: str, data: bytes) -> InputImage:
    if not isinstance(asset_id, str) or not ASSET_ID.fullmatch(asset_id):
        raise InputImageError(
            "기준 이미지 id는 영문·숫자·._- 로 된 png/jpg/jpeg/webp 파일명이어야 합니다."
        )
    if not isinstance(sha256, str) or not SHA256.fullmatch(sha256):
        raise InputImageError("기준 이미지 sha256이 잘못됐습니다.")
    if not isinstance(data, bytes | bytearray) or not data:
        raise InputImageError("기준 이미지가 비어 있습니다.")
    if len(data) > MAX_BYTES:
        raise InputImageError("기준 이미지는 25MiB 이하여야 합니다.")
    actual = hashlib.sha256(data).hexdigest()
    if not hmac.compare_digest(actual, sha256):
        raise InputImageError("기준 이미지 바이트의 sha256이 선언된 값과 다릅니다.")
    data = bytes(data)
    try:
        with Image.open(io.BytesIO(data)) as probe:
            kind = probe.format
            width, height = probe.size
            frames = getattr(probe, "n_frames", 1)
            if kind not in _FORMATS:
                raise InputImageError("기준 이미지는 PNG, JPEG, WebP만 허용합니다.")
            if frames != 1 or getattr(probe, "is_animated", False):
                raise InputImageError(
                    "애니메이션 이미지는 기준 이미지로 쓸 수 없습니다."
                )
            if (
                not (MIN_SIDE <= width <= MAX_SIDE and MIN_SIDE <= height <= MAX_SIDE)
                or width * height > MAX_PIXELS
            ):
                raise InputImageError(
                    f"기준 이미지 해상도는 {MIN_SIDE}~{MAX_SIDE}px 범위여야 합니다."
                )
            suffix = asset_id.rsplit(".", 1)[1].lower()
            if suffix not in _FORMATS[kind]:
                raise InputImageError("기준 이미지 확장자가 실제 형식과 다릅니다.")
        with Image.open(io.BytesIO(data)) as full:
            full.load()  # 잘린 파일은 여기서 걸린다.
    except InputImageError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        Image.DecompressionBombError,
    ) as exc:
        raise InputImageError("이미지로 읽을 수 없거나 손상된 파일입니다.") from exc
    return InputImage(
        asset_id=asset_id,
        sha256=sha256,
        data=data,
        format=kind,
        width=width,
        height=height,
    )
