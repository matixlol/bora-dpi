"""Archivo e índice: guarda avisos, detecta duplicados y recupera por búsqueda con caché."""

import json
import logging
from datetime import date, datetime

from . import bora
from .db import tx

log = logging.getLogger(__name__)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def upsert_listed(con, items: list[dict]) -> None:
    with tx(con):
        for it in items:
            con.execute(
                """INSERT INTO notices (id, section, bora_id, date, title, rubro, url)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     title = COALESCE(notices.title, excluded.title),
                     rubro = COALESCE(notices.rubro, excluded.rubro)""",
                (it["id"], it["section"], it["bora_id"], it["date"], it["title"] or None,
                 it.get("rubro") or None, bora.notice_url(it["id"])),
            )


def ensure_text(con, client: bora.Client, nid: str) -> dict | None:
    """Devuelve el aviso con texto, descargándolo si hace falta (reintenta los fallidos)."""
    row = con.execute("SELECT * FROM notices WHERE id=?", (nid,)).fetchone()
    if row and row["status"] == "fetched":
        return dict(row)
    if row and row["attempts"] >= 3:
        return None
    try:
        d = client.fetch_notice(nid, row["title"] if row else None)
    except bora.BoraError as e:
        mark_failed(con, nid, str(e))
        return None
    return store_fetched(con, nid, d)


def mark_failed(con, nid: str, err: str) -> None:
    """Registra la falla; el aviso se reintenta en la próxima pasada (hasta 3 intentos)."""
    section, bid, ymd = nid.split("/")
    with tx(con):
        con.execute(
            """INSERT INTO notices (id, section, bora_id, date, url, status, attempts, last_error)
               VALUES (?,?,?,?,?, 'failed', 1, ?)
               ON CONFLICT(id) DO UPDATE SET status='failed', attempts=notices.attempts+1, last_error=excluded.last_error""",
            (nid, section, bid, bora.ymd_to_iso(ymd), bora.notice_url(nid), err),
        )
    log.warning("falló %s: %s", nid, err)


def store_fetched(con, nid: str, d: dict) -> dict:
    """Guarda el aviso descargado, lo indexa y marca si duplica el texto de otro."""
    section, bid, ymd = nid.split("/")
    dup = None
    if d["sha256"]:
        r = con.execute(
            "SELECT id FROM notices WHERE sha256=? AND id<>? AND duplicate_of IS NULL", (d["sha256"], nid)
        ).fetchone()
        dup = r["id"] if r else None
    with tx(con):
        con.execute(
            """INSERT INTO notices (id, section, bora_id, date, title, rubro, url, text, text_source,
                                    raw_path, sha256, duplicate_of, status, attempts, fetched_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'fetched', 1, ?)
               ON CONFLICT(id) DO UPDATE SET
                 title=COALESCE(excluded.title, notices.title), rubro=COALESCE(excluded.rubro, notices.rubro),
                 text=excluded.text, text_source=excluded.text_source, raw_path=excluded.raw_path,
                 sha256=excluded.sha256, duplicate_of=excluded.duplicate_of, status='fetched',
                 attempts=notices.attempts+1, fetched_at=excluded.fetched_at, last_error=NULL""",
            (nid, section, bid, bora.ymd_to_iso(ymd), d["title"], d["rubro"], bora.notice_url(nid),
             d["text"], d["text_source"], d["raw_path"], d["sha256"], dup, now()),
        )
        con.execute("DELETE FROM notices_fts WHERE id=?", (nid,))
        row = con.execute("SELECT title FROM notices WHERE id=?", (nid,)).fetchone()
        con.execute("INSERT INTO notices_fts (id, title, text) VALUES (?,?,?)", (nid, row["title"], d["text"]))
    return dict(con.execute("SELECT * FROM notices WHERE id=?", (nid,)).fetchone())


def search(con, client: bora.Client, *, refresh: bool = False, **params) -> list[dict]:
    """Búsqueda avanzada en BORA con caché local; registra los avisos encontrados en el índice."""
    norm_params = {k: (v.isoformat() if isinstance(v, date) else list(v) if isinstance(v, tuple) else v)
                   for k, v in sorted(params.items())}
    key = json.dumps(norm_params, ensure_ascii=False, sort_keys=True)
    cached = con.execute("SELECT 1 FROM searches WHERE key=?", (key,)).fetchone()
    if cached and not refresh:
        rows = con.execute(
            "SELECT n.* FROM search_hits h JOIN notices n ON n.id=h.notice_id WHERE h.key=?", (key,)
        ).fetchall()
        return [dict(r) for r in rows]
    items = client.search(**params)
    upsert_listed(con, items)
    with tx(con):
        con.execute(
            "INSERT OR REPLACE INTO searches (key, params, n_results, fetched_at) VALUES (?,?,?,?)",
            (key, key, len(items), now()),
        )
        con.executemany("INSERT OR IGNORE INTO search_hits (key, notice_id) VALUES (?,?)",
                        [(key, it["id"]) for it in items])
    by_id = {it["id"]: it for it in items}
    rows = con.execute(
        f"SELECT * FROM notices WHERE id IN ({','.join('?' * len(by_id))})", list(by_id)
    ).fetchall() if by_id else []
    out = []
    for r in rows:
        d = dict(r)
        d["snippet"] = by_id[d["id"]].get("snippet")
        out.append(d)
    return out


def local_search(con, q: str, limit: int = 50) -> list[dict]:
    rows = con.execute(
        """SELECT n.id, n.section, n.date, n.title, n.rubro, n.url,
                  snippet(notices_fts, 2, '[', ']', ' … ', 24) AS snippet
           FROM notices_fts f JOIN notices n ON n.id=f.id
           WHERE notices_fts MATCH ? ORDER BY rank LIMIT ?""",
        (q, limit),
    ).fetchall()
    return [dict(r) for r in rows]
