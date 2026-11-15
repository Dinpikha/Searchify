
                 USER QUERY
                     |
                     v
             Query Understanding
                     |
             Search past queries
                     |
                     v
          File Description Retrieval
                     |
              Rank candidate files
                     |
                     v
          Select top 5–10 files
                     |
                     v
             Deep File Search
           (read actual contents)
                     |
                     v
          Hybrid Ranking + Reranking
                     |
                     v
          Present relevant results
                     |
              User selects file
                     |
                     v
          Configure / inspect / edit


searchify reads only - selected files , files stay local instead of storing complete files 



Working Order : 


1. Configuration and environment validation.
2. Supabase connection and persistent authentication.
3. File summarization pipeline.
4. Local embedding generation.
5. Summary storage and synchronization.
6. Query understanding and retrieval.
7. Deep search and reranking.