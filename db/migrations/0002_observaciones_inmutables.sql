-- 0002 · Las observaciones nunca se modifican ni se borran: solo se versionan.
--
-- Una revisión del dato oficial crea una fila nueva (version + 1, su propia
-- fecha_publicacion) y marca la anterior vigente = false; primero el UPDATE y después el
-- INSERT (el índice único de vigentes no es diferible). Lo único que puede cambiar en una
-- fila existente es:
--   * vigente: true → false (al publicarse una revisión);
--   * estado:  pendiente | cuarentena → aprobado | rechazado (al revisarla una persona).
-- Todo lo demás, DELETE y TRUNCATE incluidos, se rechaza. Cada cambio permitido queda en
-- ref.observation_events, que solo admite inserciones y solo la escriben estos triggers.
--
-- Límite conocido: el propietario de las tablas (postgres en Supabase) puede desactivar o
-- borrar triggers; esto protege contra errores, no contra un uso malintencionado de ese rol.

create table ref.observation_events (
    id              bigint generated always as identity primary key,
    observation_id  bigint      not null references ref.observations (id),
    campo           text        not null check (campo in ('vigente', 'estado')),
    antes           text        not null,
    despues         text        not null,
    por             text        not null,            -- rol de la conexión (session_user)
    revisor         text,                            -- persona, si la revisión es manual
    en              timestamptz not null default now()
);
create index observation_events_observation_idx on ref.observation_events (observation_id);

-- ── INSERT: cadena de versiones coherente ───────────────────────────────────

create function ref.guard_observation_insert() returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
    prev record;
begin
    select version, fecha_publicacion into prev
      from ref.observations
     where indicator_id = new.indicator_id
       and periodo = new.periodo
       and territorio_code = new.territorio_code
     order by version desc
     limit 1;
    if prev is null then
        if new.version <> 1 then
            raise exception 'la primera versión de % % % debe ser la 1',
                new.indicator_id, new.periodo, new.territorio_code
                using errcode = 'restrict_violation';
        end if;
    else
        if new.version <> prev.version + 1 then
            raise exception 'versión esperada % para % %', prev.version + 1, new.indicator_id, new.periodo
                using errcode = 'restrict_violation';
        end if;
        if new.fecha_publicacion < prev.fecha_publicacion then
            raise exception 'la fecha de publicación no puede ser anterior a la de la versión previa'
                using errcode = 'restrict_violation';
        end if;
    end if;
    -- Solo la ingesta automática (con su ejecución registrada y validada) entra aprobada;
    -- lo curado a mano entra pendiente hasta que una persona lo revise.
    if new.estado = 'rechazado'
       or (new.estado = 'aprobado' and new.ingest_run_id is null) then
        raise exception 'estado inicial no permitido: % (los datos manuales entran como pendiente)',
            new.estado
            using errcode = 'restrict_violation';
    end if;
    return new;
end;
$$;

create trigger observations_guard_insert
    before insert on ref.observations
    for each row execute function ref.guard_observation_insert();

-- ── UPDATE y DELETE ──────────────────────────────────────────────────────────

create function ref.guard_observations() returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
    if tg_op = 'DELETE' then
        raise exception 'ref.observations es inmutable: no se puede borrar la observación %', old.id
            using errcode = 'restrict_violation';
    end if;

    -- Every column except vigente and estado must stay the same (also future columns).
    if (to_jsonb(new) - 'vigente' - 'estado') is distinct from (to_jsonb(old) - 'vigente' - 'estado') then
        raise exception 'ref.observations es inmutable: crea una versión nueva en lugar de modificar la %', old.id
            using errcode = 'restrict_violation';
    end if;

    if new.vigente is distinct from old.vigente then
        if not (old.vigente and not new.vigente) then
            raise exception 'vigente solo puede pasar de true a false (observación %)', old.id
                using errcode = 'restrict_violation';
        end if;
        insert into ref.observation_events (observation_id, campo, antes, despues, por, revisor)
        values (old.id, 'vigente', old.vigente::text, new.vigente::text, session_user,
                nullif(current_setting('app.revisor', true), ''));
    end if;

    if new.estado is distinct from old.estado then
        if not (old.estado in ('pendiente', 'cuarentena') and new.estado in ('aprobado', 'rechazado')) then
            raise exception 'transición de estado no permitida: % → % (observación %)',
                old.estado, new.estado, old.id
                using errcode = 'restrict_violation';
        end if;
        insert into ref.observation_events (observation_id, campo, antes, despues, por, revisor)
        values (old.id, 'estado', old.estado, new.estado, session_user,
                nullif(current_setting('app.revisor', true), ''));
    end if;

    return new;
end;
$$;

create trigger observations_guard
    before update or delete on ref.observations
    for each row execute function ref.guard_observations();

create function ref.forbid_truncate() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    raise exception '% es de solo inserción: TRUNCATE no está permitido', tg_table_name
        using errcode = 'restrict_violation';
end;
$$;

create trigger observations_no_truncate
    before truncate on ref.observations
    for each statement execute function ref.forbid_truncate();

-- The event log is append-only and only these triggers write it.
create function ref.forbid_change() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    raise exception '% es de solo inserción', tg_table_name
        using errcode = 'restrict_violation';
end;
$$;

create trigger observation_events_append_only
    before update or delete on ref.observation_events
    for each row execute function ref.forbid_change();

create trigger observation_events_no_truncate
    before truncate on ref.observation_events
    for each statement execute function ref.forbid_truncate();

-- Triggers that also fire with session_replication_role = replica and on logical replicas.
alter table ref.observations       enable always trigger observations_guard_insert;
alter table ref.observations       enable always trigger observations_guard;
alter table ref.observations       enable always trigger observations_no_truncate;
alter table ref.observation_events enable always trigger observation_events_append_only;
alter table ref.observation_events enable always trigger observation_events_no_truncate;

revoke execute on function ref.guard_observation_insert(), ref.guard_observations(),
    ref.forbid_truncate(), ref.forbid_change(), ref.forbid_source_change(),
    ref.forbid_indicator_change() from public;

-- ── Defensa en profundidad para Supabase ─────────────────────────────────────
-- Estos esquemas no están expuestos por la API, pero si alguien los expusiera o diera un
-- GRANT por error, RLS sin políticas deniega todo a anon y authenticated (el propietario,
-- que usa la pipeline, no se ve afectado).

do $$
declare
    t record;
begin
    for t in
        select schemaname, tablename from pg_tables
         where schemaname in ('ref', 'evid', 'verif', 'ops')
    loop
        execute format('alter table %I.%I enable row level security', t.schemaname, t.tablename);
    end loop;
end;
$$;

revoke all on all tables    in schema ref, evid, verif, ops from public, anon, authenticated;
revoke all on all sequences in schema ref, evid, verif, ops from public, anon, authenticated;
revoke all on all functions in schema ref, evid, verif, ops from public, anon, authenticated;
alter default privileges in schema ref, evid, verif, ops revoke all on tables    from public, anon, authenticated;
alter default privileges in schema ref, evid, verif, ops revoke all on sequences from public, anon, authenticated;
alter default privileges in schema ref, evid, verif, ops revoke all on functions from public, anon, authenticated;
