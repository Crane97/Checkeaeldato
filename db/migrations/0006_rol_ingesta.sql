-- 0006 · Rol `ingesta`: lo mínimo para la ingesta diaria (`conocimiento ingerir`).
--
-- Puede insertar observaciones y fuentes, retirar la vigencia de una versión (los
-- triggers de 0002 siguen impidiendo cualquier otro cambio) y registrar sus ejecuciones.
-- No puede borrar, ni aprobar datos pendientes, ni leer el catálogo, gobiernos, políticos
-- o veredictos, ni tocar el esquema.
--
-- Se crea SIN contraseña y sin poder iniciar sesión. Para usarlo (p. ej. desde GitHub
-- Actions), el responsable ejecuta en el SQL Editor de Supabase, con una contraseña que
-- nunca se escribe en archivos del repo:
--     alter role ingesta with login password '…';
-- y usa el pooler en modo sesión (puerto 5432, sslmode=require) con el usuario
-- `ingesta.<ref del proyecto>`. La conexión directa es solo IPv6.

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'ingesta') then
        create role ingesta nologin noinherit;
    end if;
end;
$$;
-- Si ya existía con otros atributos, se normalizan (el login lo habilita el responsable).
alter role ingesta nologin noinherit connection limit 3;
-- Si la credencial se filtrara: pocas conexiones, consultas cortas, sin search_path (todo
-- el SQL del código usa nombres cualificados).
alter role ingesta set statement_timeout = '60s';
alter role ingesta set search_path = '';

-- El propietario puede adoptar el rol (`set role ingesta`) para probar sus permisos; no
-- hereda nada de él (PostgreSQL 16+: WITH INHERIT FALSE, SET TRUE).
do $$
begin
    execute format('grant ingesta to %I with inherit false, set true', current_user);
end;
$$;

grant usage on schema ref, ops to ingesta;

grant select, insert on ref.sources, ref.observations to ingesta;
-- `insert … on conflict do update set organismo = excluded.organismo` (para obtener el id)
-- necesita UPDATE sobre esa columna; forbid_source_change impide cambiar la identidad.
grant update (organismo) on ref.sources to ingesta;
-- Solo la columna vigente (true → false al llegar una revisión); guard_observations
-- rechaza cualquier otra transición.
grant update (vigente) on ref.observations to ingesta;

grant select on ops.ingest_runs, ops.source_state to ingesta;
grant insert (conector) on ops.ingest_runs to ingesta;
grant update (fin, estado, filas_nuevas, revisiones, cuarentena, errores) on ops.ingest_runs
    to ingesta;
grant insert (conector, recurso_id, hash_respuesta, ultima_modificacion_remota,
              ultima_comprobacion) on ops.source_state to ingesta;
grant update (hash_respuesta, ultima_modificacion_remota, ultima_comprobacion)
    on ops.source_state to ingesta;

-- RLS está activado sin políticas en todas las tablas (0002): para este rol se abren solo
-- las operaciones que usa (nunca DELETE), aunque un GRANT futuro diera más.
drop policy if exists ingesta_lee on ref.sources;
drop policy if exists ingesta_inserta on ref.sources;
drop policy if exists ingesta_actualiza on ref.sources;
create policy ingesta_lee on ref.sources for select to ingesta using (true);
create policy ingesta_inserta on ref.sources for insert to ingesta with check (true);
create policy ingesta_actualiza on ref.sources for update to ingesta using (true) with check (true);

drop policy if exists ingesta_lee on ref.observations;
drop policy if exists ingesta_inserta on ref.observations;
drop policy if exists ingesta_actualiza on ref.observations;
create policy ingesta_lee on ref.observations for select to ingesta using (true);
create policy ingesta_inserta on ref.observations for insert to ingesta with check (true);
create policy ingesta_actualiza on ref.observations for update to ingesta
    using (true) with check (true);

drop policy if exists ingesta_lee on ops.ingest_runs;
drop policy if exists ingesta_inserta on ops.ingest_runs;
drop policy if exists ingesta_actualiza on ops.ingest_runs;
create policy ingesta_lee on ops.ingest_runs for select to ingesta using (true);
create policy ingesta_inserta on ops.ingest_runs for insert to ingesta with check (true);
create policy ingesta_actualiza on ops.ingest_runs for update to ingesta
    using (true) with check (true);

drop policy if exists ingesta_lee on ops.source_state;
drop policy if exists ingesta_inserta on ops.source_state;
drop policy if exists ingesta_actualiza on ops.source_state;
create policy ingesta_lee on ops.source_state for select to ingesta using (true);
create policy ingesta_inserta on ops.source_state for insert to ingesta with check (true);
create policy ingesta_actualiza on ops.source_state for update to ingesta
    using (true) with check (true);

-- Un dato solo entra aprobado dentro de una ejecución de ingesta abierta: con la
-- credencial de la ingesta no se pueden colgar datos aprobados de una ejecución cerrada
-- (o inventada después) para colarlos.
create or replace function ref.guard_observation_run() returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
    if new.estado = 'aprobado' and not exists (
        select 1 from ops.ingest_runs r where r.id = new.ingest_run_id and r.estado = 'en_curso'
    ) then
        raise exception 'un dato aprobado necesita una ejecución de ingesta abierta (run %)',
            new.ingest_run_id
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;
revoke execute on function ref.guard_observation_run() from public;

drop trigger if exists observations_guard_run on ref.observations;
create trigger observations_guard_run
    before insert on ref.observations
    for each row execute function ref.guard_observation_run();
alter table ref.observations enable always trigger observations_guard_run;
