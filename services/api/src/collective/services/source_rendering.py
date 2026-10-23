"""Bounded, disposable rendering process for untrusted image/PDF bytes."""

import hashlib
import io
import json
import math
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

from collective.api.errors import DomainError

MAX_BYTES = 10 * 1024 * 1024
MAX_TEXT = 16000
MAX_PIXELS = 20_000_000
MAX_SIDE = 1800


def inspect_source(content, mime):
    if not content or len(content) > MAX_BYTES:
        raise DomainError("SOURCE_SIZE_LIMIT", "Use a nonempty source no larger than 10 MB.", 413)
    mime = mime.split(";")[0].strip().lower()
    if mime not in {"image/png", "image/jpeg", "application/pdf", "text/plain"}:
        raise DomainError("UNSUPPORTED_SOURCE", "Use PNG, JPEG, PDF or UTF-8 text.", 415)
    if mime == "text/plain":
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DomainError("UNREADABLE_TEXT", "Use UTF-8 text.") from exc
        if not text.strip() or "\x00" in text or len(text) > MAX_TEXT:
            raise DomainError("TEXT_LIMIT", "Use readable text of at most 16,000 characters.")
        return {
            "kind": "text",
            "mime": mime,
            "text": text,
            "pages": [],
            "sha256": hashlib.sha256(content).hexdigest(),
        }
    signatures = {
        "image/png": b"\x89PNG\r\n\x1a\n",
        "image/jpeg": b"\xff\xd8\xff",
        "application/pdf": b"%PDF-",
    }
    if not content.startswith(signatures[mime]):
        raise DomainError("INVALID_SOURCE", "The source does not match its declared file type.")
    with tempfile.TemporaryDirectory(prefix="collective-source-") as directory:
        root = Path(directory)
        (root / "original").write_bytes(content)
        try:
            result = subprocess.run(
                [sys.executable, "-m", __name__, str(root), mime],
                capture_output=True,
                timeout=20,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DomainError(
                "SOURCE_RENDER_TIMEOUT", "This source could not be rendered within its limit."
            ) from exc
        if result.returncode:
            raise DomainError(
                "UNREADABLE_SOURCE",
                "Use a readable static image or an unencrypted PDF of at most 10 pages.",
            )
        metadata = json.loads((root / "pages.json").read_text())
        for page in metadata:
            page["content"] = (root / f"page-{page['page']}.png").read_bytes()
        return {
            "kind": "pdf" if mime == "application/pdf" else "image",
            "mime": mime,
            "text": None,
            "pages": metadata,
            "sha256": hashlib.sha256(content).hexdigest(),
        }


def readable(width, height):
    return (
        math.isfinite(width)
        and math.isfinite(height)
        and min(width, height) >= 64
        and max(width, height) <= 20000
        and width * height <= MAX_PIXELS
    )


def crop_page(content, bbox):
    with Image.open(io.BytesIO(content)) as image:
        left, top, right, bottom = bbox
        crop = image.crop(
            (
                math.floor(left * image.width / 1000),
                math.floor(top * image.height / 1000),
                math.ceil(right * image.width / 1000),
                math.ceil(bottom * image.height / 1000),
            )
        )
        crop.info.clear()
        output = io.BytesIO()
        crop.save(output, format="PNG")
        return output.getvalue()


def save_page(image, root, page, transform):
    if image.mode in {"LA", "P"} and (image.mode == "LA" or "transparency" in image.info):
        image = image.convert("RGBA")
    if image.mode == "RGBA":
        flattened = Image.new("RGB", image.size, "white")
        flattened.paste(image, mask=image.getchannel("A"))
        image = flattened
    else:
        image = image.convert("RGB")
    image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
    if min(image.size) < 64:
        raise ValueError("Unreadable page dimensions")
    image.info.clear()
    output = io.BytesIO()
    image.save(output, format="PNG")
    data = output.getvalue()
    if len(data) > MAX_BYTES:
        raise ValueError("Rendered page exceeds size limit")
    (root / f"page-{page}.png").write_bytes(data)
    return {
        "page": page,
        "width": image.width,
        "height": image.height,
        "sha256": hashlib.sha256(data).hexdigest(),
        "transformations": transform
        | {
            "normalized_size": [image.width, image.height],
            "crop": [0, 0, 0, 0],
        },
    }


def render(root, mime):
    # Native parsing is isolated: a timeout or parser failure must not block the API worker.
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024, 384 * 1024 * 1024))
    if mime == "application/pdf":
        import pypdfium2 as pdfium

        pages = []
        with pdfium.PdfDocument((root / "original").read_bytes()) as document:
            if not 1 <= len(document) <= 10:
                raise ValueError("Page limit")
            for index in range(len(document)):
                page = document[index]
                width, height = page.get_size()
                if not all(math.isfinite(v) and 36 <= v <= 20000 for v in (width, height)):
                    raise ValueError("Page dimensions")
                bitmap = page.render(scale=MAX_SIDE / max(width, height))
                image = bitmap.to_pil().copy()
                bitmap.close()
                pages.append(
                    save_page(
                        image,
                        root,
                        index + 1,
                        {
                            "renderer": "pypdfium2:5.14.0",
                            "source_size_points": [width, height],
                            "pdf_rotation": page.get_rotation(),
                        },
                    )
                )
                page.close()
    else:
        with Image.open(root / "original") as image:
            if (
                image.format != ("PNG" if mime == "image/png" else "JPEG")
                or getattr(image, "n_frames", 1) != 1
            ):
                raise ValueError("Static image required")
            if not readable(*image.size):
                raise ValueError("Image dimensions")
            orientation = image.getexif().get(274, 1)
            source_size = list(image.size)
            normalized = ImageOps.exif_transpose(image)
            pages = [
                save_page(
                    normalized,
                    root,
                    1,
                    {
                        "renderer": "Pillow:13.0.0",
                        "source_size_pixels": source_size,
                        "exif_orientation": orientation,
                    },
                )
            ]
    (root / "pages.json").write_text(json.dumps(pages))


if __name__ == "__main__":
    try:
        render(Path(sys.argv[1]), sys.argv[2])
    except (ValueError, RuntimeError, UnidentifiedImageError, OSError):
        raise SystemExit(1) from None
