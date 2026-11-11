SET local check_function_bodies = off;

CREATE EXTENSION "vector" SCHEMA "extensions";

CREATE TABLE "public"."directories" (
  "id"           uuid                     NOT NULL DEFAULT gen_random_uuid(),
  "owner_id"     uuid                     NOT NULL,
  "path"         text                     NOT NULL,
  "name"         text,
  "enabled"      boolean                  NOT NULL DEFAULT true,
  "read_only"    boolean                  NOT NULL DEFAULT true,
  "last_scan_at" timestamp with time zone,
  "created_at"   timestamp with time zone NOT NULL DEFAULT now(),
  "updated_at"   timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT "directories_id_owner_id_key" UNIQUE (id, owner_id),
  CONSTRAINT "directories_owner_id_path_key" UNIQUE (owner_id, path),
  CONSTRAINT "directories_pkey" PRIMARY KEY (id)
);

ALTER TABLE "public"."directories"
  ENABLE ROW LEVEL SECURITY;

CREATE TABLE "public"."file_chunks" (
  "id"            uuid                     NOT NULL DEFAULT gen_random_uuid(),
  "owner_id"      uuid                     NOT NULL,
  "file_id"       uuid                     NOT NULL,
  "chunk_index"   integer                  NOT NULL,
  "content"       text                     NOT NULL,
  "token_count"   integer,
  "start_offset"  integer,
  "end_offset"    integer,
  "content_hash"  text                     NOT NULL,
  "search_vector" tsvector                 GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(content, ''::text))) STORED,
  "created_at"    timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT "file_chunks_file_id_chunk_index_key" UNIQUE (file_id, chunk_index),
  CONSTRAINT "file_chunks_id_owner_id_key" UNIQUE (id, owner_id),
  CONSTRAINT "file_chunks_pkey" PRIMARY KEY (id)
);

ALTER TABLE "public"."file_chunks"
  ENABLE ROW LEVEL SECURITY;

CREATE TABLE "public"."file_embeddings" (
  "chunk_id"   uuid                     NOT NULL,
  "owner_id"   uuid                     NOT NULL,
  "model_name" text                     NOT NULL,
  "embedding"  extensions.vector(768)   NOT NULL,
  "created_at" timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT "file_embeddings_pkey" PRIMARY KEY (chunk_id)
);

ALTER TABLE "public"."file_embeddings"
  ENABLE ROW LEVEL SECURITY;

CREATE TABLE "public"."files" (
  "id"              uuid                     NOT NULL DEFAULT gen_random_uuid(),
  "owner_id"        uuid                     NOT NULL,
  "directory_id"    uuid                     NOT NULL,
  "relative_path"   text                     NOT NULL,
  "filename"        text                     NOT NULL,
  "extension"       text,
  "mime_type"       text,
  "file_category"   text                     NOT NULL DEFAULT 'text'::text,
  "size_bytes"      bigint                   NOT NULL DEFAULT 0,
  "modified_at"     timestamp with time zone,
  "modified_ns"     bigint,
  "content_hash"    text,
  "status"          text                     NOT NULL DEFAULT 'pending'::text,
  "last_indexed_at" timestamp with time zone,
  "last_seen_at"    timestamp with time zone,
  "error_message"   text,
  "created_at"      timestamp with time zone NOT NULL DEFAULT now(),
  "updated_at"      timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT "files_directory_id_relative_path_key" UNIQUE (directory_id, relative_path),
  CONSTRAINT "files_file_category_check" CHECK ((file_category = ANY (ARRAY['code'::text, 'text'::text, 'document'::text, 'image'::text, 'video'::text]))),
  CONSTRAINT "files_id_owner_id_key" UNIQUE (id, owner_id),
  CONSTRAINT "files_pkey" PRIMARY KEY (id),
  CONSTRAINT "files_status_check" CHECK ((status = ANY (ARRAY['pending'::text, 'indexed'::text, 'skipped'::text, 'error'::text, 'metadata_only'::text])))
);

ALTER TABLE "public"."files"
  ENABLE ROW LEVEL SECURITY;

CREATE TABLE "public"."index_jobs" (
  "id"               uuid                     NOT NULL DEFAULT gen_random_uuid(),
  "owner_id"         uuid                     NOT NULL,
  "directory_id"     uuid,
  "status"           text                     NOT NULL DEFAULT 'queued'::text,
  "files_discovered" integer                  NOT NULL DEFAULT 0,
  "files_processed"  integer                  NOT NULL DEFAULT 0,
  "files_failed"     integer                  NOT NULL DEFAULT 0,
  "started_at"       timestamp with time zone,
  "completed_at"     timestamp with time zone,
  "error_message"    text,
  "created_at"       timestamp with time zone NOT NULL DEFAULT now(),
  CONSTRAINT "index_jobs_pkey" PRIMARY KEY (id),
  CONSTRAINT "index_jobs_status_check" CHECK ((status = ANY (ARRAY['queued'::text, 'running'::text, 'completed'::text, 'failed'::text])))
);

ALTER TABLE "public"."index_jobs"
  ENABLE ROW LEVEL SECURITY;

ALTER TABLE "public"."directories"
  ADD CONSTRAINT "directories_owner_id_fkey" FOREIGN KEY (owner_id) REFERENCES auth.users(id) ON DELETE CASCADE;

ALTER TABLE "public"."file_chunks"
  ADD CONSTRAINT "file_chunks_owner_id_fkey" FOREIGN KEY (owner_id) REFERENCES auth.users(id) ON DELETE CASCADE;

ALTER TABLE "public"."file_embeddings"
  ADD CONSTRAINT "file_embeddings_chunk_id_owner_id_fkey" FOREIGN KEY (chunk_id, owner_id) REFERENCES public.file_chunks(id, owner_id) ON DELETE CASCADE;

ALTER TABLE "public"."file_embeddings"
  ADD CONSTRAINT "file_embeddings_owner_id_fkey" FOREIGN KEY (owner_id) REFERENCES auth.users(id) ON DELETE CASCADE;

ALTER TABLE "public"."files"
  ADD CONSTRAINT "files_directory_id_owner_id_fkey" FOREIGN KEY (directory_id, owner_id) REFERENCES public.directories(id, owner_id) ON DELETE CASCADE;

ALTER TABLE "public"."file_chunks"
  ADD CONSTRAINT "file_chunks_file_id_owner_id_fkey" FOREIGN KEY (file_id, owner_id) REFERENCES public.files(id, owner_id) ON DELETE CASCADE;

ALTER TABLE "public"."files"
  ADD CONSTRAINT "files_owner_id_fkey" FOREIGN KEY (owner_id) REFERENCES auth.users(id) ON DELETE CASCADE;

ALTER TABLE "public"."index_jobs"
  ADD CONSTRAINT "index_jobs_directory_id_owner_id_fkey" FOREIGN KEY (directory_id, owner_id) REFERENCES public.directories(id, owner_id) ON DELETE CASCADE;

ALTER TABLE "public"."index_jobs"
  ADD CONSTRAINT "index_jobs_owner_id_fkey" FOREIGN KEY (owner_id) REFERENCES auth.users(id) ON DELETE CASCADE;

CREATE INDEX idx_chunks_file ON public.file_chunks USING btree (file_id);

CREATE INDEX idx_chunks_search ON public.file_chunks USING gin (search_vector);

CREATE INDEX idx_directories_owner ON public.directories USING btree (owner_id);

CREATE INDEX idx_embeddings_vector ON public.file_embeddings USING hnsw (embedding extensions.vector_cosine_ops);

CREATE INDEX idx_files_directory ON public.files USING btree (directory_id);

CREATE INDEX idx_files_hash ON public.files USING btree (content_hash);

CREATE INDEX idx_files_status ON public.files USING btree (status);

CREATE INDEX idx_jobs_owner_status ON public.index_jobs USING btree (owner_id, status);

CREATE POLICY "Users manage own directories" ON "public"."directories"
  FOR ALL
  TO "authenticated"
  USING ((owner_id = ( SELECT auth.uid() AS uid)))
  WITH CHECK ((owner_id = ( SELECT auth.uid() AS uid)));

CREATE POLICY "Users manage own chunks" ON "public"."file_chunks"
  FOR ALL
  TO "authenticated"
  USING ((owner_id = ( SELECT auth.uid() AS uid)))
  WITH CHECK ((owner_id = ( SELECT auth.uid() AS uid)));

CREATE POLICY "Users manage own embeddings" ON "public"."file_embeddings"
  FOR ALL
  TO "authenticated"
  USING ((owner_id = ( SELECT auth.uid() AS uid)))
  WITH CHECK ((owner_id = ( SELECT auth.uid() AS uid)));

CREATE POLICY "Users manage own files" ON "public"."files"
  FOR ALL
  TO "authenticated"
  USING ((owner_id = ( SELECT auth.uid() AS uid)))
  WITH CHECK ((owner_id = ( SELECT auth.uid() AS uid)));

CREATE POLICY "Users manage own jobs" ON "public"."index_jobs"
  FOR ALL
  TO "authenticated"
  USING ((owner_id = ( SELECT auth.uid() AS uid)))
  WITH CHECK ((owner_id = ( SELECT auth.uid() AS uid)));

COMMENT ON EXTENSION "vector" IS 'vector data type and ivfflat and hnsw access methods';

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."directories" TO "anon", "authenticated";

REVOKE ALL ON TABLE "public"."directories" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."directories" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."directories" TO "service_role";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."file_chunks" TO "anon", "authenticated";

REVOKE ALL ON TABLE "public"."file_chunks" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."file_chunks" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."file_chunks" TO "service_role";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."file_embeddings" TO "anon", "authenticated";

REVOKE ALL ON TABLE "public"."file_embeddings" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."file_embeddings" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."file_embeddings" TO "service_role";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."files" TO "anon", "authenticated";

REVOKE ALL ON TABLE "public"."files" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."files" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."files" TO "service_role";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."index_jobs" TO "anon", "authenticated";

REVOKE ALL ON TABLE "public"."index_jobs" FROM "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."index_jobs" TO "postgres";

GRANT DELETE, INSERT, MAINTAIN, REFERENCES, SELECT, TRIGGER, TRUNCATE, UPDATE ON TABLE "public"."index_jobs" TO "service_role";

