"""Write scanned pages to disk with low memory use (Pi Zero: 512 MB)."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile

from .pages import Page

log = logging.getLogger(__name__)

try:
    import img2pdf
except ImportError:
    img2pdf = None

FORMATS = ("pdf", "jpg", "png", "tiff")


class PageWriter:
    """Spools every page to a temporary file as soon as it is complete."""

    def __init__(self, output: str, fmt: str = "pdf", jpeg_quality: int = 85):
        if fmt not in FORMATS:
            raise ValueError("Format %s nicht unterstützt %s" % (fmt, FORMATS))
        self.output = output
        self.fmt = fmt
        self.quality = jpeg_quality
        self.tmpdir = tempfile.mkdtemp(prefix="brother-ads-")
        self.files: list[str] = []

    def __call__(self, page: Page) -> None:
        self.add(page)

    def add(self, page: Page) -> None:
        img = page.image
        base = os.path.join(self.tmpdir, "page-%04d" % (len(self.files) + 1))
        if page.jpeg is not None and self.fmt in ("pdf", "jpg"):
            path = base + ".jpg"
            with open(path, "wb") as f:
                f.write(page.jpeg)
        elif self.fmt == "png" or (self.fmt == "pdf" and img.mode == "1"):
            path = base + ".png"
            img.save(path, dpi=page.dpi, optimize=False)
        elif self.fmt == "tiff":
            path = base + ".tif"
            img.save(path, dpi=page.dpi,
                     compression="group4" if img.mode == "1" else "tiff_lzw")
        else:
            path = base + ".jpg"
            img.save(path, quality=self.quality, dpi=page.dpi)
        page.image = None  # free the bitmap right away
        self.files.append(path)
        log.info("Seite %d fertig (%s)", len(self.files), os.path.basename(path))

    def finish(self) -> list[str]:
        try:
            if not self.files:
                return []
            if self.fmt == "pdf":
                self._write_pdf()
                return [self.output]
            return self._write_images()
        finally:
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_pdf(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.output)), exist_ok=True)
        if img2pdf is not None:
            with open(self.output, "wb") as f:
                f.write(img2pdf.convert(self.files))
            return
        from PIL import Image

        first, *rest = (Image.open(p) for p in self.files)
        res = first.info.get("dpi", (300, 300))[0]
        first.save(self.output, save_all=True, append_images=rest, resolution=res)

    def _write_images(self) -> list[str]:
        root, ext = os.path.splitext(self.output)
        ext = ext or "." + self.fmt
        out = []
        for i, src in enumerate(self.files, 1):
            dst = self.output if len(self.files) == 1 else "%s-%03d%s" % (root, i, ext)
            os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
            if os.path.splitext(src)[1] == ext or (ext == ".jpeg" and src.endswith(".jpg")):
                shutil.move(src, dst)
            else:
                from PIL import Image

                Image.open(src).save(dst)
            out.append(dst)
        return out

    def abort(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)
