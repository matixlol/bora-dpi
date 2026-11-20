"""Texto de PDFs: capa de texto si existe, OCR (Tesseract, español) si es una imagen."""

import subprocess
import tempfile
from pathlib import Path


def pdf_text(pdf: Path) -> tuple[str, str]:
    out = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True, text=True)
    if len(out.stdout.strip()) > 200:
        return out.stdout, "pdf"
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(["pdftoppm", "-r", "300", "-gray", "-png", str(pdf), f"{tmp}/p"], check=True)
        pages = []
        for png in sorted(Path(tmp).glob("p*.png")):
            r = subprocess.run(
                ["tesseract", str(png), "-", "-l", "spa", "--psm", "3"], capture_output=True, text=True
            )
            pages.append(r.stdout)
    return "\n".join(pages), "ocr"
