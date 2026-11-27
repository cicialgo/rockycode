"""Offline smoke test for the optional PDF dependency group."""

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

import pdfplumber
from pypdf import PdfReader, PdfWriter


def main() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    output = BytesIO()
    writer.write(output)
    payload = output.getvalue()

    assert len(PdfReader(BytesIO(payload)).pages) == 1
    with TemporaryDirectory() as directory:
        pdf_path = Path(directory, "synthetic.pdf")
        pdf_path.write_bytes(payload)
        with pdfplumber.open(pdf_path) as document:
            assert len(document.pages) == 1
            assert document.pages[0].width == 72
            assert document.pages[0].height == 72


if __name__ == "__main__":
    main()
