"""Ejecución en paralelo de trabajo por afirmación (fase I).

Las búsquedas de evidencia y los veredictos son independientes entre afirmaciones y casi
todo su tiempo es espera de red. `parallel_map` los lanza en un pool de hilos limitado
(`PIPELINE_CONCURRENCY`, 4 por defecto; 1 = en serie, como antes), conserva el orden y,
si una tarea lanza una excepción (p. ej. un error de configuración que debe parar el
paso), cancela las pendientes y la propaga.
"""

from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(fn: Callable[[T], R], items: Sequence[T], max_workers: int = 1) -> list[R]:
    if max_workers <= 1 or len(items) <= 1:
        return [fn(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(max_workers, len(items))) as pool:
        futures = [pool.submit(fn, item) for item in items]
        done, _ = wait(futures, return_when=FIRST_EXCEPTION)
        for f in futures:
            if f.done() and f.exception() is not None:
                for other in futures:
                    other.cancel()
                raise f.exception()  # type: ignore[misc]
        return [f.result() for f in futures]
