"""Cliente de Gemini: único punto del agente que habla con el LLM.

- Reintenta ante errores temporales (429, 5xx, red) con espera exponencial, sin pasarse del
  presupuesto de tiempo (tiempo.restante()).
- Nunca lanza excepciones: si no consigue respuesta devuelve None.
- Cuenta el costo real en `metricas` (llamadas con respuesta y tokens de usage_metadata),
  que main.py copia a metricas.json.

Uso:
    import llm
    texto = llm.generar(prompt)            # str o None
    datos = llm.generar_json(prompt)       # dict/list o None (respuesta inválida => None)
"""
import json
import random
import re
import time

import tiempo

MODELO = "gemini-3.5-flash-lite"

MAX_INTENTOS = 6           # intentos por llamada (incluye el primero)
ESPERA_BASE = 2.0          # segundos; se duplica en cada reintento
TIMEOUT_LLAMADA = 60.0     # segundos máximos esperando una respuesta
MARGEN = 5.0               # no iniciar una llamada si queda menos que esto
CODIGOS_TEMPORALES = {408, 429, 500, 502, 503, 504}

metricas = {"llamadas": 0, "tokens_entrada": 0, "tokens_salida": 0}
_cliente = None


def _obtener_cliente():
    """Crea el cliente una sola vez; toma la clave de GEMINI_API_KEY."""
    global _cliente
    if _cliente is None:
        from google import genai
        _cliente = genai.Client()
    return _cliente


def _codigo_http(error: Exception):
    codigo = getattr(error, "code", None)
    return codigo if isinstance(codigo, int) else None


def _espera_sugerida(error: Exception):
    """Si la API indica cuánto esperar (retryDelay en un 429), lo devuelve en segundos."""
    m = re.search(r"retryDelay\W+(\d+(?:\.\d+)?)s", str(error))
    return float(m.group(1)) if m else None


def _registrar_uso(respuesta) -> None:
    uso = getattr(respuesta, "usage_metadata", None)
    metricas["llamadas"] += 1
    metricas["tokens_entrada"] += (getattr(uso, "prompt_token_count", None) or 0)
    metricas["tokens_salida"] += (getattr(uso, "candidates_token_count", None) or 0)


def _texto(respuesta):
    try:
        return respuesta.text
    except Exception:   # p. ej. respuesta bloqueada o sin partes de texto
        return None


def generar(prompt: str, json_mode: bool = False, temperatura: float = 0.2, etiqueta: str = ""):
    """Envía `prompt` al modelo y devuelve el texto de la respuesta, o None si no fue posible."""
    try:
        from google.genai import types
        cliente = _obtener_cliente()
    except Exception as e:   # sin clave, librería ausente, etc.
        print(f"[llm] no se pudo crear el cliente: {e}")
        return None

    for intento in range(1, MAX_INTENTOS + 1):
        if not tiempo.alcanza(MARGEN):
            print(f"[llm] {etiqueta}: sin tiempo para llamar al modelo")
            return None
        timeout = min(TIMEOUT_LLAMADA, tiempo.restante() - 1)
        config = types.GenerateContentConfig(
            temperature=temperatura,
            response_mime_type="application/json" if json_mode else None,
            http_options=types.HttpOptions(timeout=int(timeout * 1000)),
        )
        t = time.perf_counter()
        try:
            respuesta = cliente.models.generate_content(model=MODELO, contents=prompt, config=config)
        except Exception as e:
            codigo = _codigo_http(e)
            if codigo is not None and codigo not in CODIGOS_TEMPORALES:
                print(f"[llm] {etiqueta}: error no recuperable {codigo}: {str(e)[:200]}")
                return None
            espera = _espera_sugerida(e) or ESPERA_BASE * 2 ** (intento - 1)
            espera += random.uniform(0, 1)
            print(f"[llm] {etiqueta}: intento {intento} falló ({codigo or type(e).__name__}); "
                  f"reintento en {espera:.1f}s")
            if intento == MAX_INTENTOS or not tiempo.alcanza(espera + MARGEN):
                return None
            time.sleep(espera)
            continue

        _registrar_uso(respuesta)
        print(f"[llm] {etiqueta}: respuesta en {time.perf_counter() - t:.1f}s "
              f"(acumulado: {metricas['llamadas']} llamadas, "
              f"{metricas['tokens_entrada']}+{metricas['tokens_salida']} tokens)")
        return _texto(respuesta)
    return None


def extraer_json(texto: str):
    """Parsea JSON de forma tolerante: quita bloques ``` y busca el primer objeto o lista."""
    if not texto:
        return None
    limpio = re.sub(r"^```\w*\s*|\s*```$", "", texto.strip())
    try:
        return json.loads(limpio)
    except json.JSONDecodeError:
        pass
    for abre, cierra in (("{", "}"), ("[", "]")):
        i, j = limpio.find(abre), limpio.rfind(cierra)
        if 0 <= i < j:
            try:
                return json.loads(limpio[i:j + 1])
            except json.JSONDecodeError:
                continue
    return None


def generar_json(prompt: str, reintentos_invalidos: int = 1, **kwargs):
    """Como generar(), pero exige JSON. Si la respuesta no es JSON válido, vuelve a pedirla
    (hasta `reintentos_invalidos` veces). Devuelve el JSON parseado o None."""
    for _ in range(reintentos_invalidos + 1):
        texto = generar(prompt, json_mode=True, **kwargs)
        if texto is None:
            return None
        datos = extraer_json(texto)
        if datos is not None:
            return datos
        print(f"[llm] {kwargs.get('etiqueta', '')}: respuesta no es JSON válido; se vuelve a pedir")
    return None
