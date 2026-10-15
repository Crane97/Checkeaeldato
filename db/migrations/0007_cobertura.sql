-- 0007 · Cobertura por partido y tema (fase G).
--
-- Para comprobar que la base de conocimiento ayuda por igual a todos los partidos, cada
-- veredicto guarda quién hizo la afirmación (orador y partido, tal como los identificó el
-- análisis), su tema y si se apoyó en datos de series oficiales (base local o API). Orador,
-- partido y tema son opcionales. La lista de temas vive en pipeline/knowledge/temas.yaml
-- y se valida en Python: añadir un tema no exige una migración.

alter table verif.claims
    add column orador    text,
    add column partido   text,
    add column tema      text,
    add column con_datos boolean not null default false;

comment on column verif.claims.orador is 'Orador como lo identificó el análisis';
comment on column verif.claims.partido is 'Partido del orador en la fecha del discurso, si se identificó';
comment on column verif.claims.tema is 'Tema (temas.yaml / catálogo); para el informe de cobertura';
comment on column verif.claims.con_datos is 'El veredicto se apoyó en una serie oficial (base local o API)';

-- Filas anteriores: con datos si citaron observaciones.
update verif.claims set con_datos = true where cardinality(observation_ids) > 0;

-- `conocimiento cobertura` filtra por fecha y agrupa en SQL.
create index claims_fecha_idx on verif.claims (fecha_verificacion);
