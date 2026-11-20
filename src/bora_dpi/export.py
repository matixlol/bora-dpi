"""Exporta la API a archivos JSON para publicar la UI como sitio estático de solo lectura."""

import json
import re
import shutil
from pathlib import Path

from . import api, graph
from .text import cuit_fmt

STATIC = Path(__file__).parent / "static"


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def _lean(g: dict) -> dict:
    """Grafo sin citas ni aristas repetidas: el panel lee las citas de node/<id>.json."""
    edges = {(e["source"], e["target"], e["type"], e["status"]): {k: e[k] for k in ("id", "source", "target", "type", "status")}
             for e in g["edges"]}
    return {"nodes": g["nodes"], "edges": list(edges.values())}


def build(con, out: Path) -> dict:
    if out.exists():
        shutil.rmtree(out)
    a = out / "api"
    html = (STATIC / "index.html").read_text()
    (out).mkdir(parents=True)
    (out / "index.html").write_text(html.replace("<head>", "<head>\n<script>window.STATIC_SITE = true</script>", 1))
    _write(a / "stats.json", api.stats())
    _write(a / "chains.json", api.chains())
    _write(a / "graph.json", graph.overview(con))
    for s in ("pending", "auto"):
        _write(a / f"reviews-{s}.json", api.reviews(s, 1000))

    index, ids = [], []
    for c in con.execute("SELECT id, name, cuit, (SELECT group_concat(name, ' | ') FROM company_names n WHERE n.company_id=c.id) AS alias "
                         "FROM companies c"):
        index.append({"id": f"c{c['id']}", "type": "company", "label": c["name"], "cuit": cuit_fmt(c["cuit"]),
                      "alias": c["alias"] or ""})
    for p in con.execute("SELECT id, name FROM persons"):
        index.append({"id": f"p{p['id']}", "type": "person", "label": p["name"]})
    for o in con.execute("SELECT id, name FROM organisms"):
        index.append({"id": f"o{o['id']}", "type": "organism", "label": o["name"]})
    _write(a / "search.json", index)
    for n in index:
        ids.append(n["id"])
        _write(a / "node" / f"{n['id']}.json", api.node(n["id"]))
        _write(a / "graph" / f"{n['id']}.json", _lean(graph.neighborhood(con, n["id"], 3)))

    notices = [{"id": r["id"], "section": r["section"], "date": r["date"], "title": r["title"], "rubro": r["rubro"],
                "url": r["url"], "text": re.sub(r"\s+", " ", r["text"] or "")}
               for r in con.execute("SELECT * FROM notices WHERE status='fetched' AND duplicate_of IS NULL ORDER BY date DESC")]
    _write(a / "notices.json", notices)
    return {"nodes": len(ids), "notices": len(notices), "files": sum(1 for _ in out.rglob("*") if _.is_file())}
