"""Cliente del sitio del Boletín Oficial (boletinoficial.gob.ar).

Usa los mismos endpoints que la web pública: listado por sección y fecha, búsqueda avanzada
(texto, denominación, rango de fechas), detalle de aviso y PDF de la sección del día.
Todo lo descargado queda en el archivo local (config.ARCHIVE_DIR) con su enlace oficial.
"""

import base64
import hashlib
import json
import re
import threading
import time
from datetime import date
from pathlib import Path

import logging

import httpx

from . import config
from .text import html_to_text, segment_notice

log = logging.getLogger(__name__)
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
SECTION_NUM = {"primera": 1, "segunda": 2, "tercera": 3}


class BoraError(Exception):
    pass


_RATE_LOCK = threading.Lock()
_NEXT_SLOT = [0.0]  # instante del próximo pedido permitido, compartido entre clientes e hilos


def _wait_turn(delay: float) -> None:
    with _RATE_LOCK:
        t = max(time.monotonic(), _NEXT_SLOT[0])
        _NEXT_SLOT[0] = t + delay
    time.sleep(max(0.0, t - time.monotonic()))


def _backoff(seconds: float) -> None:
    with _RATE_LOCK:
        _NEXT_SLOT[0] = max(_NEXT_SLOT[0], time.monotonic() + seconds)


def notice_id(section: str, bora_id: str, ymd: str) -> str:
    return f"{section}/{bora_id}/{ymd}"


def notice_url(nid: str) -> str:
    return f"{config.BORA_BASE}/detalleAviso/{nid}"


def ymd_to_iso(ymd: str) -> str:
    return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"


class Client:
    def __init__(self, delay: float | None = None):
        self.delay = config.BORA_DELAY if delay is None else delay
        self.http = httpx.Client(
            headers={"User-Agent": UA, "Accept-Language": "es-AR,es;q=0.9"},
            timeout=httpx.Timeout(90, connect=20),
            follow_redirects=True,
            proxy=config.BORA_PROXY,
        )

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        last_exc = None
        for attempt in range(8):
            _wait_turn(self.delay)
            try:
                r = self.http.request(method, config.BORA_BASE + path, **kw)
                if r.status_code == 429:
                    # BORA limita la tasa por IP (Retry-After típico: 600 s): pausa global
                    wait = int(r.headers.get("Retry-After", "120") or 120) + 5
                    log.warning("BORA 429: pausa de %d s", wait)
                    _backoff(wait)
                if r.status_code == 403:
                    raise BoraError("403 de BORA: la IP parece bloqueada; configurar BORA_PROXY")
                if r.status_code >= 500 or r.status_code == 429:
                    raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
                r.raise_for_status()
                return r
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last_exc = e
                time.sleep(2 * (attempt + 1))
        raise BoraError(f"{method} {path}: {last_exc}")

    # --- Búsqueda avanzada --------------------------------------------------

    def search(
        self,
        texto: str = "",
        secciones: list[int] = (2, 3),
        desde: date | None = None,
        hasta: date | None = None,
        denominacion: str = "",
        todas_las_palabras: bool = True,
        max_pages: int = 20,
    ) -> list[dict]:
        """Devuelve [{id, section, bora_id, date, title, rubro, snippet}] sin duplicados."""
        secs = list(secciones)
        p = {
            "texto": texto, "seccion": secs, "rubros": [], "nroNorma": "", "anioNorma": "",
            "denominacion": denominacion, "tipoContratacion": "", "anioContratacion": "",
            "nroContratacion": "",
            "fechaDesde": desde.strftime("%d/%m/%Y") if desde else "",
            "fechaHasta": hasta.strftime("%d/%m/%Y") if hasta else "",
            "tipoBusqueda": "Avanzada", "numeroPagina": 1, "ultimoRubro": "", "busquedaRubro": False,
            "hayMasResultadosBusqueda": True, "ejecutandoLlamadaAsincronicaBusqueda": False,
            "ultimaSeccion": "", "todasLasPalabras": todas_las_palabras,
            "filtroPorRubrosSeccion": False, "filtroPorRubroBusqueda": False,
            "filtroPorSeccionBusqueda": False, "busquedaOriginal": True,
            "comienzaDenominacion": False, "ordenamientoSegunda": False,
            "seccionesOriginales": secs, "ultimoItemExterno": None, "ultimoItemInterno": None,
        }
        seen: dict[str, dict] = {}
        rubro = ""
        for _ in range(max_pages):
            r = self._request(
                "POST", "/busquedaAvanzada/realizarBusqueda",
                data={"params": json.dumps(p), "array_volver": "[]"},
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            j = r.json()
            if j.get("error"):
                raise BoraError(f"búsqueda rechazada: {j.get('mensajes')}")
            content = j.get("content") or {}
            html = content.get("html") or ""
            if not html.strip():
                break
            rubro = self._parse_results(html, seen, rubro)
            p.update(
                numeroPagina=content.get("sig_pag"), ultimaSeccion=content.get("ult_seccion", ""),
                ultimoRubro=content.get("ult_rubro", ""), busquedaOriginal=False,
            )
        return list(seen.values())

    @staticmethod
    def _parse_results(html: str, seen: dict, rubro: str) -> str:
        tokens = re.finditer(
            r'<h5 class="seccion-rubro[^"]*">(?P<rubro>[^<]*)</h5>|<a href="/detalleAviso/(?P<sec>\w+)/(?P<bid>\w+)/(?P<ymd>\d{8})[^"]*"[^>]*>(?P<body>.*?)</a>',
            html, re.S,
        )
        for m in tokens:
            if m.group("rubro") is not None:
                rubro = re.sub(r"\s+", " ", m.group("rubro")).strip()
                continue
            nid = notice_id(m.group("sec"), m.group("bid"), m.group("ymd"))
            if nid in seen:
                continue
            body = m.group("body")
            title = re.search(r'<p class="item">(.*?)</p>', body, re.S)
            details = [html_to_text(x) for x in re.findall(r'<p class="item-detalle"[^>]*>(.*?)</p>', body, re.S)]
            seen[nid] = {
                "id": nid, "section": m.group("sec"), "bora_id": m.group("bid"),
                "date": ymd_to_iso(m.group("ymd")),
                "title": html_to_text(title.group(1)) if title else "",
                "rubro": rubro,
                "snippet": " | ".join(d for d in details if d and not d.startswith("Fecha de Publicacion")),
            }
        return rubro

    # --- Detalle de aviso ---------------------------------------------------

    def fetch_notice(self, nid: str, known_title: str | None = None) -> dict:
        """Descarga el aviso, guarda el HTML original y devuelve título, rubro y texto.

        Los avisos históricos (escaneados) traen la página en PDF embebido: se aplica OCR.
        """
        section, bid, ymd = nid.split("/")
        r = self._request("GET", f"/detalleAviso/{nid}")
        raw = r.text
        path = config.ARCHIVE_DIR / "notices" / section / ymd / f"{bid}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
        title = re.search(r'id="tituloDetalleAviso"[^>]*>\s*<h1>(.*?)</h1>', raw, re.S)
        rubro = re.findall(r'<li><a class="puntero [^"]*" href="[^"]*\?rubro=\d+">([^<]*)</a>', raw)
        body = re.search(r'id="cuerpoDetalleAviso"[^>]*>(.*?)<p class="text-muted"><small>Fecha de publicaci', raw, re.S)
        text, source = "", "none"
        if body:
            text, source = html_to_text(body.group(1)), "html"
            text = re.sub(r"^table tr td \{[^}]*\}\s*", "", text)
        else:
            b64 = re.search(r'convertBase64InUrlBlob\("([A-Za-z0-9+/=]+)"', raw)
            if b64:
                pdf = config.ARCHIVE_DIR / "notices" / section / ymd / f"{bid}.pdf"
                pdf.write_bytes(base64.b64decode(b64.group(1)))
                from .ocr import pdf_text

                page, source = pdf_text(pdf)
                seg = segment_notice(page, html_to_text(title.group(1)) if title else known_title)
                # Página completa sin poder aislar el aviso: no se extraen datos de ella
                text, source = (seg, f"{source}_segment") if seg else (page, f"{source}_page")
        return {
            "title": html_to_text(title.group(1)) if title else None,
            "rubro": rubro[0].strip() if rubro else None,
            "text": text, "text_source": source, "raw_path": str(path),
            "sha256": hashlib.sha256(re.sub(r"\s+", " ", text).encode()).hexdigest() if text else None,
        }

    # --- Edición del día ----------------------------------------------------

    def fetch_section_pdf(self, section: str, d: date) -> Path:
        """Descarga el PDF original de la sección de una fecha (queda en el archivo)."""
        ymd = d.strftime("%Y%m%d")
        self._request("GET", f"/seccion/{section}/{ymd}")  # fija la edición en la sesión
        r = self._request(
            "POST", "/pdf/download_section", data={"nombreSeccion": section},
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        try:
            b64 = r.json().get("pdfBase64")
        except ValueError:
            b64 = None
        if not b64:
            raise BoraError("sin PDF para la fecha")
        path = config.ARCHIVE_DIR / "editions" / section / f"{ymd}.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(b64))
        return path

    def edition_exists(self, section: str, d: date) -> bool:
        """El sitio redirige o muestra la última edición si la fecha no tiene boletín."""
        ymd = d.strftime("%Y%m%d")
        r = self._request("GET", f"/seccion/{section}/{ymd}")
        return f"/detalleAviso/{section}/" in r.text and f"/{ymd}" in r.text
