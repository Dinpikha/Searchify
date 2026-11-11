CREATE TABLE "public"."file_summaries" (
  "id"                  uuid                     NOT NULL DEFAULT gen_random_uuid(),
  "owner_id"            uuid                     NOT NULL,
  "file_id"             uuid                     NOT NULL,
  "source_content_hash" text                     NOT NULL,
  "summary"             text                     NOT NULL,
  "summary_data"        jsonb                    NOT NULL DEFAULT '{}'::jsonb,
  "model_name"          text                     NOT NULL,
  "summary_version"     integer                  NOT NULL DEFAULT 1,
  "generated_at"        timestamp with time zone NOT NULL DEFAULT now(),
  "updated_at"          timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT "file_summaries_file_id_key" UNIQUE (file_id),
  CONSTRAINT "file_summaries_id_owner_id_key" UNIQUE (id, owner_id),
  CONSTRAINT "file_summaries_pkey" PRIMARY KEY (id)
);

ALTER TABLE "public"."file_summaries"
  ENABLE ROW LEVEL SECURITY;

CREATE TABLE "public"."file_summary_embeddings" (
  "summary_id" uuid                     NOT NULL,
  "owner_id"   uuid                     NOT NULL,
  "model_name" text                     NOT NULL,
  "embedding"  extensions.vector(768)   NOT NULL,
  "created_at" timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT "file_summary_embeddings_pkey" PRIMARY KEY (summary_id)
);

ALTER TABLE "public"."file_summary_embeddings"
  ENABLE ROW LEVEL SECURITY;

CREATE TABLE "public"."query_file_matches" (
  "id"                 uuid                     NOT NULL DEFAULT gen_random_uuid(),
  "owner_id"           uuid                     NOT NULL,
  "query_id"           uuid                     NOT NULL,
  "file_id"            uuid                     NOT NULL,
  "semantic_score"     double precision,
  "lexical_score"      double precision,
  "rrf_score"          double precision,
  "rerank_score"       double precision,
  "retrieval_position" integer,
  "selected"           boolean                  NOT NULL DEFAULT false,
  "matched_at"         timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT "query_file_matches_pkey" PRIMARY KEY (id),
  CONSTRAINT "query_file_matches_query_id_file_id_key" UNIQUE (query_id, file_id)
);

ALTER TABLE "public"."query_file_matches"
  ENABLE ROW LEVEL SECURITY;

CREATE TABLE "public"."query_history" (
  "id"               uuid                     NOT NULL DEFAULT gen_random_uuid(),
  "owner_id"         uuid                     NOT NULL,
  "query_text"       text                     NOT NULL,
  "normalized_query" text                     NOT NULL,
  "query_embedding"  extensions.vector(768),
  "created_at"       timestamp with time zone NOT NULL DEFAULT now(),
  "expires_at"       timestamp with time zone NOT NULL DEFAULT (now() + '30 days'::interval),
  CONSTRAINT "query_history_check" CHECK ((expires_at > created_at)),
  CONSTRAINT "query_history_id_owner_id_key" UNIQUE (id, owner_id),
  CONSTRAINT "query_history_pkey" PRIMARY KEY (id)
);

ALTER TABLE "public"."query_history"
  ENABLE ROW LEVEL SECURITY;

ALTER TABLE "public"."files"
  ADD COLUMN "summary_status" text NOT NULL DEFAULT 'pending'::text;

ALTER TABLE "public"."files"
  ADD COLUMN "summary_error" text;

ALTER TABLE "public"."files"
  ADD COLUMN "summary_updated_at" timestamp WITH time zone;

ALTER TABLE "public"."file_summaries"
  ADD CONSTRAINT "file_summaries_file_id_owner_id_fkey" FOREIGN KEY (file_id, owner_id) REFERENCES public.files(id, owner_id) ON DELETE CASCADE;

ALTER TABLE "public"."file_summaries"
  ADD CONSTRAINT "file_summaries_owner_id_fkey" FOREIGN KEY (owner_id) REFERENCES auth.users(id) ON DELETE CASCADE;

ALTER TABLE "public"."file_summary_embeddings"
  ADD CONSTRAINT "file_summary_embeddings_summary_id_owner_id_fkey" FOREIGN KEY (summary_id, owner_id) REFERENCES public.file_summaries(id, owner_id) ON DELETE CASCADE;

ALTER TABLE "public"."query_file_matches"
  ADD CONSTRAINT "query_file_matches_file_id_owner_id_fkey" FOREIGN KEY (file_id, owner_id) REFERENCES public.files(id, owner_id) ON DELETE CASCADE;

ALTER TABLE "public"."query_file_matches"
  ADD CONSTRAINT "query_file_matches_query_id_owner_id_fkey" FOREIGN KEY (query_id, owner_id) REFERENCES public.query_history(id, owner_id) ON DELETE CASCADE;

ALTER TABLE "public"."query_history"
  ADD CONSTRAINT "query_history_owner_id_fkey" FOREIGN KEY (owner_id) REFERENCES auth.users(id) ON DELETE CASCADE;

CREATE INDEX idx_file_summaries_data ON public.file_summaries USING gin (summary_data);

CREATE INDEX idx_file_summaries_hash ON public.file_summaries USING btree (source_content_hash);

CREATE INDEX idx_file_summaries_owner ON public.file_summaries USING btree (owner_id);

CREATE INDEX idx_query_history_embedding ON public.query_history USING hnsw (query_embedding extensions.vector_cosine_ops);

CREATE INDEX idx_query_history_expiry ON public.query_history USING btree (expires_at);

CREATE INDEX idx_query_history_owner ON public.query_history USING btree (owner_id, created_at DESC);

CREATE INDEX idx_query_matches_file ON public.query_file_matches USING btree (file_id);

CREATE INDEX idx_query_matches_query ON public.query_file_matches USING btree (query_id);

CREATE INDEX idx_query_matches_ranking ON public.query_file_matches USING btree (query_id, rrf_score DESC);

CREATE INDEX idx_summary_embeddings_owner ON public.file_summary_embeddings USING btree (owner_id);

CREATE INDEX idx_summary_embeddings_vector ON public.file_summary_embeddings USING hnsw (embedding extensions.vector_cosine_ops);

CREATE POLICY "Users manage their own summaries" ON "public"."file_summaries"
  FOR ALL
  TO "authenticated"
  USING ((owner_id = ( SELECT auth.uid() AS uid)))
  WITH CHECK ((owner_id = ( SELECT auth.uid() AS uid)));

CREATE POLICY "Users manage their own summary embeddings" ON "public"."file_summary_embeddings"
  FOR ALL
  TO "authenticated"
  USING ((owner_id = ( SELECT auth.uid() AS uid)))
  WITH CHECK ((owner_id = ( SELECT auth.uid() AS uid)));

CREATE POLICY "Users manage their own query matches" ON "public"."query_file_matches"
  FOR ALL
  TO "authenticated"
  USING ((owner_id = ( SELECT auth.uid() AS uid)))
  WITH CHECK ((owner_id = ( SELECT auth.uid() AS uid)));

CREATE POLICY "Users manage their own query history" ON "public"."query_history"
  FOR ALL
  TO "authenticated"
  USING ((owner_id = ( SELECT auth.uid() AS uid)))
  WITH CHECK ((owner_id = ( SELECT auth.uid() AS uid)));

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."file_summaries" TO "anon", "authenticated";

REVOKE ALL ON TABLE "public"."file_summaries" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."file_summaries" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."file_summaries" TO "service_role";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."file_summary_embeddings" TO "anon", "authenticated";

REVOKE ALL ON TABLE "public"."file_summary_embeddings" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."file_summary_embeddings" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."file_summary_embeddings" TO "service_role";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."query_file_matches" TO "anon", "authenticated";

REVOKE ALL ON TABLE "public"."query_file_matches" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."query_file_matches" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."query_file_matches" TO "service_role";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."query_history" TO "anon", "authenticated";

REVOKE ALL ON TABLE "public"."query_history" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."query_history" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."query_history" TO "service_role";

