"""Utilidades deterministas sobre el código del repositorio (sin LLM).

- Qué archivos son de test y cuáles son código fuente (el agente nunca edita tests).
- Índice de funciones y métodos de cada archivo con el AST (nombre, línea de inicio y de fin).
- Lectura de fragmentos con números de línea para construir prompts.
"""
import ast
import re
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# Mismo criterio que usan run_test.sh y verificar.py para reconocer archivos de test.
ES_TEST = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*$|_test\.py$|(^|/)conftest\.py$")
# Carpetas que no son código del proyecto (entorno, documentación, artefactos de build).
NO_FUENTE = re.compile(r"(^|/)(env|venv|\.venv|\.git|docs?|build|dist|[^/]*\.egg-info)/")


def es_test(rel: str) -> bool:
    return bool(ES_TEST.search(rel))


def es_fuente(rel: str) -> bool:
    """Archivo .py del proyecto que el agente puede localizar y modificar."""
    return rel.endswith(".py") and not es_test(rel) and not NO_FUENTE.search(rel) \
        and Path(rel).name != "setup.py"


@lru_cache(maxsize=None)
def listar_fuentes(repo: Path) -> tuple:
    """Archivos de código fuente versionados en el repo (rutas relativas a repo/)."""
    salida = subprocess.run(["git", "ls-files", "*.py"], cwd=repo, capture_output=True, text=True).stdout
    return tuple(f for f in salida.split() if es_fuente(f))


@lru_cache(maxsize=None)
def leer_lineas(repo: Path, rel: str) -> tuple:
    try:
        return tuple((repo / rel).read_text(errors="replace").splitlines())
    except OSError:
        return ()


def fragmento(repo: Path, rel: str, inicio: int, fin: int) -> str:
    """Líneas inicio..fin (desde 1, inclusivas) con su número, para mostrarle al LLM."""
    lineas = leer_lineas(repo, rel)
    inicio, fin = max(1, inicio), min(len(lineas), fin)
    ancho = len(str(fin))
    return "\n".join(f"{n:>{ancho}} | {lineas[n - 1]}" for n in range(inicio, fin + 1))


@dataclass(frozen=True)
class Bloque:
    """Una función o método de un archivo fuente."""
    archivo: str
    nombre: str        # nombre calificado, p. ej. "Clase.metodo"
    inicio: int        # incluye decoradores
    fin: int
    cuerpo: int = 0    # primera línea del cuerpo (lo que corre al llamarla, no al importarla)

    @property
    def nombre_corto(self) -> str:
        return self.nombre.rsplit(".", 1)[-1]

    @property
    def clase(self):
        return self.nombre.rsplit(".", 1)[0] if "." in self.nombre else None

    def __len__(self):
        return self.fin - self.inicio + 1


@lru_cache(maxsize=None)
def arbol(repo: Path, rel: str):
    try:
        return ast.parse("\n".join(leer_lineas(repo, rel)))
    except (SyntaxError, ValueError):
        return None


@lru_cache(maxsize=None)
def bloques(repo: Path, rel: str) -> tuple:
    """Todas las funciones y métodos del archivo (también los anidados)."""
    raiz = arbol(repo, rel)
    if raiz is None:
        return ()
    encontrados = []

    def visitar(nodo, prefijo):
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef)):
                nombre = prefijo + hijo.name
                inicio = min([hijo.lineno] + [d.lineno for d in hijo.decorator_list])
                encontrados.append(Bloque(rel, nombre, inicio, hijo.end_lineno, hijo.body[0].lineno))
                visitar(hijo, nombre + ".")
            elif isinstance(hijo, ast.ClassDef):
                visitar(hijo, prefijo + hijo.name + ".")
            else:
                visitar(hijo, prefijo)

    visitar(raiz, "")
    return tuple(encontrados)


def bloque_que_contiene(repo: Path, rel: str, linea: int):
    """La función más interna que contiene la línea, o None si es código de módulo/clase."""
    dentro = [b for b in bloques(repo, rel) if b.inicio <= linea <= b.fin]
    return min(dentro, key=len) if dentro else None


def invalidar_cache() -> None:
    """Llamar después de modificar archivos del repo: las lecturas y el AST están cacheados."""
    for f in (listar_fuentes, leer_lineas, arbol, bloques):
        f.cache_clear()


def palabras(nombre: str) -> set:
    """Divide un identificador en palabras: 'BracketTracker.mark_x' -> {bracket, tracker, mark, x}."""
    partes = re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+", nombre)
    return {p.lower() for p in partes}
