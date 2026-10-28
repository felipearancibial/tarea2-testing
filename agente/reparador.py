"""REPARADOR: modifica el código fuente de repo/ para que los tests del bug pasen.

Uso por separado (con una localización ya guardada):
    python reparador.py <carpeta_del_bug> <localizacion.json>

Recibe:
  carpeta       Path de la carpeta del bug (contiene repo/, bug.json, run_test.sh, ...)
  info          contenido de bug.json como dict
  localizacion  lo que devolvió localizar() (ver localizador.py)

Hace: deja la reparación aplicada en carpeta/repo/. Solo puede cambiar código fuente, no tests.

Devuelve un dict con, al menos:
  {"resuelto": true}      # true si run_test.sh pasa al terminar
Puedes añadir más campos (p. ej. intentos o el coste en tokens).

Piezas ya disponibles (hechas junto con el localizador):
  localizacion["regiones"]      [{archivo, inicio, fin, funcion, funcion_inicio, funcion_fin, puntaje, razones}]
                                ordenadas de más a menos sospechosa.
  localizacion["falla"]         {salida, errores, frames, tests: [{archivo, nombre, inicio, fin, codigo}],
                                 tests_pasan_originalmente}: evita volver a correr los tests al empezar.
  llm.generar(prompt) / llm.generar_json(prompt)
                                llamadas a Gemini con reintentos (429/503), conteo de tokens y sin
                                excepciones (devuelven None si no hay respuesta).
  ejecutor.ejecutar_tests_bug(repo, info["comandos_test"]).pasan / .salida_fallos
  ejecutor.ejecutar_modulos(repo, info["modulos_test"]).estados  {"archivo::Clase::test": "pasa"|...}
                                comparar .pasan antes/después del cambio para detectar regresiones.
  ejecutor.parsear_traceback, ejecutor.recortar
  codigo.es_fuente(rel) / codigo.es_test(rel)   nunca editar un archivo con es_test(rel) True.
  codigo.fragmento(repo, archivo, inicio, fin)  código con números de línea para el prompt.
  codigo.bloques / codigo.bloque_que_contiene   funciones del archivo (AST).
  codigo.invalidar_cache()                      llamarla tras editar un archivo (las lecturas se cachean).
  tiempo.restante() / tiempo.alcanza(seg)       presupuesto compartido; main.py corta en tiempo.LIMITE_DURO.
"""
import json
import sys
from pathlib import Path


def reparar(carpeta: Path, info: dict, localizacion: dict) -> dict:
    # TODO
    raise NotImplementedError


if __name__ == "__main__":
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    localizacion = json.loads(Path(sys.argv[2]).read_text())
    print(json.dumps(reparar(carpeta, info, localizacion), indent=2, ensure_ascii=False))
