"""폰에서 올린 사진 손질(아이폰 기준): HEIC → JPG, 방향 바로잡기, 위치 정보 지우기, 뻔한 이름 바꾸기."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pillow_heif
from PIL import Image, ImageOps, UnidentifiedImageError

pillow_heif.register_heif_opener()

HEIF_EXT = {".heic", ".heif", ".hif"}
JPEG_EXT = {".jpg", ".jpeg"}
# 아이폰 사파리는 사진 이름을 image.jpg 처럼 뭉개 보낸다. 이런 이름은 촬영 시각으로 바꾼다.
GENERIC_STEM = re.compile(r"^(image|photo|img|picture|video|movie|file|upload|blob|untitled)(\s*\(\d+\)|[-_ ]?\d{0,3})?$", re.IGNORECASE)
EXIF_DATETIME_ORIGINAL = 0x9003
EXIF_DATETIME = 0x0132
EXIF_IFD = 0x8769
GPS_IFD = 0x8825
ORIENTATION = 0x0112


@dataclass
class Processed:
    """손질 결과. path 는 최종 이름 후보(아직 목적지 아님), notes 는 사용자에게 보일 한 줄들."""

    path: Path
    extra: list[Path] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def is_heif(path: Path) -> bool:
    if path.suffix.lower() in HEIF_EXT:
        return True
    try:
        head = path.read_bytes()[:32]
    except OSError:
        return False
    # ISO BMFF ftyp 상자의 브랜드로 판별한다(확장자 없이 오는 경우).
    return head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1")


def taken_at(img: Image.Image) -> datetime | None:
    exif = img.getexif()
    raw = exif.get_ifd(EXIF_IFD).get(EXIF_DATETIME_ORIGINAL) or exif.get(EXIF_DATETIME)
    if not isinstance(raw, str):
        return None
    try:
        # EXIF 촬영 시각은 시간대 없는 현지 시각이다. 파일 이름에만 쓰므로 그대로 둔다.
        return datetime.strptime(raw.strip()[:19], "%Y:%m:%d %H:%M:%S")  # noqa: DTZ007
    except ValueError:
        return None


def friendly_stem(stem: str, when: datetime | None, prefix: str = "IMG") -> str:
    if stem and not GENERIC_STEM.match(stem):
        return stem
    return f"{prefix}_{(when or datetime.now().astimezone()).strftime('%Y%m%d_%H%M%S')}"


def _clean_exif(img: Image.Image, strip_location: bool, *, transposed: bool) -> Image.Exif:
    exif = img.getexif()
    # 픽셀을 돌려 저장했으면 방향 태그는 1(정방향)로 둔다. 남기면 뷰어가 한 번 더 돌린다.
    # 픽셀을 그대로 두는 경우(메타데이터만 다시 쓰기)는 태그를 건드리면 사진이 눕는다.
    if transposed and ORIENTATION in exif:
        exif[ORIENTATION] = 1
    if strip_location and GPS_IFD in exif:
        del exif[GPS_IFD]
    return exif


def process_image(src: Path, name: str, *, convert_heic: bool, strip_location: bool, keep_original: bool) -> Processed:
    """src(임시 파일)를 손질해 같은 폴더에 결과를 만든다. 사진이 아니면 이름만 다듬는다."""
    stem, ext = Path(name).stem, Path(name).suffix.lower()
    heif = is_heif(src)
    try:
        with Image.open(src) as img:
            when = taken_at(img)
            # getexif() 는 캐시된 객체라 지우기 전에 위치 정보 유무를 먼저 본다.
            had_gps = bool(img.getexif().get_ifd(GPS_IFD))
            if heif and convert_heic:
                out = src.with_name(friendly_stem(stem, when) + ".jpg")
                exif = _clean_exif(img, strip_location, transposed=True)
                fixed = ImageOps.exif_transpose(img).convert("RGB")
                fixed.save(out, "JPEG", quality=92, exif=exif.tobytes(), optimize=True)
                res = Processed(path=out, notes=["HEIC 사진을 JPG로 바꿨어요"])
                if keep_original:
                    orig = src.with_name(out.stem + (ext if ext in HEIF_EXT else ".heic"))
                    src.rename(orig)
                    res.extra.append(orig)
                    res.notes.append("원본 HEIC도 같이 남겼어요")
                if strip_location and had_gps:
                    res.notes.append("위치 정보를 지웠어요")
                return res
            final = src.with_name(friendly_stem(stem, when) + (ext or _ext_for(img.format)))
            notes: list[str] = []
            if strip_location and had_gps and img.format in ("JPEG", "PNG", "TIFF", "WEBP", "HEIF"):
                exif = _clean_exif(img, strip_location=True, transposed=False)
                kw = {"quality": "keep"} if img.format == "JPEG" else {}
                # JPEG 은 quality="keep" 으로 양자화 표를 그대로 써 화질 손실 없이 메타데이터만 다시 쓴다.
                tmp = src.with_name(src.name + ".tmp")
                img.save(tmp, img.format, exif=exif.tobytes(), **kw)
                tmp.replace(final)
                if final != src:
                    src.unlink(missing_ok=True)
                notes.append("위치 정보를 지웠어요")
            elif final != src:
                src.rename(final)
            return Processed(path=final, notes=notes)
    except (UnidentifiedImageError, OSError, ValueError):
        pass
    # 사진이 아니거나 못 읽는 파일: 이름만 다듬는다(동영상은 VID_ 접두사).
    prefix = "VID" if ext in {".mov", ".mp4", ".m4v", ".3gp"} else "FILE"
    final = src.with_name(friendly_stem(stem, None, prefix) + ext)
    if final != src:
        src.rename(final)
    return Processed(path=final)


def _ext_for(fmt: str | None) -> str:
    return {"JPEG": ".jpg", "PNG": ".png", "HEIF": ".heic", "GIF": ".gif", "WEBP": ".webp", "TIFF": ".tif"}.get(fmt or "", "")


def unique_dest(folder: Path, name: str) -> Path:
    """같은 이름이 있으면 '이름 (2).확장자' 처럼 비어 있는 이름을 찾는다."""
    target = folder / name
    stem, ext = Path(name).stem, Path(name).suffix
    n = 2
    while target.exists():
        target = folder / f"{stem} ({n}){ext}"
        n += 1
    return target
