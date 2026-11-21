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

from PIL import Image, ImageCms, ImageOps

MAX_SOURCE_BYTES = 20 * 1024 * 1024  # a phone's photo as it comes out of the phone
MAX_SOURCE_PIXELS = 60_000_000
LONGEST_SIDE = 840  # pixels: enough for a banner across a phone held upright
BUDGET_BYTES = 64 * 1024  # every byte is downloaded with the page, at every visit
MIME = "image/webp"
# What a file may be to be read at all: the formats a phone or a computer saves a photo in.
_SOURCE_FORMATS = {"JPEG", "MPO", "PNG", "WEBP"}
# The names a house's photo is looked for under, after the house's id: phones say
# ".jpeg" as often as ".jpg". The first one found is the photo.
FILE_ENDINGS = (".jpg", ".jpeg", ".png", ".webp")
_QUALITIES = (72, 64, 56, 48)


class PhotoError(ValueError):
    """The file cannot be made into a photo for the page. The message is safe for a public log."""


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


def prepare(raw: bytes) -> Photo:
    """The picture the page shows, made from a photo as its owner saved it.

    Raises ``PhotoError`` for a file that is not a photo or is beyond reason.
    """
    if len(raw) > MAX_SOURCE_BYTES:
        raise PhotoError(f"file troppo grande (oltre {MAX_SOURCE_BYTES // (1024 * 1024)} MB)")
    unreadable = "il file non è un'immagine leggibile (JPEG, PNG o WebP)"
    try:
        source = Image.open(BytesIO(raw))
    except Exception:  # noqa: BLE001 - whatever the library makes of a file that is not an image
        raise PhotoError(unreadable) from None
    with source:
        if source.format not in _SOURCE_FORMATS:
            raise PhotoError(unreadable)
        if source.width * source.height > MAX_SOURCE_PIXELS:
            raise PhotoError(f"immagine troppo grande (oltre {MAX_SOURCE_PIXELS // 1_000_000} megapixel)")
        try:
            image = ImageOps.exif_transpose(source)  # upright, as the phone shows it
            if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
                image = image.convert("RGBA")
                backdrop = Image.new("RGBA", image.size, "white")
                image = Image.alpha_composite(backdrop, image)
            if image.mode not in ("RGB", "RGBA", "L", "CMYK"):
                image = image.convert("RGB")
            image = _to_srgb(image).convert("RGB")
        except Exception:  # noqa: BLE001 - a file cut short, or one the decoder gives up on
            raise PhotoError(unreadable) from None

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
