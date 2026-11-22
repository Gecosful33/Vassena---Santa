"""A photo of each house, to tell the houses apart at a glance.

The photos are private like everything else about the houses. They are kept in
the owner's private bucket, beside the workbook of costs (see ``r2.py``): one
file per house, named after the house's id (``santa.jpg``). They never enter the
repository.

The nightly job makes a small picture of each: reduced, re-encoded, and with
none of what a camera writes into a file (where and when it was taken, with
which device). That picture is published inside the encrypted page.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from io import BytesIO
from typing import Any, Mapping

from PIL import ExifTags, Image, ImageCms

MAX_SOURCE_BYTES = 20 * 1024 * 1024  # a phone's photo as it comes out of the phone
MAX_SOURCE_PIXELS = 60_000_000
LONGEST_SIDE = 840  # pixels: enough for a banner across a phone held upright
SHAPE = (3, 2)  # width to height: the tallest shape the page shows a photo in
BUDGET_BYTES = 64 * 1024  # every byte is downloaded with the page, at every visit
MIME = "image/webp"
# What a file may be to be read at all: the formats a phone or a computer saves a photo in.
_SOURCE_FORMATS = {"JPEG", "MPO", "PNG", "WEBP"}
# The names a house's photo is looked for under, after the house's id: phones say
# ".jpeg" as often as ".jpg". The first one found is the photo.
FILE_ENDINGS = (".jpg", ".jpeg", ".png", ".webp")
_QUALITIES = (72, 64, 56, 48)


# The ways a camera may have held the phone, as it notes them in the file, and the turn that undoes each.
_TURNS = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}
# What the files a phone or a computer keeps begin with, for the words of a refusal.
_HEIC_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
_OTHER_PICTURES = {"AVIF", "GIF", "TIFF", "BMP"}
ACCEPTED = "JPEG, PNG o WebP"


class PhotoError(ValueError):
    """The file cannot be made into a photo for the page. The message is safe for a public log."""


def _size(count: int) -> str:
    """How big a file is, in round figures."""
    if count < 1024:
        return f"{count} byte"
    if count < 1024 * 1024:
        return f"{round(count / 1024)} KB"
    return f"{count / (1024 * 1024):.1f} MB".replace(".", ",")


def _kind(raw: bytes) -> str:
    """What a file is, told by the way it begins; "" if that says nothing."""
    head = raw[:32]
    if head.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WebP"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        return "HEIC" if brand in _HEIC_BRANDS else "AVIF" if brand in (b"avif", b"avis") else "un video"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "GIF"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "TIFF"
    if head.startswith(b"BM"):
        return "BMP"
    if head.startswith(b"%PDF"):
        return "un PDF"
    if head.startswith(b"PK\x03\x04"):
        return "un archivio o un documento"
    if head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith((b"<", b"{")):
        return "un testo"  # a page of errors saved in the photo's place, most likely
    return ""


def _refusal(raw: bytes, named: str = "", fault: BaseException | None = None) -> PhotoError:
    """Why a file is no photo for the page, said by what the file is: never by anything it shows.

    ``named`` is the format the decoder took the file for, when it opened it;
    ``fault`` is what stopped the decoding of a file of an accepted format.
    """
    kind, size = _kind(raw) or named, _size(len(raw))
    if not raw:
        return PhotoError("il file è vuoto (0 byte): il caricamento nel bucket non è riuscito, va caricato di nuovo")
    if kind in ("JPEG", "MPO", "PNG", "WebP", "WEBP"):
        why = f"{type(fault).__name__}, " if fault is not None else ""
        return PhotoError(f"il file {kind} è danneggiato o incompleto ({why}{size}): va caricato di nuovo")
    if kind == "HEIC":
        return PhotoError(f"il file è in formato HEIC, quello delle foto di iPhone: va caricato come JPEG ({size})")
    if kind in _OTHER_PICTURES or (kind and kind == named):
        return PhotoError(f"il file è un'immagine {kind}: servono {ACCEPTED}")
    if kind:
        return PhotoError(f"il file non è un'immagine: è {kind} ({size})")
    return PhotoError(f"il file non è un'immagine leggibile ({ACCEPTED}; è di {size})")


@dataclass(frozen=True)
class Photo:
    """A picture ready for the page: small, and carrying nothing but its pixels."""

    data: bytes
    mime: str = MIME

    def to_dict(self) -> dict[str, str]:
        return {"type": self.mime, "data": base64.b64encode(self.data).decode("ascii")}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Photo":
        if raw["type"] != MIME:
            raise ValueError("not a picture this page shows")
        try:
            data = base64.b64decode(raw["data"], validate=True)
        except (binascii.Error, TypeError) as exc:
            raise ValueError("the picture is not base64") from exc
        if not data or len(data) > BUDGET_BYTES:
            raise ValueError("the picture has an impossible size")
        return cls(data=data)


def photos_from_payload(raw: Any) -> dict[str, Photo]:
    """The photos a published page carries, by house; those not as expected are left out."""
    photos: dict[str, Photo] = {}
    if not isinstance(raw, Mapping):
        return photos
    for house, stored in raw.items():
        try:
            photos[str(house)] = Photo.from_dict(stored)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return photos


def encode(image: Image.Image, quality: int) -> bytes:
    """``image`` as WebP. Only the pixels are written: no EXIF, no XMP, no colour profile."""
    buffer = BytesIO()
    image.save(buffer, "WEBP", quality=quality, method=6)
    return buffer.getvalue()


def _to_srgb(image: Image.Image) -> Image.Image:
    """Phones save in a wider colour space and say so in a profile that is not kept: convert first."""
    profile = image.info.get("icc_profile")
    if not profile:
        return image
    try:
        return ImageCms.profileToProfile(
            image, ImageCms.ImageCmsProfile(BytesIO(profile)), ImageCms.createProfile("sRGB"), outputMode="RGB"
        )
    except Exception:  # noqa: BLE001 - an odd profile only costs a slight shift of colours
        return image


def _framed(image: Image.Image, focus: int) -> Image.Image:
    """A picture taller than the page shows it (an upright photo, most of all) cut to the band around ``focus``.

    ``focus`` says how far down the picture its subject is, in percent from the
    top. The page then crops around the middle of what is left.
    """
    width, height = image.size
    band = round(width * SHAPE[1] / SHAPE[0])
    if band >= height:
        return image
    top = max(0, min(height - band, round(height * focus / 100 - band / 2)))
    return image.crop((0, top, width, top + band))


def _upright(source: Image.Image) -> Image.Image:
    """The picture as the phone shows it: turned the way the camera noted when it was taken.

    Only that one note is read. The rest of what the camera wrote is neither
    looked at nor rewritten: none of it is kept, and a note written oddly
    must not cost the photo.
    """
    try:
        held = source.getexif().get(ExifTags.Base.Orientation, 1)
    except Exception:  # noqa: BLE001 - notes that cannot be read say nothing
        held = 1
    source.load()
    turn = _TURNS.get(held)
    return source.transpose(turn) if turn is not None else source.copy()


def prepare(raw: bytes, focus: int = 50) -> Photo:
    """The picture the page shows, made from a photo as its owner saved it.

    ``focus`` is where to cut around, for a photo taller than the page shows
    (see ``_framed``). Raises ``PhotoError`` for a file that is not a photo or
    is beyond reason, saying what the file is instead.
    """
    if len(raw) > MAX_SOURCE_BYTES:
        raise PhotoError(f"file troppo grande (oltre {MAX_SOURCE_BYTES // (1024 * 1024)} MB)")
    try:
        source = Image.open(BytesIO(raw))
    except Exception as exc:  # noqa: BLE001 - whatever the library makes of a file that is not an image
        raise _refusal(raw, fault=exc) from None
    with source:
        if source.format not in _SOURCE_FORMATS:
            raise _refusal(raw, named=str(source.format or ""))
        if source.width * source.height > MAX_SOURCE_PIXELS:
            raise PhotoError(f"immagine troppo grande (oltre {MAX_SOURCE_PIXELS // 1_000_000} megapixel)")
        try:
            image = _upright(source)
            if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
                image = image.convert("RGBA")
                backdrop = Image.new("RGBA", image.size, "white")
                image = Image.alpha_composite(backdrop, image)
            if image.mode not in ("RGB", "RGBA", "L", "CMYK"):
                image = image.convert("RGB")
            image = _to_srgb(image).convert("RGB")
        except Exception as exc:  # noqa: BLE001 - a file cut short, or one the decoder gives up on
            raise _refusal(raw, named=str(source.format or ""), fault=exc) from None

    image = _framed(image, focus)
    image.thumbnail((LONGEST_SIDE, LONGEST_SIDE), Image.LANCZOS)
    while True:
        for quality in _QUALITIES:
            data = encode(image, quality)
            if len(data) <= BUDGET_BYTES:
                return Photo(data)
        # A picture this intricate keeps its look better smaller than coarser.
        smaller = (max(1, round(image.width * 0.85)), max(1, round(image.height * 0.85)))
        if smaller == image.size:
            raise PhotoError("immagine non riducibile")
        image = image.resize(smaller, Image.LANCZOS)
