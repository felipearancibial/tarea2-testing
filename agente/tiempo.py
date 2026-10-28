"""Control del tiempo del agente: un único reloj compartido por todas las etapas.

El límite oficial es de 180 s por bug. El agente se da un presupuesto menor (PRESUPUESTO) para
dejar margen a escribir los resultados, y main.py arma además un corte duro (LIMITE_DURO) que
escribe los JSON y termina el proceso si alguna etapa se pasa.

Uso:
    import tiempo
    tiempo.iniciar()                 # una vez, al comenzar (main.py)
    tiempo.restante()                # segundos que quedan del presupuesto
    tiempo.alcanza(30)               # ¿quedan al menos 30 s?
"""
import time

PRESUPUESTO = 160.0   # segundos que pueden usar localizador + reparador
LIMITE_DURO = 172.0   # a partir de aquí main.py escribe los resultados y corta

_t0 = time.perf_counter()


def iniciar() -> None:
    global _t0
    _t0 = time.perf_counter()


def transcurrido() -> float:
    return time.perf_counter() - _t0


def restante() -> float:
    return max(0.0, PRESUPUESTO - transcurrido())


def alcanza(segundos: float) -> bool:
    return restante() >= segundos
