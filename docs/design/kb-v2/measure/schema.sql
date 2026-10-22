create table rev(id uuid primary key, body text not null, text_sha256 char(64), char_count int);
create table pas(id uuid primary key, revision_id uuid references rev(id) on delete restrict, ord int, body text, char_start int, char_end int, sha256 char(64), analyzed text,
  tsv tsvector generated always as (to_tsvector('simple', analyzed)) stored, unique(revision_id, ord));
CREATE OR REPLACE FUNCTION kb_validate_revision() RETURNS trigger AS $f$
BEGIN
  IF encode(sha256(convert_to(NEW.body, 'UTF8')), 'hex') <> NEW.text_sha256 THEN RAISE EXCEPTION 'sha'; END IF;
  IF char_length(NEW.body) <> NEW.char_count THEN RAISE EXCEPTION 'len'; END IF;
  RETURN NEW;
END; $f$ LANGUAGE plpgsql;
CREATE OR REPLACE FUNCTION kb_validate_passage() RETURNS trigger AS $f$
DECLARE rev_body text;
BEGIN
  SELECT body INTO rev_body FROM rev WHERE id = NEW.revision_id;
  IF substring(rev_body FROM NEW.char_start + 1 FOR NEW.char_end - NEW.char_start) <> NEW.body THEN RAISE EXCEPTION 'slice'; END IF;
  IF encode(sha256(convert_to(NEW.body, 'UTF8')), 'hex') <> NEW.sha256 THEN RAISE EXCEPTION 'psha'; END IF;
  RETURN NEW;
END; $f$ LANGUAGE plpgsql;
create trigger rv before insert on rev for each row execute function kb_validate_revision();
create trigger pv before insert on pas for each row execute function kb_validate_passage();
