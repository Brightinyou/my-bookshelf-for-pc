#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backfill source metadata frontmatter into existing Obsidian wiki notes."""

from __future__ import annotations

import argparse
import difflib
import re
import unicodedata
from pathlib import Path

import config as cfg
import source_metadata as smeta


META_KEYS = ("published", "publisher", "source_created", "source_modified")


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text or "")


def norm_key(text: str) -> str:
    text = nfc(text).lower()
    text = re.sub(r"\.(txt|pdf|md)$", "", text)
    text = re.sub(r"^add_pdf[_\s-]*", "", text)
    text = re.sub(r"_?(?:ko|번역|레퍼런스삭제_번역)$", "", text)
    text = re.sub(r"_home-miniui-macmini\.local_.*?_conflict$", "", text)
    text = re.sub(r"\s+—\s+.*$", "", text)
    text = re.sub(r"^\d{1,2}[_\s.-]+", "", text)
    text = re.sub(r"[^0-9a-z가-힣]+", "", text)
    return text


def parse_frontmatter(text: str) -> tuple[list[str], str] | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    return text[3:end].strip("\n").splitlines(), text[end + 4:].lstrip("\n")


def fm_value(lines: list[str], key: str) -> str:
    for line in lines:
        if line.startswith(key + ":"):
            return line.split(":", 1)[1].strip().strip('"')
    return ""


def set_fm_values(lines: list[str], values: dict[str, str]) -> tuple[list[str], bool]:
    changed = False
    out: list[str] = []
    remaining = {k: v for k, v in values.items() if v}
    for line in lines:
        key = line.split(":", 1)[0].strip() if ":" in line else ""
        if key in META_KEYS:
            val = remaining.pop(key, "")
            if val:
                new_line = smeta.yaml_line(key, val).rstrip("\n")
                out.append(new_line)
                changed = changed or new_line != line
            else:
                out.append(line)
            continue
        out.append(line)
        if key in ("source", "book", "summary", "tags"):
            insert = []
            for dkey in META_KEYS:
                if dkey in remaining:
                    insert.append(smeta.yaml_line(dkey, remaining.pop(dkey)).rstrip("\n"))
            if insert:
                out.extend(insert)
                changed = True
    if remaining:
        insert_at = len(out)
        for i, line in enumerate(out):
            if line.startswith(("model:", "generated:", "mode:", "author:", "refined:")):
                insert_at = i
                break
        out[insert_at:insert_at] = [
            smeta.yaml_line(k, remaining[k]).rstrip("\n")
            for k in META_KEYS if k in remaining
        ]
        changed = True
    return out, changed


def index_sources() -> tuple[dict[str, list[Path]], dict[str, list[Path]], list[Path]]:
    pdfs = [p for p in cfg.PDF_DIR.rglob("*.pdf") if p.is_file()]
    txt_roots = [cfg.TXT_DIR, cfg.PROCESSED_DIR, cfg.CHAPTERS_DIR]
    txts: list[Path] = []
    for root in txt_roots:
        if root.exists():
            txts.extend(p for p in root.rglob("*.txt") if p.is_file())
    pdf_idx: dict[str, list[Path]] = {}
    txt_idx: dict[str, list[Path]] = {}
    for p in pdfs:
        pdf_idx.setdefault(norm_key(p.stem), []).append(p)
    for p in txts:
        txt_idx.setdefault(norm_key(p.stem), []).append(p)
    return pdf_idx, txt_idx, txts


def note_candidates(path: Path, fm: list[str]) -> list[str]:
    vals = [
        fm_value(fm, "source"),
        fm_value(fm, "book"),
        fm_value(fm, "title"),
        path.stem,
        path.parent.name if path.parent != cfg.WIKI_DIR else "",
    ]
    seen: set[str] = set()
    out: list[str] = []
    for val in vals:
        key = norm_key(val)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def note_candidate_values(path: Path, fm: list[str]) -> list[str]:
    return [
        fm_value(fm, "source"),
        fm_value(fm, "book"),
        fm_value(fm, "title"),
        path.stem,
        path.parent.name if path.parent != cfg.WIKI_DIR else "",
    ]


def find_source(cands: list[str], pdf_idx: dict[str, list[Path]], txt_idx: dict[str, list[Path]]) -> tuple[Path | None, Path | None, str]:
    for key in cands:
        if key in pdf_idx or key in txt_idx:
            return (pdf_idx.get(key) or [None])[0], (txt_idx.get(key) or [None])[0], "exact"
    all_keys = set(pdf_idx) | set(txt_idx)
    for key in cands:
        matches = difflib.get_close_matches(key, all_keys, n=1, cutoff=0.88)
        if matches:
            hit = matches[0]
            return (pdf_idx.get(hit) or [None])[0], (txt_idx.get(hit) or [None])[0], "fuzzy"
    return None, None, ""


_DATE_PATTERNS = [
    re.compile(r"(?:발행일|출판일|간행일|발행)\s*[:：]?\s*((?:18|19|20)\d{2}[./년 -]?\s*\d{0,2}[./월 -]?\s*\d{0,2})"),
    re.compile(r"(?:Published|Publication date|Date published)\s*[:：]?\s*((?:18|19|20)\d{2}(?:[-/.]\d{1,2})?(?:[-/.]\d{1,2})?)", re.I),
    re.compile(r"(?:Copyright|©|\(c\))\s*((?:18|19|20)\d{2})", re.I),
]

_PUBLISHER_PATTERNS = [
    re.compile(r"(?:출판사|발행처|발행자|펴낸곳|펴낸 곳|출판)\s*[:：]\s*([^\n\r]{2,80})"),
    re.compile(r"(?:Publisher|Published by)\s*[:：]\s*([^\n\r]{2,100})", re.I),
]

_PUBLISHER_STOP = re.compile(
    r"(?:ISBN|ISSN|DOI|전화|팩스|주소|등록|인쇄|발행일|출판일|copyright|©|http|www\.)",
    re.I,
)


def clean_date(raw: str) -> str:
    raw = re.sub(r"\s+", "", raw.strip())
    m = re.match(r"((?:18|19|20)\d{2})(?:[./년 -]?(\d{1,2}))?(?:[./월 -]?(\d{1,2}))?", raw)
    if not m:
        return ""
    year, month, day = m.group(1), m.group(2), m.group(3)
    if month and day:
        return f"{year}-{int(month):02d}-{int(day):02d}"
    if month:
        return f"{year}-{int(month):02d}"
    return year


def extract_published(txt: Path | None) -> str:
    if not txt or not txt.exists():
        return ""
    text = txt.read_text(encoding="utf-8", errors="ignore")
    sample = text[:80_000] + "\n" + text[-40_000:]
    for pat in _DATE_PATTERNS:
        m = pat.search(sample)
        if m:
            return clean_date(m.group(1))
    return ""


def clean_publisher(raw: str) -> str:
    text = re.sub(r"\s+", " ", raw.strip(" \t|,.;·ㆍ-"))
    text = _PUBLISHER_STOP.split(text, 1)[0].strip(" \t|,.;·ㆍ-")
    text = re.sub(r"\b(?:Inc|Ltd|LLC|Press)\.$", lambda m: m.group(0), text)
    if not (2 <= len(text) <= 80):
        return ""
    if re.search(r"^\d+$", text):
        return ""
    return text


def extract_publisher(txt: Path | None) -> str:
    if not txt or not txt.exists():
        return ""
    text = txt.read_text(encoding="utf-8", errors="ignore")
    sample = text[:80_000] + "\n" + text[-40_000:]
    for pat in _PUBLISHER_PATTERNS:
        m = pat.search(sample)
        if m:
            publisher = clean_publisher(m.group(1))
            if publisher:
                return publisher
    return ""


def infer_published_from_name(values: list[str]) -> str:
    for raw in values:
        text = nfc(raw)
        arxiv = re.search(r"\b([12]\d)(0[1-9]|1[0-2])\.\d{4,5}\b", text)
        if arxiv:
            return f"20{arxiv.group(1)}-{arxiv.group(2)}"
        compact = re.search(r"\b((?:19|20)\d{2})(0[1-9]|1[0-2])\b", text)
        if compact:
            return f"{compact.group(1)}-{compact.group(2)}"
        leading = re.match(r"^\D*((?:19|20)\d{2})(?:\D|$)", text)
        if leading:
            return leading.group(1)
    return ""


def backfill_note(path: Path, pdf_idx: dict[str, list[Path]], txt_idx: dict[str, list[Path]], dry_run: bool) -> tuple[bool, str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    parsed = parse_frontmatter(text)
    if parsed is None:
        fm, body = [], text
    else:
        fm, body = parsed
    raw_values = note_candidate_values(path, fm)
    cands = []
    seen = set()
    for val in raw_values:
        key = norm_key(val)
        if key and key not in seen:
            seen.add(key)
            cands.append(key)
    pdf, txt, match = find_source(cands, pdf_idx, txt_idx)
    values: dict[str, str] = {}
    if not fm_value(fm, "published"):
        values["published"] = extract_published(txt)
        if not values["published"]:
            values["published"] = infer_published_from_name(raw_values)
    if not fm_value(fm, "publisher"):
        values["publisher"] = extract_publisher(txt)
    if pdf:
        dates = smeta.pdf_dates_for_pdf(pdf)
        for key in ("source_created", "source_modified"):
            if not fm_value(fm, key) and dates.get(key):
                values[key] = dates[key]
    values = {k: v for k, v in values.items() if v}
    if not values:
        return False, "no-date"
    new_fm, changed = set_fm_values(fm, values)
    if not changed:
        return False, "unchanged"
    if not dry_run:
        path.write_text("---\n" + "\n".join(new_fm).rstrip() + "\n---\n\n" + body, encoding="utf-8")
    fields = ",".join(values)
    return True, f"{match}:{fields}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wiki", type=Path, default=cfg.WIKI_DIR)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    pdf_idx, txt_idx, _txts = index_sources()
    stats: dict[str, int] = {}
    changed = 0
    for note in sorted(args.wiki.rglob("*.md")):
        ok, reason = backfill_note(note, pdf_idx, txt_idx, dry_run=not args.apply)
        stats[reason] = stats.get(reason, 0) + 1
        changed += int(ok)
    mode = "apply" if args.apply else "dry-run"
    print(f"{mode}: changed={changed}, total={sum(stats.values())}")
    for key in sorted(stats):
        print(f"{key}: {stats[key]}")


if __name__ == "__main__":
    main()
