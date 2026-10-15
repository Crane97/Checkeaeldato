-- 0001 · Esquema de la base de conocimiento oficial.
--
-- ref   : datos de referencia (indicadores, territorios, observaciones, fuentes, hechos,
--         gobiernos). Solo datos aprobados se usan para verificar.
-- evid  : hechos extraídos de búsquedas web, reutilizables por claves exactas.
-- verif : veredictos emitidos y los datos de los que dependen (trazabilidad).
-- ops   : ingesta (ejecuciones, estado de cada recurso remoto) y migraciones.
--
-- Estos esquemas no se exponen por la API de Supabase (PostgREST solo publica public):
-- además se retiran los permisos a anon y authenticated.

create schema if not exists ref;
create schema if not exists evid;
create schema if not exists verif;
create schema if not exists ops;

revoke all on schema ref, evid, verif, ops from public, anon, authenticated;

-- ── ops ──────────────────────────────────────────────────────────────────────

create table ops.ingest_runs (
    id            bigint generated always as identity primary key,
    conector      text        not null,
    inicio        timestamptz not null default now(),
    fin           timestamptz,
    estado        text        not null default 'en_curso'
                  check (estado in ('en_curso', 'ok', 'error', 'simulacion')),
    filas_nuevas  integer     not null default 0 check (filas_nuevas >= 0),
    revisiones    integer     not null default 0 check (revisiones >= 0),
    cuarentena    integer     not null default 0 check (cuarentena >= 0),
    errores       jsonb       not null default '[]'::jsonb,
    check (fin is null or fin >= inicio),
    check ((estado = 'en_curso') = (fin is null))
);

create table ops.source_state (
    conector                    text        not null,
    recurso_id                  text        not null,
    ultima_modificacion_remota  timestamptz,
    hash_respuesta              text,
    ultima_comprobacion         timestamptz not null default now(),
    primary key (conector, recurso_id)
);

-- ── ref: catálogo ────────────────────────────────────────────────────────────

create table ref.sources (
    id        bigint generated always as identity primary key,
    organismo text not null,
    titulo    text not null,
    url       text not null check (url ~ '^https?://'),
    fecha     date,
    tipo      text not null check (tipo in ('primaria', 'secundaria')),
    unique (organismo, titulo, url)
);

create table ref.indicators (
    id                   text primary key check (id ~ '^[a-z0-9_]+$'),
    nombre               text    not null,
    definicion           text    not null,
    unidad               text    not null,
    frecuencia           text    not null
                         check (frecuencia in ('mensual', 'trimestral', 'anual', 'puntual', 'irregular')),
    organismo            text    not null,
    notas_metodologicas  text    not null default '',
    sinonimos            text[]  not null default '{}',
    rango_min            numeric,
    rango_max            numeric,
    salto_max_pct        numeric check (salto_max_pct > 0),
    max_antiguedad_dias  integer check (max_antiguedad_dias > 0),
    tema                 text    not null check (tema in (
                             'economia', 'empleo', 'pensiones', 'vivienda', 'sanidad', 'educacion',
                             'inmigracion', 'medio_ambiente', 'energia', 'seguridad', 'fiscalidad',
                             'instituciones', 'otros')),
    check (rango_min is null or rango_max is null or rango_min <= rango_max)
);

create table ref.territories (
    code         text primary key,                -- código INE ('ES' para el país)
    nivel        text not null check (nivel in ('pais', 'ccaa', 'provincia', 'municipio')),
    nombre       text not null,
    parent_code  text references ref.territories (code),
    check ((nivel = 'pais') = (parent_code is null))
);

insert into ref.territories (code, nivel, nombre) values ('ES', 'pais', 'España');

-- ── ref: observaciones (inmutables; ver 0002) ────────────────────────────────

create table ref.observations (
    id                          bigint generated always as identity primary key,
    indicator_id                text        not null references ref.indicators (id),
    periodo                     text        not null,  -- etiqueta original: 2013T1, 2022-07, 2018
    periodo_inicio              date        not null,
    periodo_fin                 date        not null,
    territorio_code             text        not null default 'ES' references ref.territories (code),
    valor                       numeric     not null check (valor <> 'NaN'),
    unidad                      text        not null,
    fecha_publicacion           date        not null,
    fecha_publicacion_estimada  boolean     not null default false,
    version                     integer     not null default 1 check (version >= 1),
    vigente                     boolean     not null default true,
    estado                      text        not null
                                check (estado in ('pendiente', 'cuarentena', 'aprobado', 'rechazado')),
    source_id                   bigint      not null references ref.sources (id),
    ingest_run_id               bigint      references ops.ingest_runs (id),
    raw                         jsonb,
    creado_en                   timestamptz not null default now(),
    check (periodo_fin >= periodo_inicio),
    unique (indicator_id, periodo, territorio_code, version)
);

-- A lo sumo una versión vigente por dato.
create unique index observations_vigente_uq
    on ref.observations (indicator_id, periodo, territorio_code) where vigente;

-- Resolvedor: versiones aprobadas de un indicador y territorio que solapan un intervalo; se
-- elige por periodo la más reciente publicada hasta la fecha del discurso (no se filtra por
-- vigente: si la última versión está en cuarentena, la anterior sigue siendo la válida).
create index observations_resolver_aprobado_idx
    on ref.observations (indicator_id, territorio_code, periodo_inicio)
    include (periodo_fin, periodo, fecha_publicacion, version, valor)
    where estado = 'aprobado';
create index observations_source_idx on ref.observations (source_id);
create index observations_ingest_run_idx on ref.observations (ingest_run_id);

-- ── ref: hechos institucionales y gobiernos ──────────────────────────────────

create table ref.facts (
    id            bigint generated always as identity primary key,
    tema          text   not null,
    enunciado     text   not null,
    fecha         date,
    source_id     bigint not null references ref.sources (id),
    estado        text   not null default 'pendiente'
                  check (estado in ('pendiente', 'aprobado', 'rechazado')),
    revisado_por  text,
    revisado_en   timestamptz,
    check ((estado = 'pendiente') = (revisado_en is null)),
    check (estado = 'pendiente' or revisado_por is not null)
);
create index facts_source_idx on ref.facts (source_id);

create table ref.parties (
    id       text primary key,
    nombre   text   not null,
    aliases  text[] not null default '{}'
);

create table ref.governments (
    id           text primary key,
    nivel        text not null check (nivel in ('nacional', 'autonomico', 'municipal')),
    territorio   text not null,
    presidente   text not null,
    partidos     text[] not null,
    inicio       date not null,
    fin          date,
    nombramiento text,
    notas        text,
    check (fin is null or fin > inicio)
);

-- Tramos con socios de coalición (p. ej. Unidas Podemos desde el 13/01/2020).
create table ref.government_partners (
    government_id  text not null references ref.governments (id),
    partido        text not null,
    inicio         date not null,
    fin            date,
    primary key (government_id, partido, inicio),
    check (fin is null or fin > inicio)
);

create table ref.government_caretaker (
    government_id  text not null references ref.governments (id),
    inicio         date not null,
    fin            date,
    primary key (government_id, inicio),
    check (fin is null or fin > inicio)
);

-- ── evid: hechos extraídos de la web ─────────────────────────────────────────

create table evid.web_facts (
    id                  bigint generated always as identity primary key,
    enunciado           text        not null,
    -- Claves exactas de reutilización: {"indicador", "territorio", "periodo"}.
    -- Exactamente estas tres claves, todas de texto: la igualdad de jsonb distingue 2013 de
    -- "2013", así que el tipo se fija aquí y se compara con `claves = $1::jsonb`. Se usa
    -- `is not distinct from` porque un CHECK con resultado NULL (clave ausente) se cumple.
    claves              jsonb       not null check (
                            (claves - 'indicador' - 'territorio' - 'periodo') = '{}'::jsonb
                            and jsonb_typeof(claves -> 'indicador') is not distinct from 'string'
                            and jsonb_typeof(claves -> 'territorio') is not distinct from 'string'
                            and jsonb_typeof(claves -> 'periodo') is not distinct from 'string'),
    valor               numeric,
    unidad              text,
    source_url          text        not null check (source_url ~ '^https?://'),
    organismo           text,
    fecha_fuente        date,
    tipo_fuente         text        not null
                        check (tipo_fuente in ('oficial_extranjera', 'medio', 'verificador', 'otro')),
    extraido_de_query   text,
    fecha_extraccion    timestamptz not null default now(),
    caduca_en           timestamptz,           -- null: dato histórico, no caduca
    revisado            boolean     not null default false
);
-- Reutilización solo por igualdad exacta de claves (hash: sin límite de tamaño del btree).
create index web_facts_claves_idx on evid.web_facts using hash (claves);

-- ── verif: veredictos y su trazabilidad ──────────────────────────────────────

create table verif.claims (
    id                  bigint generated always as identity primary key,
    job_id              text,
    claim_id            text,
    claim_normalizado   text        not null,
    claves_exactas      jsonb,
    veredicto           text        not null check (veredicto in (
                            'verdadero', 'verdad_a_medias', 'confusion', 'falso', 'no_verificable')),
    evidencia           jsonb       not null,
    observation_ids     bigint[]    not null default '{}',
    web_fact_ids        bigint[]    not null default '{}',
    fecha_verificacion  timestamptz not null default now(),
    modelo              text,
    version_prompts     jsonb
);
-- `conocimiento impacto <observation_id>`: veredictos que dependen de un dato.
-- Consultar con `observation_ids @> %s::bigint[]` (el GIN no sirve para `= any(...)`).
create index claims_observation_ids_idx on verif.claims using gin (observation_ids);
create index claims_job_idx on verif.claims (job_id);
create index claims_web_fact_ids_idx on verif.claims using gin (web_fact_ids);

-- ── Identidad inmutable de fuentes e indicadores ─────────────────────────────
-- Las observaciones (inmutables) apuntan aquí: cambiar la URL de una fuente o la unidad de
-- un indicador alteraría su procedencia hacia atrás. Para eso se crea una fuente nueva.

create function ref.forbid_source_change() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if (new.organismo, new.titulo, new.url, new.tipo)
       is distinct from (old.organismo, old.titulo, old.url, old.tipo) then
        raise exception 'ref.sources: no se cambia la identidad de una fuente; crea una nueva'
            using errcode = 'restrict_violation';
    end if;
    return new;
end;
$$;

create function ref.forbid_indicator_change() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if (new.unidad, new.definicion, new.frecuencia)
       is distinct from (old.unidad, old.definicion, old.frecuencia) then
        raise exception 'ref.indicators: unidad, definición y frecuencia no cambian; crea otro indicador'
            using errcode = 'restrict_violation';
    end if;
    return new;
end;
$$;

create trigger sources_identity before update on ref.sources
    for each row execute function ref.forbid_source_change();
create trigger indicators_identity before update on ref.indicators
    for each row execute function ref.forbid_indicator_change();
