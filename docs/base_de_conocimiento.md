# Base de conocimiento oficial: hoja de ruta y estado

Última actualización: 2026-10-20. Las reglas detalladas de cada parte están en
`CLAUDE.md` (secciones «Base de conocimiento oficial en Supabase», «Ingesta automática»,
«El análisis con la base de conocimiento», «Políticos», «Cobertura» y «Programas
electorales»). Este documento
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
| **D** | Ingesta automática INE / Eurostat / Seguridad Social (`conocimiento ingerir`), huella para no recargar, alertas por correo, rol `ingesta` con mínimos privilegios, GitHub Actions diario | Hecha (0006); la Action carga desde el 2026-10-19 |
| **E** | El análisis lee la base de Supabase (aprobado, a la fecha del discurso), cálculo del sistema en cada extracto, veredictos guardados en `verif.claims` | Hecha |
| **F1** | Consulta a la API oficial bajo demanda cuando la base no cubre el periodo | Hecha |
| **F2** | Datos extraídos de la web reutilizables por claves exactas (`evid.web_facts`), con cita literal comprobada; los del Gobierno no evitan la búsqueda | Hecha (0008) |
| **G** | Cobertura por partido y tema (`conocimiento cobertura`), temas en `temas.yaml` revisados por neutralidad | Hecha (0007) |
| **I** | Concurrencia en evidencia/veredicto (`PIPELINE_CONCURRENCY`), inserciones por lotes (carga inicial de 105 s a 10,6 s), tiempo por paso en el manifest y el resumen | Hecha |
| **H** | Territorios del INE (19 comunidades, 52 provincias), paro EPA y población por comunidad, Padrón por provincia (interruptor `--territorial`), Padrón de ~8.100 municipios en Parquet, detección del territorio en el análisis | Hecha (0009); base en ~25 MB |
| **J1** | Programas electorales de las generales de 2016, abril y noviembre de 2019 y 2023 (candidaturas con grupo parlamentario propio, 29 programas): lista en `data/knowledge/programas.yaml`, recopilador desde el dominio oficial o su copia en archive.org (`scripts/recopilar_programas.py`), texto por página con búsqueda en español (`conocimiento buscar-programa`), revisión (`revisar --tipo programa`) | Código hecho (0010); faltan las URLs y la revisión, ver «Pendiente» |
| **J2** | Uso de los programas en el análisis: solo para afirmaciones sobre lo que dice un programa y para indicar si una promesa figura en el del partido; nunca como evidencia de hechos | Por hacer |

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
2. **Programas electorales (J1):** fijar las URLs en `programas.yaml` con
   `scripts/recopilar_programas.py --buscar` (solo dominios oficiales), recopilar,
   `conocimiento migrar` (0010), `cargar-programas` y revisar. Antes, contrastar con el
   BOCG la lista de grupos de cada legislatura (`data-verifier`): sale de prensa y
   Wikipedia. Decidir si entran los programas propios de candidaturas confederadas (En
   Comú Podem, En Marea…) y federaciones (PSC).
3. ~~Activar la ingesta en GitHub~~: hecho el 2026-10-19 (secretos en el entorno
   `Production`, rol `ingesta` con contraseña, sin `--simular`).
4. Las fases A–I están hechas. Posibles ampliaciones: más indicadores
   territoriales (paro registrado por comunidad, IPC por comunidad), municipios en más
   indicadores.
5. La afirmación de impuestos de `real-005` salió `no_verificable` (se esperaba `falso`)
   en la última evaluación: la búsqueda no trajo las rebajas fiscales oficiales.
6. Mejoras anotadas: pensión media sin conector (la serie del INSS se carga a mano);
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
uv run conocimiento espacio
uv run conocimiento cargar-territorios
uv run python scripts/recopilar_programas.py [--buscar] [--solo ID]
uv run conocimiento cargar-programas
uv run conocimiento buscar-programa "texto" [--partido X] [--eleccion AAAA-MM-DD] [--pendientes]
uv run conocimiento aprobar-oficiales
uv run python scripts/recopilar_territorios.py
uv run python scripts/recopilar_padron_municipal.py
uv run conocimiento cargar-politicos [--actualizar]
uv run python scripts/recopilar_politicos.py # regenera politicians.json
```
