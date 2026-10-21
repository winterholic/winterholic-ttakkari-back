drop table if exists mp; drop table if exists mp_terms;
create table mp(id text primary key, body text, analyzed text, tsv tsvector generated always as (to_tsvector('simple', analyzed)) stored);
\copy mp(id,body,analyzed) from 'pas.tsv' with (format text, delimiter E'\t')
create index mp_tsv on mp using gin(tsv);
create table mp_terms as select word term, ndoc df from ts_stat('select tsv from mp');
create index on mp_terms(term);
analyze mp;
select count(*), pg_size_pretty(pg_table_size('mp')), pg_size_pretty(pg_relation_size('mp_tsv')) from mp;
select count(*) terms, count(*) filter (where df > 0.01*100000) df_gt_1pct, max(df) from mp_terms;
