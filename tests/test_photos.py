"""The houses' photos: what the nightly job makes of a file. Every picture here is drawn by the test."""

import base64
import random
from io import BytesIO

import pytest
from PIL import ExifTags, Image, ImageCms

from casecal import photos
from casecal.photos import BUDGET_BYTES, LONGEST_SIDE, MIME, Photo, PhotoError, photos_from_payload, prepare

EMPTY = "il file è vuoto (0 byte): il caricamento nel bucket non è riuscito, va caricato di nuovo"


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
    # One wider than the page shows keeps its shape: the page crops its sides itself.
    assert opened(prepare(saved(drawn((2400, 800))))).size == (LONGEST_SIDE, LONGEST_SIDE // 3)


def striped(size=(600, 1200)) -> Image.Image:
    """An upright picture in three bands: red at the top, green in the middle, blue at the bottom."""
    image = Image.new("RGB", size, (200, 40, 40))
    third = size[1] // 3
    image.paste((40, 160, 60), (0, third, size[0], 2 * third))
    image.paste((40, 60, 200), (0, 2 * third, size[0], size[1]))
    return image


def test_an_upright_photo_is_cut_to_the_pages_shape_around_its_subject():
    red, green, blue = (200, 40, 40), (40, 160, 60), (40, 60, 200)
    source = saved(striped(), "PNG")

    def cut(focus):
        made = opened(prepare(source, focus))
        assert made.size == (600, 400)  # three to two, as wide as it was
        return made.getpixel((300, 10)), made.getpixel((300, 390))

    # Around the middle unless told otherwise; around the point given, in percent from the top.
    assert all(near(pixel, green) for pixel in cut(50))
    assert prepare(source) == prepare(source, 50)
    top, bottom = cut(26)
    assert near(top, red) and near(bottom, green)
    top, bottom = cut(74)
    assert near(top, green) and near(bottom, blue)
    # Never beyond the picture's own edges.
    assert all(near(pixel, red) for pixel in cut(0)) and all(near(pixel, red) for pixel in cut(5))
    assert all(near(pixel, blue) for pixel in cut(100))
    # The cut comes before the reduction: a big upright photo ends as large as a landscape one.
    assert opened(prepare(saved(striped((1500, 2000))))).size == (LONGEST_SIDE, 560)
    # A photo no taller than that shape is left as it is, wherever its subject is said to be.
    assert opened(prepare(saved(drawn((900, 600))), 10)).size == (LONGEST_SIDE, 560)
    assert opened(prepare(saved(drawn((800, 400))), 90)).size == (800, 400)


def test_a_photo_taken_with_the_phone_on_its_side_comes_out_upright():
    # The camera stores the sensor's picture and a note on how to turn it: 6 = a quarter turn clockwise.
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    source = saved(drawn((800, 400)), exif=exif)
    # Turned, it is 400 wide and 800 tall, and what was the left half is now the top half:
    # cut around its top it is all of the first colour, around its bottom all of the second.
    top, bottom = opened(prepare(source, 0)), opened(prepare(source, 100))
    assert top.size == bottom.size == (400, 267)
    assert near(top.getpixel((200, 20)), (200, 60, 40)) and near(top.getpixel((200, 250)), (200, 60, 40))
    assert near(bottom.getpixel((200, 20)), (40, 90, 200)) and near(bottom.getpixel((200, 250)), (40, 90, 200))


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


ERROR_PAGE = b'<?xml version="1.0" encoding="UTF-8"?><Error><Code>NoSuchKey</Code><Key>villa al lago.jpg</Key></Error>'


@pytest.mark.parametrize(
    "junk, reason",
    [
        # An upload that left nothing behind: the name is in the bucket, the photo is not.
        (b"", EMPTY),
        (b"non sono una foto", "il file non è un'immagine leggibile (JPEG, PNG o WebP; è di 17 byte)"),
        (b"\x00" * 3000, "il file non è un'immagine leggibile (JPEG, PNG o WebP; è di 3 KB)"),
        # A workbook put under the wrong name; a page of errors saved in the photo's place.
        (b"PK\x03\x04" + b"\x00" * 64, "il file non è un'immagine: è un archivio o un documento (68 byte)"),
        (ERROR_PAGE, f"il file non è un'immagine: è un testo ({len(ERROR_PAGE)} byte)"),
        (b"  {\"errore\": \"villa al lago\"}", "il file non è un'immagine: è un testo (29 byte)"),
        (b"%PDF-1.7\n" + b"\x00" * 2000, "il file non è un'immagine: è un PDF (2 KB)"),
        (b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00qt  " + b"\x00" * 3_000_000, "il file non è un'immagine: è un video (2,9 MB)"),
        # Pictures, in a format the page is not made from.
        (saved(drawn(), "GIF"), "il file è un'immagine GIF: servono JPEG, PNG o WebP"),
        (saved(drawn(), "BMP"), "il file è un'immagine BMP: servono JPEG, PNG o WebP"),
        (saved(drawn(), "TIFF"), "il file è un'immagine TIFF: servono JPEG, PNG o WebP"),
        (saved(drawn(), "ICO"), "il file è un'immagine ICO: servono JPEG, PNG o WebP"),
        # HEIC, as an iPhone keeps its photos and may send them, whatever the name says.
        (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64,
         "il file è in formato HEIC, quello delle foto di iPhone: va caricato come JPEG (88 byte)"),
        (b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00mif1heic" + b"\x00" * 2_500_000,
         "il file è in formato HEIC, quello delle foto di iPhone: va caricato come JPEG (2,4 MB)"),
        # A photo of a format the page takes, cut short on its way or spoiled.
        (saved(drawn((2000, 1500)), quality=95)[:4000], "il file JPEG è danneggiato o incompleto (OSError, 4 KB): va caricato di nuovo"),
        (saved(drawn((2000, 1500)), "PNG")[:700], "il file PNG è danneggiato o incompleto (OSError, 700 byte): va caricato di nuovo"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 40, "il file JPEG è danneggiato o incompleto (UnidentifiedImageError, 44 byte): va caricato di nuovo"),
    ],
    ids=["empty", "text", "zeros", "zip", "error page", "json", "pdf", "video", "gif", "bmp", "tiff", "ico",
         "heic", "heic whole", "jpeg cut short", "png cut short", "jpeg spoiled"],
)
def test_what_is_not_a_photo_is_refused_by_saying_what_the_file_is_and_nothing_of_what_is_in_it(junk, reason):
    with pytest.raises(PhotoError) as caught:
        prepare(junk)
    assert str(caught.value) == reason
    assert "villa" not in str(caught.value) and "lago" not in str(caught.value)
    assert caught.value.__cause__ is None  # nothing of the file in a traceback either


def test_notes_of_the_camera_written_oddly_do_not_cost_the_photo():
    # Only the way the phone was held is read from them; they are never written again.
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 8
    exif[ExifTags.Base.Make] = "ACME-CAMERA"
    source = saved(drawn((800, 400)), exif=exif)
    marker = source.index(b"ACME-CAMERA")
    # The same file with its notes spoiled past the point where the orientation is read.
    spoiled = source[:marker] + b"\xff" * 11 + source[marker + 11:]
    for raw in (source, spoiled):
        made = opened(prepare(raw, 0))
        assert made.size == (400, 267) and near(made.getpixel((200, 20)), (40, 90, 200))  # a quarter turn the other way
    # Notes that say nothing readable at all: the picture is taken as it is stored.
    no_notes = saved(drawn((800, 400)), exif=b"Exif\x00\x00" + b"\x00" * 40)
    assert opened(prepare(no_notes)).size == (800, 400)
    # An orientation no camera writes.
    exif[ExifTags.Base.Orientation] = 42
    assert opened(prepare(saved(drawn((800, 400)), exif=exif))).size == (800, 400)


def test_a_phone_photo_with_a_second_picture_inside_is_read_as_its_first():
    # What a phone saves for a photo with depth or brightness data: two pictures in one file,
    # the notes of the camera, the profile of its screen. The page is made from the first.
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    buffer = BytesIO()
    drawn((1600, 800)).save(buffer, "MPO", save_all=True, append_images=[Image.new("L", (400, 200), 128)],
                            exif=exif, icc_profile=profile, quality=90)
    source = buffer.getvalue()
    with Image.open(BytesIO(source)) as original:
        assert original.format == "MPO" and original.n_frames == 2
    made = opened(prepare(source, 0))
    assert made.format == "WEBP" and made.size == (800, 533)
    assert near(made.getpixel((400, 20)), (200, 60, 40)) and near(made.getpixel((400, 500)), (200, 60, 40))


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
