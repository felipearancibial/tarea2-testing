"""Agente de reparación de bugs: punto de entrada.

Uso (el entorno del bug debe estar instalado con setup.sh):
    python main.py <carpeta_del_bug>
    python main.py ../dataset/black-2

Flujo: localizar -> reparar -> escribir localizacion.json y metricas.json en <carpeta_del_bug>/.
La reparación queda aplicada en <carpeta_del_bug>/repo/, que es lo que se evalúa.

Garantías de la orquestación:
  - Los dos JSON se escriben SIEMPRE: si una etapa lanza una excepción se usa un resultado de
    respaldo, y si el proceso recibe SIGTERM o se agota el tiempo (tiempo.LIMITE_DURO) se escriben
    los resultados y se termina antes del límite de 3 minutos.
  - Las métricas son las reales: las acumula llm.py en cada respuesta del modelo.
"""
import json
import os
import signal
import sys
import threading
import traceback
from pathlib import Path

import ejecutor
import llm
import tiempo
from localizador import localizar, region_de_respaldo
from reparador import reparar

_escritura = threading.Lock()
_estado = {"carpeta": None, "info": None, "localizacion": None, "terminado": False}


def escribir_resultados() -> None:
    """Escribe localizacion.json y metricas.json con lo que haya hasta ahora (una sola vez)."""
    with _escritura:
        if _estado["terminado"]:
            return
        _estado["terminado"] = True
        carpeta, info = _estado["carpeta"], _estado["info"]
        localizacion = _estado["localizacion"]
        if not localizacion or not localizacion.get("regiones"):
            localizacion = {"regiones": region_de_respaldo(carpeta / "repo")}
        (carpeta / "localizacion.json").write_text(json.dumps(localizacion, indent=2, ensure_ascii=False))
        metricas = {
            "bug": info["id"],
            "modelo": llm.MODELO,
            "llamadas": llm.metricas["llamadas"],
            "tokens_entrada": llm.metricas["tokens_entrada"],
            "tokens_salida": llm.metricas["tokens_salida"],
            "segundos": round(tiempo.transcurrido(), 1),
        }
        (carpeta / "metricas.json").write_text(json.dumps(metricas, indent=2))


def cortar(motivo: str) -> None:
    """Termina el agente de inmediato dejando los resultados escritos."""
    print(f"[main] corte: {motivo} ({tiempo.transcurrido():.1f}s)", flush=True)
    ejecutor.matar_todos()
    escribir_resultados()
    os._exit(0)


def main():
    if len(sys.argv) != 2:
        sys.exit("Uso: python main.py <carpeta_del_bug>")
    tiempo.iniciar()
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    _estado.update(carpeta=carpeta, info=info)

    # Corte duro por tiempo y por SIGTERM (p. ej. el timeout de ejecutar_lote.sh).
    reloj = threading.Timer(tiempo.LIMITE_DURO, cortar, args=("límite de tiempo",))
    reloj.daemon = True
    reloj.start()
    signal.signal(signal.SIGTERM, lambda *_: cortar("SIGTERM"))

    resultado = {"resuelto": False}
    try:
        try:
            localizacion = localizar(carpeta, info)
        except Exception:
            traceback.print_exc()
            print("[main] el localizador falló; se usa una localización de respaldo")
            localizacion = {"regiones": region_de_respaldo(carpeta / "repo")}
        _estado["localizacion"] = localizacion
        print(f"[main] localización: {len(localizacion['regiones'])} regiones "
              f"({tiempo.transcurrido():.1f}s)", flush=True)

        try:
            resultado = reparar(carpeta, info, localizacion)
        except Exception:
            traceback.print_exc()
            print("[main] el reparador falló")
    finally:
        reloj.cancel()
        escribir_resultados()
    print(f"{info['id']}: {'RESUELTO' if resultado.get('resuelto') else 'NO RESUELTO'} "
          f"({tiempo.transcurrido():.1f}s, {llm.metricas['llamadas']} llamadas)")


if __name__ == "__main__":
    main()
