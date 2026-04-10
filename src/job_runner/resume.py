"""Resume ingestion and validation utilities."""

from __future__ import annotations

import re
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from job_runner.config import RESUME_PATH, RESUME_PDF_PATH


_CORRUPTION_MARKERS = (
    "endobj",
    "/type /structelem",
    "/k [",
)

_PLACEHOLDER_MARKERS = (
    "your_legal_name",
    "your city, your state/province",
)


def _split_joined_word_on_case(token: str) -> str:
    """Split ``BuildItOnce`` style tokens into words."""
    if not token:
        return token
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", token)


def _repair_spaced_letters_line(line: str) -> str:
    """Repair only clearly broken single-letter spacing on one line.

    Safeguards:
    - Skip bullet lines and lines containing digits (dates).
    - Skip lines that already contain normal words.
    - Revert if replacement would collapse all spacing unexpectedly.
    """
    raw = line.rstrip("\n")
    stripped = raw.strip()
    if not stripped:
        return raw
    if re.match(r"^[-•*]\s+", stripped):
        return raw
    if re.search(r"\d", stripped):
        return raw

    tokens = re.findall(r"[A-Za-z]+", stripped)
    if not tokens:
        return raw
    single_letter = sum(1 for t in tokens if len(t) == 1)
    normal_words = sum(1 for t in tokens if len(t) >= 3)
    if single_letter < 6 or normal_words > 0:
        return raw

    pattern = re.compile(r"\b[A-Za-z](?:\s+[A-Za-z]){2,}\b")

    def _fix_match(match: re.Match[str]) -> str:
        compact = "".join(re.findall(r"[A-Za-z]", match.group(0)))
        return _split_joined_word_on_case(compact)

    fixed = pattern.sub(_fix_match, raw)

    # Safeguard: if all spaces vanished in a line that originally had many, keep original.
    if raw.count(" ") >= 6 and fixed.count(" ") == 0:
        return raw
    return fixed


def _rejoin_spaced_letters(text: str) -> str:
    """Fix extraction artifacts like ``B u i l d I t O n c e`` -> ``Build It Once``.

    Applies only to clearly broken lines; avoids touching normal prose, titles,
    company names, and date lines.
    """
    s = text or ""
    lines = s.split("\n")
    out = [_repair_spaced_letters_line(line) for line in lines]
    return "\n".join(out)


def _normalize_resume_text(text: str) -> str:
    s = (text or "").replace("\x00", " ")
    s = re.sub(r"[\u0001-\u0008\u000b-\u001f\u007f]", " ", s)
    s = re.sub(r"\r\n?", "\n", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = _rejoin_spaced_letters(s)
    return s.strip()


def is_corrupted_resume_text(text: str) -> bool:
    """Heuristic detector for broken PDF extraction output."""
    s = (text or "").strip()
    if len(s) < 80:
        return True

    lower = s.lower()
    if any(m in lower for m in _PLACEHOLDER_MARKERS):
        return True

    marker_hits = sum(1 for m in _CORRUPTION_MARKERS if m in lower)
    if marker_hits >= 2:
        return True

    if len(re.findall(r"\bendobj\b", lower)) >= 3:
        return True
    if len(re.findall(r"\b\d+\s+\d+\s+obj\b", lower)) >= 5:
        return True
    if re.search(r"\b[0-9a-f]{40,}\b", lower):
        return True

    words = re.findall(r"[A-Za-z]{2,}", s)
    symbols = re.findall(r"[^A-Za-z0-9\s.,;:()'\"/@&+\-]", s)
    if not words:
        return True
    readable_ratio = sum(len(w) for w in words) / max(1, len(s))
    symbol_ratio = len(symbols) / max(1, len(s))
    if readable_ratio < 0.20 or symbol_ratio > 0.20:
        return True
    return False


def _extract_text_from_docx(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    chunks = [n.text for n in root.findall(".//w:t", ns) if n.text]
    return _normalize_resume_text("\n".join(chunks))


def _extract_text_from_pdf_pypdf(path: Path) -> str:
    from pypdf import PdfReader  # type: ignore

    reader = PdfReader(str(path))
    pages = [(p.extract_text() or "") for p in reader.pages]
    return _normalize_resume_text("\n\n".join(pages))


def _extract_text_from_pdf_pdftotext(path: Path) -> str:
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        out_path = Path(tmp.name)
    try:
        cmd = ["pdftotext", "-layout", "-enc", "UTF-8", str(path), str(out_path)]
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        txt = out_path.read_text(encoding="utf-8", errors="ignore")
        return _normalize_resume_text(txt)
    finally:
        if out_path.exists():
            out_path.unlink()


def _extract_text_from_plain(path: Path) -> str:
    return _normalize_resume_text(path.read_text(encoding="utf-8", errors="ignore"))


def _candidate_resume_sources(pdf_path: Path) -> list[Path]:
    out: list[Path] = []
    for ext in (".txt", ".docx"):
        p = pdf_path.with_suffix(ext)
        if p.exists():
            out.append(p)
    return out


def _extract_pdf_with_fallback(pdf_path: Path) -> tuple[str, str]:
    methods: list[tuple[str, callable]] = [
        ("pypdf", _extract_text_from_pdf_pypdf),
        ("pdftotext", _extract_text_from_pdf_pdftotext),
    ]
    errors: list[str] = []
    for name, fn in methods:
        try:
            txt = fn(pdf_path)
            if txt and not is_corrupted_resume_text(txt):
                return txt, f"{pdf_path} ({name})"
            errors.append(f"{name}: extracted text looked corrupted")
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    for alt in _candidate_resume_sources(pdf_path):
        try:
            txt = _extract_text_from_docx(alt) if alt.suffix.lower() == ".docx" else _extract_text_from_plain(alt)
            if txt and not is_corrupted_resume_text(txt):
                return txt, str(alt)
            errors.append(f"{alt.name}: extracted text looked corrupted")
        except Exception as exc:
            errors.append(f"{alt.name}: {exc}")

    raise ValueError(
        "Resume parse failed. Could not extract clean text from PDF. "
        "Provide a plain-text (.txt) or .docx resume source. "
        f"Details: {' | '.join(errors)}"
    )


def extract_resume_text(path: Path) -> str:
    """Extract clean, human-readable resume text from .txt, .docx, or .pdf."""
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"Resume file not found: {p}")

    suffix = p.suffix.lower()
    if suffix == ".txt":
        txt = _extract_text_from_plain(p)
        if is_corrupted_resume_text(txt):
            raise ValueError("Resume parse failed: text source appears corrupted or templated.")
        return txt
    if suffix == ".docx":
        txt = _extract_text_from_docx(p)
        if is_corrupted_resume_text(txt):
            raise ValueError("Resume parse failed: DOCX extraction looked corrupted.")
        return txt
    if suffix == ".pdf":
        txt, _src = _extract_pdf_with_fallback(p)
        return txt
    raise ValueError(f"Unsupported resume format: {suffix}. Use .txt, .docx, or .pdf")


def extract_resume_text_with_source(path: Path) -> tuple[str, str]:
    """Like ``extract_resume_text`` but also returns the source used."""
    p = Path(path).expanduser().resolve()
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_with_fallback(p)
    return extract_resume_text(p), str(p)


def ensure_clean_resume_text() -> tuple[str, str]:
    """Load/repair resume text for scoring; writes only clean text to resume.txt."""
    if RESUME_PATH.exists():
        txt = _extract_text_from_plain(RESUME_PATH)
        if not is_corrupted_resume_text(txt):
            return txt, str(RESUME_PATH)

    if RESUME_PDF_PATH.exists():
        txt, used = extract_resume_text_with_source(RESUME_PDF_PATH)
        RESUME_PATH.write_text(txt, encoding="utf-8")
        return txt, used

    if RESUME_PATH.exists():
        raise ValueError(
            f"Resume parse failed: {RESUME_PATH} looks corrupted and no PDF fallback is available. "
            "Provide a cleaner .txt, .docx, or .pdf via `job_runner init`."
        )

    raise FileNotFoundError(
        f"Resume not found. Add one via `job_runner init` ({RESUME_PATH} or {RESUME_PDF_PATH})."
    )
