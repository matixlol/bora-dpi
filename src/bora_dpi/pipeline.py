"""Relevamiento de adjudicaciones e investigación CUIT → socios (DNI) → otras sociedades."""

import difflib
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

from . import archive, bora, sources
from .archive import now
from .db import tx
from .extract import Extractor, LLMError
from .text import company_key, cuit_fmt, dni_variants, norm, person_key

log = logging.getLogger(__name__)

# Términos para identificar compras de tecnología en la Tercera Sección (se combinan con
# "adjudicación"); el modelo confirma luego si el objeto es tecnológico.
TECH_TERMS = [
    "software", "licencias", "informático", "informáticos", "informática", "servidores",
    "computadoras", "notebooks", "hardware", "telecomunicaciones", "redes", "ciberseguridad",
    "datos", "sistema", "nube", "conectividad", "impresoras", "almacenamiento",
]
NAME_SEARCH_MAX_HITS = 40  # nombres más comunes que esto no se siguen sin DNI
WORKERS = 2  # descargas en paralelo (BORA limita la tasa con un limitador global)
LLM_WORKERS = 8
NAME_SEARCH_MAX_PEOPLE = 15
SOC_PER_COMPANY = 30  # avisos societarios por empresa: los más recientes y los más antiguos (constitución)
SOC_PER_PERSON = 20
AWARDS_PER_COMPANY = 15


def cap(hits: list[dict], n: int) -> list[str]:
    ids = sorted({h["id"]: h["date"] for h in hits}.items(), key=lambda kv: kv[1])
    if len(ids) > n:
        ids = ids[:5] + ids[-(n - 5):]
    return [i for i, _ in ids]


class Pipeline:
    def __init__(self, con, extractor: Extractor | None = None, client: bora.Client | None = None):
        self.con = con
        self.ex = extractor or Extractor()
        self.client = client or bora.Client()
        self._clients = [bora.Client() for _ in range(WORKERS)]
        # Registro Nacional de Sociedades y padrón ARCA (ver sources.py): resuelven coincidencias
        # sin trabajo manual cuando están indexados (`bora-dpi sources`)
        self.src = sources.connect() if sources.available() else None

    # --- Descarga + extracción en paralelo ---------------------------------

    def prepare(self, notice_ids: list[str], kind: str) -> dict[str, tuple[dict, dict] | None]:
        """Descarga el texto y extrae datos (con caché) para varios avisos a la vez."""
        todo = [n for n in dict.fromkeys(notice_ids)]
        cached = {
            r["notice_id"]: json.loads(r["data"])
            for r in self.con.execute(
                f"SELECT notice_id, data FROM extractions WHERE kind=? AND notice_id IN ({','.join('?' * len(todo))})",
                [kind, *todo],
            )
        } if todo else {}

        status = {r["id"]: (r["status"], r["attempts"], r["title"]) for r in self.con.execute(
            f"SELECT id, status, attempts, title FROM notices WHERE id IN ({','.join('?' * len(todo))})", todo)} if todo else {}

        def work(i_nid):
            i, nid = i_nid
            client = self._clients[i % WORKERS]
            st, attempts, title = status.get(nid, (None, 0, None))
            if st == "failed" and attempts >= 3:
                return nid, None, "demasiados intentos"
            if st != "fetched":
                try:
                    d = client.fetch_notice(nid, title)
                except bora.BoraError as e:
                    return nid, None, str(e)
                return nid, d, None
            return nid, None, None

        fetched = {}
        with ThreadPoolExecutor(WORKERS) as pool:
            for nid, d, err in pool.map(work, enumerate(todo)):
                fetched[nid] = (d, err)
        # Escritura en la base (secuencial) reutilizando la lógica de archivo
        notices = {}
        for nid in todo:
            d, err = fetched[nid]
            if d is not None:
                notices[nid] = archive.store_fetched(self.con, nid, d)
            elif err:
                archive.mark_failed(self.con, nid, err)
                notices[nid] = None
            else:
                notices[nid] = dict(self.con.execute("SELECT * FROM notices WHERE id=?", (nid,)).fetchone())

        def extract(nid):
            n = notices.get(nid)
            if not n or not n.get("text") or (n.get("text_source") or "").endswith("_page"):
                return nid, None
            if nid in cached:
                return nid, cached.get(nid)
            try:
                data = self.ex.award(n["text"]) if kind == "award" else self.ex.societario(n["text"])
            except LLMError as e:
                log.warning("extracción falló %s: %s", nid, e)
                return nid, None
            return nid, data

        out = {}
        with ThreadPoolExecutor(LLM_WORKERS) as pool:
            for fut in as_completed([pool.submit(extract, n) for n in todo]):
                nid, data = fut.result()
                if data is not None and nid not in cached:
                    with tx(self.con):
                        self.con.execute(
                            "INSERT OR REPLACE INTO extractions (notice_id, kind, model, data, created_at) VALUES (?,?,?,?,?)",
                            (nid, kind, self.ex.model, json.dumps(data, ensure_ascii=False), now()),
                        )
                out[nid] = (notices[nid], data) if data is not None and notices.get(nid) else None
        return out

    # --- Entidades ----------------------------------------------------------

    def company(self, name: str, cuit: str | None, source: str | None, origin: str, depth: int | None) -> int:
        con, key = self.con, company_key(name)
        row = None
        if cuit:
            row = con.execute("SELECT * FROM companies WHERE cuit=?", (cuit,)).fetchone()
        if not row:
            row = con.execute(
                "SELECT c.* FROM companies c JOIN company_names n ON n.company_id=c.id WHERE n.key=? "
                "AND (c.cuit IS NULL OR ? IS NULL) ORDER BY c.id LIMIT 1", (key, cuit),
            ).fetchone()
        with tx(con):
            if row:
                cid = row["id"]
                if cuit and not row["cuit"]:
                    con.execute("UPDATE companies SET cuit=?, cuit_source=? WHERE id=?", (cuit, source, cid))
                if depth is not None and (row["depth"] is None or depth < row["depth"]):
                    con.execute("UPDATE companies SET depth=? WHERE id=?", (depth, cid))
            else:
                cid = con.execute(
                    "INSERT INTO companies (cuit, cuit_source, name, key, origin, depth) VALUES (?,?,?,?,?,?)",
                    (cuit, source if cuit else None, name.strip(), key, origin, depth),
                ).lastrowid
            con.execute("INSERT OR IGNORE INTO company_names (company_id, key, name) VALUES (?,?,?)",
                        (cid, key, name.strip()))
        return cid

    def organism(self, name: str) -> int:
        key = norm(name)
        with tx(self.con):
            self.con.execute("INSERT OR IGNORE INTO organisms (name, key) VALUES (?,?)", (name.strip(), key))
        return self.con.execute("SELECT id FROM organisms WHERE key=?", (key,)).fetchone()["id"]

    def review(self, kind: str, table: str, ref: int, summary: str) -> None:
        self.con.execute(
            "INSERT OR IGNORE INTO reviews (kind, ref_table, ref_id, summary) VALUES (?,?,?,?)",
            (kind, table, ref, summary),
        )

    # --- Adjudicaciones -----------------------------------------------------

    def process_awards(self, notice_ids: list[str], depth: int = 0) -> list[int]:
        """Registra adjudicaciones; devuelve las empresas adjudicatarias de tecnología."""
        companies = []
        for nid, res in self.prepare(notice_ids, "award").items():
            if not res:
                continue
            notice, d = res
            if not d.get("is_award") or not d.get("awardees"):
                continue
            org = self.organism(" ".join((d["organism"] or notice["title"] or "Organismo sin identificar").split()))
            for a in d["awardees"]:
                if not a.get("name"):
                    continue
                cuit = a["cuit"] if a.get("cuit_valid") else None
                cuit_src = nid if cuit else None
                if not cuit and self.src:
                    found = sources.awardee_cuit(self.src, a["name"])
                    if found:
                        cuit, cuit_src = found
                tech = bool(d.get("is_tech"))
                # Solo las adjudicatarias de tecnología son puntos de partida de la investigación
                cid = self.company(a["name"], cuit, cuit_src, "award", depth if tech else None)
                ok = tech and cuit and a.get("evidence_score", 0) >= 0.8
                status = "confirmed" if ok else ("pending_review" if tech else "not_tech")
                with tx(self.con):
                    cur = self.con.execute(
                        """INSERT INTO awards (notice_id, organism_id, company_id, awardee_name, cuit_raw, cuit_valid,
                             procedure, act_date, object, amount, is_tech, tech_reason, status, evidence, evidence_score)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(notice_id, awardee_name) DO NOTHING""",
                        (nid, org, cid, a["name"], a.get("cuit_raw"), int(bool(a.get("cuit_valid"))),
                         d.get("procedure"), d.get("act_date") or notice["date"], d.get("object"), a.get("amount"),
                         int(tech), d.get("tech_reason"), status, a.get("evidence"), a.get("evidence_score")),
                    )
                    if cur.rowcount and status == "pending_review":
                        aid = cur.lastrowid
                        if not cuit:
                            self.review("cuit_missing", "awards", aid,
                                        f"Adjudicación a {a['name']} sin CUIT válido ({a.get('cuit_raw') or 'no consta'})")
                        else:
                            self.review("award_evidence", "awards", aid,
                                        f"La cita de la adjudicación a {a['name']} no coincide con el aviso")
                if tech:
                    companies.append(cid)
        return list(dict.fromkeys(companies))

    def survey(self, desde: date, hasta: date, terms: list[str] = TECH_TERMS) -> list[int]:
        """Relevamiento: busca actos de adjudicación de tecnología en la Tercera Sección."""
        ids = []
        for t in terms:
            for n in archive.search(self.con, self.client, texto=f"adjudicación {t}", secciones=(3,),
                                    desde=desde, hasta=hasta, max_pages=5):
                ids.append(n["id"])
        log.info("relevamiento %s..%s: %d avisos candidatos", desde, hasta, len(set(ids)))
        return self.process_awards(ids, depth=0)

    # --- Avisos societarios -------------------------------------------------

    def process_societarios(self, notice_ids: list[str], depth: int, owner: int | None = None) -> set[int]:
        """Registra sociedades y personas de avisos de la Segunda Sección.

        owner: empresa a la que se atribuye el aviso cuando se buscó por su CUIT o denominación.
        Devuelve las empresas tocadas.
        """
        touched = set()
        for nid, res in self.prepare(notice_ids, "soc").items():
            if not res:
                continue
            notice, d = res
            name = notice["title"] or d.get("company")
            if not name or not is_corporate(notice["rubro"]):
                continue
            cuit = d.get("cuit")
            owner_cid = None
            if owner is not None:
                o = self.con.execute("SELECT * FROM companies WHERE id=?", (owner,)).fetchone()
                known = {r["key"] for r in self.con.execute("SELECT key FROM company_names WHERE company_id=?", (owner,))}
                head = (notice["text"] or "")[:300]
                # El aviso es de la empresa si su denominación coincide, o si abre con su CUIT
                # (un aviso que solo menciona el CUIT, p. ej. de una accionista o cliente, no cuenta).
                if company_key(name) in known or company_key(d.get("company") or "") in known or (
                    o["cuit"] and cuit == o["cuit"] and cuit_fmt(o["cuit"]) in head
                ):
                    owner_cid = owner
                else:
                    continue  # aviso de otra sociedad que solo menciona a la buscada
            name_only = False
            if owner_cid:
                # Atribución solo por denominación a una sociedad con CUIT conocido: puede ser un
                # homónimo (p. ej. una sociedad disuelta con el mismo nombre); sus vínculos van a revisión.
                name_only = bool(o["cuit"]) and cuit != o["cuit"] and cuit_fmt(o["cuit"]) not in (notice["text"] or "")
                if name_only and self.src:
                    verdict = sources.company_attribution(self.src, name, o["cuit"], notice["date"])
                    if verdict == "other":
                        continue  # homónimo según el Registro Nacional de Sociedades
                    name_only = verdict != "same"
                cid = owner_cid
                with tx(self.con):
                    self.con.execute("INSERT OR IGNORE INTO company_names (company_id, key, name) VALUES (?,?,?)",
                                     (cid, company_key(name), name.strip()))
                    if cuit and not o["cuit"]:
                        self.con.execute("UPDATE companies SET cuit=?, cuit_source=? WHERE id=?", (cuit, nid, cid))
            else:
                cid = self.company(name, cuit, nid, "partner_link", depth)
            if d.get("objeto"):
                with tx(self.con):
                    self.con.execute(
                        "UPDATE companies SET objeto=?, objeto_tech=?, objeto_source=? WHERE id=? AND objeto IS NULL",
                        (d["objeto"], int(bool(d.get("objeto_tech"))), nid, cid),
                    )
            for p in d.get("people") or []:
                if p.get("name"):
                    self._role(nid, cid, p, notice["date"], company_by_name=name_only)
            touched.add(cid)
        return touched

    def _role(self, nid: str, cid: int, p: dict, notice_date: str, company_by_name: bool = False) -> None:
        con, key = self.con, person_key(p["name"])
        dni = p.get("dni")
        if not dni and p.get("dni_status", "missing") == "missing" and self.src:
            dni = sources.unique_person_dni(self.src, p["name"])
            if dni:
                p = {**p, "dni": dni, "dni_status": "registry"}  # DNI del padrón ARCA: nombre único
        match, person_id, cand = "unmatched", None, None
        review = None
        with tx(con):
            if dni:
                row = con.execute("SELECT * FROM persons WHERE dni=?", (dni,)).fetchone()
                if row:
                    person_id, match = row["id"], "dni"
                    if not names_compatible(row["name"], p["name"]):
                        match = "pending_review"
                        review = ("dni_inconsistent",
                                  f"El mismo DNI figura como «{row['name']}» y como «{p['name']}»")
                else:
                    person_id = con.execute("INSERT INTO persons (name, key, dni) VALUES (?,?,?)",
                                            (p["name"], key, dni)).lastrowid
                    match = "dni"
                # Menciones previas del mismo nombre sin DNI pasan a revisión contra esta persona
                for r in con.execute(
                    """SELECT r.id, r.mention_name, c.name AS company FROM roles r
                       JOIN persons x ON x.id=r.person_id JOIN companies c ON c.id=r.company_id
                       WHERE x.dni IS NULL AND r.match='unmatched' AND r.mention_key=?""", (key,),
                ).fetchall():
                    con.execute("UPDATE roles SET match='pending_review', candidate_person_id=? WHERE id=?",
                                (person_id, r["id"]))
                    self.review("name_match", "roles", r["id"],
                                f"«{r['mention_name']}» ({r['company']}) coincide por nombre, sin DNI, con una persona identificada por DNI")
            else:
                # Mismo nombre en la misma sociedad: se reutiliza la persona solo si ese vínculo ya está
                # verificado (un vínculo pendiente no puede validar a otro)
                local = con.execute(
                    """SELECT person_id FROM roles WHERE company_id=? AND mention_key=? AND person_id IS NOT NULL
                       AND match IN ('dni','confirmed') LIMIT 1""",
                    (cid, key),
                ).fetchone()
                if local:
                    person_id = local["person_id"]
                    match = "dni" if con.execute("SELECT dni FROM persons WHERE id=?", (person_id,)).fetchone()["dni"] else "unmatched"
                else:
                    others = con.execute(
                        """SELECT DISTINCT x.* FROM persons x JOIN roles r ON r.person_id=x.id
                           WHERE x.key=? AND r.company_id<>? ORDER BY (x.dni IS NULL), x.id""", (key, cid),
                    ).fetchall()
                    person_id = con.execute("INSERT INTO persons (name, key) VALUES (?,?)",
                                            (p["name"], key)).lastrowid
                    if others:
                        cand, match = others[0]["id"], "pending_review"
                        review = ("name_match",
                                  f"«{p['name']}» coincide por nombre con «{others[0]['name']}» de otra sociedad, sin DNI suficiente"
                                  + (f" (DNI {p['dni_status']})" if p.get("dni_status") not in (None, "missing") else ""))
            if company_by_name and match in ("dni", "unmatched"):
                c = con.execute("SELECT name, cuit FROM companies WHERE id=?", (cid,)).fetchone()
                match = "pending_review"
                review = ("company_name_match",
                          f"Aviso atribuido a {c['name']} (CUIT {cuit_fmt(c['cuit'])}) solo por denominación: "
                          f"confirmar que es la misma sociedad antes de vincular a «{p['name']}»")
            role = (p.get("role") or "").strip().lower() + (" (cesa)" if p.get("ceased") else "")
            cur = con.execute(
                """INSERT INTO roles (notice_id, company_id, person_id, candidate_person_id, mention_name, mention_key,
                     role, act_date, dni_status, match, evidence, evidence_score)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                (nid, cid, person_id, cand, p["name"], key, role, p.get("date") or notice_date,
                 p.get("dni_status") or "missing", match, p.get("evidence"), p.get("evidence_score")),
            )
            if cur.rowcount and review:
                self.review(review[0], "roles", cur.lastrowid, review[1])

    # --- Investigación ------------------------------------------------------

    def expand(self, cid: int) -> None:
        """Un paso del flujo: avisos de la empresa, sus adjudicaciones y las sociedades de sus socios."""
        c = self.con.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()
        depth = c["depth"] or 0
        log.info("expandiendo [%d] %s (CUIT %s, nivel %d)", cid, c["name"], c["cuit"], depth)
        # 1. Avisos societarios por CUIT y por denominación (con sus variantes)
        soc = []
        if c["cuit"]:
            soc += archive.search(self.con, self.client, texto=cuit_fmt(c["cuit"]), secciones=(2,))
        names = [r["name"] for r in self.con.execute("SELECT name FROM company_names WHERE company_id=?", (cid,))]
        keys = {company_key(n) for n in names}
        for n in names:
            hits = archive.search(self.con, self.client, denominacion=strip_type(n), secciones=(2,), max_pages=3)
            soc += [h for h in hits if company_key(h["title"] or "") in keys]
        self.process_societarios(cap(soc, SOC_PER_COMPANY), depth, owner=cid)
        c = self.con.execute("SELECT * FROM companies WHERE id=?", (cid,)).fetchone()  # puede haber ganado CUIT
        # 2. Otras adjudicaciones por CUIT
        if c["cuit"]:
            hits = archive.search(self.con, self.client, texto=cuit_fmt(c["cuit"]), secciones=(3,))
            hits = sorted(hits, key=lambda h: h["date"], reverse=True)[:AWARDS_PER_COMPANY]  # el resto queda indexado
            self.process_awards([h["id"] for h in hits], depth)
        # 3. Seguir a cada socia o autoridad por DNI hacia otras sociedades
        people = self.con.execute(
            """SELECT DISTINCT x.id, x.name, x.dni FROM roles r JOIN persons x ON x.id=r.person_id
               WHERE r.company_id=? AND r.match IN ('dni','confirmed')""", (cid,),
        ).fetchall()
        for x in people:
            if x["dni"]:
                ids = []
                for v in dni_variants(x["dni"]):
                    ids += [h["id"] for h in archive.search(self.con, self.client, texto=v, secciones=(2,))]
                self.process_societarios(list(dict.fromkeys(ids))[:SOC_PER_PERSON], depth + 1)
        # 4. Sin DNI: búsqueda por nombre; las coincidencias quedan pendientes de revisión.
        # En directorios grandes (más de NAME_SEARCH_MAX_PEOPLE personas) se omite: costo alto y
        # coincidencias poco informativas.
        no_dni = self.con.execute(
            """SELECT DISTINCT x.name FROM roles r JOIN persons x ON x.id=r.person_id
               WHERE r.company_id=? AND x.dni IS NULL""", (cid,),
        ).fetchall()
        for x in (no_dni if len(no_dni) <= NAME_SEARCH_MAX_PEOPLE else []):
            if len(norm(x["name"]).split()) < 3:
                continue
            hits = archive.search(self.con, self.client, texto=f'{x["name"]}', secciones=(2,), max_pages=1)
            hits = [h for h in hits if company_key(h["title"] or "") not in keys]
            if 0 < len(hits) <= NAME_SEARCH_MAX_HITS:
                self.process_societarios([h["id"] for h in hits[:5]], depth + 1)
        with tx(self.con):
            self.con.execute("UPDATE companies SET expanded_at=? WHERE id=?", (now(), cid))

    def investigate(self, seeds: list[int], max_depth: int = 2, limit: int = 200) -> int:
        """Recorre el grafo desde las semillas; sigue también por sociedades sin adjudicaciones."""
        n = 0
        for cid in seeds:
            with tx(self.con):
                self.con.execute("UPDATE companies SET depth=0 WHERE id=? AND (depth IS NULL OR depth>0)", (cid,))
        while n < limit:
            row = self.con.execute(
                # Primero las sociedades con personas identificadas por DNI (son las que llevan a otras
                # sociedades); luego por nivel, y SRL/SAS antes que SA: sus avisos listan socios con DNI.
                """SELECT id FROM companies c WHERE expanded_at IS NULL AND depth<=?
                   ORDER BY EXISTS(SELECT 1 FROM roles r JOIN persons x ON x.id=r.person_id
                                   WHERE r.company_id=c.id AND x.dni IS NOT NULL AND r.match IN ('dni','confirmed')) DESC,
                            depth, (upper(name) GLOB '*S.R.L*' OR upper(name) GLOB '*SRL*' OR upper(name) GLOB '*S.A.S*'
                                    OR upper(name) GLOB '*RESPONSABILIDAD*') DESC, id LIMIT 1""",
                (max_depth,),
            ).fetchone()
            if not row:
                break
            self.expand(row["id"])
            n += 1
        return n

    # --- Resolución automática con registros oficiales ---------------------

    def _attach_dni(self, person_id: int, dni: str) -> int:
        """Asigna un DNI a una persona; si otra ya lo tiene, las une. Devuelve la persona resultante."""
        con = self.con
        other = con.execute("SELECT id FROM persons WHERE dni=?", (dni,)).fetchone()
        if other and other["id"] != person_id:
            con.execute("UPDATE roles SET person_id=? WHERE person_id=?", (other["id"], person_id))
            con.execute("UPDATE roles SET candidate_person_id=? WHERE candidate_person_id=?", (other["id"], person_id))
            return other["id"]
        con.execute("UPDATE persons SET dni=? WHERE id=?", (dni, person_id))
        return person_id

    def _close(self, rid: int, ok: bool, note: str) -> None:
        self.con.execute("UPDATE reviews SET status=?, decided_at=?, note=? WHERE id=?",
                         ("confirmed" if ok else "rejected", now(), "Automático: " + note, rid))

    def autoresolve(self) -> dict:
        """Resuelve revisiones y menciones sin DNI con el RNS y el padrón ARCA. Lo dudoso sigue en la cola."""
        if not self.src:
            raise RuntimeError("faltan las fuentes oficiales: correr `bora-dpi sources`")
        con, src, out = self.con, self.src, {}
        bump = lambda k: out.__setitem__(k, out.get(k, 0) + 1)
        with tx(con):
            # 1. Adjudicaciones sin CUIT
            for rv in con.execute("""SELECT rv.id, a.id AS aid, a.awardee_name, a.company_id FROM reviews rv
                                     JOIN awards a ON a.id=rv.ref_id WHERE rv.status='pending' AND rv.kind='cuit_missing'""").fetchall():
                found = sources.awardee_cuit(src, rv["awardee_name"])
                if not found:
                    continue
                cuit, origin = found
                owner = con.execute("SELECT id FROM companies WHERE cuit=?", (cuit,)).fetchone()
                if owner and owner["id"] != rv["company_id"]:
                    con.execute("UPDATE awards SET company_id=? WHERE id=?", (owner["id"], rv["aid"]))
                else:
                    con.execute("UPDATE companies SET cuit=?, cuit_source=? WHERE id=?", (cuit, origin, rv["company_id"]))
                con.execute("UPDATE awards SET status='confirmed' WHERE id=?", (rv["aid"],))
                self._close(rv["id"], True, f"CUIT {cuit_fmt(cuit)} según {origin} (nombre único)")
                bump("cuit_missing")
            # 2. Avisos atribuidos solo por denominación
            for rv in con.execute("""SELECT rv.id, r.id AS role_id, r.dni_status, n.title, n.date, c.cuit FROM reviews rv
                                     JOIN roles r ON r.id=rv.ref_id JOIN notices n ON n.id=r.notice_id
                                     JOIN companies c ON c.id=r.company_id
                                     WHERE rv.status='pending' AND rv.kind='company_name_match'""").fetchall():
                verdict = sources.company_attribution(src, rv["title"], rv["cuit"], rv["date"])
                if not verdict:
                    continue
                ok = verdict == "same"
                match = ("dni" if rv["dni_status"] in ("ok", "registry") else "unmatched") if ok else "rejected"
                con.execute("UPDATE roles SET match=? WHERE id=?", (match, rv["role_id"]))
                self._close(rv["id"], ok, "el Registro Nacional de Sociedades " + (
                    "asigna esa denominación solo a este CUIT" if ok else "indica que el aviso es de un homónimo"))
                bump(f"company_name_{verdict}")
            # 3. Coincidencias de nombre sin DNI
            for rv in con.execute("""SELECT rv.id, r.id AS role_id, r.person_id, r.candidate_person_id, r.mention_name,
                                            cp.dni AS cand_dni FROM reviews rv JOIN roles r ON r.id=rv.ref_id
                                     LEFT JOIN persons cp ON cp.id=r.candidate_person_id
                                     WHERE rv.status='pending' AND rv.kind='name_match'""").fetchall():
                dni = sources.unique_person_dni(src, rv["mention_name"])
                if not dni:
                    continue
                pid = self._attach_dni(rv["person_id"], dni)
                con.execute("UPDATE roles SET person_id=?, match='dni', dni_status='registry' WHERE id=?", (pid, rv["role_id"]))
                same = rv["cand_dni"] in (None, dni)
                if rv["cand_dni"] is None and rv["candidate_person_id"]:
                    self._attach_dni(rv["candidate_person_id"], dni)  # mismo nombre único: la candidata también
                self._close(rv["id"], same, "nombre único en el padrón ARCA; " + (
                    "su DNI coincide con la candidata" if same else "su DNI es otro: no es la misma persona"))
                bump("name_match_" + ("same" if same else "other"))
            # 3b. Mismo nombre completo (salvo tildes y mayúsculas) que una persona ya identificada en la MISMA sociedad
            for rv in con.execute("""SELECT rv.id, r.id AS role_id, r.mention_name, r.company_id, cp.id AS cand, cp.name AS cand_name,
                                            c.name AS company FROM reviews rv JOIN roles r ON r.id=rv.ref_id
                                     JOIN persons cp ON cp.id=r.candidate_person_id JOIN companies c ON c.id=r.company_id
                                     WHERE rv.status='pending' AND rv.kind='name_match' AND EXISTS (
                                       SELECT 1 FROM roles o WHERE o.person_id=cp.id AND o.company_id=r.company_id
                                         AND o.id<>r.id AND o.match IN ('dni','confirmed'))""").fetchall():
                if norm(rv["mention_name"]) != norm(rv["cand_name"]):
                    continue
                con.execute("UPDATE roles SET person_id=?, match='confirmed', dni_status='same_company' WHERE id=?",
                            (rv["cand"], rv["role_id"]))
                self._close(rv["id"], True, f"mismo nombre completo que una persona identificada por DNI en {rv['company']}")
                bump("name_match_same_company")
            # 3c. Menciones sin DNI con el mismo nombre completo en la misma sociedad: una sola persona
            groups = {}
            for r in con.execute("""SELECT r.id, r.company_id, r.person_id, r.mention_name, x.dni FROM roles r
                                    JOIN persons x ON x.id=r.person_id WHERE r.match IN ('dni','confirmed','unmatched')"""):
                groups.setdefault((r["company_id"], norm(r["mention_name"])), []).append(r)
            for (cid, _), rs in groups.items():
                pids = {r["person_id"] for r in rs}
                with_dni = {r["dni"] for r in rs if r["dni"]}
                if len(pids) < 2 or len(with_dni) > 1:  # dos DNI distintos con el mismo nombre: homónimos reales
                    continue
                keep = next((r["person_id"] for r in rs if r["dni"]), min(pids))
                for r in rs:
                    if r["person_id"] != keep:
                        con.execute("UPDATE roles SET person_id=?, dni_status=CASE WHEN ?=1 THEN 'same_company' ELSE dni_status END, "
                                    "match=CASE WHEN match='unmatched' AND ?=1 THEN 'confirmed' ELSE match END WHERE id=?",
                                    (keep, bool(with_dni), bool(with_dni), r["id"]))
                        con.execute("UPDATE roles SET candidate_person_id=? WHERE candidate_person_id=?", (keep, r["person_id"]))
                        bump("same_company_merged")
            # 3d. Red societaria: mismo nombre completo y un socio en común con la persona identificada
            #     (q figura en esta sociedad y en otra donde también figura la candidata). Se repite hasta
            #     que no haya cambios, porque cada unión puede habilitar otra.
            while True:
                n = 0
                for rv in con.execute("""SELECT rv.id, r.id AS role_id, r.mention_name, r.company_id, cp.id AS cand, cp.name AS cand_name,
                                                (SELECT q.name || ' (también en ' || d.name || ')' FROM roles a JOIN roles b ON b.person_id=a.person_id
                                                   JOIN roles e ON e.company_id=b.company_id AND e.person_id=cp.id
                                                   JOIN persons q ON q.id=a.person_id JOIN companies d ON d.id=b.company_id
                                                  WHERE a.company_id=r.company_id AND a.person_id<>cp.id AND b.company_id<>r.company_id
                                                    AND a.match IN ('dni','confirmed') AND b.match IN ('dni','confirmed')
                                                    AND e.match IN ('dni','confirmed','unmatched') LIMIT 1) AS via
                                         FROM reviews rv JOIN roles r ON r.id=rv.ref_id JOIN persons cp ON cp.id=r.candidate_person_id
                                         WHERE rv.status='pending' AND rv.kind='name_match'""").fetchall():
                    long_name = len([t for t in norm(rv["mention_name"]).split() if len(t) > 2]) >= 4  # p. ej. 4+ palabras
                    if not (rv["via"] or long_name) or norm(rv["mention_name"]) != norm(rv["cand_name"]):
                        continue
                    con.execute("UPDATE roles SET person_id=?, match='confirmed', dni_status='co_partner' WHERE id=?",
                                (rv["cand"], rv["role_id"]))
                    self._close(rv["id"], True, f"mismo nombre completo y socio en común: {rv['via']}" if rv["via"]
                                else "nombre completo idéntico de cuatro o más palabras")
                    bump("name_match_co_partner"); n += 1
                if not n:
                    break
            # 3e. Aviso atribuido por denominación: es de la sociedad si otra persona del mismo aviso ya figura
            #     en ella por avisos con CUIT o DNI (mismo DNI o mismo nombre completo)
            for rv in con.execute("""SELECT rv.id, r.id AS role_id, r.notice_id, r.company_id, r.dni_status FROM reviews rv
                                     JOIN roles r ON r.id=rv.ref_id WHERE rv.status='pending' AND rv.kind='company_name_match'""").fetchall():
                known = {}
                for k in con.execute("""SELECT x.name, x.dni FROM roles k JOIN persons x ON x.id=k.person_id
                                          WHERE k.company_id=? AND k.notice_id<>? AND k.match IN ('dni','confirmed')""",
                                       (rv["company_id"], rv["notice_id"])):
                    known[norm(k["name"])] = k["name"]
                    if k["dni"]:
                        known["dni:" + k["dni"]] = k["name"]
                hit = None
                for o in con.execute("""SELECT o.mention_name, x.dni FROM roles o LEFT JOIN persons x ON x.id=o.person_id
                                          WHERE o.notice_id=? AND o.company_id=?""", (rv["notice_id"], rv["company_id"])):
                    hit = known.get("dni:" + (o["dni"] or "")) or known.get(norm(o["mention_name"]))
                    if hit:
                        break
                if not hit:
                    continue
                con.execute("UPDATE roles SET match=? WHERE id=?",
                            ("dni" if rv["dni_status"] in ("ok", "registry") else "unmatched", rv["role_id"]))
                self._close(rv["id"], True, f"el aviso nombra a {hit}, que ya figura en la sociedad por avisos con CUIT")
                bump("company_name_known_person")
            # 4. Menciones sin DNI ni revisión (p. ej. directorios de S.A.): DNI del padrón si el nombre es único
            # (también si la persona ya recibió ese mismo DNI por otra mención)
            for r in con.execute("""SELECT r.id, r.person_id, r.mention_name, x.dni FROM roles r JOIN persons x ON x.id=r.person_id
                                    WHERE r.match='unmatched' AND r.dni_status='missing'""").fetchall():
                dni = sources.unique_person_dni(src, r["mention_name"])
                if dni and r["dni"] in (None, dni):
                    pid = self._attach_dni(r["person_id"], dni)
                    con.execute("UPDATE roles SET person_id=?, match='dni', dni_status='registry' WHERE id=?", (pid, r["id"]))
                    bump("unmatched_identified")
        return out

    # --- Actualización diaria ----------------------------------------------

    def daily(self, d: date, sections=("segunda", "tercera")) -> dict:
        """Registra la edición del día: PDF original, índice de avisos y relevamiento de adjudicaciones."""
        out = {}
        for s in sections:
            row = self.con.execute("SELECT * FROM editions WHERE section=? AND date=?", (s, d.isoformat())).fetchone()
            if row and row["status"] in ("ok", "missing"):
                out[s] = row["status"]
                continue
            status, err, pdf, sha, n_notices = "ok", None, None, None, None
            try:
                items = self.client.search(secciones=[bora.SECTION_NUM[s]], desde=d, hasta=d, max_pages=50)
                if not items:
                    status = "missing"  # sin edición (feriado o fin de semana)
                else:
                    archive.upsert_listed(self.con, items)
                    n_notices = len(items)
                    path = self.client.fetch_section_pdf(s, d)
                    import hashlib

                    pdf, sha = str(path), hashlib.sha256(path.read_bytes()).hexdigest()
            except bora.BoraError as e:
                status, err = "failed", str(e)
            with tx(self.con):
                self.con.execute(
                    """INSERT INTO editions (section, date, status, attempts, pdf_path, pdf_sha256, n_notices, last_error, checked_at)
                       VALUES (?,?,?,1,?,?,?,?,?)
                       ON CONFLICT(section, date) DO UPDATE SET status=excluded.status, attempts=editions.attempts+1,
                         pdf_path=excluded.pdf_path, pdf_sha256=excluded.pdf_sha256, n_notices=excluded.n_notices,
                         last_error=excluded.last_error, checked_at=excluded.checked_at""",
                    (s, d.isoformat(), status, pdf, sha, n_notices, err, now()),
                )
            out[s] = status
        if out.get("tercera") == "ok":
            out["tech_awardees"] = len(self.survey(d, d))
        return out

    def backfill(self, desde: date, hasta: date) -> list[dict]:
        out, d = [], desde
        while d <= hasta:
            out.append({"date": d.isoformat(), **self.daily(d)})
            d += timedelta(days=1)
        return out


NON_CORPORATE = ("EDICTOS", "SUCESIONES", "REMATES", "CITACIONES", "QUIEBRAS", "PARTIDOS")


def is_corporate(rubro: str | None) -> bool:
    """Solo avisos societarios: se excluyen edictos judiciales, sucesiones, remates y partidos."""
    return not any(w in (rubro or "").upper() for w in NON_CORPORATE)


def names_compatible(a: str, b: str) -> bool:
    ta, tb = set(person_key(a).split()), set(person_key(b).split())
    if len(ta & tb) >= 2 or ta <= tb or tb <= ta:
        return True
    return difflib.SequenceMatcher(None, person_key(a), person_key(b)).ratio() >= 0.85


def strip_type(name: str) -> str:
    """Denominación sin tipo societario para la búsqueda en BORA."""
    import re

    return re.sub(r"\s+(S\.?\s?A\.?\s?U?\.?|S\.?\s?R\.?\s?L\.?|S\.?\s?A\.?\s?S\.?|SOCIEDAD .*)$", "", name.strip(),
                  flags=re.I).strip() or name
