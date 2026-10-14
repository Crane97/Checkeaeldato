-- 0003 · Los gobiernos y partidos se curan a mano: entran pendientes y una persona los
-- aprueba con `uv run conocimiento revisar`. Solo los aprobados se usan para resolver el
-- contexto político ("este Gobierno", "cuando ustedes gobernaban").

alter table ref.governments
    add column estado       text not null default 'pendiente'
        check (estado in ('pendiente', 'aprobado', 'rechazado')),
    add column revisado_por text,
    add column revisado_en  timestamptz,
    add constraint governments_revision_chk
        check ((estado = 'pendiente') = (revisado_en is null)
               and (estado = 'pendiente' or revisado_por is not null));

alter table ref.parties
    add column estado       text not null default 'pendiente'
        check (estado in ('pendiente', 'aprobado', 'rechazado')),
    add column revisado_por text,
    add column revisado_en  timestamptz,
    add constraint parties_revision_chk
        check ((estado = 'pendiente') = (revisado_en is null)
               and (estado = 'pendiente' or revisado_por is not null));

create index government_partners_government_idx on ref.government_partners (government_id);
