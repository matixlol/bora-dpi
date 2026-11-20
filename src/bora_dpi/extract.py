"""Extracción estructurada con un modelo de lenguaje detrás de una interfaz intercambiable.

Cualquier endpoint compatible con OpenAI sirve (OpenRouter en desarrollo, Ollama/vLLM local en
producción). Cada dato devuelto trae la cita textual que lo respalda; la cita se verifica contra
el texto fuente y los DNI se aceptan solo si aparecen literalmente en el aviso.
"""

import json
import re
import time

import httpx

from . import config
from .text import cuit_digits, cuit_valid, dni_digits, dni_in_text, locate_evidence

AWARD_PROMPT = """Sos un analista de compras públicas argentinas. Del aviso de la Tercera Sección del
Boletín Oficial que sigue, extraé el acto de adjudicación. Respondé SOLO un objeto JSON:
{
 "is_award": bool,               // true si el aviso informa una adjudicación (no un llamado ni una apertura)
 "organism": str|null,           // organismo contratante
 "procedure": str|null,          // tipo y número de procedimiento
 "act_date": "AAAA-MM-DD"|null,  // fecha del acto de adjudicación si consta
 "object": str|null,             // objeto de la contratación, breve
 "is_tech": bool,                // el objeto es tecnología: software, hardware, licencias, redes,
                                 // telecomunicaciones, servicios informáticos, nube, datos, ciberseguridad
 "tech_reason": str|null,
 "awardees": [ {"name": str, "cuit": str|null, "amount": str|null,
                "evidence": str} ]  // evidence: fragmento LITERAL del aviso que nombra a la adjudicataria
}
No inventes CUIT: copialo solo si figura en el texto."""

SOC_PROMPT = """Sos un analista societario argentino. Del aviso de la Segunda Sección del Boletín
Oficial que sigue, extraé los datos de la sociedad. Respondé SOLO un objeto JSON:
{
 "company": str|null,            // denominación de la sociedad objeto del aviso
 "cuit": str|null,               // solo si figura en el texto
 "objeto": str|null,             // objeto social resumido, si consta
 "objeto_tech": bool|null,       // el objeto incluye software, informática, telecomunicaciones o tecnología
 "people": [ {"name": str,       // nombre completo tal como figura
              "dni": str|null,   // número de DNI/documento tal como figura, o null
              "role": str,       // socio, accionista, presidente, director titular, director suplente,
                                 // gerente, vicepresidente, síndico, etc. (uno por entrada)
              "date": "AAAA-MM-DD"|null,  // fecha del acto (asamblea, constitución, cesión)
              "ceased": bool,    // true si renuncia, cesa, cede sus cuotas o es removido
              "evidence": str} ] // fragmento LITERAL del aviso que menciona a la persona y su rol
}
Si una persona tiene varios roles (p. ej. suscribe cuotas o acciones y además es gerente), devolvé
una entrada por rol: quien suscribe cuotas/acciones o las recibe en una cesión es "socio".
Incluí solo personas humanas con un rol en esta sociedad (no escribanos, abogados ni autorizados)."""


class LLMError(Exception):
    pass


class Extractor:
    def __init__(self, base_url=None, api_key=None, model=None):
        self.base_url = (base_url or config.LLM_BASE_URL).rstrip("/")
        self.model = model or config.LLM_MODEL
        self.http = httpx.Client(
            timeout=httpx.Timeout(config.LLM_TIMEOUT, connect=15), headers={"Authorization": f"Bearer {api_key or config.LLM_API_KEY}"}
        )

    def _complete(self, system: str, text: str) -> dict:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": text[:24000]}],
            "temperature": 0,
            "max_tokens": 4000,  # corta generaciones desbocadas
            "response_format": {"type": "json_object"},
        }
        # Extracción literal: sin cadena de razonamiento (más rápida y sin texto fuera del JSON)
        if "openrouter.ai" in self.base_url:
            body["reasoning"] = {"enabled": False}
        else:
            body["reasoning_effort"] = "none"  # Ollama / vLLM
        last = None
        for attempt in range(3):
            try:
                r = self.http.post(f"{self.base_url}/chat/completions", json=body)
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"] or ""
                content = re.sub(r"^```(?:json)?|```$", "", content.strip()).strip()
                return json.loads(content[content.find("{") : content.rfind("}") + 1])
            except (httpx.HTTPError, KeyError, ValueError) as e:
                last = e
                time.sleep(3 * (attempt + 1))
        raise LLMError(str(last))

    def award(self, text: str) -> dict:
        d = self._complete(AWARD_PROMPT, text)
        for a in d.get("awardees") or []:
            _check_evidence(a, text)
            raw = a.get("cuit")
            a["cuit_raw"] = raw
            a["cuit"] = cuit_digits(raw) if raw and cuit_digits(raw) and cuit_digits(raw) in re.sub(r"\D", "", text) else None
            a["cuit_valid"] = cuit_valid(a["cuit"])
        return d

    def societario(self, text: str) -> dict:
        d = self._complete(SOC_PROMPT, text)
        c = cuit_digits(d.get("cuit"))
        d["cuit"] = c if c and cuit_valid(c) and c in re.sub(r"\D", "", text) else None
        for p in d.get("people") or []:
            _check_evidence(p, text)
            raw = p.get("dni")
            dd = dni_digits(raw)
            if not raw:
                p["dni"], p["dni_status"] = None, "missing"
            elif not dd:
                p["dni"], p["dni_status"] = None, "illegible"
            elif not dni_in_text(dd, text):
                p["dni"], p["dni_status"] = None, "inconsistent"  # el modelo devolvió un número que no está
            else:
                p["dni"], p["dni_status"] = dd, "ok"
        return d


def _check_evidence(item: dict, text: str) -> None:
    loc = locate_evidence(item.get("evidence"), text)
    item["evidence_score"] = round(loc[2], 3) if loc else 0.0
