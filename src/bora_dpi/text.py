"""Normalización de texto, validación de CUIT y DNI, y verificación de evidencia."""

import difflib
import html as htmllib
import re
import unicodedata

CUIT_RE = re.compile(r"\b(\d{2})[-‐–\s.]?(\d{8})[-‐–\s.]?(\d)\b")
DNI_TOKEN_RE = re.compile(r"\b\d{1,2}\.?\d{3}\.?\d{3}\b")
COMPANY_SUFFIXES = [
    "sociedad anonima unipersonal", "sociedad anonima", "sociedad de responsabilidad limitada",
    "sociedad por acciones simplificada", "s a u", "s r l", "s a s", "s a", "sau", "srl", "sas", "sa",
    "s a i c", "saic", "s a c i", "saci", "sociedad colectiva", "s c", "s h", "ltda", "limitada",
]


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def norm(s: str) -> str:
    """Minúsculas, sin acentos ni puntuación, espacios simples."""
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9ñ ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def company_key(name: str) -> str:
    """Clave de denominación: sin tipo societario, para comparar variantes del nombre."""
    s = norm(name)
    s = re.sub(r"\b([a-z])\s(?=[a-z]\b)", r"\1", s)  # "s a" -> "sa" en abreviaturas sueltas
    for suf in sorted(COMPANY_SUFFIXES, key=len, reverse=True):
        suf_c = suf.replace(" ", "")
        s = re.sub(rf"\s(?:{re.escape(suf)}|{re.escape(suf_c)})$", "", s)
    return s.strip()


def person_key(name: str) -> str:
    """Clave de nombre de persona: tokens ordenados (tolera 'Apellido, Nombre')."""
    return " ".join(sorted(t for t in norm(name).split() if len(t) > 1))


def html_to_text(fragment: str) -> str:
    fragment = re.sub(r"<(script|style)\b.*?</\1>", "", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", fragment, flags=re.I)
    t = htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment))
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


# --- CUIT -----------------------------------------------------------------

CUIT_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)


def cuit_digits(s: str | None) -> str | None:
    if not s:
        return None
    d = re.sub(r"\D", "", s)
    return d if len(d) == 11 else None


def cuit_valid(s: str | None) -> bool:
    d = cuit_digits(s)
    if not d or d[:2] not in {"20", "23", "24", "25", "26", "27", "30", "33", "34"}:
        return False
    total = sum(int(a) * w for a, w in zip(d[:10], CUIT_WEIGHTS))
    dv = 11 - total % 11
    dv = 0 if dv == 11 else dv
    return dv != 10 and dv == int(d[10])


def cuit_fmt(s: str | None) -> str | None:
    d = cuit_digits(s)
    return f"{d[:2]}-{d[2:10]}-{d[10]}" if d else None


def find_cuits(text: str) -> list[str]:
    out = []
    for m in CUIT_RE.finditer(text or ""):
        d = "".join(m.groups())
        if cuit_valid(d) and d not in out:
            out.append(d)
    return out


# --- DNI ------------------------------------------------------------------


def dni_digits(s: str | None) -> str | None:
    if not s:
        return None
    d = re.sub(r"\D", "", str(s))
    return d if 6 <= len(d) <= 8 and int(d) >= 100000 else None


def dni_variants(d: str) -> list[str]:
    """Formas en que un DNI aparece en los avisos: '30128147' y '30.128.147'."""
    dotted = f"{int(d):,}".replace(",", ".")
    return [d, dotted]


def dni_in_text(d: str, text: str) -> bool:
    digits_only = re.sub(r"(?<=\d)[.\s](?=\d{3}\b)", "", text or "")
    return re.search(rf"(?<!\d){re.escape(d)}(?!\d)", digits_only) is not None


# --- Evidencia ------------------------------------------------------------


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", strip_accents(s or "")).strip().lower()


def locate_evidence(quote: str | None, text: str) -> tuple[int, int, float] | None:
    """Ubica la cita en el texto fuente. Devuelve (inicio, fin, similitud) o None."""
    if not quote or not text:
        return None
    q, t = _squash(quote), _squash(text)
    if not q:
        return None
    i = t.find(q)
    if i >= 0:
        return i, i + len(q), 1.0
    sm = difflib.SequenceMatcher(None, t, q, autojunk=False)
    m = sm.find_longest_match(0, len(t), 0, len(q))
    if m.size < 12:
        return None
    start = max(0, m.a - m.b)
    window = t[start : start + len(q)]
    ratio = difflib.SequenceMatcher(None, window, q, autojunk=False).ratio()
    return (start, start + len(q), ratio) if ratio >= 0.8 else None


# --- Segmentación de páginas históricas -------------------------------------

SEGMENT_RE = re.compile(r"#I(\d+)I#(.*?)#F\1F#", re.S)


def segment_notice(page_text: str, title: str | None) -> str | None:
    """Recorta un aviso de la página completa en que BORA sirve los avisos históricos.

    Cada aviso está delimitado por marcas #I<n>I# … #F<n>F#. Se elige el segmento que abre con la
    denominación del aviso; si ninguno abre con ella, el que la menciona. Devuelve None si la página
    no tiene marcas o ningún segmento coincide (el aviso no se puede aislar de forma confiable).
    """
    key = company_key(title or "")
    if not key:
        return None
    segs = []
    for m in SEGMENT_RE.finditer(page_text or ""):
        body = SEGMENT_RE.sub(" ", m.group(2))  # quita avisos anidados por el orden de columnas
        segs.append(re.sub(r"[ \t]+", " ", body).strip())
    flat = lambda s: norm(s).replace(" ", "")
    k = key.replace(" ", "")
    for s in segs:
        if flat(s[:160]).startswith(k[: max(8, len(k) // 2)]) or k in flat(s[:160]):
            return s
    hits = [s for s in segs if k in flat(s)]
    return hits[0] if len(hits) == 1 else None
