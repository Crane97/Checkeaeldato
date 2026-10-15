# Base de conocimiento oficial: hoja de ruta y estado

Última actualización: 2026-10-14. Las reglas detalladas de cada parte están en
`CLAUDE.md` (secciones «Base de conocimiento oficial en Supabase», «Ingesta automática»,
«El análisis con la base de conocimiento», «Políticos» y «Cobertura»). Este documento
dice **qué está hecho, qué falta y cómo seguir**.

## Para qué sirve

Que los veredictos sobre cifras oficiales (paro, IPC, SMI, deuda, pensiones…) se apoyen en
series descargadas de la fuente primaria, **revisadas por una persona**, **resueltas a la
fecha del discurso** y **trazables** (cada veredicto guarda qué datos usó), en lugar de
depender de una búsqueda web. Y saber quién es quién (políticos) sin gastar búsquedas.

## Fases

Orden acordado: A+B → C → D → E+F → G → I → H.

| Fase | Contenido | Estado |
|---|---|---|
| **A+B** | Esquema en Supabase (`ref`, `evid`, `verif`, `ops`), observaciones inmutables y versionadas, gobiernos y partidos revisables, migrador (`conocimiento migrar`), repositorio con resolución a fecha (`resolve_as_of`) | Hecha (migraciones 0001–0003) |
| **C** | Catálogo de indicadores (`pipeline/knowledge/catalog.yaml`), validación antes de cargar (rango, saltos, unidad → cuarentena), carga de las series JSON, revisión humana (`conocimiento revisar` / `retirar`), golden tests, trazabilidad (`conocimiento impacto`) | Hecha (0004) |
| **Políticos** | Base de 78 políticos elegidos por cargo (`data/knowledge/politicians_seed.yaml`), recopilador BOE + Wikidata + Junta de Portavoces (`scripts/recopilar_politicos.py`), uso en `speaker_research`, oradores nuevos guardados como `automatico` | Hecha (0005); revisión de fichas en curso, ver «Pendiente» |
| **D** | Ingesta automática INE / Eurostat / Seguridad Social (`conocimiento ingerir`), huella para no recargar, alertas por correo, rol `ingesta` con mínimos privilegios, GitHub Actions diario | Hecha (0006); la Action está en simulación |
| **E** | El análisis lee la base de Supabase (aprobado, a la fecha del discurso), cálculo del sistema en cada extracto, veredictos guardados en `verif.claims` | Hecha |
| **F1** | Consulta a la API oficial bajo demanda cuando la base no cubre el periodo | Hecha |
| **F2** | Hechos extraídos de la web reutilizables por claves exactas (`evid.web_facts`, tabla ya creada) | **Pendiente** |
| **G** | Cobertura por partido y tema (`conocimiento cobertura`), temas en `temas.yaml` revisados por neutralidad | Hecha (0007) |
| **I** | Concurrencia (async) en evidencia/veredicto, inserciones por lotes en la carga, métricas; evaluación antes y después (objetivo: 0 verdaderos marcados como falsos) | **Pendiente** |
| **H** | Datos por comunidad autónoma y provincia (interruptor), municipios en Parquet; antes, estimar espacio frente al plan Free (500 MB; hoy ~20 MB) | **Pendiente** |

## Pendiente, por orden

1. **Revisión humana (responsable del proyecto):**
   - Ya no hace falta aprobar series: los datos de fuentes oficiales primarias entran
     aprobados si pasan la validación (decisión del 2026-10-14; SMI, pensiones y
     pensionistas aprobados así con `conocimiento aprobar-oficiales`).
   - Quedan en cuarentena 3 valores reales con saltos anómalos (turistas de julio de
     2020; deuda en millones de 2009 y 2012): aprobarlos o no con `revisar`.
   - 66 fichas de políticos pendientes (`--tipo politico`). Garriga, Abascal, Junqueras y
     Rollán no se han podido verificar (las webs bloquean descargas).
   - 7 gobiernos y 5 partidos pendientes (`--tipo gobierno`, `--tipo partido`).
2. **Activar la ingesta en GitHub:** contraseña del rol `ingesta` en el SQL Editor
   (`alter role ingesta with login password '…'`), secretos del repositorio
   (`SUPABASE_DB_URL` con el pooler y usuario `ingesta.<ref>`, `SMTP_*`, `EMAIL_FROM`,
   `ALERT_EMAIL_TO`) y, cuando se decida, quitar `--simular` de
   `.github/workflows/ingesta.yml`.
3. **Fase I** (rendimiento): la carga inicial de la base JSON tarda ~2 min por insertar
   fila a fila; la evidencia y el veredicto van en serie.
4. **Fase F2** y **fase H**.
5. Mejoras anotadas: pensión media sin conector (la serie del INSS se carga a mano);
   ampliar el SMI a 1996; la pista «siete años» no acota el periodo del contexto; la
   tabla de gobiernos no tiene tramos en funciones antes de 2015 (los políticos los
   calculan del BOE); actualizar políticos y gobiernos tras las elecciones del
   29-11-2026.

## Cómo trabajar en esto

- Conexión: `SUPABASE_DB_URL` en `.env` (pooler de sesión, IPv4; la conexión directa es
  solo IPv6). Sin ella, el análisis usa la base JSON y los tests de base se omiten.
- Tests: `uv run pytest` (los marcados `supabase` corren en el proyecto de desarrollo
  dentro de una transacción que se deshace); `uv run pytest -m "not supabase"` sin red.
- Cada migración nueva en `db/migrations/NNNN_*.sql` pasa por el agente
  `database-reviewer`; cada cambio de prompts, por la evaluación (`eval/run_eval.py
  --rapida`) y `neutrality-auditor`; los datos curados, por `data-verifier` antes de
  aprobarlos.
- Nunca UPDATE ni DELETE sobre observaciones (versiones nuevas); nada pendiente o en
  cuarentena se usa para verificar.

## Comandos

```
uv run conocimiento migrar [--simular]       # aplica db/migrations/
uv run conocimiento catalogo                 # sincroniza catalog.yaml
uv run conocimiento ingerir [--simular] [--avisar] [--indicador X] [--forzar]
uv run conocimiento revisar [--lista] [--tipo T] [--indicador X]
uv run conocimiento retirar <tipo> <id>      # rechaza algo ya aprobado
uv run conocimiento impacto <observation_id> # veredictos que usaron un dato
uv run conocimiento cobertura [--desde AAAA-MM-DD]
uv run conocimiento cargar-politicos [--actualizar]
uv run python scripts/recopilar_politicos.py # regenera politicians.json
```
