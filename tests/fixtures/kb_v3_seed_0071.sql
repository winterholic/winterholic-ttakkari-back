-- 0071 스키마에 대표 기존 데이터를 넣는다: personal 문서 2, work 문서 2(제목 충돌 포함), revision/passage/evidence.
-- body 'ABCDEFGHIJ' 의 sha256 은 DB 에서 직접 계산해 원장 검증 트리거를 통과시킨다.
DO $$
DECLARE
    spec record;
    src uuid; doc uuid; rev uuid; pas uuid;
    body text;
BEGIN
    FOR spec IN SELECT * FROM (VALUES
        ('p1', 'personal', 'personal-note-1', 'Shared Title'),
        ('p2', 'personal', 'personal-note-2', 'Personal Only'),
        ('w1', 'work',     'work-spec-1',     'Shared Title'),
        ('w2', 'work',     'work-spec-2',     'Work Only')
    ) AS t(tag, scope, doc_key, title)
    LOOP
        body := 'ABCDEFGHIJ-' || spec.tag;
        src := gen_random_uuid(); doc := gen_random_uuid(); rev := gen_random_uuid(); pas := gen_random_uuid();
        INSERT INTO kb_sources (id, source_key, kind, sha256, byte_size, store_path, collected_at, origin_meta, created_at)
        VALUES (src, 'src-' || spec.tag, 'upload',
                encode(sha256(convert_to(body, 'UTF8')), 'hex'), octet_length(body), 'p/' || spec.tag, now(), '{}'::jsonb, now());
        INSERT INTO kb_documents (id, doc_key, title, lang, created_at, updated_at)
        VALUES (doc, spec.doc_key, spec.title, 'ko', now(), now());
        INSERT INTO kb_revisions (id, document_id, source_id, rev_no, parser_version, body, text_sha256, char_count, created_at)
        VALUES (rev, doc, src, 1, 'raw-v1', body, encode(sha256(convert_to(body, 'UTF8')), 'hex'), char_length(body), now());
        INSERT INTO kb_passages (id, revision_id, ord, heading, body, char_start, char_end, sha256, created_at)
        VALUES (pas, rev, 0, NULL, body, 0, char_length(body),
                encode(sha256(convert_to(body, 'UTF8')), 'hex'), now());
        INSERT INTO kb_evidence (id, passage_id, span_start, span_end, sha256, note, created_at)
        VALUES (gen_random_uuid(), pas, 0, 5,
                encode(sha256(convert_to(substring(body FROM 1 FOR 5), 'UTF8')), 'hex'), spec.tag, now());
        INSERT INTO kb_document_access (document_id, tenant_id, scope, hidden)
        VALUES (doc, 'owner', spec.scope, false);
    END LOOP;
END $$;
