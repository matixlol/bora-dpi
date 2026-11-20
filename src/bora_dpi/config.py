"""Configuración por variables de entorno."""

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("BORA_DATA_DIR", Path.cwd() / "data"))
DB_PATH = Path(os.environ.get("BORA_DB", DATA_DIR / "bora.db"))
ARCHIVE_DIR = DATA_DIR / "archive"

BORA_BASE = os.environ.get("BORA_BASE", "https://www.boletinoficial.gob.ar")
# Salida alternativa si BORA bloquea la IP: túnel SOCKS por una máquina con IP argentina,
# p. ej. `ssh -N -D 1080 usuario@maquina-tailscale` y BORA_PROXY=socks5h://127.0.0.1:1080
BORA_PROXY = os.environ.get("BORA_PROXY") or None
BORA_DELAY = float(os.environ.get("BORA_DELAY", "3.0"))  # segundos entre pedidos

# Modelo de extracción: cualquier endpoint compatible con OpenAI (/chat/completions).
# Desarrollo: OpenRouter con un modelo de pesos abiertos. Producción: Ollama/vLLM local,
# p. ej. LLM_BASE_URL=http://gpu:11434/v1 LLM_MODEL=qwen3:32b
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1")
LLM_API_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen/qwen3.8-27b")
LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))  # subir en hardware local lento
