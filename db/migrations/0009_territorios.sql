-- 0009 · Territorios (fase H): comunidades y ciudades autónomas y provincias del INE.
--
-- ref.territories existía con el país (0001). Se añaden el código del INE y los nombres
-- alternativos (forma habitual, cooficial) para reconocer el territorio de una afirmación.
-- Las filas se cargan con `uv run conocimiento cargar-territorios`
-- (data/knowledge/territorios.json, generado desde la API del INE).

alter table ref.territories
    add column codigo_ine text,
    add column alias      text[] not null default '{}';

comment on column ref.territories.codigo_ine is 'Código del INE (comunidad 01–19, provincia 01–52)';
comment on column ref.territories.alias is 'Nombres alternativos: forma habitual, corta y cooficial';

create unique index territories_nivel_ine_idx on ref.territories (nivel, codigo_ine)
    where codigo_ine is not null;

-- Fuentes territoriales de cada indicador (catalog.yaml → ref.indicators).
alter table ref.indicators add column territorial jsonb not null default '[]'::jsonb;
