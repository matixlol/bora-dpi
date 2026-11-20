"""API HTTP y UI del grafo y la cola de revisión."""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import archive, graph
from .archive import now
from .db import connect, tx
from .text import cuit_fmt

app = FastAPI(title="BORA DPI")
con = connect()
STATIC = Path(__file__).parent / "static"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/stats")
def stats():
    one = lambda q: con.execute(q).fetchone()[0]
    return {
        "notices": one("SELECT COUNT(*) FROM notices WHERE status='fetched'"),
        "notices_failed": one("SELECT COUNT(*) FROM notices WHERE status='failed'"),
        "duplicates": one("SELECT COUNT(*) FROM notices WHERE duplicate_of IS NOT NULL"),
        "ocr_notices": one("SELECT COUNT(*) FROM notices WHERE text_source LIKE 'ocr%' OR text_source LIKE 'pdf%'"),
        "editions": {r[0]: r[1] for r in con.execute("SELECT status, COUNT(*) FROM editions GROUP BY status")},
        "tech_awards": one("SELECT COUNT(*) FROM awards WHERE is_tech=1 AND status='confirmed'"),
        "awardees": one("SELECT COUNT(DISTINCT company_id) FROM awards WHERE is_tech=1 AND status='confirmed'"),
        "companies": one("SELECT COUNT(*) FROM companies"),
        "companies_expanded": one("SELECT COUNT(*) FROM companies WHERE expanded_at IS NOT NULL"),
        "persons_dni": one("SELECT COUNT(*) FROM persons WHERE dni IS NOT NULL"),
        "roles": one("SELECT COUNT(*) FROM roles"),
        "reviews_pending": one("SELECT COUNT(*) FROM reviews WHERE status='pending'"),
        "reviews_auto": one("SELECT COUNT(*) FROM reviews WHERE note LIKE 'Automático:%'"),
        "persons_registry": one("SELECT COUNT(DISTINCT person_id) FROM roles WHERE dni_status='registry'"),
    }


@app.get("/api/search")
def search(q: str):
    return graph.search(con, q)


@app.get("/api/graph")
def get_graph(node: str | None = None, hops: int = 2):
    return graph.neighborhood(con, node, min(hops, 4)) if node else graph.overview(con)


@app.get("/api/node/{node}")
def node(node: str):
    kind, rid = node[0], int(node[1:])
    if kind == "c":
        c = con.execute("SELECT * FROM companies WHERE id=?", (rid,)).fetchone()
        if not c:
            raise HTTPException(404)
        names = [r[0] for r in con.execute("SELECT name FROM company_names WHERE company_id=?", (rid,))]
        return {"type": "company", "name": c["name"], "cuit": cuit_fmt(c["cuit"]), "cuit_source": c["cuit_source"],
                "names": names, "objeto": c["objeto"], "objeto_tech": c["objeto_tech"],
                "objeto_source": c["objeto_source"], "origin": c["origin"], "depth": c["depth"],
                "expanded_at": c["expanded_at"], "edges": graph.edges_of(con, "company", rid)}
    if kind == "p":
        p = con.execute("SELECT id, name, dni IS NOT NULL AS has_dni FROM persons WHERE id=?", (rid,)).fetchone()
        if not p:
            raise HTTPException(404)
        return {"type": "person", "name": p["name"], "identified_by_dni": bool(p["has_dni"]),
                "edges": graph.edges_of(con, "person", rid)}
    o = con.execute("SELECT * FROM organisms WHERE id=?", (rid,)).fetchone()
    if not o:
        raise HTTPException(404)
    return {"type": "organism", "name": o["name"], "edges": graph.edges_of(con, "organism", rid)}


@app.get("/api/notice/{nid:path}")
def notice(nid: str):
    n = graph.notice(con, nid)
    if not n:
        raise HTTPException(404)
    return n


@app.get("/api/notices")
def notices(q: str):
    return archive.local_search(con, q)


@app.get("/api/chains")
def chains():
    return graph.chains(con)


@app.get("/api/reviews")
def reviews(status: str = "pending", limit: int = 200):
    """status: pending | auto (resueltas con el RNS y el padrón ARCA)."""
    q = ("SELECT * FROM reviews WHERE note LIKE 'Automático:%' ORDER BY decided_at DESC, id LIMIT ?" if status == "auto"
         else "SELECT * FROM reviews WHERE status=? ORDER BY id LIMIT ?")
    out = []
    for r in con.execute(q, (limit,) if status == "auto" else (status, limit)):
        item = dict(r)
        if r["ref_table"] == "roles":
            x = con.execute(
                """SELECT r.*, c.name AS company, c.cuit AS company_cuit, n.url, n.date AS notice_date,
                          cp.name AS candidate_name FROM roles r
                   JOIN companies c ON c.id=r.company_id JOIN notices n ON n.id=r.notice_id
                   LEFT JOIN persons cp ON cp.id=r.candidate_person_id WHERE r.id=?""", (r["ref_id"],)).fetchone()
            cand = [dict(e) for e in con.execute(
                """SELECT c.name AS company, r.role, n.url, r.notice_id, n.date, r.evidence FROM roles r JOIN companies c ON c.id=r.company_id
                   JOIN notices n ON n.id=r.notice_id WHERE r.person_id=? LIMIT 5""", (x["candidate_person_id"],))]
            item["detail"] = {"mention": x["mention_name"], "company": x["company"], "role": x["role"],
                              "dni_status": x["dni_status"], "url": x["url"], "evidence": x["evidence"],
                              "notice_id": x["notice_id"], "date": x["notice_date"], "company_cuit": cuit_fmt(x["company_cuit"]),
                              "candidate": x["candidate_name"], "candidate_node": f"p{x['candidate_person_id']}",
                              "candidate_roles": [dict(c) for c in cand]}
        else:
            x = con.execute(
                """SELECT a.*, n.url, n.date AS notice_date, o.name AS organism FROM awards a JOIN notices n ON n.id=a.notice_id
                   LEFT JOIN organisms o ON o.id=a.organism_id WHERE a.id=?""", (r["ref_id"],)).fetchone()
            item["detail"] = {"awardee": x["awardee_name"], "cuit_raw": x["cuit_raw"], "organism": x["organism"],
                              "object": x["object"], "url": x["url"], "evidence": x["evidence"],
                              "notice_id": x["notice_id"], "date": x["notice_date"]}
        out.append(item)
    return out


class Decision(BaseModel):
    decision: str  # confirm | reject
    note: str | None = None
    cuit: str | None = None  # para completar un CUIT faltante desde otra fuente oficial


@app.post("/api/reviews/{rid}")
def decide(rid: int, d: Decision):
    from .text import cuit_digits, cuit_valid

    r = con.execute("SELECT * FROM reviews WHERE id=?", (rid,)).fetchone()
    if not r or d.decision not in ("confirm", "reject"):
        raise HTTPException(400)
    ok = d.decision == "confirm"
    with tx(con):
        if r["ref_table"] == "roles":
            if ok:
                if r["kind"] == "name_match":
                    con.execute("UPDATE roles SET person_id=candidate_person_id, match='confirmed' WHERE id=?",
                                (r["ref_id"],))
                else:
                    # misma sociedad o mismo DNI confirmado: el vínculo se activa
                    con.execute("UPDATE roles SET match=CASE WHEN dni_status='ok' THEN 'dni' ELSE 'confirmed' END "
                                "WHERE id=?", (r["ref_id"],))
            else:
                con.execute("UPDATE roles SET match='rejected' WHERE id=?", (r["ref_id"],))
        else:
            if ok and r["kind"] == "cuit_missing":
                if not cuit_valid(d.cuit):
                    raise HTTPException(400, "CUIT inválido (dígito verificador)")
                a = con.execute("SELECT company_id FROM awards WHERE id=?", (r["ref_id"],)).fetchone()
                con.execute("UPDATE companies SET cuit=?, cuit_source=? WHERE id=?",
                            (cuit_digits(d.cuit), f"revisión: {d.note or 'otra fuente oficial'}", a["company_id"]))
            con.execute("UPDATE awards SET status=? WHERE id=?", ("confirmed" if ok else "rejected", r["ref_id"]))
        con.execute("UPDATE reviews SET status=?, decided_at=?, note=? WHERE id=?",
                    ("confirmed" if ok else "rejected", now(), d.note, rid))
        if r["kind"] == "company_name_match":
            # La decisión es sobre el aviso: se aplica a todas las personas del mismo aviso
            role = con.execute("SELECT notice_id, company_id FROM roles WHERE id=?", (r["ref_id"],)).fetchone()
            for s in con.execute(
                """SELECT rv.id, rv.ref_id FROM reviews rv JOIN roles x ON x.id=rv.ref_id
                   WHERE rv.kind='company_name_match' AND rv.status='pending' AND x.notice_id=? AND x.company_id=?""",
                (role["notice_id"], role["company_id"])).fetchall():
                con.execute("UPDATE roles SET match=? WHERE id=?",
                            ("rejected" if not ok else "confirmed", s["ref_id"]))
                if ok:
                    con.execute("UPDATE roles SET match='dni' WHERE id=? AND dni_status='ok'", (s["ref_id"],))
                con.execute("UPDATE reviews SET status=?, decided_at=?, note=? WHERE id=?",
                            ("confirmed" if ok else "rejected", now(), d.note, s["id"]))
    return {"ok": True}


@app.get("/api/export/graph.json")
def export():
    """Exportación del grafo con fuentes."""
    return graph.overview(con)
