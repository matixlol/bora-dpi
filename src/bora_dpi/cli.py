"""Línea de comandos: relevamiento, investigación, actualización diaria, prueba X→Y→Z y servidor."""

import argparse
import json
import logging
from datetime import date, timedelta

from .db import connect


def main():
    ap = argparse.ArgumentParser(prog="bora-dpi")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("survey", help="relevar adjudicaciones de tecnología en la Tercera Sección")
    s.add_argument("--desde", type=date.fromisoformat, required=True)
    s.add_argument("--hasta", type=date.fromisoformat, default=date.today())
    s = sub.add_parser("investigate", help="seguir CUIT → socios (DNI) → otras sociedades")
    s.add_argument("--cuit", action="append", default=[], help="CUIT de partida (se puede repetir)")
    s.add_argument("--depth", type=int, default=2)
    s.add_argument("--limit", type=int, default=200)
    s = sub.add_parser("daily", help="registrar la edición del día (o --fecha) y relevar adjudicaciones")
    s.add_argument("--fecha", type=date.fromisoformat, default=date.today())
    s = sub.add_parser("backfill", help="registrar ediciones de un rango de fechas")
    s.add_argument("--desde", type=date.fromisoformat, required=True)
    s.add_argument("--hasta", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    sub.add_parser("sources", help="descargar e indexar el Registro Nacional de Sociedades y el padrón ARCA")
    sub.add_parser("autoresolve", help="resolver la cola de revisión con los registros oficiales")
    sub.add_parser("acceptance", help="prueba de punta a punta X→Y→Z sobre los datos cargados")
    s = sub.add_parser("export", help="exportar la UI como sitio estático de solo lectura")
    s.add_argument("--out", default="dist")
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8000)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if a.cmd == "serve":
        import uvicorn

        uvicorn.run("bora_dpi.api:app", host="0.0.0.0", port=a.port)
        return
    con = connect()
    if a.cmd == "acceptance":
        raise SystemExit(acceptance(con))
    if a.cmd == "export":
        from pathlib import Path

        from . import export

        print(json.dumps(export.build(con, Path(a.out))))
        return
    if a.cmd == "sources":
        from . import sources

        print(json.dumps(sources.build(sources.connect())))
        return
    from .pipeline import Pipeline

    p = Pipeline(con)
    if a.cmd == "survey":
        print(json.dumps({"tech_awardees": len(p.survey(a.desde, a.hasta))}))
    elif a.cmd == "investigate":
        seeds = []
        for c in a.cuit:
            from .text import cuit_digits

            row = con.execute("SELECT id FROM companies WHERE cuit=?", (cuit_digits(c),)).fetchone()
            seeds.append(row["id"] if row else p.company(f"CUIT {c}", cuit_digits(c), None, "manual", 0))
        print(json.dumps({"expanded": p.investigate(seeds, a.depth, a.limit)}))
    elif a.cmd == "autoresolve":
        print(json.dumps(p.autoresolve()))
    elif a.cmd == "daily":
        print(json.dumps(p.daily(a.fecha)))
    elif a.cmd == "backfill":
        for r in p.backfill(a.desde, a.hasta):
            print(json.dumps(r))


def acceptance(con) -> int:
    from . import graph

    chains = graph.chains(con)
    pending = con.execute(
        "SELECT COUNT(*) FROM roles WHERE match='pending_review' AND dni_status<>'ok'").fetchone()[0]
    leaked = con.execute("SELECT COUNT(*) FROM persons WHERE dni IS NOT NULL").fetchone()[0]
    print(f"Cadenas X→Y→Z encontradas: {len(chains)}")
    for c in chains[:5]:
        print(f"\nX  {c['x']['label']} (CUIT {c['x']['cuit']})")
        for aw in c["x_awards"][:3]:
            print(f"   adjudicación: {aw['organism']} — {aw['object']} ({aw['act_date']}) {aw['url']}")
        print(f"   └ {c['p1']['name']}: {c['p1']['in_x']['role']}  {c['p1']['in_x']['url']}")
        print(f"Y  {c['y']['label']} (CUIT {c['y']['cuit']}, adjudicaciones tecnológicas: {c['y_tech_awards']})")
        print(f"   └ {c['p1']['name']}: {c['p1']['in_y']['role']}  {c['p1']['in_y']['url']}")
        print(f"   └ {c['p2']['name']}: {c['p2']['in_y']['role']}  {c['p2']['in_y']['url']}")
        print(f"Z  {c['z']['label']} (CUIT {c['z']['cuit']})")
        print(f"   └ {c['p2']['name']}: {c['p2']['in_z']['role']}  {c['p2']['in_z']['url']}")
    print(f"\nCoincidencias por nombre sin DNI suficiente pendientes de revisión: {pending}")
    print(f"Personas identificadas por DNI: {leaked}")
    ok = bool(chains) and all(c["p1"]["in_x"]["url"] and c["p2"]["in_z"]["url"] for c in chains)
    print("RESULTADO:", "OK" if ok and pending else "INCOMPLETO")
    return 0 if ok and pending else 1
