-- 0005 · Base de políticos: quién es quién, con cargos y partidos fechados y fuente por dato.
--
-- Ahorra búsquedas al identificar a los oradores (paso speaker_research). Dos orígenes:
-- - curado: carga inicial por cargo (data/knowledge/politicians.json). Entra 'pendiente' y
--   no se usa hasta que una persona lo aprueba con `uv run conocimiento revisar`.
-- - investigacion: lo que el análisis averiguó de un orador nuevo, con sus citas. Entra
--   'automatico' y se usa enseguida (decisión del responsable, 2026-10-14); si se rechaza,
--   deja de usarse.
-- Cargos y partidos llevan periodo (inicio/fin) o, si vienen de una investigación, la fecha
-- en que se constataron (constatado_en): un dato constatado no dice desde cuándo vale.
-- Las fuentes secundarias (Wikipedia/Wikidata) se admiten marcadas como tales
-- (ref.sources.tipo = 'secundaria'), p. ej. para la fecha de nacimiento.

create table ref.politicians (
    id                          text primary key check (id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),
    nombre                      text not null check (btrim(nombre) <> ''),
    alias                       text[] not null default '{}',
    fecha_nacimiento            date,
    fecha_nacimiento_source_id  bigint references ref.sources (id),
    origen                      text not null check (origen in ('curado', 'investigacion')),
    estado                      text not null
                                check (estado in ('pendiente', 'automatico', 'aprobado', 'rechazado')),
    revisado_por                text,
    revisado_en                 timestamptz,
    creado_en                   timestamptz not null default now(),
    constraint politicians_revision_chk check (case
        when estado in ('pendiente', 'automatico') then revisado_en is null and revisado_por is null
        else revisado_en is not null and nullif(btrim(revisado_por), '') is not null
    end),
    -- Solo lo investigado por el análisis se usa sin revisar.
    constraint politicians_automatico_chk check (estado <> 'automatico' or origen = 'investigacion'),
    constraint politicians_nacimiento_fuente_chk
        check (fecha_nacimiento is null or fecha_nacimiento_source_id is not null)
);
create index politicians_alias_idx on ref.politicians using gin (alias);
create index politicians_nacimiento_source_idx on ref.politicians (fecha_nacimiento_source_id);

create table ref.politician_roles (
    id             bigint generated always as identity primary key,
    politician_id  text   not null references ref.politicians (id),
    cargo          text   not null check (btrim(cargo) <> ''),  -- "Ministra de Hacienda"
    organo         text,                                        -- "Gobierno de España"
    territorio     text   not null default 'ES',
    inicio         date,
    fin            date,
    en_funciones   boolean not null default false,              -- este tramo, en funciones
    constatado_en  date,
    source_id      bigint not null references ref.sources (id),
    -- O un periodo (inicio, fin opcional) o una fecha de constatación, nunca las dos cosas.
    constraint politician_roles_fecha_chk check ((inicio is null) <> (constatado_en is null)),
    constraint politician_roles_constatado_chk check (constatado_en is null or fin is null),
    constraint politician_roles_periodo_chk check (fin is null or inicio is null or fin >= inicio)
);
create index politician_roles_politician_idx on ref.politician_roles (politician_id);
create index politician_roles_source_idx on ref.politician_roles (source_id);

create table ref.politician_parties (
    id             bigint generated always as identity primary key,
    politician_id  text   not null references ref.politicians (id),
    partido        text   not null check (btrim(partido) <> ''),  -- como lo nombra la fuente
    inicio         date,
    fin            date,
    constatado_en  date,
    source_id      bigint not null references ref.sources (id),
    constraint politician_parties_fecha_chk check ((inicio is null) <> (constatado_en is null)),
    constraint politician_parties_constatado_chk check (constatado_en is null or fin is null),
    constraint politician_parties_periodo_chk check (fin is null or inicio is null or fin >= inicio)
);
create index politician_parties_politician_idx on ref.politician_parties (politician_id);
create index politician_parties_source_idx on ref.politician_parties (source_id);

-- Misma defensa que el resto de ref (0002): RLS sin políticas y sin privilegios para la API.
alter table ref.politicians        enable row level security;
alter table ref.politician_roles   enable row level security;
alter table ref.politician_parties enable row level security;
revoke all on ref.politicians, ref.politician_roles, ref.politician_parties
    from public, anon, authenticated;
