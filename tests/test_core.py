from bora_dpi.db import connect
from bora_dpi.pipeline import Pipeline
from bora_dpi.text import company_key, cuit_valid


def test_cuit_check_digit():
    assert cuit_valid("30-71883194-2")
    assert cuit_valid("30631658810")
    assert not cuit_valid("30-71883194-3")  # dígito verificador incorrecto
    assert not cuit_valid("99-71883194-2")  # prefijo inexistente


def test_company_key_ignores_type():
    assert company_key("Striano Software S.R.L.") == company_key("STRIANO SOFTWARE SRL")
    assert company_key("Axoft Argentina S.A.") == company_key("AXOFT ARGENTINA SOCIEDAD ANONIMA")


def test_segment_notice_isolates_one_notice_from_page():
    from bora_dpi.text import segment_notice

    page = """#I1I#
TELEFONICA COMUNICACIONES
PERSONALES SA cambia su denominación por TELEFONICA MOVILES ARGENTINA S.A., CUIT 30-67881435-7
#I3I# texto de otra columna #F3F#
#F1F#
#I2I#
BALREY INVESTMENT
SOCIEDAD DE RESPONSABILIDAD LIMITADA Socios: Oscar Miguel Balbi, D.N.I. 20.251.575
#F2F#"""
    tel = segment_notice(page, "TELEFONICA MOVILES ARGENTINA S.A.")
    assert "TELEFONICA MOVILES" in tel and "Balbi" not in tel and "otra columna" not in tel
    assert "Balbi" in segment_notice(page, "BALREY INVESTMENT S.R.L.")
    assert segment_notice("página sin marcas", "BALREY INVESTMENT S.R.L.") is None


def test_dni_links_and_name_only_goes_to_review(tmp_path):
    con = connect(tmp_path / "t.db")
    p = Pipeline(con, extractor=object())
    p.src = None
    for nid in ("segunda/A1/20250101", "segunda/A2/20250102", "segunda/A3/20250103"):
        con.execute("INSERT INTO notices (id, section, bora_id, date, url) VALUES (?, 'segunda', 'x', '2025-01-01', 'u')", (nid,))
    x = p.company("X S.A.", "30718831942", None, "award", 0)
    y = p.company("Y S.R.L.", None, None, "partner_link", 1)
    z = p.company("Z S.A.", None, None, "partner_link", 1)
    ana = {"name": "Ana María Gómez", "role": "socio", "dni": "26353113", "dni_status": "ok"}
    p._role("segunda/A1/20250101", x, ana, "2025-01-01")
    p._role("segunda/A2/20250102", y, {**ana, "name": "GOMEZ Ana Maria"}, "2025-01-02")
    p._role("segunda/A3/20250103", z, {**ana, "dni": None, "dni_status": "missing"}, "2025-01-03")
    roles = {r["company_id"]: r for r in con.execute("SELECT * FROM roles")}
    assert roles[x]["person_id"] == roles[y]["person_id"]  # mismo DNI: misma persona aunque cambie el formato del nombre
    assert roles[x]["match"] == roles[y]["match"] == "dni"
    assert roles[z]["match"] == "pending_review"  # mismo nombre sin DNI: no se une, queda en revisión
    assert roles[z]["person_id"] != roles[x]["person_id"]
    assert roles[z]["candidate_person_id"] == roles[x]["person_id"]
    assert con.execute("SELECT kind FROM reviews").fetchone()["kind"] == "name_match"


def test_notice_attributed_only_by_name_does_not_link(tmp_path):
    """Un homónimo sin el CUIT de la adjudicataria no la vincula con sus socios (queda en revisión)."""
    con = connect(tmp_path / "t.db")
    p = Pipeline(con, extractor=object())
    p.src = None  # sin registros oficiales: decide solo con el aviso
    x = p.company("ICAP S.A.", "30503825984", None, "award", 0)
    notices = {
        "segunda/H1/20070906": ("ICAP S.A.", "Por asamblea los socios resolvieron la cancelación. Socios: Elena Gómez, DNI 4.728.926"),
        "segunda/A2/20240101": ("ICAP S.A.", "CUIT 30-50382598-4. Se designa Presidente a Juan Pérez, DNI 20.251.575"),
    }
    for nid, (title, text) in notices.items():
        con.execute("INSERT INTO notices (id, section, bora_id, date, title, rubro, url, text, status) "
                    "VALUES (?, 'segunda', 'x', '2020-01-01', ?, 'REFORMA SA', 'u', ?, 'fetched')", (nid, title, text))
    data = {
        "segunda/H1/20070906": {"cuit": None, "people": [{"name": "Elena Gómez", "dni": "4728926", "dni_status": "ok", "role": "socio"}]},
        "segunda/A2/20240101": {"cuit": "30503825984", "people": [{"name": "Juan Pérez", "dni": "20251575", "dni_status": "ok", "role": "presidente"}]},
    }
    p.prepare = lambda ids, kind: {n: (dict(con.execute("SELECT * FROM notices WHERE id=?", (n,)).fetchone()), data[n]) for n in ids}
    p.process_societarios(list(notices), 0, owner=x)
    match = {r["mention_name"]: r["match"] for r in con.execute("SELECT * FROM roles WHERE company_id=?", (x,))}
    assert match == {"Juan Pérez": "dni", "Elena Gómez": "pending_review"}
    # Un aviso posterior con el CUIT pero sin DNI no se apoya en el vínculo pendiente
    p._role("segunda/A2/20240101", x, {"name": "Elena Gómez", "dni": None, "dni_status": "missing", "role": "presidente"}, "2024-01-01")
    later = con.execute("SELECT match FROM roles WHERE company_id=? AND role='presidente' AND mention_name='Elena Gómez'", (x,)).fetchone()
    assert later["match"] != "dni"
    assert con.execute("SELECT kind FROM reviews").fetchone()["kind"] == "company_name_match"


def _registry(tmp_path):
    from bora_dpi import sources

    src = sources.connect(tmp_path / "sources.db")
    src.executemany("INSERT INTO rns VALUES (?,?,?,?,?)", [
        ("30503825984", "ICAP", "icap", "2010-05-01", "SOCIEDAD ANONIMA"),
        ("30999999993", "ICAP", "icap", "1975-01-01", "SOCIEDAD ANONIMA"),
        ("30714236888", "NANOTEC", "nanotec", "2013-08-01", "SOCIEDAD DE RESPONSABILIDAD LIMITADA"),
    ])
    src.executemany("INSERT INTO arca VALUES (?,?,?,?)", [
        ("20268228374", "bacigalupo luciano hugo", "bacigalupo", 0),
        ("20111111112", "perez juan carlos", "perez", 0),
        ("20222222223", "perez juan carlos", "perez", 0),
        ("27333333334", "gonzalez de la fuente maria ce", "gonzalez", 1),  # truncado a 30
    ])
    return src


def test_registry_rules(tmp_path):
    from bora_dpi import sources

    src = _registry(tmp_path)
    # Persona: nombre único en el padrón (en cualquier orden) -> DNI; homónimos o nombre corto -> nada
    assert sources.unique_person_dni(src, "Luciano Hugo BACIGALUPO") == "26822837"
    assert sources.unique_person_dni(src, "Juan Carlos Pérez") is None
    assert sources.unique_person_dni(src, "Luciano Bacigalupo") is None
    assert sources.unique_person_dni(src, "María Celeste González de la Fuente") == "33333333"
    # Sociedad: aviso anterior al contrato social es de un homónimo; posterior a ambos, indecidible
    assert sources.company_attribution(src, "ICAP S.A.", "30503825984", "2007-09-06") == "other"
    assert sources.company_attribution(src, "ICAP S.A.", "30503825984", "2015-01-01") is None
    assert sources.company_attribution(src, "NANOTEC", "30714236888", "2013-08-27") == "same"
    assert sources.awardee_cuit(src, "Nanotec S.R.L.") == ("30714236888", "Registro Nacional de Sociedades")


def test_autoresolve_links_by_registry_and_rejects_other_person(tmp_path):
    con = connect(tmp_path / "t.db")
    p = Pipeline(con, extractor=object())
    p.src = None
    for nid in ("segunda/A1/20250101", "segunda/A2/20250102", "segunda/A3/20250103"):
        con.execute("INSERT INTO notices (id, section, bora_id, date, url) VALUES (?, 'segunda', 'x', '2025-01-01', 'u')", (nid,))
    x = p.company("X S.A.", "30718831942", None, "award", 0)
    y = p.company("Y S.A.", None, None, "partner_link", 1)
    z = p.company("Z S.A.", None, None, "partner_link", 1)
    p._role("segunda/A1/20250101", x, {"name": "Luciano Hugo Bacigalupo", "dni": "26822837", "dni_status": "ok", "role": "socio"}, "2025-01-01")
    p._role("segunda/A2/20250102", y, {"name": "Luciano Hugo Bacigalupo", "dni": None, "dni_status": "missing", "role": "presidente"}, "2025-01-02")
    p._role("segunda/A3/20250103", z, {"name": "Juan Carlos Pérez", "dni": None, "dni_status": "missing", "role": "director"}, "2025-01-03")
    p.src = _registry(tmp_path)
    p.autoresolve()
    r = {row["company_id"]: row for row in con.execute("SELECT * FROM roles")}
    assert r[y]["person_id"] == r[x]["person_id"] and r[y]["match"] == "dni"  # padrón: nombre único con ese DNI
    assert r[z]["match"] == "unmatched"  # homónimos en el padrón: no se identifica
    assert con.execute("SELECT status, note FROM reviews").fetchone()["status"] == "confirmed"


def test_autoresolve_links_name_via_shared_partner(tmp_path):
    con = connect(tmp_path / "t.db")
    p = Pipeline(con, extractor=object())
    p.src = None
    for nid in ("segunda/B1/20250101", "segunda/B2/20250102", "segunda/B3/20250103", "segunda/B4/20250104"):
        con.execute("INSERT INTO notices (id, section, bora_id, date, url) VALUES (?, 'segunda', 'x', '2025-01-01', 'u')", (nid,))
    a = p.company("A S.A.", "30718831942", None, "award", 0)
    b = p.company("B S.R.L.", None, None, "partner_link", 1)
    c = p.company("C S.R.L.", None, None, "partner_link", 1)
    # Ana (DNI) en A y en C; Juan Gómez Ruiz (DNI) en A; en C aparece "Juan Gomez Ruiz" sin DNI
    p._role("segunda/B1/20250101", a, {"name": "Ana María Sosa", "dni": "20111222", "dni_status": "ok", "role": "socio"}, "2025-01-01")
    p._role("segunda/B1/20250101", a, {"name": "Juan Gómez Ruiz", "dni": "22333444", "dni_status": "ok", "role": "gerente"}, "2025-01-01")
    p._role("segunda/B2/20250102", c, {"name": "Ana María Sosa", "dni": "20111222", "dni_status": "ok", "role": "socio"}, "2025-01-02")
    p._role("segunda/B3/20250103", c, {"name": "Juan Gomez Ruiz", "dni": None, "dni_status": "missing", "role": "socio"}, "2025-01-03")
    # En B, sin socio en común con A, el mismo nombre sigue pendiente
    p._role("segunda/B4/20250104", b, {"name": "Juan Gómez Ruiz", "dni": None, "dni_status": "missing", "role": "socio"}, "2025-01-04")
    p.src = _registry(tmp_path)
    p.autoresolve()
    r = {row["company_id"]: row for row in con.execute("SELECT * FROM roles WHERE mention_name LIKE 'Juan%'")}
    assert r[c]["match"] == "confirmed" and r[c]["person_id"] == r[a]["person_id"]
    assert r[b]["match"] == "pending_review"
