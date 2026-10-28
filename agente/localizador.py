"""LOCALIZADOR: decide en qué parte del código fuente está el bug.

Uso por separado (para probarlo solo):
    python localizador.py <carpeta_del_bug>      # imprime la localización en JSON

Recibe:
  carpeta  Path de la carpeta del bug (contiene repo/, bug.json, run_test.sh, ...)
  info     contenido de bug.json como dict (id, comandos_test, archivos_test, modulos_test, ...)

Devuelve un dict con, al menos, este formato (se guarda como localizacion.json y se evalúa):
  {
    "regiones": [
      {"archivo": "black.py", "inicio": 120, "fin": 145}
    ]
  }
  - archivo: ruta relativa a repo/, de un archivo de código fuente (no de test)
  - inicio, fin: líneas (desde 1, ambas incluidas) del archivo con el bug
Puedes añadir más campos (p. ej. tu análisis o el coste en tokens) para usarlos en el reparador.

Cómo localiza:
  1. Ejecuta los tests del bug bajo coverage: obtiene la salida (traceback, aserción) y qué
     líneas del código fuente se ejecutaron.
  2. Señales deterministas -> puntaje para cada función/método del repo:
       - traceback: funciones del código fuente por las que pasó el error (más cerca del error = más),
       - código del test: identificadores que usa el test y palabras del nombre del test,
       - mensaje de error: identificadores que aparecen en él,
       - cobertura: funciones ejecutadas por el test (las no ejecutadas se castigan).
  3. Una llamada al LLM reordena los mejores candidatos viendo su código y la falla.
  4. Devuelve las regiones (función completa, o una ventana si la función es enorme).
Además devuelve en "falla" la información de la falla, para que el reparador no repita el trabajo.
"""
import ast
import json
import math
import re
import shlex
import sys
from collections import defaultdict
from pathlib import Path

import codigo
import ejecutor
import llm
import tiempo

MAX_REGIONES = 6            # regiones que se reportan en localizacion.json
MAX_CANDIDATOS_LLM = 20     # candidatos que ve el LLM para reordenar
MAX_LINEAS_REGION = 150     # funciones más largas se recortan alrededor de la línea sospechosa
VENTANA = 40                # líneas antes/después de la línea sospechosa al recortar
MAX_CHARS_SALIDA = 5000     # salida de tests en el prompt
MAX_CHARS_TEST = 4000       # código de tests en el prompt
MAX_CHARS_CANDIDATOS = 18000
LINEAS_POR_CANDIDATO = 25
# Palabras de nombres de test que no dicen nada sobre dónde está el bug.
PALABRAS_GENERICAS = {"test", "tests", "get", "set", "the", "and", "for", "with", "without", "not",
                      "error", "errors", "none", "should", "works", "case", "basic", "simple"}


# ---------------------------------------------------------------- tests objetivo

def tests_objetivo(repo: Path, comandos: list) -> list:
    """Identifica los tests que ejecutan los comandos: [(archivo, [Clase, ] funcion), ...].
    Entiende ids de pytest (ruta.py::Clase::test) y de unittest (paquete.modulo.Clase.test)."""
    encontrados = []
    for comando in comandos:
        try:
            tokens = shlex.split(comando)
        except ValueError:
            tokens = comando.split()
        for tok in tokens:
            if tok.startswith("-"):
                continue
            if "::" in tok:
                ruta, *nombres = tok.split("::")
                if (repo / ruta).is_file():
                    encontrados.append((ruta, [re.sub(r"\[.*$", "", n) for n in nombres]))
            elif tok.endswith(".py") and (repo / tok).is_file():
                encontrados.append((tok, []))
            elif re.fullmatch(r"[\w.]+", tok) and "." in tok:
                partes = tok.split(".")
                for i in range(len(partes), 0, -1):
                    base = "/".join(partes[:i])
                    for ruta in (base + ".py", base + "/__init__.py"):
                        if (repo / ruta).is_file():
                            encontrados.append((ruta, partes[i:]))
                            break
                    else:
                        continue
                    break
    return encontrados


def _buscar_funcion(raiz, nombres: list):
    """Nodo de la función de test indicada por [Clase, ] funcion dentro del módulo."""
    nodo = raiz
    for nombre in nombres:
        hijos = [h for h in ast.walk(nodo) if isinstance(h, (ast.ClassDef, ast.FunctionDef,
                                                              ast.AsyncFunctionDef)) and h.name == nombre]
        if not hijos:
            return nodo if nodo is not raiz else None
        nodo = hijos[0]
    return nodo if nodo is not raiz else None


def codigo_de_tests(repo: Path, tests: list) -> list:
    """Código fuente de cada test objetivo y de los métodos auxiliares de su clase que usa."""
    resultado = []
    for ruta, nombres in tests:
        raiz = codigo.arbol(repo, ruta)
        if raiz is None or not nombres:
            continue
        nodo = _buscar_funcion(raiz, nombres)
        if nodo is None:
            continue
        lineas = codigo.leer_lineas(repo, ruta)
        texto = "\n".join(lineas[nodo.lineno - 1:nodo.end_lineno])
        # métodos auxiliares de la misma clase llamados como self.algo(...)
        clase = _buscar_funcion(raiz, nombres[:-1]) if len(nombres) > 1 else None
        if isinstance(clase, ast.ClassDef):
            usados = {n.attr for n in ast.walk(nodo) if isinstance(n, ast.Attribute)
                      and isinstance(n.value, ast.Name) and n.value.id == "self"}
            for m in clase.body:
                if isinstance(m, ast.FunctionDef) and m.name in usados and m is not nodo:
                    texto += "\n\n" + "\n".join(lineas[m.lineno - 1:m.end_lineno])
        resultado.append({"archivo": ruta, "nombre": ".".join(nombres), "inicio": nodo.lineno,
                          "fin": nodo.end_lineno, "codigo": texto, "nodo": nodo, "raiz": raiz})
    return resultado


def identificadores_de_tests(tests_codigo: list) -> tuple:
    """(identificadores usados en los tests, módulos fuente que importan, palabras del nombre)."""
    usados, modulos, palabras = set(), set(), set()
    for t in tests_codigo:
        for n in ast.walk(t["nodo"]):
            if isinstance(n, ast.Name):
                usados.add(n.id)
            elif isinstance(n, ast.Attribute):
                usados.add(n.attr)
        for n in ast.walk(t["raiz"]):
            if isinstance(n, ast.ImportFrom) and n.module:
                modulos.add(n.module)
                modulos.update(f"{n.module}.{a.name}" for a in n.names)
            elif isinstance(n, ast.Import):
                modulos.update(a.name for a in n.names)
        palabras |= codigo.palabras(t["nombre"].rsplit(".", 1)[-1])
    palabras = {p for p in palabras if len(p) >= 3 and p not in PALABRAS_GENERICAS}
    return usados, modulos, palabras


def _archivo_de_modulo(repo: Path, modulo: str):
    # Se compara con los archivos que conoce git (no con el disco): en macOS el disco no distingue
    # mayúsculas y "scrapy.http.Request" (una clase) parecería la carpeta scrapy/http/request.
    base = modulo.replace(".", "/")
    fuentes = codigo.listar_fuentes(repo)
    for ruta in (base + ".py", base + "/__init__.py"):
        if ruta in fuentes:
            return ruta
    return None


# ---------------------------------------------------------------- puntaje de candidatos

def puntuar(repo: Path, frames: list, cobertura, usados: set, modulos: set,
            palabras_test: set, errores: list) -> dict:
    """Puntaje de sospecha por bloque (función/método). Devuelve {Bloque: (puntaje, razones)}."""
    puntaje = defaultdict(float)
    razones = defaultdict(list)
    foco = {}   # bloque -> línea concreta señalada por el traceback

    def sumar(b, valor, razon):
        puntaje[b] += valor
        if razon not in razones[b]:
            razones[b].append(razon)

    # 1) Traceback: frames del código fuente; el más profundo (cercano al error) pesa más.
    fuente = [f for f in frames if not f.es_test and codigo.es_fuente(f.archivo)]
    for distancia, f in enumerate(reversed(fuente)):
        b = codigo.bloque_que_contiene(repo, f.archivo, f.linea)
        if b is None:   # código a nivel de módulo o clase: ventana alrededor de la línea
            b = codigo.Bloque(f.archivo, f"<módulo:{f.linea}>", max(1, f.linea - 10), f.linea + 10)
        sumar(b, 10 * 0.75 ** distancia, f"en el traceback (línea {f.linea})")
        foco.setdefault(b, f.linea)

    # Universo de archivos a revisar: los del traceback, los ejecutados y los importados por el test.
    archivos = {f.archivo for f in fuente}
    archivos |= set(cobertura or {})
    importados = {a for a in (_archivo_de_modulo(repo, m) for m in modulos) if a}
    archivos |= importados
    if not archivos:
        archivos = set(codigo.listar_fuentes(repo))

    todos = [b for a in archivos for b in codigo.bloques(repo, a)]
    por_nombre = defaultdict(int)
    for b in todos:
        por_nombre[b.nombre_corto] += 1
    palabras_error = set(re.findall(r"[A-Za-z_]\w{2,}", " ".join(errores)))

    for b in todos:
        # 2) Identificadores que usa el test (más valor si el nombre es poco común en el repo).
        if b.nombre_corto in usados:
            sumar(b, 3 / math.sqrt(por_nombre[b.nombre_corto]), "usado por el test")
        elif b.clase and b.clase.rsplit(".", 1)[-1] in usados:
            sumar(b, 0.5, "método de una clase usada por el test")
        # 3) Palabras del nombre del test presentes en el nombre de la función.
        comunes = palabras_test & codigo.palabras(b.nombre)
        if comunes:
            sumar(b, 1.5 * len(comunes), f"coincide con el nombre del test ({', '.join(sorted(comunes))})")
        # 4) Identificadores mencionados en el mensaje de error.
        if b.nombre_corto in palabras_error or (b.clase and b.clase.rsplit(".", 1)[-1] in palabras_error):
            sumar(b, 2, "mencionado en el error")
        if b.archivo in importados:
            sumar(b, 0.3, "archivo importado por el test")
        # 5) Cobertura: ejecutadas suman; no ejecutadas se castigan (si hay cobertura).
        if cobertura:
            ejecutadas = cobertura.get(b.archivo, set())
            n = sum(1 for l in range(max(b.cuerpo, b.inicio), b.fin + 1) if l in ejecutadas)
            if n:
                sumar(b, 1.0, f"ejecutada por el test ({n} líneas)")
            elif b in puntaje:
                puntaje[b] *= 0.3

    return {b: (round(puntaje[b], 2), razones[b], foco.get(b)) for b in puntaje if puntaje[b] > 0}


def anidados_fuera(candidatos: list) -> list:
    """Si una función y otra anidada en ella son candidatas, conserva solo la de mayor puntaje."""
    elegidos = []
    for b, datos in candidatos:
        if any(o.archivo == b.archivo and not (o.fin < b.inicio or b.fin < o.inicio) for o, _ in elegidos):
            continue
        elegidos.append((b, datos))
    return elegidos


# ---------------------------------------------------------------- reordenamiento con el LLM

PROMPT_LOCALIZAR = """Eres un experto en depuración de proyectos Python. Un test falla por un bug \
en el código fuente del proyecto (no en el test). Debes decidir en qué función está el bug.

## Salida de los tests que fallan
{salida}

## Código de los tests que fallan
{tests}

## Funciones candidatas (elegidas por traceback, cobertura y uso en el test)
{candidatos}

Ordena las candidatas de más a menos probable de contener el bug (la línea que hay que \
modificar para que el test pase). Considera qué espera el test y qué produce el código.
Responde SOLO con JSON con este formato, con hasta {k} ids de la lista:
{{"ranking": [{{"id": 3, "razon": "breve"}}, {{"id": 7, "razon": "breve"}}]}}"""


def _texto_candidato(repo: Path, i: int, b, razones, foco) -> str:
    if foco and len(b) > LINEAS_POR_CANDIDATO:
        ini = max(b.inicio, foco - LINEAS_POR_CANDIDATO // 2)
    else:
        ini = b.inicio
    fin = min(b.fin, ini + LINEAS_POR_CANDIDATO - 1)
    cola = "" if fin >= b.fin else f"\n   ... (la función sigue hasta la línea {b.fin})"
    return (f"[id {i}] {b.archivo} :: {b.nombre} (líneas {b.inicio}-{b.fin}); señales: {'; '.join(razones)}\n"
            f"{codigo.fragmento(repo, b.archivo, ini, fin)}{cola}")


def reordenar_con_llm(repo: Path, candidatos: list, salida: str, tests_codigo: list) -> list:
    """Devuelve los candidatos reordenados según el LLM (los no mencionados quedan después,
    en su orden original). Si el LLM falla, devuelve el orden original."""
    if len(candidatos) < 2:
        return candidatos
    bloques_txt, total = [], 0
    for i, (b, (_, razones, foco)) in enumerate(candidatos[:MAX_CANDIDATOS_LLM]):
        texto = _texto_candidato(repo, i, b, razones, foco)
        if total + len(texto) > MAX_CHARS_CANDIDATOS:
            break
        bloques_txt.append(texto)
        total += len(texto)
    tests_txt = "\n\n".join(f"# {t['archivo']}\n{t['codigo']}" for t in tests_codigo) or "(no disponible)"
    prompt = PROMPT_LOCALIZAR.format(
        salida=ejecutor.recortar(salida, MAX_CHARS_SALIDA),
        tests=ejecutor.recortar(tests_txt, MAX_CHARS_TEST),
        candidatos="\n\n".join(bloques_txt), k=min(5, len(bloques_txt)))
    respuesta = llm.generar_json(prompt, etiqueta="localizador")
    if not isinstance(respuesta, dict) or not isinstance(respuesta.get("ranking"), list):
        print("[localizador] el LLM no entregó un ranking válido; se usa el orden heurístico")
        return candidatos
    orden = []
    for item in respuesta["ranking"]:
        i = item.get("id") if isinstance(item, dict) else item
        if isinstance(i, int) and 0 <= i < len(bloques_txt) and i not in orden:
            orden.append(i)
    print(f"[localizador] ranking del LLM: {orden}")
    return [candidatos[i] for i in orden] + [c for j, c in enumerate(candidatos) if j not in orden]


# ---------------------------------------------------------------- regiones

def a_region(b, puntaje, razones, foco) -> dict:
    inicio, fin = b.inicio, b.fin
    if len(b) > MAX_LINEAS_REGION and foco:
        inicio, fin = max(b.inicio, foco - VENTANA), min(b.fin, foco + VENTANA)
    return {"archivo": b.archivo, "inicio": inicio, "fin": fin, "funcion": b.nombre,
            "funcion_inicio": b.inicio, "funcion_fin": b.fin, "puntaje": puntaje, "razones": razones}


def region_de_respaldo(repo: Path) -> list:
    """Si no hay ninguna señal: el comienzo del archivo fuente más grande (formato válido)."""
    fuentes = codigo.listar_fuentes(repo)
    if not fuentes:
        return []
    archivo = max(fuentes, key=lambda f: len(codigo.leer_lineas(repo, f)))
    n = max(1, len(codigo.leer_lineas(repo, archivo)))
    return [{"archivo": archivo, "inicio": 1, "fin": min(n, MAX_LINEAS_REGION), "razones": ["respaldo"]}]


def _ajustar(repo: Path, region: dict) -> dict:
    """Garantiza que inicio/fin estén dentro del archivo."""
    n = max(1, len(codigo.leer_lineas(repo, region["archivo"])))
    region["inicio"] = min(max(1, int(region["inicio"])), n)
    region["fin"] = min(max(region["inicio"], int(region["fin"])), n)
    return region


def localizar(carpeta: Path, info: dict) -> dict:
    repo = carpeta / "repo"

    # 1) Ejecutar los tests del bug con cobertura.
    resultado = ejecutor.ejecutar_tests_bug(repo, info.get("comandos_test", []), cobertura=True)
    salida = resultado.salida_fallos or "\n".join(c.salida for c in resultado.comandos)
    print(f"[localizador] tests del bug: {'pasan' if resultado.pasan else 'fallan'} "
          f"({tiempo.transcurrido():.1f}s); archivos ejecutados: {len(resultado.cobertura or {})}")

    # 2) Señales deterministas.
    frames = ejecutor.parsear_traceback(salida, repo)
    errores = ejecutor.lineas_de_error(salida)
    tests = tests_objetivo(repo, info.get("comandos_test", []))
    tests_codigo = codigo_de_tests(repo, tests)
    usados, modulos, palabras_test = identificadores_de_tests(tests_codigo)
    puntajes = puntuar(repo, frames, resultado.cobertura, usados, modulos, palabras_test, errores)
    candidatos = anidados_fuera(sorted(puntajes.items(), key=lambda x: -x[1][0]))
    print(f"[localizador] {len(candidatos)} candidatos; mejores: "
          + ", ".join(f"{b.nombre}={d[0]}" for b, d in candidatos[:5]))

    # 3) Reordenar con el LLM.
    candidatos = reordenar_con_llm(repo, candidatos, salida, tests_codigo)

    # 4) Regiones.
    regiones = [_ajustar(repo, a_region(b, *datos)) for b, datos in candidatos[:MAX_REGIONES]]
    if not regiones:
        regiones = [_ajustar(repo, r) for r in region_de_respaldo(repo)]

    return {
        "regiones": regiones,
        "falla": {
            "tests_pasan_originalmente": resultado.pasan,
            "salida": ejecutor.recortar(salida, 8000),
            "errores": errores,
            "frames": [{"archivo": f.archivo, "linea": f.linea, "funcion": f.funcion, "es_test": f.es_test}
                       for f in frames],
            "tests": [{k: t[k] for k in ("archivo", "nombre", "inicio", "fin", "codigo")}
                      for t in tests_codigo],
        },
    }


if __name__ == "__main__":
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    tiempo.iniciar()
    print(json.dumps(localizar(carpeta, info), indent=2, ensure_ascii=False))
    print(json.dumps(llm.metricas), file=sys.stderr)
