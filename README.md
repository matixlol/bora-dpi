# BORA DPI

Herramienta para saber qué empresas y qué personas están detrás de la tecnología que compra el Estado argentino.

El Boletín Oficial publica las adjudicaciones de compras públicas (Tercera Sección) y los avisos de las sociedades: constituciones, cambios de directorio, cesiones de cuotas (Segunda Sección). Esa información es pública pero está dispersa. BORA DPI la junta:

- Busca adjudicaciones de tecnología y el CUIT de cada empresa adjudicataria.
- Lee los avisos societarios de esa empresa y extrae socios y autoridades.
- Sigue a esas personas a otras sociedades, aunque esas sociedades no tengan contratos con el Estado.
- Muestra todo como un mapa, donde cada vínculo cita el aviso original.

Las personas se identifican por DNI. Cuando un aviso no trae DNI, el vínculo queda marcado como dudoso hasta que alguien lo revise o se pueda confirmar con el Registro Nacional de Sociedades o el padrón de ARCA.

Lo desarrollan ODIA y DATySOC. Sitio de prueba: https://dpi.datos.party

## Uso

Requiere Python 3.13, [uv](https://docs.astral.sh/uv/), y `tesseract-ocr-spa` y `poppler-utils` para los avisos escaneados. La extracción usa un modelo de lenguaje, hospedado o local (ver abajo).

```sh
uv sync
uv run bora-dpi survey --desde 2025-01-01   # adjudicaciones de tecnología
uv run bora-dpi sources                     # Registro Nacional de Sociedades y padrón ARCA
uv run bora-dpi investigate                 # socios y otras sociedades
uv run bora-dpi autoresolve                 # resolver coincidencias dudosas con los registros
uv run bora-dpi serve                       # interfaz en http://localhost:8000
```

Para publicar una versión de solo lectura: `uv run bora-dpi export` y `npx wrangler deploy`.

## Modelo local

Funciona con cualquier servidor compatible con la API de OpenAI, como [Ollama](https://ollama.com), vLLM o llama.cpp. Usamos Qwen3.8 27B (27 mil millones de parámetros, pesos abiertos); con Ollama:

```sh
ollama pull qwen3.8:27b
export LLM_BASE_URL=http://localhost:11434/v1
export LLM_MODEL=qwen3.8:27b
export LLM_TIMEOUT=300   # en hardware lento, cada aviso puede tardar más de un minuto
```

Sin estas variables usa OpenRouter (`OPENROUTER_API_KEY`) con el mismo modelo (`qwen/qwen3.8-27b`).
