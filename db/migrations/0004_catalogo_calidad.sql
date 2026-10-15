-- 0004 · Campos del catálogo para controlar la calidad y fechar los datos.
--
-- salto_max_puntos: para indicadores que ya son tasas o porcentajes (paro, IPC, déficit…),
--   el salto entre periodos se mide en puntos, no en %: de 0,1 a 0,5 no es un "+400 %".
-- retraso_publicacion_dias: retraso habitual entre el fin del periodo y su publicación,
--   según el calendario del organismo. Se usa para estimar fecha_publicacion cuando la
--   fuente no la da (siempre con fecha_publicacion_estimada = true).
-- conector / codigo_fuente: de dónde se descarga la serie (fase de ingesta).
-- Exactamente un umbral de salto por indicador, como exige catalog.py.

alter table ref.indicators
    add column salto_max_puntos          numeric check (salto_max_puntos > 0),
    add column retraso_publicacion_dias  integer not null default 0
        check (retraso_publicacion_dias >= 0),
    add column conector                  text not null default 'manual'
        check (conector in ('ine', 'eurostat', 'seguridad_social', 'sepe', 'bde', 'boe', 'manual')),
    add column codigo_fuente             text,
    add constraint indicators_un_umbral_chk
        check (num_nonnulls(salto_max_pct, salto_max_puntos) = 1);
