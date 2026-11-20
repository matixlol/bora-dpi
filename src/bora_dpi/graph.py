"""Consultas del grafo: organismos → adjudicatarias ← personas → otras sociedades.

El DNI se usa solo para el cotejo interno: ninguna función de este módulo lo devuelve.
"""

from collections import deque

from .bora import notice_url
from .text import cuit_fmt

LINKED = ("dni", "confirmed")


def _company_node(c) -> dict:
    return {
        "id": f"c{c['id']}", "type": "company", "label": c["name"], "cuit": cuit_fmt(c["cuit"]),
        "has_tech_award": bool(c["has_tech_award"]), "objeto_tech": c["objeto_tech"],
    }


def companies(con, ids=None) -> dict:
    q = """SELECT c.*, EXISTS(SELECT 1 FROM awards a WHERE a.company_id=c.id AND a.is_tech=1
                                AND a.status='confirmed') AS has_tech_award FROM companies c"""
    rows = con.execute(q + (f" WHERE c.id IN ({','.join('?' * len(ids))})" if ids is not None else ""),
                       list(ids or [])).fetchall()
    return {r["id"]: r for r in rows}


def edges_of(con, kind: str, rid: int) -> list[dict]:
    """Aristas incidentes a un nodo (kind: company | person | organism)."""
    out = []
    if kind in ("company", "organism"):
        col = "company_id" if kind == "company" else "organism_id"
        for a in con.execute(
            f"""SELECT a.*, n.date, n.url FROM awards a JOIN notices n ON n.id=a.notice_id
                WHERE a.{col}=? AND a.is_tech=1 AND a.status IN ('confirmed','pending_review')""", (rid,)):
            out.append({
                "id": f"a{a['id']}", "source": f"o{a['organism_id']}", "target": f"c{a['company_id']}",
                "type": "award", "status": a["status"], "label": "adjudicó",
                "object": a["object"], "amount": a["amount"], "date": a["act_date"] or a["date"],
                "procedure": a["procedure"], "evidence": a["evidence"],
                "notice_id": a["notice_id"], "url": a["url"],
            })
    if kind in ("company", "person"):
        col = "company_id" if kind == "company" else "person_id"
        for r in con.execute(
            f"""SELECT r.*, n.date, n.url FROM roles r JOIN notices n ON n.id=r.notice_id
                WHERE r.{col}=? AND r.match<>'rejected'""", (rid,)):
            out.append({
                "id": f"r{r['id']}", "source": f"p{r['person_id']}", "target": f"c{r['company_id']}",
                "type": "role", "status": "confirmed" if r["match"] in LINKED or r["match"] == "unmatched" else r["match"],
                "label": r["role"], "date": r["act_date"] or r["date"], "evidence": r["evidence"],
                "notice_id": r["notice_id"], "url": r["url"], "identified_by": (
                    "padrón ARCA" if r["match"] == "dni" and r["dni_status"] == "registry"
                    else "misma sociedad" if r["dni_status"] == "same_company"
                    else "red societaria" if r["dni_status"] == "co_partner"
                    else "DNI" if r["match"] == "dni" else "revisión" if r["match"] == "confirmed" else "nombre"),
            })
    if kind == "person":
        for r in con.execute(
            """SELECT r.id, r.person_id, r.candidate_person_id, rv.id AS review_id FROM roles r
               LEFT JOIN reviews rv ON rv.ref_table='roles' AND rv.ref_id=r.id
               WHERE r.match='pending_review' AND r.candidate_person_id IS NOT NULL
                 AND (r.person_id=? OR r.candidate_person_id=?)""", (rid, rid)):
            out.append({
                "id": f"m{r['id']}", "source": f"p{r['person_id']}", "target": f"p{r['candidate_person_id']}",
                "type": "possible_same", "status": "pending_review", "label": "¿misma persona?",
                "review_id": r["review_id"],
            })
    return out


def neighborhood(con, start: str, hops: int = 2, max_nodes: int = 400) -> dict:
    """BFS desde un nodo ('c12', 'p3', 'o5') sobre el grafo bipartito."""
    kinds = {"c": "company", "p": "person", "o": "organism"}
    seen, edges, q = {start}, {}, deque([(start, 0)])
    while q and len(seen) < max_nodes:
        node, d = q.popleft()
        if d >= hops:
            continue
        for e in edges_of(con, kinds[node[0]], int(node[1:])):
            if e["type"] == "possible_same":  # los homónimos sin DNI se cuentan en el nodo, no se dibujan
                continue
            edges[e["id"]] = e
            for n in (e["source"], e["target"]):
                if n not in seen:
                    seen.add(n)
                    q.append((n, d + 1))
    return build(con, seen, edges.values())


def build(con, node_ids, edges) -> dict:
    cids = [int(n[1:]) for n in node_ids if n[0] == "c"]
    pids = [int(n[1:]) for n in node_ids if n[0] == "p"]
    oids = [int(n[1:]) for n in node_ids if n[0] == "o"]
    nodes = [_company_node(c) for c in companies(con, cids).values()]
    if pids:
        for p in con.execute(f"""SELECT p.id, p.name, (SELECT COUNT(*) FROM roles r WHERE r.match='pending_review'
                                     AND r.candidate_person_id=p.id) AS homonyms
                                   FROM persons p WHERE p.id IN ({','.join('?' * len(pids))})""", pids):
            nodes.append({"id": f"p{p['id']}", "type": "person", "label": p["name"], "homonyms": p["homonyms"]})
    if oids:
        for o in con.execute(f"SELECT id, name FROM organisms WHERE id IN ({','.join('?' * len(oids))})", oids):
            nodes.append({"id": f"o{o['id']}", "type": "organism", "label": o["name"]})
    present = {n["id"] for n in nodes}
    return {"nodes": nodes, "edges": [e for e in edges if e["source"] in present and e["target"] in present]}


def overview(con) -> dict:
    """Mapa general: todas las adjudicatarias de tecnología y los vínculos por persona entre sociedades."""
    seeds = [f"c{r['company_id']}" for r in con.execute(
        "SELECT DISTINCT company_id FROM awards WHERE is_tech=1 AND status='confirmed'")]
    # Solo personas que unen sociedades: las que aparecen en una sola son hojas y se ven al abrir la sociedad
    seen, edges, persons = set(seeds), {}, set()
    for s in seeds:
        for e in edges_of(con, "company", int(s[1:])):
            if e["type"] == "award":
                edges[e["id"]] = e
                seen.update((e["source"], e["target"]))
            else:
                persons.update(n for n in (e["source"], e["target"]) if n[0] == "p")
    for p in persons:
        es = edges_of(con, "person", int(p[1:]))
        if len({e["target"] for e in es if e["type"] == "role"}) > 1:
            for e in [e for e in es if e["type"] != "possible_same"]:
                edges[e["id"]] = e
                seen.update((e["source"], e["target"]))
    # Una arista por par y tipo (los avisos repetidos se ven en el panel); sin citas: el panel las pide a /api/node
    edges = {(e["source"], e["target"], e["type"], e["status"]): {k: e[k] for k in ("id", "source", "target", "type", "status")}
             for e in edges.values()}
    return build(con, seen, edges.values())


def search(con, q: str, limit: int = 20) -> list[dict]:
    like = f"%{q.strip()}%"
    digits = "".join(ch for ch in q if ch.isdigit())
    out = []
    for c in con.execute(
        "SELECT DISTINCT c.id, c.name, c.cuit FROM companies c LEFT JOIN company_names n ON n.company_id=c.id "
        "WHERE c.name LIKE ? OR n.name LIKE ? OR (? <> '' AND c.cuit LIKE ?) LIMIT ?",
        (like, like, digits, f"%{digits}%", limit)):
        out.append({"id": f"c{c['id']}", "type": "company", "label": c["name"], "cuit": cuit_fmt(c["cuit"])})
    for p in con.execute("SELECT id, name FROM persons WHERE name LIKE ? LIMIT ?", (like, limit)):
        out.append({"id": f"p{p['id']}", "type": "person", "label": p["name"]})
    for o in con.execute("SELECT id, name FROM organisms WHERE name LIKE ? LIMIT ?", (like, limit)):
        out.append({"id": f"o{o['id']}", "type": "organism", "label": o["name"]})
    return out


def chains(con, limit: int = 20) -> list[dict]:
    """Prueba de punta a punta: X (adjudicación tecnológica) –socia P1 (DNI)→ Y –socio P2 (DNI)→ Z."""
    rows = con.execute(
        """WITH link AS (SELECT DISTINCT person_id, company_id FROM roles WHERE match IN ('dni','confirmed')),
                tech AS (SELECT DISTINCT company_id FROM awards WHERE is_tech=1 AND status='confirmed')
           SELECT x.company_id AS x, y.company_id AS y, z.company_id AS z,
                  MIN(printf('%012d:%012d', l1.person_id, l2.person_id)) AS pp  -- un par de personas distintas
           FROM tech t JOIN link x ON x.company_id=t.company_id
           JOIN link y  ON y.person_id=x.person_id AND y.company_id<>x.company_id
           JOIN link l1 ON l1.person_id=x.person_id AND l1.company_id=x.company_id
           JOIN link l2 ON l2.company_id=y.company_id AND l2.person_id<>l1.person_id
           JOIN link z  ON z.person_id=l2.person_id AND z.company_id NOT IN (x.company_id, y.company_id)
           GROUP BY x.company_id, y.company_id, z.company_id
           ORDER BY (SELECT COUNT(*) FROM awards a WHERE a.company_id=y.company_id AND a.is_tech=1) ASC""").fetchall()
    # Variedad: una cadena por par X→Y y como mucho cuatro por cada X
    pairs, per_x, picked = set(), {}, []
    for r in rows:
        if (r["x"], r["y"]) in pairs or (r["x"], r["z"]) in pairs or per_x.get(r["x"], 0) >= 4 or len(picked) >= limit:
            continue
        pairs.update({(r["x"], r["y"]), (r["x"], r["z"])})
        per_x[r["x"]] = per_x.get(r["x"], 0) + 1
        picked.append(r)
    out = []
    for r in picked:
        r = {**dict(r), **dict(zip(("p1", "p2"), map(int, r["pp"].split(":"))))}

        def src(pid, cid):
            s = con.execute(
                """SELECT r.role, r.act_date, r.notice_id, n.url, r.evidence FROM roles r JOIN notices n ON n.id=r.notice_id
                   WHERE r.person_id=? AND r.company_id=? AND r.match IN ('dni','confirmed') LIMIT 1""",
                (pid, cid)).fetchone()
            return dict(s)

        cs = companies(con, [r["x"], r["y"], r["z"]])
        name = lambda pid: con.execute("SELECT name FROM persons WHERE id=?", (pid,)).fetchone()["name"]
        aw = con.execute(
            """SELECT a.object, a.act_date, o.name AS organism, n.url FROM awards a JOIN organisms o ON o.id=a.organism_id
               JOIN notices n ON n.id=a.notice_id WHERE a.company_id=? AND a.is_tech=1 AND a.status='confirmed'""",
            (r["x"],)).fetchall()
        out.append({
            "x": _company_node(cs[r["x"]]), "y": _company_node(cs[r["y"]]), "z": _company_node(cs[r["z"]]),
            "x_awards": [dict(a) for a in aw],
            "y_tech_awards": con.execute("SELECT COUNT(*) FROM awards WHERE company_id=? AND is_tech=1",
                                         (r["y"],)).fetchone()[0],
            "p1": {"id": f"p{r['p1']}", "name": name(r["p1"]), "in_x": src(r["p1"], r["x"]), "in_y": src(r["p1"], r["y"])},
            "p2": {"id": f"p{r['p2']}", "name": name(r["p2"]), "in_y": src(r["p2"], r["y"]), "in_z": src(r["p2"], r["z"])},
        })
    return out


def notice(con, nid: str) -> dict | None:
    r = con.execute("SELECT id, section, date, title, rubro, url, text, text_source, duplicate_of FROM notices WHERE id=?",
                    (nid,)).fetchone()
    if not r:
        return None
    return {**dict(r), "text": r["text"], "url": r["url"] or notice_url(nid)}
