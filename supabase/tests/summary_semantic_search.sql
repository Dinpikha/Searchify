-- Transactional SQL acceptance checks for the real summary-search RPC and RLS.
-- Two synthetic owners and known vector directions verify ranking, limits, model
-- compatibility, stale/incomplete filtering, and rejection of missing identity.

-- Run on a disposable migrated Supabase database with:
-- psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f supabase/tests/summary_semantic_search.sql
-- Fixtures and changes are rolled back. No original file contents are used.
BEGIN;
INSERT INTO auth.users(id) VALUES
 ('10000000-0000-0000-0000-000000000001'),
 ('10000000-0000-0000-0000-000000000002');
INSERT INTO public.directories(id, owner_id, path) VALUES
 ('20000000-0000-0000-0000-000000000001', '10000000-0000-0000-0000-000000000001', '~/alpha'),
 ('20000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000002', '~/beta');
INSERT INTO public.files(id, owner_id, directory_id, relative_path, filename, content_hash, summary_status, status)
SELECT ('30000000-0000-0000-0000-' || lpad(i::text, 12, '0'))::uuid,
       ('10000000-0000-0000-0000-' || lpad((CASE WHEN i = 3 THEN 2 ELSE 1 END)::text, 12, '0'))::uuid,
       ('20000000-0000-0000-0000-' || lpad((CASE WHEN i = 3 THEN 2 ELSE 1 END)::text, 12, '0'))::uuid,
       i::text || '.py', i::text || '.py', 'hash-' || i, 'completed', 'indexed'
FROM generate_series(1, 3) AS i;
INSERT INTO public.file_summaries(id, owner_id, file_id, source_content_hash, summary, model_name)
SELECT ('40000000-0000-0000-0000-' || lpad(row_number() OVER (ORDER BY f.id)::text, 12, '0'))::uuid,
       f.owner_id, f.id, f.content_hash, 'Derived description of ' || f.filename, 'qwen2.5-coder:7b'
FROM public.files f WHERE f.id::text LIKE '30000000-0000-0000-0000-%';
INSERT INTO public.file_summary_embeddings(summary_id, owner_id, model_name, embedding)
SELECT s.id, s.owner_id, 'embeddinggemma-2:270m',
       ('[' || CASE WHEN s.file_id = '30000000-0000-0000-0000-000000000002'
                    THEN '0.6,0.8' ELSE '1,0' END || repeat(',0', 766) || ']')::extensions.vector
FROM public.file_summaries s WHERE s.id::text LIKE '40000000-0000-0000-0000-%';

DO $$ BEGIN
    IF has_function_privilege('anon', 'public.match_file_summaries(extensions.vector,integer,text)', 'EXECUTE') THEN
        RAISE EXCEPTION 'Anonymous callers must not have execute permission';
    END IF;
END $$;
-- Leave the privileged fixture-creation role and simulate owner one’s JWT so
-- subsequent RPC queries exercise caller privileges and row-level policies.
SET LOCAL ROLE authenticated;
SELECT set_config('request.jwt.claim.sub', '10000000-0000-0000-0000-000000000001', true);
SELECT set_config('request.jwt.claims', '{"sub":"10000000-0000-0000-0000-000000000001"}', true);
DO $$
DECLARE
    query extensions.vector := ('[1' || repeat(',0', 767) || ']')::extensions.vector;
    ids uuid[];
    scores double precision[];
    paths text[];
BEGIN
    SELECT array_agg(file_id ORDER BY similarity DESC), array_agg(similarity ORDER BY similarity DESC),
           array_agg(path ORDER BY similarity DESC)
    INTO ids, scores, paths FROM public.match_file_summaries(query, 10);
    IF ids IS DISTINCT FROM ARRAY['30000000-0000-0000-0000-000000000001'::uuid,
                                  '30000000-0000-0000-0000-000000000002'::uuid] THEN
        RAISE EXCEPTION 'Ranking or user isolation failed: %', ids;
    END IF;
    IF abs(scores[1] - 1) > 0.00001 OR abs(scores[2] - 0.6) > 0.00001 OR paths[1] <> '~/alpha/1.py' THEN
        RAISE EXCEPTION 'Incorrect similarity scores or paths';
    END IF;
    IF (SELECT count(*) FROM public.match_file_summaries(query, 1)) <> 1 THEN
        RAISE EXCEPTION 'Result limit was ignored';
    END IF;
    IF EXISTS (SELECT 1 FROM public.match_file_summaries(query, 10, 'other-model')) THEN
        RAISE EXCEPTION 'Incompatible model was matched';
    END IF;
    BEGIN
        PERFORM public.match_file_summaries(query, 0);
        RAISE EXCEPTION 'Invalid limit was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    BEGIN
        PERFORM public.match_file_summaries(('[0' || repeat(',0', 767) || ']')::extensions.vector, 1);
        RAISE EXCEPTION 'Zero vector was accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
    BEGIN
        PERFORM public.match_file_summaries('[1,0]'::extensions.vector, 1);
        RAISE EXCEPTION 'Wrong dimensions were accepted';
    EXCEPTION WHEN invalid_parameter_value THEN NULL;
    END;
END $$;
SELECT set_config('request.jwt.claim.sub', '10000000-0000-0000-0000-000000000002', true);
SELECT set_config('request.jwt.claims', '{"sub":"10000000-0000-0000-0000-000000000002"}', true);
DO $$ BEGIN
    IF (SELECT array_agg(file_id) FROM public.match_file_summaries(
        ('[1' || repeat(',0', 767) || ']')::extensions.vector, 10))
        IS DISTINCT FROM ARRAY['30000000-0000-0000-0000-000000000003'::uuid] THEN
        RAISE EXCEPTION 'Second user can see another user''s files';
    END IF;
END $$;
-- Incomplete and stale summaries must not be searchable, even for their owner.
RESET ROLE;
UPDATE public.files SET content_hash = 'new-hash'
WHERE id = '30000000-0000-0000-0000-000000000003';
SET LOCAL ROLE authenticated;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM public.match_file_summaries(
        ('[1' || repeat(',0', 767) || ']')::extensions.vector, 10)) THEN
        RAISE EXCEPTION 'Stale summary was returned';
    END IF;
END $$;
RESET ROLE;
UPDATE public.files SET content_hash = 'hash-3', summary_status = 'processing'
WHERE id = '30000000-0000-0000-0000-000000000003';
SET LOCAL ROLE authenticated;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM public.match_file_summaries(
        ('[1' || repeat(',0', 767) || ']')::extensions.vector, 10)) THEN
        RAISE EXCEPTION 'Incomplete summary was returned';
    END IF;
END $$;
SELECT set_config('request.jwt.claim.sub', '', true);
SELECT set_config('request.jwt.claims', '{}', true);
DO $$ BEGIN
    BEGIN
        PERFORM public.match_file_summaries(('[1' || repeat(',0', 767) || ']')::extensions.vector, 10);
        RAISE EXCEPTION 'Missing identity was accepted';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END $$;
ROLLBACK;
