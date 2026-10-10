# CLAUDE.md — Verificador de discursos políticos

## Qué es este proyecto

Plataforma web que analiza vídeos de discursos y debates políticos en español y muestra,
de todas las afirmaciones factuales que se hacen, cuántas son verdaderas, engañosas, falsas
o no verificables, con fuentes para cada una.

Contexto: elecciones generales en España el **30 de noviembre de 2026**. La campaña empieza
~15 días antes. Objetivo: **beta pública antes del 13 de noviembre**.

Fase futura: análisis en tiempo real (streaming) de debates en directo.

## Fases

1. **Prototipo CLI (actual)** — script en Python: vídeo → audio → transcripción con oradores
   → afirmaciones → evidencia → veredictos en JSON. Sin web.
2. **Calidad** — conjunto de prueba (gold set) con verificaciones publicadas por Maldita,
   Newtral, EFE Verifica; ajuste de prompts; métricas.
3. **Backend** — worker + cola de trabajos + Postgres/pgvector + caché de afirmaciones.
4. **Web** — subida, progreso, panel de resultados, reproductor sincronizado.
5. **Beta** — metodología pública, avisos legales, auditoría de neutralidad, límites de uso.
6. **Casi en vivo** — procesar bloques de 3–5 min durante un debate.
7. **Tiempo real** — transcripción en streaming y veredictos provisionales.

No adelantes trabajo de fases posteriores salvo que se pida. Diseña las interfaces para
que no haya que reescribir al pasar de fase.

## Arquitectura objetivo

```
[Next.js en Vercel] --subida directa--> [Almacenamiento: Supabase Storage / R2]
        |                                         |
        | crea job                                v
        +----------> [Cola] ----------> [Worker Python (FastAPI)]
                                                |
                     ffmpeg → ASR con diarización → detección de afirmaciones
                     → normalización → caché (pgvector) → búsqueda de evidencia
                     → veredicto (Claude) → agregación
                                                |
                                                v
                                    [Supabase Postgres + pgvector]
```

- El procesamiento pesado **nunca** va en funciones serverless de Vercel.
- Nada de modelos locales pesados: la máquina de desarrollo es un MacBook Air Intel.
  Transcripción y LLM siempre vía API.

## Stack

- **Pipeline / worker:** Python 3.11+, `uv` para dependencias, `pydantic` para modelos,
  `httpx` para HTTP, `ruff` para lint/format, `pytest` para tests.
- **LLM:** Claude. `LLM_BACKEND=api` (API de Anthropic; obligatorio para la beta pública) o
  `LLM_BACKEND=claude-code` (suscripción local vía `claude -p`, solo para pruebas en local:
  sin herramientas, sin MCP, desde una carpeta vacía y sin `ANTHROPIC_API_KEY`). Modelo configurable con `ANTHROPIC_MODEL` y, por
  paso, `ANTHROPIC_MODEL_DETECTION` / `ANTHROPIC_MODEL_VERDICT`; nunca hardcodeado. Para
  la demo local: `claude-haiku-5-5` (mínimo coste); subir de modelo solo si la evaluación
  lo justifica.
- **Transcripción:** proveedor en la nube detrás de una interfaz `Transcriber`. Elegido:
  **ElevenLabs Scribe v2** (por lotes, diarización incluida). Requisitos: español,
  diarización, timestamps por palabra o segmento.
- **Búsqueda web:** **Tavily** (plan gratuito, 1.000 créditos/mes) detrás de una interfaz,
  para fuentes oficiales y web general.
- **Audio:** `ffmpeg` (instalado con Homebrew).
- **Web (fase 4):** Next.js + TypeScript, Tailwind, desplegado en Vercel.
- **Datos (fase 3):** Supabase (Postgres, pgvector, Storage, Auth).

## Estructura de carpetas (fase 1)

```
pipeline/
  __init__.py
  cli.py              # entrada: `uv run verificar <video|texto>`
  run.py              # monta los pasos (texto o vídeo) y los ejecuta; clientes perezosos
  config.py           # carga de variables de entorno
  http.py             # peticiones HTTP con reintentos (evidencia y transcripción)
  llm.py              # llamadas a Claude (API) con JSON validado y registro de coste
  llm_claude_code.py  # misma interfaz vía `claude -p` (suscripción local, solo pruebas)
  costs.py            # estimación de coste; precios en pricing.toml
  jobs.py             # data/<job_id>/, manifest y pasos reanudables
  models.py           # modelos pydantic (Segment, Claim, Evidence, Verdict, Report)
  neutrality.py       # palabras prohibidas y nombres de partidos (prompts y salidas)
  audio.py            # extracción con ffmpeg
  text_input.py       # entrada .txt/.md: un segmento por párrafo, sin tiempos
  transcribe/         # interfaz Transcriber + proveedores
  claims.py           # detección y normalización de afirmaciones
  evidence/           # base.py (interfaz y recolector), factcheck_api.py, tavily.py,
                      # oficiales.py (+ fuentes_oficiales.toml), web.py
  verdict.py          # veredicto basado solo en evidencia
  report.py           # agregación y salida JSON
  evaluation.py       # formato gold, emparejamiento y métricas
  web/                # web LOCAL de pruebas (FastAPI + una página): `uv run verificar-web`.
                      # No es la web de la fase 4; solo escucha en 127.0.0.1
prompts/              # prompts versionados (.md), NUNCA incrustados en el código
eval/
  gold/               # casos de prueba (JSON) con veredicto esperado
  run_eval.py         # ejecuta el pipeline contra el gold set y saca métricas
tests/
data/                 # vídeos y salidas locales (en .gitignore)
```

## Modelo de datos central

```python
class Claim:
    id: str
    speaker: str                 # orador diarizado; mapeable a nombre real ("autor" en texto)
    text_original: str           # cita literal; debe poder encontrarse en el original
    start: float | None          # segundos en el vídeo; None si la entrada es texto
    end: float | None            # (los dos o ninguno)
    text_normalized: str         # autocontenida: quién, qué, cifra, periodo
    claim_type: Literal["estadistica", "hecho_historico", "legal", "comparacion", "otro"]
    verdict: Literal["verdadero", "enganoso", "falso", "no_verificable"]
    confidence: float            # 0–1
    explanation: str             # breve, neutral, en español
    sources: list[Source]        # url, título, organismo, fecha
    matched_factcheck: str | None  # URL si coincide con un desmentido existente
```

## Categorías de veredicto (definiciones fijas)

- **verdadero** — la evidencia respalda la afirmación tal como se formuló.
- **enganoso** — tiene base real pero omite contexto, exagera, mezcla periodos o compara
  mal.
- **falso** — la evidencia la contradice claramente.
- **no_verificable** — no hay evidencia suficiente o fiable. Es el valor por defecto ante
  la duda.

En la interfaz se puede etiquetar como **"bulo"** solo una afirmación `falso` con
`matched_factcheck` (desmentido publicado por un verificador reconocido).

**Nunca** usar las palabras "mentira" ni "miente" en salidas, prompts ni interfaz.

## Principios no negociables

1. **Solo evidencia recuperada.** El veredicto se basa exclusivamente en las fuentes
   encontradas en ese análisis. Nada de conocimiento previo del modelo. Sin evidencia →
   `no_verificable`.
2. **Toda afirmación con veredicto distinto de `no_verificable` lleva al menos una fuente.**
3. **Neutralidad.** Mismo método y mismos prompts para todos los partidos y oradores. Los
   prompts no mencionan partidos concretos. Las explicaciones describen hechos, no juzgan
   intenciones.
4. **Opiniones y promesas no se verifican.** La detección debe descartarlas.
5. **Contexto temporal.** Cada veredicto considera la fecha del discurso; las fuentes
   llevan fecha.
6. **Trazabilidad.** Cada Claim cita literalmente la frase original y enlaza a sus
   fuentes; en vídeo, además, a su timestamp.
7. **Privacidad y derechos.** Los vídeos subidos se borran tras procesarlos; se conserva
   la transcripción y el informe.

## Entradas

- **Vídeo** (`.mp4`, etc.): audio → transcripción con oradores → afirmaciones con
  timestamp.
- **Texto** (`.txt` / `.md`): sin audio ni transcripción; un segmento por párrafo y orador
  `autor` (nombre real con `--autor`). Sirve para probar sin coste de transcripción.
- `--fecha AAAA-MM-DD` es obligatoria (contexto temporal). `--oradores` es opcional; si
  solo cambian los nombres, se regenera el informe sin coste.
- `job_id` = primeros 12 caracteres del SHA-256 del archivo de entrada.

## Fuentes de evidencia (cascada, por orden de prioridad)

1. Google Fact Check Tools API (ClaimReview): Maldita, Newtral, EFE Verifica, Verificat…
   → fuentes de tipo `factcheck`.
2. Fuentes oficiales: búsqueda (Tavily) restringida a la lista editable de dominios
   oficiales (INE, Eurostat, SEPE, Banco de España, BOE, Hacienda, Seguridad Social,
   AIReF, CIS, Congreso, OCDE…) → tipo `oficial`.
3. Búsqueda web general, solo si lo oficial no da resultados relevantes → tipo `web`
   (un resultado de dominio oficial se marca `oficial`).

El informe indica en qué se apoya cada veredicto (`basis`: desmentido, oficial o solo web);
un veredicto apoyado solo en web tiene confianza máxima 0,6. Límites por afirmación: hasta
2 búsquedas oficiales y 1 web (≈30–45 créditos de Tavily por artículo de ~15 afirmaciones).
Resultados de Tavily con relevancia < 0,4 se descartan (umbral provisional, ajustar con la
evaluación). Pendiente: las fuentes aún no filtran por `--fecha`; el veredicto la tiene en
cuenta, pero pueden llegar fuentes posteriores al discurso. Por ahora **no hay lista de medios
"reconocidos"**: es una decisión de neutralidad pendiente del responsable del proyecto.

## Fallos y trazabilidad

- Errores de configuración (clave inválida, API sin habilitar, créditos agotados) **paran**
  el trabajo con un mensaje claro.
- Un fallo puntual se registra donde ocurre y el trabajo sigue: `claims.json`
  (`failed_blocks`, `rejected`), `evidence.json` (`errors` por afirmación) y en el informe
  (`analysis_error`, `evidence_errors`, `warnings`). Un fallo de búsqueda nunca se presenta
  como "no hay evidencia".
- Si un paso falla en todos sus elementos (o una fuente en ≥3 búsquedas seguidas de todas
  las afirmaciones), se para: un informe vacío por un error sería un fallo silencioso.
- Una fuente sin clave en `.env` se desactiva y el informe lo avisa.
- El coste gastado por un paso que falla queda en el `manifest.json`.

## Convenciones

- Código en inglés (nombres de variables, funciones); textos para el usuario, prompts y
  explicaciones en español.
- Tipado completo; modelos pydantic para todo lo que cruce módulos.
- Cada paso del pipeline es una función pura con entrada/salida serializable, para poder
  cachearlo y reanudarlo. Guardar resultados intermedios en `data/<job_id>/`.
- Llamadas a APIs externas con reintentos y timeouts; nunca fallar el job entero por una
  afirmación.
- Registrar coste estimado (tokens, minutos de audio) por ejecución.
- Secretos solo en `.env` (en `.gitignore`); mantener `.env.example` actualizado.

## Variables de entorno

```
LLM_BACKEND=claude-code         # api | claude-code
ANTHROPIC_API_KEY=              # solo con LLM_BACKEND=api
ANTHROPIC_MODEL=
ANTHROPIC_MODEL_DETECTION=      # opcional; por defecto ANTHROPIC_MODEL
ANTHROPIC_MODEL_VERDICT=        # opcional; por defecto ANTHROPIC_MODEL
TRANSCRIPTION_PROVIDER=elevenlabs
TRANSCRIPTION_API_KEY=
GOOGLE_FACTCHECK_API_KEY=
TAVILY_API_KEY=
```

## Comandos

```
uv sync                         # instalar dependencias
uv run verificar data/x.mp4 --fecha 2026-11-20               # vídeo
uv run verificar data/articulo.txt --fecha 2026-11-20         # texto
uv run verificar data/x.mp4 --fecha 2026-11-20 --from-step evidence  # reanudar
uv run verificar-web            # web local de pruebas en http://localhost:8000
uv run pytest                   # tests
uv run python eval/run_eval.py                   # evaluación completa contra el gold set
uv run python eval/run_eval.py --modo veredicto  # solo veredicto, desde las citas gold
uv run ruff check . && uv run ruff format .
```

## Cómo trabajar en este repo

- Antes de cambios grandes, proponer un plan y esperar aprobación.
- Commits pequeños y descriptivos, uno por cambio lógico.
- Al modificar cualquier prompt de `prompts/`, ejecutar la evaluación y reportar las
  métricas antes/después.
- Si una decisión de diseño no está cubierta aquí, preguntar en vez de suponer, y
  actualizar este archivo cuando se decida.
- Desarrollo guiado por tests (skill `tdd` en `.claude/skills/`): test que falla primero,
  APIs siempre simuladas en tests.

## Harness de Claude Code (`.claude/`)

- **Hooks** (`settings.json`): bloquean el acceso a `.env`; pasan `ruff` a cada `.py`
  editado; revisan la neutralidad de `prompts/*.md` y recuerdan ejecutar la evaluación.
- **Agentes** (bajo demanda): `refactor-cleaner` (código muerto),
  `silent-failure-hunter` (errores silenciados; tras los pasos de evidencia y veredicto),
  `type-design-analyzer` (modelos pydantic). Adaptados de Everything Claude Code (MIT).
