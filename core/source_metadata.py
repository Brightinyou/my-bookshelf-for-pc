#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Source metadata helpers for Obsidian frontmatter."""

from __future__ import annotations

import datetime as _dt
import re
import shutil
import subprocess
from pathlib import Path

import config as cfg


def yaml_line(key: str, value: object) -> str:
    """Return one YAML frontmatter line, or an empty string for blank values."""
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    if re.search(r"[:#\[\]{}&,*!|>'\"%@`]", text):
        text = '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{key}: {text}\n"


def _pdfinfo_binary() -> str | None:
    pdftotext = str(getattr(cfg, "PDFTOTEXT", "") or "")
    if pdftotext:
        base = Path(pdftotext)
        for name in ("pdfinfo.exe", "pdfinfo"):
            sibling = base.with_name(name)
            if sibling.exists():
                return str(sibling)
    return shutil.which("pdfinfo")


def _parse_pdf_date(value: str) -> str:
    """Parse Poppler/PDF dates to ISO date when possible."""
    value = value.strip()
    m = re.search(r"D:(\d{4})(\d{2})?(\d{2})?", value)
    if m:
        year, month, day = m.group(1), m.group(2) or "01", m.group(3) or "01"
        try:
            return _dt.date(int(year), int(month), int(day)).isoformat()
        except ValueError:
            return year
    for fmt in ("%a %b %d %H:%M:%S %Y", "%b %d %Y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(value[:24], fmt).date().isoformat()
        except ValueError:
            pass
    m = re.search(r"\b(18|19|20)\d{2}\b", value)
    return m.group(0) if m else ""


def find_source_pdf(txt_path: Path) -> Path | None:
    """Find the archived original PDF for a converted TXT by matching the stem."""
    stem = txt_path.stem
    direct = cfg.PDF_DIR / f"{stem}.pdf"
    if direct.exists():
        return direct
    try:
        matches = sorted(p for p in cfg.PDF_DIR.rglob("*.pdf") if p.stem == stem)
        return matches[0] if matches else None
    except Exception:
        return None


def pdf_dates_for_pdf(pdf: Path) -> dict[str, str]:
    """Return PDF creation/modification dates for a PDF file."""
    exe = _pdfinfo_binary()
    if not (pdf and exe):
        return {}
    try:
        r = subprocess.run([exe, str(pdf)], capture_output=True, text=True, timeout=8)
    except Exception:
        return {}
    if r.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in r.stdout.splitlines():
        if line.startswith("CreationDate:"):
            out["source_created"] = _parse_pdf_date(line.split(":", 1)[1])
        elif line.startswith("ModDate:"):
            out["source_modified"] = _parse_pdf_date(line.split(":", 1)[1])
    return {k: v for k, v in out.items() if v}


def pdf_dates_for_txt(txt_path: Path) -> dict[str, str]:
    """Return PDF creation/modification dates for the TXT's original PDF."""
    pdf = find_source_pdf(txt_path)
    return pdf_dates_for_pdf(pdf) if pdf else {}


def frontmatter_lines(meta: dict[str, object]) -> str:
    return "".join(yaml_line(k, v) for k, v in meta.items())
