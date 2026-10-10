---
name: tdd
description: Flujo de desarrollo guiado por tests (rojo → verde → refactor) para este repo, con pytest y APIs simuladas. Usar al implementar un paso o función nueva del pipeline o de eval/, y al corregir un bug (primero un test que lo reproduzca).
---

<!-- Adaptado de agents/tdd-guide.md y skills/tdd-workflow de Everything Claude Code (MIT, © 2026 Affaan Mustafa). -->

# TDD en este repo

## Ciclo

1. **Interfaz mínima.** Crea la firma de la función o clase con tipos completos y un cuerpo
   `raise NotImplementedError`, para que el test falle por comportamiento y no por un import.
2. **Rojo.** Escribe los tests en `tests/test_<módulo>.py` describiendo el comportamiento
   esperado. Ejecuta `uv run pytest tests/test_<módulo>.py -q` y **comprueba que fallan por la
   razón correcta**. Un test que pasa sin implementación no prueba nada.
3. **Verde.** Implementa lo mínimo para que pasen.
4. **Refactor.** Limpia nombres y duplicados con los tests en verde.
5. **Suite completa.** `uv run pytest -q` y `uv run ruff check . && uv run ruff format .`.
6. **Commit.** Test e implementación en el mismo commit (uno por cambio lógico, como pide CLAUDE.md).

Para un **bug**: primero un test que lo reproduzca y falle; luego el arreglo. El test se queda
como regresión.

## Reglas del proyecto

- **Nunca llamar a APIs reales en tests** ni gastar créditos. Simula:
  - HTTP (`httpx`): `respx`.
  - Anthropic: un cliente falso inyectado que devuelve objetos con la forma de la respuesta.
  - `subprocess`/ffmpeg: `monkeypatch` sobre el módulo (ver `tests/test_audio.py`).
- **Los tests no dependen del `.env` del desarrollador**: `Settings(_env_file=None)` y
  `monkeypatch` para variables de entorno.
- **Respuestas de ejemplo** de proveedores en `tests/fixtures/` como JSON, sin datos personales.
- Inyecta dependencias (cliente, reloj, rutas) por parámetro en lugar de parchear globales cuando
  sea posible.
- Usa `tmp_path` para cualquier archivo; nunca escribas en `data/`.

## Qué probar primero en este dominio

1. **Invariantes de CLAUDE.md**: sin evidencia → `no_verificable`; veredicto ≠ `no_verificable`
   con al menos una fuente; `matched_factcheck` solo de un verificador; sin palabras prohibidas;
   prompts sin nombres de partidos.
2. **Fallos parciales**: una afirmación o un bloque que falla no tumba el trabajo y queda
   registrado.
3. **Reanudación**: los pasos leen y escriben `data/<job_id>/` y no repiten llamadas de pago.
4. **Coste**: cada llamada de pago registra su `CostEntry`.
5. Casos límite: listas vacías, texto vacío, timestamps en el borde de un bloque, Unicode y tildes.

## Cobertura

`uv run pytest --cov=pipeline --cov-report=term-missing`. Objetivo ≥ 80 % en `pipeline/`, pero
no escribas tests solo para subir el número: cada test debe proteger un comportamiento.

## Fuera del alcance de los tests unitarios

La **calidad** de los prompts (si detecta bien afirmaciones, si acierta veredictos) no se mide con
tests unitarios sino con `eval/run_eval.py` contra el gold set. Al cambiar un prompt, ejecuta la
evaluación y reporta métricas antes/después.
