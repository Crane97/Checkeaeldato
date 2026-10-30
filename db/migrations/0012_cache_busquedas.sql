-- 0012 · Caché de búsquedas de Tavily en Postgres.
--
-- La caché en disco (data/cache/search/) se pierde en un servidor con disco efímero y no se
-- comparte entre el Mac y el servidor: cada búsqueda repetida gastaría créditos. Misma clave
-- (hash de la consulta normalizada y los parámetros) y misma caducidad que en disco; solo
-- se guardan respuestas correctas.

create table ops.search_cache (
    clave        text primary key check (clave ~ '^[0-9a-f]{64}$'),
    consulta     jsonb       not null,   -- la petición, para auditar
    respuesta    jsonb       not null,
    guardado_en  timestamptz not null
);
create index search_cache_guardado_idx on ops.search_cache (guardado_en);  -- para purgar

alter table ops.search_cache enable row level security;
revoke all on ops.search_cache from public, anon, authenticated;
