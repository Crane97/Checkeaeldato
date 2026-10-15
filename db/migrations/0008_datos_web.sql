-- 0008 · Datos extraídos de la web reutilizables (fase F2).
--
-- Tras un veredicto apoyado en la web sobre un indicador del catálogo que la base no
-- cubre, se extrae el dato (valor, unidad, periodo) con su CITA LITERAL de la fuente, y se
-- reutiliza en afirmaciones posteriores con las mismas claves exactas (indicador,
-- territorio, periodo). Tipos de fuente: 'oficial' (organismo público), 'oficial_gobierno'
-- (comunicados del Gobierno o de un ministerio: se reutilizan pero no evitan la búsqueda,
-- porque para un orador del partido del Gobierno cuentan como neutrales), y los de 0001.
-- (Revisado por database-reviewer y neutrality-auditor, 2026-10-14.)

alter table evid.web_facts drop constraint if exists web_facts_tipo_fuente_check;
alter table evid.web_facts add constraint web_facts_tipo_fuente_check
    check (tipo_fuente in ('oficial', 'oficial_gobierno', 'oficial_extranjera', 'medio',
                           'verificador', 'otro'));

-- La tabla está vacía hasta esta fase: valor y cita pasan a ser obligatorios.
alter table evid.web_facts
    alter column valor set not null,
    add column cita   text not null check (btrim(cita) <> ''),
    add column modelo text;

comment on column evid.web_facts.cita is 'Frase literal de la fuente que contiene el dato (comprobada por el código)';

-- El mismo dato de la misma fuente no se guarda dos veces (jsonb::text es canónico).
create unique index web_facts_unico_idx
    on evid.web_facts (md5(claves::text), source_url, valor);
