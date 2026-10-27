-- 0011 · Informes en Supabase (fase 4): el informe, sus afirmaciones y promesas con
-- versiones de veredicto, sus archivos en Storage y la cola de análisis.
--
-- Estados del informe: en_analisis → borrador → publicado → retirado, y error. La base
-- impone las transiciones permitidas, las fechas y la nota de cada estado, que solo se
-- borran borradores y análisis fallidos y que una versión de veredicto no se modifica.
--
-- Cada afirmación o promesa tiene una o más versiones (la 1 es la del análisis; las demás,
-- re-evaluaciones o correcciones manuales) y exactamente una vigente: el original nunca se
-- pierde.

create schema if not exists app;

create table app.reports (
    id              text primary key check (id ~ '^[0-9a-f]{12}$'),         -- job_id
    slug            text not null unique
                    check (slug ~ '^\d{4}-\d{2}-\d{2}-[a-z0-9]+(-[a-z0-9]+)*$'),
    titulo          text not null check (btrim(titulo) <> ''),
    entradilla      text,
    tipo            text check (tipo in ('debate', 'mitin', 'entrevista', 'sesion_parlamentaria')),
    fecha_discurso  date not null,
    input_kind      text not null check (input_kind in ('video', 'texto')),
    fuente          text not null,                       -- nombre del archivo de entrada
    origen          text not null check (origen in ('servidor', 'local')),
    estado          text not null default 'en_analisis'
                    check (estado in ('en_analisis', 'borrador', 'publicado', 'retirado', 'error')),
    error           text,
    progreso        jsonb not null default '{}',         -- paso actual y pasos terminados
    -- Resto del modelo Report: oradores, perfiles, coste, tiempos por paso y avisos.
    meta            jsonb not null default '{}',
    creado_en       timestamptz not null default now(),
    actualizado_en  timestamptz not null default now(),
    analizado_en    timestamptz,
    publicado_en    timestamptz,
    retirado_en     timestamptz,
    nota_retirada   text,
    constraint reports_error_chk check (estado <> 'error' or nullif(btrim(error), '') is not null),
    constraint reports_publicado_chk
        check (estado not in ('publicado', 'retirado') or (publicado_en is not null and tipo is not null)),
    constraint reports_retirado_chk check (estado <> 'retirado'
        or (retirado_en is not null and nullif(btrim(nota_retirada), '') is not null)),
    constraint reports_analizado_chk
        check (estado not in ('borrador', 'publicado', 'retirado') or analizado_en is not null)
);
create index reports_estado_idx on app.reports (estado, fecha_discurso desc);

create table app.claim_versions (
    id          bigint generated always as identity primary key,
    report_id   text    not null references app.reports (id) on delete cascade,
    claim_id    text    not null,
    orden       integer not null check (orden >= 0),    -- posición en el informe
    kind        text    not null check (kind in ('afirmacion', 'promesa')),
    version     integer not null check (version >= 1),
    vigente     boolean not null,
    origen      text    not null check (origen in ('analisis', 'reevaluacion', 'manual')),
    etiqueta    text    not null,
    categoria   text,
    orador      text    not null,
    partido     text,
    confianza   numeric check (confianza between 0 and 1),
    contenido   jsonb   not null,                       -- Claim o Promise completo
    motivo      text,                                   -- de una re-evaluación o corrección
    creado_por  text,
    creado_en   timestamptz not null default now(),
    unique (report_id, claim_id, version),
    -- La categoría casa con el veredicto (Claim.category); coalesce: un NULL no pasa.
    constraint claim_versions_etiqueta_chk check (coalesce(case kind
        when 'afirmacion' then case etiqueta
            when 'verdadero' then categoria in ('verdadero', 'verdadero_sin_fuente_oficial')
            when 'falso' then categoria in ('falso_sin_fuente_oficial', 'falso_con_fuente_oficial')
            when 'verdad_a_medias' then categoria = etiqueta
            when 'confusion' then categoria = etiqueta
            when 'no_verificable' then categoria = etiqueta
            else false
        end
        else etiqueta in ('viable', 'dudosa', 'inviable', 'no_evaluable') and categoria is null
    end, false)),
    constraint claim_versions_motivo_chk
        check (origen = 'analisis' or nullif(btrim(motivo), '') is not null)
);
create unique index claim_versions_vigente_idx
    on app.claim_versions (report_id, claim_id) where vigente;
create index claim_versions_orador_idx on app.claim_versions (partido, orador) where vigente;

create table app.report_files (
    id         bigint generated always as identity primary key,
    report_id  text   not null references app.reports (id) on delete cascade,
    tipo       text   not null check (tipo ~ '^[a-z_]+$'),  -- informe_html, veredictos…
    ruta       text   not null unique,                      -- ruta en el bucket
    sha256     text   not null check (sha256 ~ '^[0-9a-f]{64}$'),
    bytes      bigint not null check (bytes >= 0),
    creado_en  timestamptz not null default now()
);
create index report_files_report_idx on app.report_files (report_id);

-- Cola persistente: sobrevive a reinicios; el worker toma trabajos con FOR UPDATE SKIP LOCKED.
create table app.analysis_queue (
    id            bigint generated always as identity primary key,
    report_id     text not null references app.reports (id) on delete cascade,
    estado        text not null default 'pendiente'
                  check (estado in ('pendiente', 'en_curso', 'hecho', 'error', 'interrumpido')),
    solicitud     jsonb not null,                     -- texto o ruta del vídeo, fecha, oradores
    worker        text,                               -- máquina, proceso e hilo
    intentos      integer not null default 0 check (intentos >= 0),
    creado_en     timestamptz not null default now(),
    empezado_en   timestamptz,
    -- El worker lo renueva mientras analiza: un trabajo `en_curso` sin latido reciente se
    -- da por interrumpido (proceso caído), sin tocar los de otro proceso que sigue vivo.
    latido_en     timestamptz,
    terminado_en  timestamptz,
    constraint analysis_queue_en_curso_chk
        check (estado <> 'en_curso' or (empezado_en is not null and latido_en is not null))
);
create index analysis_queue_pendiente_idx on app.analysis_queue (creado_en) where estado = 'pendiente';
create index analysis_queue_report_idx on app.analysis_queue (report_id);
-- Un análisis activo por informe: dos peticiones simultáneas del mismo texto no se duplican.
create unique index analysis_queue_activa_idx
    on app.analysis_queue (report_id) where estado in ('pendiente', 'en_curso');

-- ── Invariantes ──────────────────────────────────────────────────────────────

create or replace function app.touch_report() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    new.actualizado_en := now();
    return new;
end;
$$;
create trigger reports_touch before update on app.reports
    for each row execute function app.touch_report();

-- Transiciones permitidas (volver a analizar un borrador o un error lo pone en_analisis).
create or replace function app.guard_report_state() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if new.estado is distinct from old.estado and not (
           (old.estado = 'en_analisis' and new.estado in ('borrador', 'error'))
        or (old.estado = 'borrador'    and new.estado in ('en_analisis', 'publicado', 'error'))
        or (old.estado = 'error'       and new.estado in ('en_analisis', 'borrador'))
        or (old.estado = 'publicado'   and new.estado = 'retirado')
    ) then
        raise exception 'un informe % no pasa a %', old.estado, new.estado
            using errcode = 'check_violation';
    end if;
    if old.estado in ('publicado', 'retirado') and new.publicado_en is distinct from old.publicado_en then
        raise exception 'la fecha de publicación no cambia' using errcode = 'check_violation';
    end if;
    return new;
end;
$$;
create trigger reports_guard_state before update of estado, publicado_en on app.reports
    for each row execute function app.guard_report_state();

-- Solo se borran borradores y análisis fallidos (con todo lo suyo, en cascada).
create or replace function app.guard_report_delete() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if old.estado not in ('borrador', 'error') then
        raise exception 'solo se eliminan borradores o análisis con error (estado: %)', old.estado
            using errcode = 'check_violation';
    end if;
    return old;
end;
$$;
create trigger reports_guard_delete before delete on app.reports
    for each row execute function app.guard_report_delete();

-- Una versión no cambia: solo pasa a vigente o deja de serlo. Se borra únicamente en
-- cascada, cuando su informe ya no existe.
create or replace function app.guard_claim_version() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if tg_op = 'DELETE' then
        if exists (select 1 from app.reports r where r.id = old.report_id) then
            raise exception 'una versión de veredicto no se borra' using errcode = 'check_violation';
        end if;
        return old;
    end if;
    if (to_jsonb(new) - 'vigente') is distinct from (to_jsonb(old) - 'vigente') then
        raise exception 'una versión de veredicto no se modifica (crea una versión nueva)'
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;
create trigger claim_versions_guard before update or delete on app.claim_versions
    for each row execute function app.guard_claim_version();

-- TRUNCATE se saltaría las reglas de borrado e inmutabilidad (función de 0002).
create trigger reports_no_truncate before truncate on app.reports
    for each statement execute function ref.forbid_truncate();
create trigger claim_versions_no_truncate before truncate on app.claim_versions
    for each statement execute function ref.forbid_truncate();

-- Como en 0002: también con session_replication_role = replica (restauraciones).
alter table app.reports        enable always trigger reports_guard_delete;
alter table app.reports        enable always trigger reports_guard_state;
alter table app.reports        enable always trigger reports_no_truncate;
alter table app.claim_versions enable always trigger claim_versions_guard;
alter table app.claim_versions enable always trigger claim_versions_no_truncate;

-- Misma defensa que ref (0002): RLS sin políticas y sin privilegios para la API de Supabase
-- (el esquema app tampoco está entre los expuestos por PostgREST).
alter table app.reports        enable row level security;
alter table app.claim_versions enable row level security;
alter table app.report_files   enable row level security;
alter table app.analysis_queue enable row level security;
revoke all on all tables    in schema app from public, anon, authenticated;
revoke all on all sequences in schema app from public, anon, authenticated;
revoke all on all functions in schema app from public, anon, authenticated;
revoke all on schema app from public, anon, authenticated;
alter default privileges in schema app revoke all on tables    from public, anon, authenticated;
alter default privileges in schema app revoke all on sequences from public, anon, authenticated;
alter default privileges in schema app revoke all on functions from public, anon, authenticated;
-- EXECUTE a PUBLIC es un permiso global por defecto: `in schema` no lo quita. Afecta solo a
-- las funciones que cree en adelante el rol de las migraciones (los permisos explícitos de
-- Supabase a anon/authenticated en `public` siguen); una función que deba llamar el rol
-- `ingesta` necesitará su `grant execute`.
alter default privileges revoke execute on functions from public;
