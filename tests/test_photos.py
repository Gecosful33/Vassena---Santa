"""The houses' photos: what the nightly job makes of a file. Every picture here is drawn by the test."""

import base64
import random
from io import BytesIO

import pytest
from PIL import ExifTags, Image, ImageCms

from casecal import photos
from casecal.photos import BUDGET_BYTES, LONGEST_SIDE, MIME, Photo, PhotoError, photos_from_payload, prepare

UNREADABLE = "il file non è un'immagine leggibile (JPEG, PNG o WebP)"


def drawn(size=(1200, 800), colours=((200, 60, 40), (40, 90, 200)), mode="RGB") -> Image.Image:
    """Left half one colour, right half another: easy to tell which way up it ends."""
    image = Image.new("RGB", size, colours[0])
    image.paste(colours[1], (size[0] // 2, 0, size[0], size[1]))
    return image.convert(mode)


def saved(image: Image.Image, kind="JPEG", **options) -> bytes:
    buffer = BytesIO()
    image.save(buffer, kind, **options)
    return buffer.getvalue()


def opened(photo: Photo) -> Image.Image:
    image = Image.open(BytesIO(photo.data))
    image.load()
    return image


def near(pixel, colour, slack=40) -> bool:
    return all(abs(a - b) <= slack for a, b in zip(pixel, colour))


def test_a_photo_becomes_a_small_webp_of_the_same_shape():
    photo = prepare(saved(drawn((3000, 2000)), quality=95))
    assert photo.mime == MIME == "image/webp" and len(photo.data) <= BUDGET_BYTES
    made = opened(photo)
    assert made.format == "WEBP" and made.size == (LONGEST_SIDE, LONGEST_SIDE * 2 // 3)
    assert near(made.getpixel((10, 10)), (200, 60, 40)) and near(made.getpixel((made.width - 10, 10)), (40, 90, 200))
    # A picture already small is not blown up.
    assert opened(prepare(saved(drawn((300, 200))))).size == (300, 200)
    # Upright pictures are limited on their height.
    assert opened(prepare(saved(drawn((1500, 2000))))).size == (LONGEST_SIDE * 3 // 4, LONGEST_SIDE)


def test_a_photo_taken_with_the_phone_on_its_side_comes_out_upright():
    # The camera stores the sensor's picture and a note on how to turn it: 6 = a quarter turn clockwise.
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    made = opened(prepare(saved(drawn((800, 400)), exif=exif)))
    assert made.size == (400, 800)
    # What was the left half is now the top half.
    assert near(made.getpixel((200, 100)), (200, 60, 40)) and near(made.getpixel((200, 700)), (40, 90, 200))


def test_nothing_the_camera_wrote_about_the_photo_is_kept():
    exif = Image.Exif()
    exif[ExifTags.Base.Make] = "ACME-CAMERA"
    exif[ExifTags.Base.Model] = "Telefono di Ermenegilda"
    exif[ExifTags.Base.DateTime] = "2026:08:15 10:30:00"
    gps = exif.get_ifd(ExifTags.IFD.GPSInfo)
    gps[ExifTags.GPS.GPSLatitudeRef] = "N"
    gps[ExifTags.GPS.GPSLatitude] = (45.0, 30.0, 15.5)
    gps[ExifTags.GPS.GPSLongitudeRef] = "E"
    gps[ExifTags.GPS.GPSLongitude] = (9.0, 20.0, 40.25)
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    source = saved(drawn(), exif=exif, icc_profile=profile, comment=b"scattata in giardino")
    # The file as the phone saved it does say all of that.
    with Image.open(BytesIO(source)) as original:
        assert original.getexif().get_ifd(ExifTags.IFD.GPSInfo) and original.info.get("icc_profile")
    assert b"ACME-CAMERA" in source and b"Ermenegilda" in source

    photo = prepare(source)
    made = opened(photo)
    assert len(made.getexif()) == 0 and not made.getexif().get_ifd(ExifTags.IFD.GPSInfo)
    assert not {"exif", "xmp", "icc_profile", "comment"} & set(made.info)
    for trace in (b"ACME", b"Ermenegilda", b"2026:08:15", b"giardino", b"Exif", b"ICCP", b"XMP "):
        assert trace not in photo.data


@pytest.mark.parametrize(
    "source",
    [
        saved(drawn(mode="L")),                       # black and white
        saved(drawn(mode="CMYK")),                    # as a printer's file
        saved(drawn(mode="P"), "PNG"),                # a palette
        saved(drawn(), "PNG"),
        saved(drawn(), "WEBP", quality=90),
        saved(drawn(), "MPO"),                        # what some phones call their JPEGs
    ],
    ids=["grey", "cmyk", "palette", "png", "webp", "mpo"],
)
def test_the_formats_a_photo_is_saved_in_are_all_read(source):
    made = opened(prepare(source))
    assert made.format == "WEBP" and made.mode == "RGB" and made.size == (LONGEST_SIDE, 560)


def test_a_see_through_picture_is_laid_on_white():
    image = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
    image.paste((200, 60, 40, 255), (100, 100, 300, 200))
    made = opened(prepare(saved(image, "PNG")))
    assert made.mode == "RGB"
    assert near(made.getpixel((10, 10)), (255, 255, 255), 8) and near(made.getpixel((200, 150)), (200, 60, 40))


def test_a_picture_too_intricate_for_its_budget_is_made_smaller_not_refused():
    noise = random.Random(7)
    image = Image.frombytes("RGB", (1200, 800), bytes(noise.getrandbits(8) for _ in range(1200 * 800 * 3)))
    photo = prepare(saved(image, quality=95))
    made = opened(photo)
    assert len(photo.data) <= BUDGET_BYTES
    assert made.width < LONGEST_SIDE and abs(made.width / made.height - 1.5) < 0.02  # smaller, same shape


@pytest.mark.parametrize(
    "junk",
    [
        b"",
        b"non sono una foto",
        b"PK\x03\x04" + b"\x00" * 64,                    # a workbook put under the wrong name
        saved(drawn(), "GIF"),
        saved(drawn(), "BMP"),
        saved(drawn(), "TIFF"),
        saved(drawn((2000, 1500)), quality=95)[:4000],   # a transfer cut short
        b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64,  # HEIC, as an iPhone may send it
    ],
    ids=["empty", "text", "zip", "gif", "bmp", "tiff", "truncated", "heic"],
)
def test_what_is_not_a_photo_is_refused_in_words_that_say_nothing_of_it(junk):
    with pytest.raises(PhotoError) as caught:
        prepare(junk)
    assert str(caught.value) == UNREADABLE
    assert caught.value.__cause__ is None  # nothing of the file in a traceback either


def test_a_file_beyond_reason_is_refused_before_it_is_decoded(monkeypatch):
    source = saved(drawn((400, 300)))
    monkeypatch.setattr(photos, "MAX_SOURCE_PIXELS", 400 * 300 - 1)
    with pytest.raises(PhotoError, match="immagine troppo grande"):
        prepare(source)
    monkeypatch.setattr(photos, "MAX_SOURCE_PIXELS", 400 * 300)
    assert prepare(source)
    monkeypatch.setattr(photos, "MAX_SOURCE_BYTES", len(source) - 1)
    with pytest.raises(PhotoError, match="file troppo grande"):
        prepare(source)


def test_a_photo_travels_in_the_page_and_comes_back_the_same():
    photo = prepare(saved(drawn()))
    stored = photo.to_dict()
    assert stored["type"] == "image/webp" and base64.b64decode(stored["data"]) == photo.data
    assert Photo.from_dict(stored) == photo
    assert photos_from_payload({"mare": stored, "lago": stored}) == {"mare": photo, "lago": photo}


def test_photos_stored_in_an_unexpected_shape_are_left_out():
    good = prepare(saved(drawn())).to_dict()
    odd = {
        "svg": {"type": "image/svg+xml", "data": good["data"]},   # never a format that can carry a script
        "jpeg": {"type": "image/jpeg", "data": good["data"]},
        "not base64": {"type": MIME, "data": "###"},
        "empty": {"type": MIME, "data": ""},
        "huge": {"type": MIME, "data": base64.b64encode(b"x" * (BUDGET_BYTES + 1)).decode()},
        "no data": {"type": MIME},
        "a string": "foto",
        "nothing": None,
        "good": good,
    }
    assert set(photos_from_payload(odd)) == {"good"}
    for stored in (None, [], "foto", 7, [good]):
        assert photos_from_payload(stored) == {}
