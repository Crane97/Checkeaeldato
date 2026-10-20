-- 0010 · Programas electorales de las elecciones generales, con su texto para buscar.
--
-- Qué programas entran (decisión del responsable, 2026-10-20): los de las candidaturas
-- cuyos diputados formaron grupo parlamentario propio en el Congreso en la legislatura
-- siguiente (ni el Mixto ni grupos de varias formaciones creados para dividirlo, como el
-- Plural), en las elecciones de 2016, abril de 2019, noviembre de 2019 y 2023. La lista
-- está en data/knowledge/programas.yaml.
--
-- Un programa es la postura de un partido, no evidencia de hechos: solo sirve para
-- afirmaciones sobre lo que dice un programa ("lo llevaban en su programa de 2019").
-- No es una fuente oficial primaria, así que entra 'pendiente' y no se usa hasta que una
-- persona comprueba que es el programa completo de esa elección (`conocimiento revisar`).
-- No se guarda el PDF (decisión del responsable): la URL, la copia de archive.org si la
-- hay y el SHA-256 del PDF permiten comprobar de qué documento sale cada fragmento.

create table ref.programs (
    id             text primary key check (id ~ '^[a-z0-9]+(-[a-z0-9]+)*$'),  -- psoe-2023-07
    eleccion       date not null,                    -- día de la votación
    ambito         text not null default 'generales' check (ambito in ('generales')),
    candidatura    text not null check (btrim(candidatura) <> ''),  -- como se presentó
    partido        text not null check (btrim(partido) <> ''),      -- partido o coalición
    grupo          text not null check (btrim(grupo) <> ''),        -- grupo que formó
    titulo         text not null check (btrim(titulo) <> ''),
    idioma         text not null check (idioma ~ '^[a-z]{2}$'),     -- es, ca, eu, gl
    url            text not null check (url ~ '^https?://'),        -- la descargada
    url_original   text check (url_original ~ '^https?://'),        -- web del partido
    sha256         text not null check (sha256 ~ '^[0-9a-f]{64}$'),
    paginas        integer not null check (paginas > 0),
    source_id      bigint not null references ref.sources (id),
    estado         text not null default 'pendiente'
                   check (estado in ('pendiente', 'aprobado', 'rechazado')),
    revisado_por   text,
    revisado_en    timestamptz,
    cargado_en     timestamptz not null default now(),
    constraint programs_revision_chk check (case
        when estado = 'pendiente' then revisado_en is null and revisado_por is null
        else revisado_en is not null and nullif(btrim(revisado_por), '') is not null
    end),
    unique (eleccion, partido)
);
create index programs_source_idx on ref.programs (source_id);

-- El texto en fragmentos de una o pocas páginas, para citar «programa X, pág. N».
create table ref.program_chunks (
    program_id  text    not null references ref.programs (id),
    ordinal     integer not null check (ordinal >= 0),
    pagina      integer not null check (pagina > 0),
    texto       text    not null check (btrim(texto) <> ''),
    -- Búsqueda de texto completo en español (los programas en otras lenguas se buscan
    -- igual, con peor recuperación: se avisa en `buscar-programa`).
    tsv         tsvector generated always as (to_tsvector('spanish', texto)) stored,
    primary key (program_id, ordinal)
);
create index program_chunks_tsv_idx on ref.program_chunks using gin (tsv);

-- Un documento cargado no cambia: si el partido publica otra versión, es otro SHA-256 y
-- se decide a mano (rechazar el viejo y cargar el nuevo con otro id).
create or replace function ref.guard_program_text() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if tg_op = 'DELETE' then
        raise exception 'el texto de un programa no se borra (rechaza el programa)'
            using errcode = 'check_violation';
    end if;
    raise exception 'el texto de un programa no se modifica (carga otra versión)'
        using errcode = 'check_violation';
end;
$$;
create trigger program_chunks_inmutables
    before update or delete on ref.program_chunks
    for each row execute function ref.guard_program_text();

create or replace function ref.guard_program_identity() returns trigger
language plpgsql
set search_path = ''
as $$
begin
    if tg_op = 'DELETE' then
        raise exception 'un programa no se borra (recházalo)' using errcode = 'check_violation';
    end if;
    if (new.id, new.eleccion, new.partido, new.url, new.sha256, new.paginas, new.source_id)
       is distinct from
       (old.id, old.eleccion, old.partido, old.url, old.sha256, old.paginas, old.source_id) then
        raise exception 'solo cambian la revisión y los datos descriptivos de un programa'
            using errcode = 'check_violation';
    end if;
    return new;
end;
$$;
create trigger programs_identidad
    before update or delete on ref.programs
    for each row execute function ref.guard_program_identity();

-- Misma defensa que el resto de ref (0002): RLS sin políticas y sin privilegios para la API.
alter table ref.programs       enable row level security;
alter table ref.program_chunks enable row level security;
revoke all on ref.programs, ref.program_chunks from public, anon, authenticated;
