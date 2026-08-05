from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .store import WorkbenchError, safe_path


def render_tiff_page(
    source: Path,
    destination: Path,
    *,
    page: int,
    data_root: Path,
) -> Path:
    if page < 1 or page > 500:
        raise WorkbenchError("TIFF page is outside the allowed range")
    source = safe_path(data_root, source, must_exist=True)
    destination = safe_path(data_root, destination)
    if destination.exists():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        from PIL import Image, ImageSequence
    except ImportError as exc:
        raise WorkbenchError("TIFF renderer is unavailable. Install Pillow.") from exc

    Image.MAX_IMAGE_PIXELS = None
    temporary = destination.with_suffix(".part.png")
    try:
        with Image.open(source) as image:
            frame_count = getattr(image, "n_frames", 1)
            if page > frame_count:
                raise WorkbenchError("TIFF page does not exist")
            for index, frame in enumerate(ImageSequence.Iterator(image), start=1):
                if index == page:
                    frame.convert("RGB").save(temporary)
                    break
        if not temporary.exists():
            raise WorkbenchError("TIFF renderer did not create the expected image")
        temporary.replace(destination)
        return destination
    except WorkbenchError:
        temporary.unlink(missing_ok=True)
        raise
    except (OSError, ValueError) as exc:
        temporary.unlink(missing_ok=True)
        raise WorkbenchError(f"Pillow could not render the TIFF: {exc}") from exc


def render_pdf_page(
    source: Path,
    destination: Path,
    *,
    page: int,
    data_root: Path,
    dpi: int = 150,
) -> Path:
    if page < 1 or page > 500:
        raise WorkbenchError("PDF page is outside the allowed range")
    source = safe_path(data_root, source, must_exist=True)
    destination = safe_path(data_root, destination)
    if destination.exists():
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        import fitz  # type: ignore[import-not-found]
    except ImportError:
        fitz = None
    if fitz is not None:
        temporary = destination.with_suffix(".part.png")
        try:
            with fitz.open(source) as document:
                if page > document.page_count:
                    raise WorkbenchError("PDF page does not exist")
                pdf_page = document.load_page(page - 1)
                scale = dpi / 72
                pixmap = pdf_page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                pixmap.save(temporary)
            temporary.replace(destination)
            return destination
        except (RuntimeError, ValueError) as exc:
            temporary.unlink(missing_ok=True)
            raise WorkbenchError(f"PyMuPDF could not render the PDF: {exc}") from exc

    try:
        import pypdfium2 as pdfium  # type: ignore[import-not-found]
    except ImportError:
        pdfium = None
    if pdfium is not None:
        temporary = destination.with_suffix(".part.png")
        try:
            document = pdfium.PdfDocument(str(source))
            try:
                if page > len(document):
                    raise WorkbenchError("PDF page does not exist")
                pdf_page = document[page - 1]
                scale = dpi / 72
                bitmap = pdf_page.render(scale=scale)
                image = bitmap.to_pil()
                image.save(temporary)
            finally:
                document.close()
            temporary.replace(destination)
            return destination
        except WorkbenchError:
            temporary.unlink(missing_ok=True)
            raise
        except (RuntimeError, ValueError, OSError) as exc:
            temporary.unlink(missing_ok=True)
            raise WorkbenchError(f"pypdfium2 could not render the PDF: {exc}") from exc

    executable = _pdftoppm_executable()
    if not executable:
        raise WorkbenchError(
            "PDF renderer is unavailable. Install PyMuPDF or Poppler pdftoppm."
        )
    prefix = destination.parent / destination.stem
    poppler_source = _ascii_pdf_source(source, destination)
    command = [
        executable,
        "-f",
        str(page),
        "-l",
        str(page),
        "-singlefile",
        "-png",
        "-r",
        str(dpi),
        str(poppler_source),
        str(prefix),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        destination.unlink(missing_ok=True)
        raise WorkbenchError(f"pdftoppm could not render the PDF: {exc}") from exc
    finally:
        if poppler_source != source:
            poppler_source.unlink(missing_ok=True)
    if not destination.exists():
        raise WorkbenchError("PDF renderer did not create the expected image")
    return destination


def _ascii_pdf_source(source: Path, destination: Path) -> Path:
    try:
        str(source).encode("ascii")
        return source
    except UnicodeEncodeError:
        pass
    temporary = destination.parent / f"{destination.stem}.source.pdf"
    shutil.copyfile(source, temporary)
    return temporary


def _pdftoppm_executable() -> str | None:
    executable = shutil.which("pdftoppm")
    if not executable:
        return None
    path = Path(executable)
    if path.suffix.casefold() not in {".cmd", ".bat"}:
        return executable
    direct = _direct_poppler_exe(path)
    return str(direct) if direct and direct.exists() else executable


def _direct_poppler_exe(wrapper: Path) -> Path | None:
    parts = {part.casefold() for part in wrapper.parts}
    if "override" in parts and len(wrapper.parents) >= 3:
        dependencies = wrapper.parents[2]
        return dependencies / "native" / "poppler" / "Library" / "bin" / "pdftoppm.exe"
    if "native" in parts and "poppler" in parts:
        return wrapper.parent.parent / "Library" / "bin" / "pdftoppm.exe"
    return None
