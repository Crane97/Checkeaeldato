# Checkea el Dato — verificador de discursos políticos (prototipo CLI)

Analiza un vídeo, un texto o una **emisión en directo** en español, detecta las afirmaciones
verificables y, para cada una, busca evidencia (desmentidos publicados, fuentes oficiales y,
como último recurso, la web) y decide si es **verdadera**, **verdad a medias**, una
**confusión**, **falsa** o **no verificable**, siempre con fuentes. Las promesas concretas se
valoran aparte por su viabilidad.

Las reglas del proyecto (neutralidad, categorías, principios) están en
[`CLAUDE.md`](CLAUDE.md).

## Puesta en marcha

1. Instala las dependencias: `uv sync`
2. Copia la plantilla de configuración: `cp .env.example .env` y rellena las claves:

   | Variable | Para qué | Dónde conseguirla |
   |---|---|---|
   | `LLM_BACKEND` | `claude-code` usa tu suscripción de Claude Code (pruebas en local, sin pagar aparte); `api` usa la API de Anthropic (créditos prepago; necesaria para la beta) | — |
   | `ANTHROPIC_MODEL` | Modelo de Claude; para la demo, `claude-haiku-5-5` | — |
   | `ANTHROPIC_API_KEY` | Solo con `LLM_BACKEND=api` | console.anthropic.com → API Keys |
   | `GOOGLE_FACTCHECK_API_KEY` | Desmentidos publicados (gratis) | Google Cloud Console: crea una clave y habilita *Fact Check Tools API* |
   | `TAVILY_API_KEY` | Búsqueda en webs oficiales y en internet (gratis, 1.000 créditos/mes) | app.tavily.com |
   | `TRANSCRIPTION_API_KEY` | Solo para vídeo (ElevenLabs Scribe v2) | elevenlabs.io → API Keys |

   Si falta la clave de Google o de Tavily, esa fuente se desactiva y el informe lo avisa.

   Para recibir el informe por correo, rellena además `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`
   y `SMTP_PASSWORD` (ver `.env.example`). El correo trae un resumen y un adjunto
   `informe-….html` que se abre en el navegador y permite filtrar por orador y por tipo.
3. Para vídeo y directo hace falta además `ffmpeg` (`brew install ffmpeg` en macOS,
   `winget install Gyan.FFmpeg` en Windows).

## Uso

### Web local (la forma más cómoda de probar)

```bash
uv run verificar-web
```

Se abre `http://localhost:8000`: pega el texto, pon la fecha y (opcional) el orador y un
correo al que enviar el informe, y pulsa **Analizar**. Verás el progreso por pasos y el resultado con cada afirmación, su veredicto,
la explicación y las fuentes. Solo es accesible desde tu ordenador. Para pararla: Ctrl+C.

### En directo

En `http://localhost:8000/directo` pega la URL de una emisión (HLS `.m3u8`, RTMP…) o la ruta
de un vídeo grabado, que se reproduce a su ritmo real para ensayar. El audio se corta en
bloques de un minuto; cada bloque se transcribe y se analiza mientras llega el siguiente, y
el panel muestra en vivo los bloques, las afirmaciones en análisis, los veredictos con sus
fuentes, los totales por categoría y por orador, la transcripción y el coste. Las etiquetas
de cada bloque (`speaker_0@3`) se pueden nombrar sobre la marcha pulsando sobre ellas. La
sesión se guarda en `data/live/<sesión>/` y el informe se descarga como en el modo texto.

### Terminal

```bash
# Texto (.txt o .md): no necesita transcripción
uv run verificar data/articulo.txt --fecha 2026-11-20 --autor "Nombre"

# Vídeo
uv run verificar data/debate.mp4 --fecha 2026-11-20 --oradores "speaker_0=Nombre A,speaker_1=Nombre B"

# Reanudar o recalcular desde un paso (audio, transcript, claims, evidence, verdicts, report)
uv run verificar data/debate.mp4 --fecha 2026-11-20 --from-step evidence
```

Cada ejecución guarda sus pasos en `data/<job_id>/` (`transcript.json`, `claims.json`,
`evidence.json`, `verdicts.json`, `report.json`). Repetir el mismo comando no vuelve a
gastar: solo se recalcula lo que falta, lo que falló o lo que depende de un prompt
modificado. Cambiar solo los nombres de los oradores regenera el informe sin coste.

## Calidad y desarrollo

```bash
uv run pytest                                    # tests (sin llamadas reales a APIs)
uv run python eval/run_eval.py                   # evaluación contra eval/gold/
uv run ruff check . && uv run ruff format .
```

El gold set (`eval/gold/*.json`) tiene un caso sintético de ejemplo y casos reales
(`real_*.json`) basados en verificaciones publicadas; se amplía con cada regla nueva.
