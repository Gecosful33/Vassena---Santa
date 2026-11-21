"""Invented pictures, drawn on the spot: what a test hands over where a photo is expected."""

from io import BytesIO

from PIL import Image


def picture(size: tuple[int, int] = (1200, 800), kind: str = "JPEG") -> bytes:
    """A file of two flat colours side by side, in the format a phone or a computer saves."""
    image = Image.new("RGB", size, (200, 60, 40))
    image.paste((40, 90, 200), (size[0] // 2, 0, size[0], size[1]))
    buffer = BytesIO()
    image.save(buffer, kind)
    return buffer.getvalue()
