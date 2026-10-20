# CLAUDE.md — Verificador de discursos políticos

## Qué es este proyecto

Plataforma web que analiza vídeos de discursos y debates políticos en español y muestra,
de todas las afirmaciones factuales que se hacen, cuántas son verdaderas, engañosas, falsas
o no verificables, con fuentes para cada una.

Contexto: elecciones generales en España el **domingo 29 de noviembre de 2026** (Real Decreto
806/2026, de 5 de octubre, BOE-A-2026-20742: disuelve las Cortes el 6 de octubre). La campaña
va del **13 al 27 de noviembre**. Objetivo: **beta pública antes del 13 de noviembre** (el
primer día de campaña).

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
  la demo local: detección con `claude-haiku-5-5` y **veredicto y contexto con
  `claude-sonnet-5-5`** (`ANTHROPIC_MODEL_VERDICT`): en la evaluación del 2026-10-11 acertó
  lo mismo que Opus 5.5 (17/17) a la mitad de coste. El veredicto es ~96 % del gasto: se le
  envían como máximo 8 fuentes (oficiales y verificadores primero) con extractos de 2.500
  caracteres.
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
  speakers.py         # etapa 1: interlocutores y lo que el texto dice de ellos, con citas
  speaker_research.py # etapa 2: partido y cargo de cada orador (oficial → web), con citas
  politicians.py      # base de políticos: emparejar nombres, partido y cargos en una fecha
  claims.py           # detección y normalización de afirmaciones y promesas
  promises.py         # viabilidad de las promesas y consecuencias (fuentes + razonamiento)
  governments.py      # tabla de gobiernos y lógica de fechas (sin LLM)
  indicators.py       # base local de indicadores oficiales (data/knowledge/indicators/)
  indicator_update.py # descarga INE / Eurostat / Seguridad Social (scripts/actualizar_indicadores.py)
  duplicates.py       # repeticiones del mismo orador: se verifican una vez
  context.py          # resolución de "ustedes", "este Gobierno"… → actor y periodos
  evidence/           # base.py (interfaz y recolector), factcheck_api.py, tavily.py,
                      # oficiales.py (+ fuentes_oficiales.toml), web.py
  verdict.py          # veredicto basado solo en evidencia
  report.py           # agregación y salida JSON
  evaluation.py       # formato gold, emparejamiento y métricas
  web/                # web LOCAL de pruebas (FastAPI + una página): `uv run verificar-web`.
                      # No es la web de la fase 4; solo escucha en 127.0.0.1.
                      # static/report.{css,js}: pintado compartido con el informe HTML
  report_html.py      # informe HTML autocontenido e interactivo (filtros orador/categoría)
  email_report.py     # envío por SMTP: resumen estático en el cuerpo + informe adjunto
                      # (los clientes de correo bloquean JavaScript)
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
    kind: Literal["afirmacion", "promesa"]  # las promesas van a Report.promises (viabilidad)
    text_original: str           # cita literal; debe poder encontrarse en el original
    start: float | None          # segundos en el vídeo; None si la entrada es texto
    end: float | None            # (los dos o ninguno)
    text_normalized: str         # autocontenida: quién, qué, cifra, periodo
    claim_type: Literal["estadistica", "hecho_historico", "legal", "comparacion", "otro"]
    context_status: Literal["no_necesario", "resuelto", "no_resuelto"]
    resolved_actor: str | None   # "PP (gobiernos de José María Aznar y Mariano Rajoy)"
    time_scope: list[DateRange]  # periodos calculados con la tabla de gobiernos
    resolution_confidence: float | None
    resolution_notes: str | None # razonamiento breve de la resolución
    verdict: Literal["verdadero", "verdad_a_medias", "confusion", "falso", "no_verificable"]
    category: ...                # calculada: veredicto + si cita una fuente oficial (ver abajo)
    confidence: float            # 0–1
    explanation: str             # breve, neutral, en español
    sources: list[Source]        # url, título, organismo, fecha
    matched_factcheck: str | None  # URL si coincide con un desmentido existente
```

## Categorías de veredicto (definiciones fijas)

Decididas por el responsable del proyecto (2026-10-10). Claude decide el **veredicto**:

- **verdadero** — la evidencia respalda **todas** las partes de la afirmación tal como se
  formuló. Se admite el redondeo a la precisión usada por quien habla o una desviación
  mínima (≤ 5 % relativo): "15 %" por un 15,4 % es válido.
- **verdad_a_medias** — solo para afirmaciones **generales sin cifra** ("ha subido los
  impuestos a las familias") que la evidencia respalda en parte y contradice en parte. Una
  sola categoría (no "verdad a medias" / "falso a medias"): decidir hacia qué lado se
  inclina sería una valoración subjetiva. La explicación detalla ambas partes. Si las
  medidas de un sentido **pesan claramente más** (personas afectadas e importe), el
  veredicto es el de ese sentido. En afirmaciones sobre un grupo amplio ("las familias"),
  una medida que afecta a una pequeña minoría pesa poco, en los dos sentidos.
- Las proposiciones, enmiendas e intervenciones de grupos parlamentarios y partidos son
  posturas, no evidencia, aunque se publiquen en webs oficiales.
- **Recaudación no es subida de impuestos** (decisión del responsable del proyecto,
  2026-10-11): más recaudación puede venir del crecimiento, el empleo o la inflación; hacen
  falta medidas concretas o una fuente que atribuya el aumento a decisiones fiscales.
- **No exigir probar una ausencia:** si la evidencia oficial muestra medidas del sentido
  contrario para la mayoría del grupo y ninguna del sentido afirmado que afecte a una parte
  sustancial, es `falso`, no `no_verificable`.
- **IRPF sin deflactar** (decisión del responsable del proyecto, 2026-10-13): no es una
  subida legislada; en "han subido los impuestos" no cuenta por sí sola (se menciona como
  contexto), en "pagamos más impuestos" / "ha subido la presión fiscal" sí cuenta.

### Cómo se decide y se comprueba un veredicto (decidido 2026-10-13)

- **Orden del esquema:** `evidence_used` → `analysis` (claim_parsed, evidence_assessment con
  `stance` apoya/contradice/neutral por fuente, numeric_comparison, nuances, conclusion) →
  `numeric_check` → `verdict` → `confidence` → `explanation`. El campo se llama `analysis` y
  no `reasoning`: los modelos 5.5 rechazan (`reasoning_extraction`, y se cobra) las
  peticiones de escribir su razonamiento interno en la respuesta. El análisis se guarda en
  `verdicts.json` para auditar y **no** va al informe ni a la interfaz.
- **Comprobaciones en código** (`pipeline/verdict.py`): un `E#` citado que no existe →
  `needs_review` + `no_verificable`; `sources` = evidencia usada y mencionada, **fuentes
  primarias primero** (`[primarias]` en `fuentes_oficiales.toml`); `falso` exige una fuente
  `contradice` y `verdadero` una `apoya` (simétrico por neutralidad); una explicación o
  conclusión que contradice la etiqueta ("coincide", "respalda", "es correcta"…, teniendo
  en cuenta la negación) se pregunta **una vez más** y, si sigue incoherente, `needs_review`.
- **Fuentes del propio orador:** si el orador es de un partido del Gobierno en la fecha, La
  Moncloa y las salas de prensa de los ministerios (`*.gob.es/prensa…`) se marcan
  `del_gobierno_del_orador` y cuentan como `neutral`. Los institutos de estadística y el BOE
  no (publican datos, no posiciones). Pendiente: webs de partidos.
- **Razonamiento interno** solo en el veredicto: `VERDICT_THINKING` (sin definir = lo que
  haga el modelo), `VERDICT_EFFORT`, `VERDICT_THINKING_BUDGET`. Con la API, los modelos 4.6+
  usan razonamiento adaptativo y la profundidad se controla con el esfuerzo (un presupuesto
  fijo de tokens solo lo aceptan los modelos hasta 4.5; Sonnet 5.5 no puede apagarlo del
  todo, su mínimo es `between_tools`). Con `claude-code` se traduce a `--effort`. El resumen
  del razonamiento (API) se guarda en `verdicts.json`. Cambiar el ajuste recalcula los
  veredictos.
- **confusion** — el contenido es correcto pero una referencia secundaria está mal (número
  de artículo o ley, nombre de la norma u organismo, fecha del documento) sin cambiar el
  sentido: "el artículo 32 de la Constitución dice que el Rey es el Jefe del Estado" (es el
  56). Una cifra equivocada nunca es confusión; atribuir a una norma algo que no dice es
  falso.
- **falso** — la evidencia contradice al menos una parte esencial, directamente o por
  cálculo con las cifras de la evidencia (límites, totales, imposibilidades). **Una cifra
  exagerada o rebajada más allá del redondeo es falsa** ("bajó un 20 %" si bajó un 15 %).
  No existe la categoría "engañoso".
- **no_verificable** — la evidencia no permite confirmar ni refutar, ni calculando. Es el
  valor por defecto ante la duda.

El código combina el veredicto con las fuentes citadas para dar la **categoría** que ve el
usuario. "Fuente oficial" = organismo público o internacional **o verificador profesional**
(Maldita, Newtral, EFE Verifica…):

| Categoría | Veredicto | Fuentes citadas |
|---|---|---|
| Verdadero | verdadero | al menos una oficial o de verificador |
| Verdadero sin fuente oficial | verdadero | solo web (confianza máx. 0,6) |
| Verdad a medias | verdad_a_medias | cualquiera (se indica en qué fuente se apoya) |
| Confusión | confusion | cualquiera (se indica en qué fuente se apoya) |
| No verificable | no_verificable | — |
| Falso sin fuente oficial | falso | solo web (confianza máx. 0,6) |
| Falso con fuente oficial | falso | al menos una oficial o de verificador |

## Promesas

Decidido por el responsable del proyecto (2026-10-13). Paso `promises` (tras `verdicts`,
prompt `promesa.md`, modelo de veredicto), con la misma evidencia en cascada. Valoración:

- **viable** — cabe en el marco legal y es realista con los datos y recursos disponibles.
- **dudosa** — no choca con la ley ni con los datos, pero depende de condiciones inciertas
  (mayorías, financiación, acuerdos con la UE u otras administraciones).
- **inviable** — contradice el marco legal vigente (derecho de la UE: España es Estado
  miembro; Constitución; competencias) o es irrealista según los datos (paro del 0 %).
- **no_evaluable** — sin evidencia suficiente; valor por defecto (como `no_verificable`).

Cada promesa indica sus obstáculos (`legal`, `competencias`, `presupuesto`, `datos`,
`otro`), las **consecuencias que señalan las fuentes** citadas y, aparte, un breve
**razonamiento propio** del modelo, siempre marcado como tal ("Razonamiento propio (no
procede de las fuentes): …"). No se valora si la promesa es deseable. En el informe van en
una sección aparte y **no cuentan** en los totales de afirmaciones.

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
4. **Las opiniones no se verifican; las promesas concretas se valoran aparte.** La
   detección descarta opiniones, eslóganes y promesas vagas. Sí se extraen las afirmaciones
   generales sobre acciones concretas contrastables aunque no lleven cifra ("este Gobierno
   ha subido los impuestos a las familias", "han recortado la sanidad"). Las **promesas
   concretas** (medida identificable o meta medible) se extraen con `kind="promesa"` y no
   reciben veredicto de verdad, sino una valoración de viabilidad (ver "Promesas").
5. **Contexto temporal.** Cada veredicto considera la fecha del discurso; las fuentes
   llevan fecha.
6. **Trazabilidad.** Cada Claim cita literalmente la frase original y enlaza a sus
   fuentes; en vídeo, además, a su timestamp.
7. **Privacidad y derechos.** Los vídeos subidos se borran tras procesarlos; se conserva
   la transcripción y el informe.

## Entradas

- **Vídeo** (`.mp4`, etc.): audio → transcripción con oradores → afirmaciones con
  timestamp.
- **Texto** (`.txt` / `.md`): sin audio ni transcripción. Oradores:
  - **Diálogo** ("Nombre: texto" en varias líneas): un segmento por intervención y orador.
    Se descartan rótulos que no son personas (Fuente, Nota, Crónica…). Los párrafos sin
    etiqueta **continúan el turno** de quien hablaba cuando los turnos van separados por
    líneas en blanco (los diarios de sesiones solo rotulan el primer párrafo); en un diálogo
    compacto (un turno por línea), una línea en blanco devuelve el texto al `autor`. Un
    rótulo de documento ("Nota:", "Crónica:") también lo devuelve al `autor`.
  - **Prosa**: todo es del `autor` (nombre real con `--autor`), salvo que el texto recoja lo
    que **dijo una persona concreta** ("la ministra afirmó que…"): entonces la detección le
    atribuye la afirmación. Citar una fuente de datos ("según el INE") no cambia el orador.
  - Variantes del mismo nombre se unifican ("Sánchez" → "Pedro Sánchez") salvo si es ambiguo.
  - Criterio general: **ante la duda, no atribuir** a nadie una afirmación que no consta que
    hiciera.
- `--fecha AAAA-MM-DD` es obligatoria (contexto temporal). `--oradores` es opcional; si
  solo cambian los nombres, se regenera el informe sin coste.
- `job_id` = primeros 12 caracteres del SHA-256 del archivo de entrada.

## Oradores y contexto político

- **Etapa 1 — interlocutores** (paso `speakers`, tras la transcripción): cuántos hay y qué
  turno es de cada uno (diarización en vídeo, rótulos en texto) y etiqueta → nombre,
  partido y rol. Fuentes por orden: `--speakers speakers.json` o el campo "Quién es quién"
  de la web; presentaciones y vocativos del texto (Claude debe citar frases literales, que
  el código comprueba; el partido solo se acepta si aparece en ellas); la tabla de
  gobiernos para presidentes. Por debajo de 0,6 de confianza, desconocido. **Nunca se
  toma un partido de lo que el modelo "sabe".**
- **Etapa 2 — investigación de los oradores** (paso `speaker_research`, salida
  `speaker_research.json`, que es lo que leen los pasos siguientes): para cada orador con
  nombre y sin partido, búsqueda Tavily primero en dominios oficiales (Congreso, Senado, La
  Moncloa, BOE…) y, solo si no lo aclaran, en la web general. Claude extrae partido, cargo
  en la fecha del discurso y nombre completo citando frases literales; el código exige que
  las frases estén en los resultados y nombren a la persona (todas las palabras del nombre
  en las páginas citadas) y al partido. El orador guarda las URLs (`references`) y la base
  (`oficial` o `web`). Sin `TAVILY_API_KEY` el paso no hace nada.
- **Resolución de contexto** (paso `context`, entre detección y evidencia): "ustedes",
  "nosotros", "este Gobierno", "el Gobierno anterior", "cuando gobernaban", "con el señor
  X", "hace diez años". Claude **solo interpreta** el referente; **las fechas las calcula
  código Python** (`pipeline/governments.py`) con la tabla. La afirmación se reescribe con
  periodos explícitos y las búsquedas se acotan a esos años. Si no se resuelve con
  confianza (orador o interlocutor sin partido, audiencia, ambigüedad), la afirmación queda
  `no_verificable` con el motivo y no se gastan búsquedas.
- "Nosotros" dicho por alguien del partido que gobierna, sin pista de otra época, se
  entiende como el mandato en curso (decisión del responsable del proyecto, 2026-10-11).
- **Tabla de gobiernos** `data/knowledge/governments.json` (versionada; única excepción
  al `.gitignore` de `data/`): mandatos con partidos, socios de coalición, periodos en
  funciones y decretos de nombramiento, contrastados con La Moncloa y el BOE. **Se
  actualiza a mano cuando cambia el Gobierno** (y tras las elecciones del 29-11-2026). El
  esquema admite niveles autonómico y municipal; solo se usa el nacional.

## Base de indicadores oficiales (decidido 2026-10-13)

`data/knowledge/indicators/<id>.json` (versionada): series con valor, unidad, periodo y
**fuente primaria por dato** (organismo, documento, URL, fecha) y notas metodológicas.
- **Automáticos** (`estado: automatico`), con `uv run python scripts/actualizar_indicadores.py`
  (revisar el diff antes del commit): paro EPA y paro de menores de 25 (INE, trimestral),
  IPC interanual (INE, mensual), población (INE, ECP), PIB y su crecimiento, deuda (% PIB y
  millones) y déficit (Eurostat, anual, Protocolo de Déficit Excesivo), afiliación media
  (CSV de la Seguridad Social, mensual).
- **Manuales** (`estado: pendiente_revision` hasta que el responsable los revisa; no se usan
  antes): SMI (24 reales decretos leídos del BOE, 2004-2026, con los cambios de 2004 y 2021),
  pensiones y pensionistas por separado (serie histórica del informe del INSS).
- **Uso:** la detección marca a qué indicador (`indicator`) y años (`years`) se refiere cada
  afirmación; si el indicador cubre **todo** el periodo (el del contexto resuelto o esos años),
  la evidencia es el extracto de la serie (`indicador_local`, fuente primaria) más la Fact
  Check API (gratuita), sin Tavily. Si no, se busca en la web como siempre; la ronda
  adicional sigue disponible. Un cambio en la base recalcula detección y evidencia.

## Base de conocimiento oficial en Supabase (en construcción, 2026-10-14)

**Hoja de ruta, estado de cada fase y pendientes: `docs/base_de_conocimiento.md`.**

Sustituirá a la base JSON. Esquemas `ref` (catálogo, observaciones, gobiernos, partidos,
hechos), `evid` (hechos de la web), `verif` (veredictos y sus `observation_ids`) y `ops`
(ingesta, migraciones), en `db/migrations/NNNN_*.sql` (cada migración pasa por
`database-reviewer`). Conexión: `SUPABASE_DB_URL` en `.env` (proyecto de desarrollo).
- **Observaciones inmutables:** ni UPDATE ni DELETE; una revisión del dato oficial es una
  versión nueva. Solo se usan las `aprobado`; nunca las `pendiente` ni `cuarentena`.
- **Catálogo** `pipeline/knowledge/catalog.yaml`: definición precisa, sinónimos, rango,
  un umbral de salto (% en niveles, puntos en tasas), antigüedad máxima, tema, retraso de
  publicación y fuente. Unidad, definición y frecuencia no cambian: otro id.
- **Validación** (`validate.py`) antes de entrar: unidad, rango y salto → cuarentena;
  igual al vigente → nada; distinto → versión nueva. **Los datos de fuentes oficiales
  primarias (BOE, INE, Eurostat, INSS…) entran aprobados si pasan la validación**, también
  los copiados a mano (decisión del responsable, 2026-10-14); solo lo que no venga de una
  fuente oficial primaria entraría `pendiente`. La cuarentena (anomalías) sigue igual.
  `conocimiento aprobar-oficiales` aprueba lo pendiente con fuente primaria.
- **Fecha de publicación:** la real si la fuente la da (BOE); si no, **fin del periodo +
  retraso habitual del calendario** del organismo, marcada como estimada (decisión del
  responsable, 2026-10-14). El resolvedor usa la versión publicada a la fecha del discurso.
- **Revisión humana:** `uv run conocimiento revisar` muestra cada elemento pendiente
  (observaciones, gobiernos, partidos, hechos) con fuente, motivos y valores con los que
  compararlo, y registra quién aprueba o rechaza. Antes de aprobar datos curados, agente
  `data-verifier`.
- **Trazabilidad:** cada veredicto guardado en `verif.claims` lleva los `observation_ids`
  que usó; `conocimiento impacto` los lista con el comando para recalcular cada trabajo.
- **Golden tests** (`tests/test_knowledge_golden.py`): valores contrastados con su fuente;
  si fallan, se investiga el cambio, no se corrige el test.

### Ingesta automática (fase D, 2026-10-14)

- `uv run conocimiento ingerir [--simular] [--indicador X] [--forzar] [--avisar]`
  (`pipeline/knowledge/ingest.py`): descarga los indicadores del catálogo con conector
  `ine`, `eurostat` o `seguridad_social` (APIs públicas, **sin clave**), compara una huella
  de los valores con `ops.source_state` y, si cambió, carga con la validación (nuevo →
  aprobado; dudoso → cuarentena; revisión → versión nueva) en una ejecución de
  `ops.ingest_runs` por conector. Un fallo de un indicador se registra y el resto sigue
  (salida con código 2). `--avisar` envía a `ALERT_EMAIL_TO` los errores, cuarentenas y
  datos más viejos que `max_antiguedad_dias`.
- Manuales (sin conector): SMI (BOE), pensiones, pensionistas y pensión media (INSS), IVA.
- **Rol `ingesta`** (migración 0006): lee el catálogo, inserta observaciones y fuentes,
  retira vigencias y registra ejecuciones; no borra, no aprueba, no ve políticos ni
  veredictos. Se crea sin login: el responsable le pone contraseña en el SQL Editor
  (`alter role ingesta with login password '…'`), nunca en archivos.
- **GitHub Actions** (`.github/workflows/ingesta.yml`): diario a las 07:30 UTC, **carga
  de verdad** desde el 2026-10-19 (antes, en simulación). Secretos (del entorno
  `Production` de GitHub, no del repositorio):
  `SUPABASE_DB_URL` (pooler, usuario `ingesta.<ref>`), `SMTP_*`, `EMAIL_FROM`,
  `ALERT_EMAIL_TO`.

### El análisis con la base de conocimiento (fases E y F, 2026-10-14)

- **Fuente local** (`pipeline/knowledge/local_base.py`): con `SUPABASE_DB_URL`, la base
  local de indicadores son las observaciones **aprobadas** de Supabase, resueltas **a la
  fecha del discurso** (la versión publicada entonces; el extracto dice si se revisó
  después y si la fecha de publicación es estimada). Sin la variable, o si la base no
  responde, se usa la base JSON y el informe lo avisa. Lo pendiente (p. ej. el SMI hasta
  que se apruebe) no aparece en el catálogo de la detección.
- **Cálculo del sistema**: cada extracto de serie termina con la aritmética del periodo
  hecha en Python (primer y último valor, diferencia en puntos o en %, máximo y mínimo).
- **API bajo demanda** (`pipeline/knowledge/on_demand.py`): si la base no cubre el
  periodo y el indicador tiene conector, se descarga la serie entera en el momento, se
  valida como en la ingesta (lo anómalo se descarta) y se usa como evidencia `oficial`
  marcada «valores actuales de la API»; no se guarda en la base ni se busca en la web.
- **Trazabilidad**: cada veredicto se guarda en `verif.claims` con la evidencia citada y
  los `observation_ids` de las series locales (`conocimiento impacto` los encuentra). Un
  fallo al guardar no para el análisis; el informe avisa.
- **Datos web reutilizables** (F2, `pipeline/knowledge/web_facts.py`, prompt
  `extraccion_dato.md`): tras un veredicto apoyado en la web sobre un indicador del
  catálogo y un único año, el modelo de detección extrae el dato con su cita literal (el
  código exige que la cita esté en la fuente y contenga el número; el enunciado no admite
  palabras valorativas) y se guarda en `evid.web_facts` por claves exactas. Después se usa
  como evidencia; si viene de un organismo oficial o un verificador, sin buscar en la web.
  Los comunicados del Gobierno o de ministerios (`oficial_gobierno`) se reutilizan pero
  **nunca evitan la búsqueda** (para un orador del partido del Gobierno son neutrales).
  Caducan a 30 días salvo periodos cerrados. Los veredictos guardan los `web_fact_ids`.
- Concurrencia: `PIPELINE_CONCURRENCY` afirmaciones a la vez en evidencia y veredicto.

### Datos territoriales (fase H, 2026-10-14)

- **Territorios** (`data/knowledge/territorios.json`, de la API del INE con
  `scripts/recopilar_territorios.py`; `conocimiento cargar-territorios`): 19 comunidades y
  ciudades autónomas (código ISO `ES-AN`) y 52 provincias (`ES-P28`). Alias: nombre oficial
  en cada lengua cooficial y forma castellana habitual, igual para todos (revisado por
  `neutrality-auditor`); nada de denominaciones políticas. Un nombre ambiguo ("Madrid",
  "Valencia": comunidad, provincia y municipio) no se resuelve.
- **Series por comunidad y provincia** (catálogo, campo `territorial`, con rango y salto
  propios del nivel): paro EPA y población (ECP) por comunidad; población del Padrón por
  provincia. Solo se cargan con `conocimiento ingerir --territorial` (interruptor); antes,
  `conocimiento espacio` estima el tamaño (hoy ~25 MB de 500). La ingesta avisa si falta
  algún territorio.
- **Municipios** en Parquet local (`data/knowledge/municipios/padron_municipal.parquet`,
  `scripts/recopilar_padron_municipal.py`, CSV oficiales del INE): población del Padrón de
  ~8.100 municipios; un municipio solo se reconoce si su nombre es único o se da la
  provincia, y nunca si coincide con una comunidad o provincia.
- **En el análisis**: la detección marca `territory`; la evidencia sale de la serie de ese
  territorio (nunca de la nacional) o del Parquet municipal.

### Cobertura por partido y tema (fase G, 2026-10-14)

- Cada veredicto guardado lleva orador, partido, tema y `con_datos` (si se apoyó en una
  serie local o en la API). El tema sale del indicador o, si no hay, del tema con más
  palabras clave de `pipeline/knowledge/temas.yaml` (términos jurídicos o técnicos, no de
  encuadre; revisado por `neutrality-auditor`; cambiarlo exige nueva auditoría).
- `uv run conocimiento cobertura [--desde]` compara partidos (con al menos 10 veredictos)
  en los dos sentidos y señala temas frecuentes sin indicadores. Las alertas describen,
  no valoran, y recuerdan que la diferencia puede deberse al tipo de afirmaciones.

### Programas electorales (decidido 2026-10-20)

Para responder a afirmaciones sobre lo que un partido llevaba en su programa sin buscar en
la web (`pipeline/knowledge/programs.py`; `ref.programs` y `ref.program_chunks`, migración
0010).
- **Qué entra** (`data/knowledge/programas.yaml`): elecciones generales de 2016, abril y
  noviembre de 2019 y 2023; las candidaturas cuyos diputados formaron **grupo
  parlamentario propio** en la legislatura siguiente (no el Mixto ni el Plural). Misma
  regla para todas las elecciones. La lista de grupos sale de fuentes secundarias:
  contrastarla con el BOCG (`data-verifier`) antes de aprobar.
- **De dónde:** solo del **dominio oficial del partido** o de su copia en archive.org
  (el código rechaza cualquier otra URL); `scripts/recopilar_programas.py --buscar`
  propone PDFs archivados de ese dominio en los 75 días anteriores a la votación. Nunca
  copias de medios o agregadores.
- **Qué se guarda:** URL, SHA-256 y páginas del PDF, y el texto en fragmentos que no
  cruzan páginas, con búsqueda de texto completo en español (`tsvector`). **No se guarda
  el PDF** (decisión del responsable). El texto es inmutable; otro PDF es otro programa.
- **Revisión:** entran `pendiente` (un partido no es fuente oficial primaria) y solo se
  usan los `aprobado`: `conocimiento revisar --tipo programa` (que sea el programa
  completo de esa elección, no un resumen).
- **Uso (fase J2, por hacer):** un programa es una **postura, no evidencia de hechos**.
  Solo sirve para afirmaciones sobre lo que dice un programa y para indicar si una
  promesa figura en el del partido del orador.
- Pendiente de decidir: programas propios de candidaturas confederadas dentro de un
  grupo (En Comú Podem, En Marea…) y de federaciones (PSC).

### Políticos (decidido 2026-10-14)

Base de quién es quién para identificar oradores sin gastar búsquedas
(`pipeline/politicians.py`; en Supabase, `ref.politicians`, `ref.politician_roles`,
`ref.politician_parties`, migración 0005).
- **Selección por cargo, nunca por partido** (`data/knowledge/politicians_seed.yaml`):
  Gobierno; miembros del Gobierno salidos en la legislatura; expresidentes del Gobierno;
  presidencias autonómicas y de Ceuta y Melilla; presidencias del Congreso y del Senado;
  portavoces de los grupos del Congreso; dirección nacional de los partidos con
  representación; alcaldías de las cinco mayores ciudades. Si no se puede confirmar quién
  ocupa un cargo, no se incluye (se anota en la semilla).
- **Por persona:** nombre oficial completo, alias, fecha de nacimiento, partidos y cargos
  con fechas (inicio, fin y tramos **en funciones**), cada dato con su fuente:
  - cargos del **BOE** (primaria): nombramientos y ceses, también los decretos colectivos
    del Gobierno; el fin es el cese o el siguiente nombramiento para el mismo cargo. El
    "cese de los miembros del Gobierno" tras unas elecciones **no** termina el cargo: abre
    el tramo en funciones (de la tabla de gobiernos);
  - nacimiento, partidos y cargos que el BOE no publica, de **Wikidata** marcados como
    fuente **secundaria** (Wikipedia/Wikidata solo como secundaria: decisión del
    responsable, 2026-10-14). Solo fechas con precisión de día. Wikidata puede estar
    desactualizada: los cargos actuales se comprueban en el BOE o en una página oficial;
  - un dato visto en una investigación lleva `constatado_en` (no periodo) y vale ±365 días.
- **Regenerar:** `uv run python scripts/recopilar_politicos.py` (BOE y Wikidata sin clave,
  con pausa y caché en `data/cache/collect/`), revisar el diff de
  `data/knowledge/politicians.json` y `politicians_avisos.txt`, `uv run conocimiento
  cargar-politicos` y aprobar con `uv run conocimiento revisar --tipo politico` (antes,
  `data-verifier`). Lo curado entra `pendiente` y **no se usa hasta aprobarlo**.
  Actualizar tras las elecciones del 29-11-2026.
- **Uso en el análisis:** `speaker_research` busca primero en la base (todas las palabras
  del nombre en el nombre o un alias; ante ambigüedad, nada) el partido y los cargos en la
  fecha del discurso; solo si no los sabe, investiga en la web.
- **Oradores nuevos:** lo que `speaker_research` averigua se guarda en
  `data/cache/politicians.json` y en Supabase como `automatico`: **se reutiliza enseguida**
  (decisión del responsable, 2026-10-14), aparece en `revisar` y, si se rechaza, deja de
  usarse y no se vuelve a guardar. Solo se usan `aprobado` y `automatico`.

## Fuentes de evidencia (cascada, por orden de prioridad)

1. Google Fact Check Tools API (ClaimReview): Maldita, Newtral, EFE Verifica, Verificat…
   → fuentes de tipo `factcheck`.
2. Fuentes oficiales: búsqueda (Tavily) restringida a la lista editable de dominios
   oficiales (INE, Eurostat, SEPE, Banco de España, BOE, Hacienda, Seguridad Social,
   AIReF, CIS, Congreso, OCDE…) → tipo `oficial`.
3. Búsqueda web general, solo si lo oficial no da resultados relevantes → tipo `web`
   (un resultado de dominio oficial se marca `oficial`).

Son **oficiales** también los documentos de la UE, de organismos internacionales y de las
administraciones de **cualquier país** (decisión del responsable del proyecto,
2026-10-13): `fuentes_oficiales.toml` tiene `[dominios]` (también filtran la búsqueda
oficial) y `[sufijos]` (`.int`, `.gov`, `gov.uk`, `gouv.fr`, `bund.de`…; solo para
reconocer resultados como oficiales, no se envían como filtro).

El informe indica en qué se apoya cada veredicto (`basis`: desmentido, oficial o solo web);
un veredicto apoyado solo en web tiene confianza máxima 0,6. Límites por afirmación: hasta
3 búsquedas oficiales y 1 web, más hasta 3 oficiales de la ronda adicional cuando hace falta;
**como máximo 2 búsquedas en la web general** contando la ronda adicional. **Si la Fact Check
API devuelve una verificación de la misma afirmación** (similitud ≥ 80 % y mismas cifras;
`settled_by_factcheck`), no se busca en Tavily para ella, ni en la ronda adicional
(decisiones del responsable del proyecto, 2026-10-13).

**Caché de búsquedas** (`pipeline/evidence/search_cache.py`): toda búsqueda de Tavily pasa
por `data/cache/search/` (clave: hash de la consulta en minúsculas, sin tildes ni palabras
vacías, con los tokens ordenados, más los parámetros; las entradas de la clave anterior se
migran; solo respuestas correctas). Caduca a los `TAVILY_CACHE_DAYS` (30), salvo las
búsquedas de **periodos cerrados** (su año más reciente ≤ año actual − 2), que no caducan.
Las afirmaciones casi idénticas del mismo orador (≥ 90 %, mismas cifras, actor y periodo)
se verifican una sola vez y comparten veredicto. `--no-cache` (CLI
y evaluación) no la lee pero la actualiza. Cada paso registra en el manifest las búsquedas
reales, las servidas desde caché y los créditos (`provider="tavily"`, precio por crédito en
`pricing.toml`) y los aciertos de la base de indicadores (`provider="indicadores"`), y se
muestran al terminar cada ejecución (total y por paso), en el informe y en la web.
De cada página se pide el texto completo y se envían a Claude los pasajes más relevantes
para la afirmación (≈3.500 caracteres por fuente), no solo el principio de la página. Si
con la evidencia no puede decidir pero sabe qué dato lo resolvería, Claude pide **una
ronda de búsqueda adicional** (hasta 3 consultas en fuentes oficiales y web) y decide con
todo; esas consultas quedan en `follow_up_queries`. Resultados de Tavily con relevancia < 0,4 se descartan (umbral provisional, ajustar con la
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
- Un paso con fallos puntuales de IA (límite de uso, red) termina como **incompleto**
  (`partial`): se conserva y se informa, pero se repite en la siguiente ejecución en vez de
  reutilizarse. No aplica a la evidencia (sus fallos pueden ser permanentes y gastarían
  créditos en cada ejecución).

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
TAVILY_SEARCH_DEPTH=basic       # basic (1 crédito) | advanced (2)
TAVILY_MAX_RESULTS=5            # 1–20
TAVILY_CACHE_DAYS=30            # días que se reutiliza una búsqueda guardada
PIPELINE_CONCURRENCY=4          # afirmaciones a la vez en evidencia y veredicto (1 = en serie)
VERDICT_THINKING=               # opcional: true/false; sin definir = defecto del modelo
VERDICT_EFFORT=                 # low | medium | high | xhigh | max
VERDICT_THINKING_BUDGET=        # tokens; solo modelos hasta Claude 4.5
SMTP_HOST= / SMTP_PORT=587 / SMTP_USER= / SMTP_PASSWORD= / EMAIL_FROM=   # informes por correo
ALERT_EMAIL_TO=                 # avisos de la ingesta diaria
SUPABASE_DB_URL=                # base de conocimiento (pooler de sesión, IPv4)
```

## Comandos

```
uv sync                         # instalar dependencias
uv run verificar data/x.mp4 --fecha 2026-11-20               # vídeo
uv run verificar data/articulo.txt --fecha 2026-11-20         # texto
uv run verificar data/x.mp4 --fecha 2026-11-20 --from-step evidence  # reanudar
uv run verificar data/x.mp4 --fecha 2026-11-20 --no-cache  # sin reutilizar búsquedas
uv run verificar-web            # web local de pruebas en http://localhost:8000
uv run pytest                   # tests
uv run python eval/run_eval.py                   # evaluación completa contra el gold set
uv run python eval/run_eval.py --modo veredicto  # solo veredicto, desde las citas gold
uv run python eval/run_eval.py --rapida          # 5 casos propensos a error (~mitad de búsquedas)
uv run python scripts/actualizar_indicadores.py  # refresca INE / Eurostat / Seguridad Social
uv run conocimiento migrar [--simular]          # aplica db/migrations/ en Supabase
uv run conocimiento catalogo                     # sincroniza catalog.yaml
uv run conocimiento cargar-json                  # carga validada de la base JSON
uv run conocimiento revisar [--lista] [--indicador X] [--tipo gobierno]  # aprobar/rechazar
uv run conocimiento impacto <observation_id>     # veredictos que usaron un dato
uv run conocimiento retirar politico <id>        # rechaza algo ya aprobado (deja de usarse)
uv run conocimiento ingerir [--simular] [--avisar] # ingesta INE/Eurostat/Seg. Social
uv run conocimiento cobertura [--desde AAAA-MM-DD] # cobertura por partido y tema
uv run conocimiento espacio                      # tamaño de la base y estimación territorial
uv run conocimiento ingerir --territorial        # también series por comunidad y provincia
uv run conocimiento aprobar-oficiales            # aprueba lo pendiente de fuente oficial primaria
uv run conocimiento cargar-politicos [--actualizar]  # carga politicians.json (pendiente)
uv run python scripts/recopilar_politicos.py    # regenera politicians.json (BOE + Wikidata)
uv run python scripts/recopilar_programas.py --buscar  # propone URLs de programas (archive.org)
uv run python scripts/recopilar_programas.py    # descarga y extrae los programas con URL
uv run conocimiento cargar-programas             # carga lo recopilado (pendiente)
uv run conocimiento buscar-programa "texto" [--partido pp] [--eleccion 2023-07-23]
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
  APIs siempre simuladas en tests. `tests/conftest.py` lo impone: sin claves ni `.env`, y
  cualquier conexión externa sin mock o `claude -p` hace fallar el test (`RealNetworkInTest`).

## Harness de Claude Code (`.claude/`)

- **Hooks** (`settings.json`): bloquean el acceso a `.env`; pasan `ruff` a cada `.py`
  editado; revisan la neutralidad de `prompts/*.md` y recuerdan ejecutar la evaluación.
- **Agentes** (bajo demanda): `refactor-cleaner` (código muerto),
  `silent-failure-hunter` (errores silenciados; tras los pasos de evidencia y veredicto),
  `type-design-analyzer` (modelos pydantic). Adaptados de Everything Claude Code (MIT).

## Agentes y herramientas

Cuándo usar cada agente (los del proyecto están en `.claude/agents/`; los de ECC, en
`~/.claude/agents/`):

| Agente | Cuándo |
|---|---|
| `data-verifier` | **Antes de aprobar datos curados a mano** (SMI, hechos institucionales, gobiernos): contrasta cada dato pendiente con su fuente primaria. No aprueba nada. |
| `neutrality-auditor` | **Tras cambiar cualquier prompt de `prompts/`** y antes de cada publicación. |
| `verdict-reviewer` | **Tras cada análisis completo**: revisa cada veredicto `falso` antes de publicarlo. |
| `database-reviewer` (ECC) | **En cada migración** de base de datos (`db/migrations/`). |
| `planner`, `architect` (ECC) | Planes de cambios grandes (usan Opus: más caros). |
| `python-reviewer`, `security-reviewer` (ECC) | Revisión de código Python y de seguridad. |
| `refactor-cleaner`, `silent-failure-hunter`, `type-design-analyzer` | Ver "Harness de Claude Code". |

Skills de ECC instaladas en `~/.claude/skills/` (solo estas): `postgres-patterns`,
`database-migrations`, `tdd-workflow`, `python-testing`, `verification-loop`. La skill
`tdd` del proyecto sigue siendo la de referencia para los tests.

- **MCP de Supabase** (`.mcp.json`): proyecto de **desarrollo**, en modo **solo lectura**
  (`read_only=true`) salvo que el responsable del proyecto diga lo contrario. El token se
  lee de la variable `CHECKEA_SUPABASE_TOKEN` del entorno; nunca se escribe en archivos ni
  se pide por el chat.
- **gitleaks** (`.pre-commit-config.yaml`): bloquea commits con claves. En un clon nuevo:
  `pre-commit install`. Para revisar todo el historial: `gitleaks git --redact .`
