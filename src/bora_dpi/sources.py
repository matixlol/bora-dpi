"""Fuentes oficiales complementarias para resolver revisiones sin trabajo manual.

- Registro Nacional de Sociedades (Ministerio de Justicia, datos.jus.gob.ar): CUIT, razón social y
  fecha del contrato social de todas las personas jurídicas.
- Padrón de ARCA "apellido, nombre y denominación": CUIT y nombre de todos los contribuyentes. En las
  personas humanas el CUIT contiene el DNI (posiciones 3 a 10).

Se indexan en una base aparte (data/sources.db). Los DNI derivados se usan solo para el cotejo.
"""

import csv
import io
import logging
import re
import sqlite3
import zipfile
from pathlib import Path

import httpx

from . import config
from .text import company_key, norm

log = logging.getLogger(__name__)

RNS_URL = ("https://datos.jus.gob.ar/dataset/ee83de85-4305-4c53-9a9f-fd3d15e42c36/resource/"
           "13a1a66f-9f49-4d2d-9582-7b4540ef1b83/download/registro-nacional-sociedades-2026.zip")
ARCA_URL = "https://www.afip.gob.ar/genericos/cInscripcion/archivos/apellidoNombreDenominacion.zip"
PERSON_PREFIXES = ("20", "23", "24", "27")
ARCA_NAME_WIDTH = 30  # el padrón trunca el nombre en 30 caracteres
PARTICLES = {"de", "del", "la", "las", "los", "y", "da", "di", "van", "von"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS rns (cuit TEXT PRIMARY KEY, name TEXT, key TEXT, contract_date TEXT, type TEXT);
CREATE INDEX IF NOT EXISTS rns_key ON rns(key);
CREATE TABLE IF NOT EXISTS arca (cuit TEXT PRIMARY KEY, name TEXT, first TEXT, truncated INTEGER);
CREATE INDEX IF NOT EXISTS arca_first ON arca(first);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DATA_DIR / "sources.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _download(url: str, dest: Path) -> Path:
    if not dest.exists():
        log.info("descargando %s", url)
        with httpx.stream("GET", url, timeout=600, follow_redirects=True) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
    return dest


def build(con: sqlite3.Connection) -> dict:
    """Descarga (si falta) e indexa ambas fuentes."""
    raw = config.DATA_DIR / "sources"
    raw.mkdir(parents=True, exist_ok=True)
    rns_zip = _download(RNS_URL, raw / "rns-2026.zip")
    arca_zip = _download(ARCA_URL, raw / "arca-padron.zip")
    con.execute("DELETE FROM rns")
    with zipfile.ZipFile(rns_zip) as z:
        name = next(n for n in z.namelist() if n.endswith(".csv"))
        with z.open(name) as f:
            rows, seen = [], set()
            for r in csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
                if r["cuit"] in seen:  # una fila por actividad
                    continue
                seen.add(r["cuit"])
                rows.append((r["cuit"], r["razon_social"], company_key(r["razon_social"]),
                             (r["fecha_hora_contrato_social"] or "")[:10] or None, r["tipo_societario"]))
                if len(rows) >= 50000:
                    con.executemany("INSERT OR IGNORE INTO rns VALUES (?,?,?,?,?)", rows)
                    rows = []
            con.executemany("INSERT OR IGNORE INTO rns VALUES (?,?,?,?,?)", rows)
    con.execute("DELETE FROM arca")
    with zipfile.ZipFile(arca_zip) as z:
        with z.open(z.namelist()[0]) as f:
            rows = []
            for line in io.TextIOWrapper(f, encoding="latin-1"):
                cuit, raw_name = line[:11], line[11 : 11 + ARCA_NAME_WIDTH]
                if not cuit.isdigit() or cuit[:2] not in PERSON_PREFIXES:
                    continue
                toks = norm(raw_name).split()
                if not toks:
                    continue
                truncated = int(len(raw_name.rstrip()) >= ARCA_NAME_WIDTH)
                rows.append((cuit, " ".join(toks), toks[0], truncated))
                if len(rows) >= 100000:
                    con.executemany("INSERT OR IGNORE INTO arca VALUES (?,?,?,?)", rows)
                    rows = []
            con.executemany("INSERT OR IGNORE INTO arca VALUES (?,?,?,?)", rows)
    con.commit()
    n = {"rns": con.execute("SELECT COUNT(*) FROM rns").fetchone()[0],
         "arca_personas": con.execute("SELECT COUNT(*) FROM arca").fetchone()[0]}
    log.info("fuentes indexadas: %s", n)
    return n


def _tokens(name: str) -> list[str]:
    return [t for t in norm(name).split() if t not in PARTICLES and len(t) > 1]


def persons_named(con: sqlite3.Connection, name: str) -> list[str]:
    """CUIT de las personas del padrón cuyo nombre tiene exactamente los mismos tokens.

    Tolera el orden ('Apellido Nombre') y el truncado a 30 caracteres del padrón (el último token
    truncado se compara como prefijo). Exige al menos tres tokens o dos poco frecuentes: con nombres
    cortos la unicidad en el padrón no alcanza.
    """
    want = _tokens(name)
    if len(want) < 2:
        return []
    out = []
    marks = ",".join("?" * len(want))
    for r in con.execute(f"SELECT cuit, name, truncated FROM arca WHERE first IN ({marks})", want):
        have = [t for t in r["name"].split() if t not in PARTICLES and len(t) > 1]
        if r["truncated"] and have:
            *full, last = have
            rest = list(want)
            ok = True
            for t in full:
                if t in rest:
                    rest.remove(t)
                else:
                    ok = False
                    break
            if ok and len(rest) == 1 and rest[0].startswith(last):
                out.append(r["cuit"])
        elif sorted(have) == sorted(want):
            out.append(r["cuit"])
    return out


def unique_person_dni(con: sqlite3.Connection, name: str) -> str | None:
    """DNI de la única persona del padrón con ese nombre, o None si no hay o hay homónimos."""
    if len(_tokens(name)) < 3:
        return None  # 'Juan Pérez': demasiado probable que haya homónimos fuera del padrón
    cuits = persons_named(con, name)
    return str(int(cuits[0][2:10])) if len(cuits) == 1 else None


def companies_named(con: sqlite3.Connection, name: str) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM rns WHERE key=?", (company_key(name),))]


def company_attribution(con: sqlite3.Connection, title: str, cuit: str, notice_date: str) -> str | None:
    """¿El aviso con esa denominación es de la sociedad con ese CUIT?

    'same': el RNS asigna ese nombre solo a ese CUIT, o los homónimos se constituyeron después del aviso.
    'other': el aviso es anterior al contrato social de la sociedad, o el nombre pertenece solo a otros CUIT.
    None: no se puede decidir (queda para revisión).
    """
    rows = companies_named(con, title)
    if not rows:
        return None
    mine = [r for r in rows if r["cuit"] == cuit]
    if not mine:
        return "other"
    contract = mine[0]["contract_date"]
    if contract and notice_date < contract:
        return "other"  # ninguna sociedad publica avisos antes de existir
    others = [r for r in rows if r["cuit"] != cuit]
    if all(r["contract_date"] and r["contract_date"] > notice_date for r in others):
        return "same"
    return None


def awardee_cuit(con: sqlite3.Connection, name: str) -> tuple[str, str] | None:
    """CUIT de una adjudicataria por nombre si es único: (cuit, fuente)."""
    rows = companies_named(con, name)
    if len(rows) == 1:
        return rows[0]["cuit"], "Registro Nacional de Sociedades"
    if not rows:
        people = persons_named(con, name)
        if len(people) == 1 and len(_tokens(name)) >= 3:
            return people[0], "padrón ARCA"
    return None


def available() -> bool:
    p = config.DATA_DIR / "sources.db"
    return p.exists() and p.stat().st_size > 1_000_000
