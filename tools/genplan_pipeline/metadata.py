from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


@dataclass(slots=True)
class FileMetadata:
    detected_format: str
    media_type: str
    width_px: int | None = None
    height_px: int | None = None
    page_count: int | None = None
    method: str = "magic"
    warning: str = ""


IMAGE_TYPES = {
    "jpeg": "image/jpeg",
    "png": "image/png",
    "tiff": "image/tiff",
}


def detect_format(path: Path) -> str:
    with path.open("rb") as handle:
        header = handle.read(16)
    extension = path.suffix.lower()
    if header.startswith(b"%PDF-"):
        return "pdf"
    if header.startswith(b"\xff\xd8"):
        return "jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header[:4] in {b"II*\x00", b"MM\x00*"}:
        return "tiff"
    if header.startswith(b"PK\x03\x04"):
        if extension == ".docx":
            return "docx"
        if extension == ".pptx":
            return "pptx"
        return "zip"
    return extension.lstrip(".") or "binary"


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) < 24 or not header.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid PNG header")
    return struct.unpack(">II", header[16:24])


def _jpeg_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        if handle.read(2) != b"\xff\xd8":
            raise ValueError("invalid JPEG header")
        while True:
            byte = handle.read(1)
            while byte == b"\xff":
                byte = handle.read(1)
            if not byte:
                break
            marker = byte[0]
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            length_bytes = handle.read(2)
            if len(length_bytes) != 2:
                break
            segment_length = struct.unpack(">H", length_bytes)[0]
            if segment_length < 2:
                raise ValueError("invalid JPEG segment length")
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                data = handle.read(5)
                if len(data) != 5:
                    break
                height, width = struct.unpack(">HH", data[1:5])
                return width, height
            handle.seek(segment_length - 2, 1)
    raise ValueError("JPEG size marker not found")


def _tiff_scalar(data: bytes, endian: str, value_type: int, count: int) -> int | None:
    if count != 1:
        return None
    if value_type == 3:
        return struct.unpack(f"{endian}H", data[:2])[0]
    if value_type == 4:
        return struct.unpack(f"{endian}I", data)[0]
    return None


def _tiff_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(8)
        if len(header) != 8:
            raise ValueError("short TIFF header")
        byte_order = header[:2]
        if byte_order == b"II":
            endian = "<"
        elif byte_order == b"MM":
            endian = ">"
        else:
            raise ValueError("invalid TIFF byte order")
        if struct.unpack(f"{endian}H", header[2:4])[0] != 42:
            raise ValueError("invalid TIFF signature")
        ifd_offset = struct.unpack(f"{endian}I", header[4:8])[0]
        handle.seek(ifd_offset)
        count_data = handle.read(2)
        if len(count_data) != 2:
            raise ValueError("missing TIFF IFD")
        entry_count = struct.unpack(f"{endian}H", count_data)[0]
        width = height = None
        for _ in range(entry_count):
            entry = handle.read(12)
            if len(entry) != 12:
                break
            tag, value_type, count = struct.unpack(f"{endian}HHI", entry[:8])
            value = _tiff_scalar(entry[8:12], endian, value_type, count)
            if tag == 256:
                width = value
            elif tag == 257:
                height = value
        if width is None or height is None:
            raise ValueError("TIFF width/height tags not found")
        return width, height


PAGE_PATTERN = re.compile(rb"/Type\s*/Page\b")


def _pdf_page_count_fallback(path: Path) -> int:
    count = 0
    carry = b""
    overlap = 64
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            data = carry + chunk
            if len(data) > overlap:
                count += len(PAGE_PATTERN.findall(data[:-overlap]))
                carry = data[-overlap:]
            else:
                carry = data
    count += len(PAGE_PATTERN.findall(carry))
    return count


def _pdf_page_count(path: Path) -> tuple[int | None, str, str]:
    try:
        from pypdf import PdfReader  # type: ignore[import-not-found]

        return len(PdfReader(str(path)).pages), "pypdf", ""
    except ImportError:
        pass
    except Exception as exc:
        warning = f"pypdf_failed: {exc}"
    else:
        warning = ""

    pdfinfo = os.getenv("PDFINFO_BINARY") or shutil.which("pdfinfo")
    if pdfinfo and Path(pdfinfo).suffix.casefold() in {".cmd", ".bat"}:
        wrapper = Path(pdfinfo).resolve()
        bundled_exe = wrapper.parents[2] / "native" / "poppler" / "Library" / "bin" / "pdfinfo.exe"
        if bundled_exe.exists():
            pdfinfo = str(bundled_exe)
    if pdfinfo:
        try:
            process = subprocess.run(
                [pdfinfo, str(path)],
                capture_output=True,
                check=False,
                text=True,
                timeout=60,
            )
            match = re.search(r"(?m)^Pages:\s+(\d+)\s*$", process.stdout)
            if process.returncode == 0 and match:
                return int(match.group(1)), "pdfinfo", ""
            if process.stderr.strip():
                warning = f"pdfinfo_failed: {process.stderr.strip()[:300]}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            warning = f"pdfinfo_failed: {exc}"

    try:
        count = _pdf_page_count_fallback(path)
    except OSError as exc:
        return None, "pdf_regex", f"pdf_read_failed: {exc}"
    if count:
        return count, "pdf_regex", locals().get("warning", "")
    return None, "pdf_regex", "PDF page count could not be determined"


def _office_page_count(path: Path, detected: str) -> tuple[int | None, str]:
    try:
        with zipfile.ZipFile(path) as package:
            if detected == "pptx":
                slides = [
                    name
                    for name in package.namelist()
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
                ]
                return len(slides), "openxml_slide_count"
            try:
                app_xml = package.read("docProps/app.xml")
            except KeyError:
                return None, "openxml_no_page_metadata"
            root = ElementTree.fromstring(app_xml)
            for element in root.iter():
                if element.tag.rsplit("}", 1)[-1] == "Pages" and element.text:
                    return int(element.text), "openxml_app_pages"
    except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError):
        return None, "openxml_failed"
    return None, "openxml_no_page_metadata"


def inspect_file(path: Path) -> FileMetadata:
    detected = detect_format(path)
    if detected == "png":
        width, height = _png_dimensions(path)
        return FileMetadata(detected, IMAGE_TYPES[detected], width, height, method="png_header")
    if detected == "jpeg":
        width, height = _jpeg_dimensions(path)
        return FileMetadata(detected, IMAGE_TYPES[detected], width, height, method="jpeg_sof")
    if detected == "tiff":
        width, height = _tiff_dimensions(path)
        return FileMetadata(detected, IMAGE_TYPES[detected], width, height, method="tiff_ifd")
    if detected == "pdf":
        pages, method, warning = _pdf_page_count(path)
        return FileMetadata(
            detected,
            "application/pdf",
            page_count=pages,
            method=method,
            warning=warning,
        )
    if detected in {"docx", "pptx"}:
        pages, method = _office_page_count(path, detected)
        media_type = {
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }[detected]
        return FileMetadata(detected, media_type, page_count=pages, method=method)
    media_type = {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "zip": "application/zip",
    }.get(detected, "application/octet-stream")
    return FileMetadata(detected, media_type, method="magic_and_extension")
