-- 0003 · Los gobiernos y partidos se curan a mano: entran pendientes y una persona los
-- aprueba con `uv run conocimiento revisar`. Solo los aprobados se usan para resolver el
-- contexto político ("este Gobierno", "cuando ustedes gobernaban").

alter table ref.governments
    add column estado       text not null default 'pendiente'
        check (estado in ('pendiente', 'aprobado', 'rechazado')),
    add column revisado_por text,
    add column revisado_en  timestamptz,
    add constraint governments_revision_chk check (case estado
        when 'pendiente' then revisado_en is null and revisado_por is null
        else revisado_en is not null and nullif(btrim(revisado_por), '') is not null
    end);

alter table ref.parties
    add column estado       text not null default 'pendiente'
        check (estado in ('pendiente', 'aprobado', 'rechazado')),
    add column revisado_por text,
    add column revisado_en  timestamptz,
    add constraint parties_revision_chk check (case estado
        when 'pendiente' then revisado_en is null and revisado_por is null
        else revisado_en is not null and nullif(btrim(revisado_por), '') is not null
    end);

-- Los socios de coalición deben ser partidos conocidos. (governments.partidos es un array y
-- no admite FK: el código de aprobación y de resolución comprueba que estén aprobados.)
alter table ref.government_partners
    add constraint government_partners_partido_fk foreign key (partido) references ref.parties (id);
