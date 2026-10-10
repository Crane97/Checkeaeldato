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
- **LLM:** API de Anthropic (Claude). Modelo configurable con `ANTHROPIC_MODEL`; nunca
  hardcodeado.
- **Transcripción:** proveedor en la nube detrás de una interfaz `Transcriber`
  (candidatos: ElevenLabs Scribe, Deepgram, AssemblyAI, Whisper API). Requisitos: español,
  diarización, timestamps por palabra o segmento.
- **Audio:** `ffmpeg` (instalado con Homebrew).
- **Web (fase 4):** Next.js + TypeScript, Tailwind, desplegado en Vercel.
- **Datos (fase 3):** Supabase (Postgres, pgvector, Storage, Auth).

## Estructura de carpetas (fase 1)

```
pipeline/
  __init__.py
  cli.py              # entrada: `uv run verificar <video>`
  config.py           # carga de variables de entorno
  models.py           # modelos pydantic (Segment, Claim, Evidence, Verdict, Report)
  audio.py            # extracción con ffmpeg
  transcribe/         # interfaz Transcriber + proveedores
  claims.py           # detección y normalización de afirmaciones
  evidence/           # fuentes: factcheck_api.py, oficiales.py, web.py
  verdict.py          # veredicto basado solo en evidencia
  report.py           # agregación y salida JSON
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
    speaker: str                 # orador diarizado; mapeable a nombre real
    start: float                 # segundos
    end: float
    text_original: str           # literal de la transcripción
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
6. **Trazabilidad.** Cada Claim enlaza a su timestamp en el vídeo y a sus fuentes.
7. **Privacidad y derechos.** Los vídeos subidos se borran tras procesarlos; se conserva
   la transcripción y el informe.

## Fuentes de evidencia (por orden de prioridad)

1. Google Fact Check Tools API (ClaimReview): Maldita, Newtral, EFE Verifica, Verificat…
2. Fuentes oficiales: INE, Eurostat, SEPE, Banco de España, BOE, Ministerio de Hacienda.
3. Búsqueda web general (último recurso; priorizar medios y organismos reconocidos).

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
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=
TRANSCRIPTION_PROVIDER=
TRANSCRIPTION_API_KEY=
GOOGLE_FACTCHECK_API_KEY=
```

## Comandos

```
uv sync                         # instalar dependencias
uv run verificar data/x.mp4     # ejecutar pipeline completo
uv run pytest                   # tests
uv run python eval/run_eval.py  # evaluación contra el gold set
uv run ruff check . && uv run ruff format .
```

## Cómo trabajar en este repo

- Antes de cambios grandes, proponer un plan y esperar aprobación.
- Commits pequeños y descriptivos, uno por cambio lógico.
- Al modificar cualquier prompt de `prompts/`, ejecutar la evaluación y reportar las
  métricas antes/después.
- Si una decisión de diseño no está cubierta aquí, preguntar en vez de suponer, y
  actualizar este archivo cuando se decida.
