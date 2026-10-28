"""Ejecución e interpretación de tests (pieza 2), independiente del runner.

Los tests se ejecutan en el entorno del bug (repo/env, Python 3.8) en subprocesos, igual que
run_test.sh: con caché de usuario limpia y restaurando antes los archivos de test.

- ejecutar_tests_bug: corre comandos_test; dice si pasan, guarda la salida y (opcional) la cobertura.
- ejecutar_modulos:   corre modulos_test con pytest (que también entiende unittest) y devuelve
                      el estado de cada test, para detectar regresiones.
- parsear_traceback:  extrae los frames (archivo, línea, función) de la salida de pytest o unittest.
"""
import json
import os
import re
import shlex
import signal
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import codigo
import tiempo

TIMEOUT_COMANDO = 60.0
_procesos = set()   # procesos en curso, para poder matarlos si se acaba el tiempo


def ejecutar(repo: Path, comando: str, timeout: float = TIMEOUT_COMANDO, env_extra: dict = None):
    """Ejecuta `comando` en repo/ con el entorno del bug activado. Devuelve (código, salida).
    Si supera el timeout mata todo el grupo de procesos y devuelve código -1."""
    timeout = max(1.0, min(timeout, tiempo.restante()))
    with tempfile.TemporaryDirectory() as cache:
        env = dict(os.environ, XDG_CACHE_HOME=cache, PYTHONDONTWRITEBYTECODE="1", **(env_extra or {}))
        env.pop("PYTHONPATH", None)
        p = subprocess.Popen(["bash", "-c", f"source env/bin/activate && {comando}"], cwd=repo, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             errors="replace", start_new_session=True)
        _procesos.add(p)
        try:
            salida, _ = p.communicate(timeout=timeout)
            return p.returncode, salida
        except subprocess.TimeoutExpired:
            _matar(p)
            return -1, (p.communicate()[0] or "") + f"\n[agente] TIMEOUT: el comando superó {timeout:.0f}s"
        finally:
            _procesos.discard(p)


def _matar(p) -> None:
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def matar_todos() -> None:
    """Mata los tests en curso (lo usa main.py al cortar por tiempo)."""
    for p in list(_procesos):
        _matar(p)


def restaurar_tests(repo: Path) -> list:
    """Deja los archivos de test como en el original (algunos tests reescriben sus datos)."""
    cambiados = subprocess.run(["git", "diff", "--name-only"], cwd=repo,
                               capture_output=True, text=True).stdout.split()
    cambiados = [f for f in cambiados if codigo.es_test(f)]
    if cambiados:
        subprocess.run(["git", "checkout", "-q", "--", *cambiados], cwd=repo)
    return cambiados


# ---------------------------------------------------------------- tests del bug

@dataclass
class ResultadoComando:
    comando: str
    pasa: bool
    salida: str


@dataclass
class ResultadoTests:
    pasan: bool                                   # True si todos los comandos pasan
    comandos: list = field(default_factory=list)  # ResultadoComando por comando
    cobertura: dict = None                        # archivo fuente -> set de líneas ejecutadas

    @property
    def salida_fallos(self) -> str:
        return "\n".join(f"$ {c.comando}\n{c.salida}" for c in self.comandos if not c.pasa)


def _con_cobertura(comando: str, rcfile: str):
    """Reescribe 'python -m X ...' o 'pytest ...' para que corra bajo coverage. None si no se puede."""
    m = re.match(r"\s*(?:python[\d.]*\s+-m\s+(\S+)|(pytest|py\.test))(.*)$", comando, re.S)
    if not m:
        return None
    modulo = m.group(1) or "pytest"
    return f"python -m coverage run --rcfile={shlex.quote(rcfile)} --omit='env/*' -m {modulo}{m.group(3)}"


def _leer_cobertura(repo: Path, datos: str, rcfile: str) -> dict:
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        destino = tmp.name
    try:
        codigo_salida, _ = ejecutar(repo, f"python -m coverage json --rcfile={shlex.quote(rcfile)} "
                                          f"-o {shlex.quote(destino)}", timeout=30,
                                    env_extra={"COVERAGE_FILE": datos})
        if codigo_salida != 0:
            return {}
        archivos = json.loads(Path(destino).read_text()).get("files", {})
    except (OSError, ValueError):
        return {}
    finally:
        Path(destino).unlink(missing_ok=True)
    cobertura = {}
    for ruta, info in archivos.items():
        rel = _relativa(repo, ruta)
        if rel and codigo.es_fuente(rel):
            cobertura[rel] = set(info.get("executed_lines", []))
    return cobertura


def ejecutar_tests_bug(repo: Path, comandos: list, cobertura: bool = False,
                       timeout: float = TIMEOUT_COMANDO) -> ResultadoTests:
    """Corre los comandos que exponen el bug. Con cobertura=True además mide qué líneas del
    código fuente se ejecutaron (si coverage no está disponible, sigue sin cobertura)."""
    restaurar_tests(repo)
    resultado = ResultadoTests(pasan=True, cobertura={} if cobertura else None)
    with tempfile.TemporaryDirectory() as tmp:
        rcfile = str(Path(tmp) / "coveragerc")
        Path(rcfile).write_text("[run]\n")   # ignora la configuración de coverage del proyecto
        for i, comando in enumerate(comandos):
            codigo_salida, salida = None, ""
            datos = str(Path(tmp) / f"datos{i}")
            cmd_cov = _con_cobertura(comando, rcfile) if cobertura else None
            if cmd_cov:
                codigo_salida, salida = ejecutar(repo, cmd_cov, timeout, {"COVERAGE_FILE": datos})
                if "No module named coverage" in salida:
                    codigo_salida, cmd_cov = None, None
            if codigo_salida is None:
                codigo_salida, salida = ejecutar(repo, comando, timeout)
            resultado.comandos.append(ResultadoComando(comando, codigo_salida == 0, salida))
            resultado.pasan &= codigo_salida == 0
            if cmd_cov and Path(datos).exists():
                for rel, lineas in _leer_cobertura(repo, datos, rcfile).items():
                    resultado.cobertura.setdefault(rel, set()).update(lineas)
    restaurar_tests(repo)
    return resultado


# ---------------------------------------------------------------- módulos de test (regresión)

@dataclass
class ResultadoModulos:
    completo: bool                                 # False si hubo timeout o no se pudo leer el reporte
    estados: dict = field(default_factory=dict)    # "archivo::Clase::test" -> pasa|falla|error|omitido
    mensajes: dict = field(default_factory=dict)   # test que no pasa -> mensaje de error (recortado)
    salida: str = ""

    @property
    def pasan(self) -> set:
        return {t for t, e in self.estados.items() if e == "pasa"}


def ejecutar_modulos(repo: Path, modulos: list, timeout: float = 120.0) -> ResultadoModulos:
    """Corre los módulos de test completos con pytest y devuelve el estado de cada test."""
    restaurar_tests(repo)
    # pytest no recolecta tests definidos en un __init__.py: se pasa su carpeta
    rutas = [str(Path(m).parent) if m.endswith("__init__.py") else m for m in modulos]
    opciones = ("-o 'python_files=__init__.py test_*.py *_test.py' "
                if any(m.endswith("__init__.py") for m in modulos) else "")
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
        xml = tmp.name
    codigo_salida, salida = ejecutar(
        repo, f"python -m pytest -p no:cacheprovider -q --continue-on-collection-errors "
              f"--junitxml={shlex.quote(xml)} {opciones}{' '.join(shlex.quote(r) for r in rutas)}",
        timeout)
    resultado = ResultadoModulos(completo=codigo_salida != -1, salida=salida)
    try:
        for caso in ET.parse(xml).getroot().iter("testcase"):
            nombre = f"{caso.get('classname')}::{caso.get('name')}"
            estado = "pasa"
            for hijo in caso:
                if hijo.tag in ("failure", "error", "skipped"):
                    estado = {"failure": "falla", "error": "error", "skipped": "omitido"}[hijo.tag]
                    if estado != "omitido":
                        texto = (hijo.get("message") or "") + "\n" + (hijo.text or "")
                        resultado.mensajes[nombre] = texto.strip()[-1500:]
            resultado.estados[nombre] = estado
    except (ET.ParseError, FileNotFoundError):
        resultado.completo = False
    finally:
        Path(xml).unlink(missing_ok=True)
    restaurar_tests(repo)
    return resultado


# ---------------------------------------------------------------- tracebacks

@dataclass(frozen=True)
class Frame:
    archivo: str     # relativo a repo/
    linea: int
    funcion: str
    es_test: bool


# Formato de Python/unittest:  File "/ruta/x.py", line 12, in funcion
_FRAME_PY = re.compile(r'File "([^"]+\.py)", line (\d+), in (\S+)')
# Formato de pytest (--tb=auto/long/short):  ruta/x.py:12: in funcion   |   ruta/x.py:12: TipoError
_FRAME_PYTEST = re.compile(r"^(\S+\.py):(\d+):(?: in (\S+))?", re.M)


def _relativa(repo: Path, ruta: str):
    """Ruta relativa a repo/ si el archivo pertenece al repo (y no a su entorno); si no, None."""
    p = Path(ruta)
    if not p.is_absolute():
        p = repo / p
    try:
        rel = p.resolve().relative_to(repo.resolve()).as_posix()
    except (ValueError, OSError):
        return None
    return None if codigo.NO_FUENTE.search(rel) else rel


def parsear_traceback(salida: str, repo: Path) -> list:
    """Frames del repo en el orden en que aparecen (de afuera hacia adentro), sin duplicados.
    Incluye frames de tests (es_test=True) y de código fuente."""
    encontrados = []
    for m in list(_FRAME_PY.finditer(salida)) + list(_FRAME_PYTEST.finditer(salida)):
        rel = _relativa(repo, m.group(1))
        if rel:
            encontrados.append((m.start(), Frame(rel, int(m.group(2)), m.group(3) or "?",
                                                 codigo.es_test(rel))))
    frames, vistos = [], set()
    for _, f in sorted(encontrados, key=lambda x: x[0]):
        if (f.archivo, f.linea) not in vistos:
            vistos.add((f.archivo, f.linea))
            frames.append(f)
    return frames


def lineas_de_error(salida: str, maximo: int = 15) -> list:
    """Líneas con el tipo y mensaje de la excepción o aserción (lo más informativo de la falla)."""
    # "E   ..." de pytest, o una línea sin sangría "modulo.TipoExcepcion: mensaje" / "KeyError"
    patron = re.compile(r"^(E\s+.+|(\w+\.)*[A-Z][a-z]\w*: .+|(\w+\.)*[A-Z]\w*(Error|Exception))$")
    return [l.rstrip() for l in salida.splitlines() if patron.match(l.rstrip())][:maximo]


def recortar(texto: str, maximo: int) -> str:
    """Recorta un texto largo conservando el principio y, sobre todo, el final (donde está el error)."""
    if len(texto) <= maximo:
        return texto
    cabeza = maximo // 4
    return texto[:cabeza] + "\n[... recortado ...]\n" + texto[-(maximo - cabeza):]
