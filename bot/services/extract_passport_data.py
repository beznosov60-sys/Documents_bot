import re
import unicodedata
from typing import List, Dict, Optional

from natasha import (
    Segmenter, MorphVocab, NewsEmbedding, NewsMorphTagger,
    NamesExtractor, Doc,
)

# --- Natasha init ---
segmenter = Segmenter()
morph_vocab = MorphVocab()
emb = NewsEmbedding()
morph_tagger = NewsMorphTagger(emb)
names_extractor = NamesExtractor(morph_vocab)

# --- Regex ---
SERIES_HINT = re.compile(r"(серия|сер\.)", re.IGNORECASE)
NUMBER_HINT = re.compile(r"(номер|№)", re.IGNORECASE)
SER_NUM_RE = re.compile(r"\b(\d{2}\s?\d{2})\s?(?:№|N|№\.|N\.)?\s?(\d{6})\b")
DATE_RE = re.compile(r"\b(\d{2}[./-]\d{2}[./-]\d{4})\b")
DEPT_RE = re.compile(r"код\s*подразделения\s*[:\-]?\s*(\d{3}[-–]\d{3})", re.IGNORECASE)
ISSUE_HINT = re.compile(r"(выдан|выд[аы]ч[аи]|дата\s*выдачи)", re.IGNORECASE)
BIRTH_HINT = re.compile(r"(рожд|родил[ас][ья]|дата\s*рождения)", re.IGNORECASE)

# --- Нормализация текста ---
SUBS = {
    "₽": "Р", "€": "Е", "@": "а", "§": "С", "™": "т", "©": "с",
    "0Р": "ОР", "0р": "Ор", "1С": "ИС", "1с": "Ис",
    "<": " ", ">": " ",
}
def normalize_text(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    for a, b in SUBS.items():
        t = t.replace(a, b)
    # unify separators
    t = t.replace("–", "-").replace("—", "-")
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t

# --- Helpers ---
def _split_lines(text: str) -> List[str]:
    return [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines() if ln.strip()]

def _best_series_number(lines: List[str]) -> Dict[str, str]:
    out = {"series": "", "number": "", "series_raw": "", "number_raw": ""}
    for i, ln in enumerate(lines):
        m = SER_NUM_RE.search(ln)
        if m:
            out.update({"series": m.group(1).replace(" ", ""), "number": m.group(2), "series_raw": ln, "number_raw": ln})
            return out
        if SERIES_HINT.search(ln) or NUMBER_HINT.search(ln):
            chunk = " ".join(lines[max(0, i - 1): i + 2])
            m2 = SER_NUM_RE.search(chunk)
            if m2:
                out.update({"series": m2.group(1).replace(" ", ""), "number": m2.group(2), "series_raw": chunk, "number_raw": chunk})
                return out
    return out

def _pick_dates_with_context(lines: List[str]) -> Dict[str, str]:
    dates = []
    for ln in lines:
        dates.extend(DATE_RE.findall(ln))

    issue = birth = ""
    for i, ln in enumerate(lines):
        dts = DATE_RE.findall(ln)
        if not dts:
            continue
        prev = lines[i - 1] if i > 0 else ""
        ctx = ln + " " + prev
        if ISSUE_HINT.search(ctx) and not issue:
            issue = dts[0]
        if BIRTH_HINT.search(ctx) and not birth:
            birth = dts[0]

    if (not issue or not birth) and len(set(dates)) >= 2:
        uniq = sorted(set(dates))
        birth = birth or uniq[0]
        issue = issue or uniq[-1]

    out: Dict[str, str] = {}
    if birth: out["birth_date"] = birth
    if issue: out["issue_date"] = issue
    if dates: out["dates"] = dates
    return out

def _extract_names(text: str) -> Dict[str, object]:
    # Natasha + фильтр мусора
    names = []
    for n in names_extractor(text):
        if hasattr(n, "fact"):
            f = n.fact
            first = getattr(f, "first", None)
            last = getattr(f, "last", None)
            middle = getattr(f, "middle", None)
        else:
            first = getattr(n, "first", None)
            last = getattr(n, "last", None)
            middle = getattr(n, "middle", None)

        def ok(s: Optional[str]) -> bool:
            return bool(s) and len(s) >= 3 and re.search(r"[А-Яа-яЁё]", s)

        if ok(first) or ok(last) or ok(middle):
            names.append({"first": first, "last": last, "middle": middle})

    # эвристика: склеенное ФИО из капсов (ФАМИЛИЯ ИМЯ ОТЧЕСТВО)
    fio = ""
    caps = re.findall(r"\b[А-ЯЁ]{3,}\b", text)
    if len(caps) >= 2:
        fam = caps[0].title()
        first = caps[1].title() if len(caps) > 1 else ""
        middle = caps[2].title() if len(caps) > 2 else ""
        fio = " ".join([x for x in [fam, first, middle] if x])

    out: Dict[str, object] = {}
    if names: out["names"] = names
    if fio: out["fio"] = fio
    return out

def extract_fields(text: str) -> dict:
    fields: Dict[str, object] = {}
    lines = _split_lines(text)

    fields.update(_best_series_number(lines))

    for ln in lines:
        m = DEPT_RE.search(ln)
        if m:
            fields["department_code"] = m.group(1)
            break

    fields.update(_pick_dates_with_context(lines))
    fields.update(_extract_names(text))

    return fields
