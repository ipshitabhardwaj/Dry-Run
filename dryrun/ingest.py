"""Turn an upload into plain text, and plain text into addressable sentences."""
from __future__ import annotations

import io
import re
from typing import List

from .model import Sentence

MARKER = re.compile(r"^\s*(?:\(?\d+(?:\.\d+)*[.)]|\(?[a-zA-Z][.)]|[-*•–])\s+")
BOUNDARY = re.compile(r"(?<=[.!?])[\"'”’)]*\s+(?=[\"'“‘(]?[A-Z0-9])")


def read_upload(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return unwrap("\n".join((page.extract_text() or "") for page in reader.pages))
    if name.endswith(".docx"):
        import docx
        document = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in document.paragraphs if p.text.strip())
    return data.decode("utf-8", errors="replace").strip()


def unwrap(text: str) -> str:
    """PDF text arrives hard-wrapped. Join a line onto the previous one unless the
    previous line ended a sentence, was blank, or this line starts a new numbered item."""
    out: List[str] = []
    for line in (l.strip() for l in text.split("\n")):
        if out and out[-1] and line and not MARKER.match(line + " ") \
                and not re.search(r"[.!?:][\"'”’)]*$", out[-1]):
            out[-1] += " " + line
        else:
            out.append(line)
    return "\n".join(out).strip()


def split_sentences(text: str) -> List[Sentence]:
    """Sentences keep exact character offsets into the original text."""
    sentences: List[Sentence] = []
    pos = 0
    for line in text.split("\n"):
        line_start = pos
        pos += len(line) + 1
        m = MARKER.match(line)
        offset = m.end() if m else len(line) - len(line.lstrip())
        body = line[offset:]
        cursor = 0
        pieces = BOUNDARY.split(body)
        for piece in pieces:
            idx = body.index(piece, cursor)
            cursor = idx + len(piece)
            clean = piece.strip()
            if len(clean) < 12:  # headings, stray numbers
                continue
            start = line_start + offset + idx + (len(piece) - len(piece.lstrip()))
            sentences.append(Sentence(id=f"c{len(sentences) + 1}", text=clean,
                                      start=start, end=start + len(clean)))
    return sentences
