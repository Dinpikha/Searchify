-- Keep skipped/unavailable files out of summary search while retaining valid AI artifacts.
-- Query derived summaries only. Original source text is neither read nor stored.
CREATE OR REPLACE FUNCTION public.match_file_summaries(
    query_embedding extensions.vector(768),
    match_count integer DEFAULT 10,
    embedding_model text DEFAULT 'embeddinggemma-2:270m'
)
RETURNS TABLE (
    file_id uuid,
    summary_id uuid,
    summary text,
    directory_id uuid,
    filename text,
    relative_path text,
    path text,
    similarity double precision
)
LANGUAGE plpgsql
STABLE
SECURITY INVOKER
SET search_path = ''
AS $$
BEGIN
    IF auth.uid() IS NULL THEN
        RAISE EXCEPTION 'Authentication required' USING ERRCODE = '42501';
    END IF;
    IF match_count IS NULL OR match_count < 1 OR match_count > 1000 THEN
        RAISE EXCEPTION 'match_count must be between 1 and 1000' USING ERRCODE = '22023';
    END IF;
    IF query_embedding IS NULL OR extensions.vector_dims(query_embedding) <> 768
       OR (query_embedding OPERATOR(extensions.<#>) query_embedding) = 0 THEN
        RAISE EXCEPTION 'Expected a nonzero 768-dimensional query vector' USING ERRCODE = '22023';
    END IF;
    IF embedding_model IS NULL OR pg_catalog.btrim(embedding_model) = '' THEN
        RAISE EXCEPTION 'Embedding model is required' USING ERRCODE = '22023';
    END IF;

    RETURN QUERY
    SELECT f.id, s.id, s.summary, f.directory_id, f.filename, f.relative_path,
           pg_catalog.rtrim(d.path, '/') || '/' || f.relative_path,
           1.0 - (e.embedding OPERATOR(extensions.<=>) query_embedding) AS similarity
    FROM public.file_summary_embeddings AS e
    JOIN public.file_summaries AS s ON s.id = e.summary_id AND s.owner_id = e.owner_id
    JOIN public.files AS f ON f.id = s.file_id AND f.owner_id = s.owner_id
    JOIN public.directories AS d ON d.id = f.directory_id AND d.owner_id = f.owner_id
    WHERE e.owner_id = auth.uid()
      AND s.owner_id = auth.uid() AND f.owner_id = auth.uid() AND d.owner_id = auth.uid()
      AND d.enabled
      AND f.status IN ('indexed','metadata_only')
      AND f.summary_status = 'completed'
      AND f.content_hash = s.source_content_hash
      AND e.model_name = embedding_model
      AND (e.embedding OPERATOR(extensions.<#>) e.embedding) < 0
    ORDER BY e.embedding OPERATOR(extensions.<=>) query_embedding, f.id
    LIMIT match_count;
END;
$$;

REVOKE ALL ON FUNCTION public.match_file_summaries(extensions.vector, integer, text) FROM PUBLIC, anon;
GRANT EXECUTE ON FUNCTION public.match_file_summaries(extensions.vector, integer, text) TO authenticated;
GRANT SELECT ON public.file_summary_embeddings, public.file_summaries, public.files, public.directories TO authenticated;
GRANT USAGE ON SCHEMA extensions TO authenticated;
