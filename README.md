<div align="center">

# 🔍 Searchify

![Searchify Mascot](searchify/ui/searchify_mascot.png)

**New to the codebase?** If you're a developer looking to understand the repository, start with the [Developer Guide](docs/DEVELOPER_GUIDE.md) for a guided tour of the architecture, source files, and execution flows.

### **♫ Your Personal AI File Assistant ♫**
### **● LOCAL-FIRST FILE DISCOVERY**

*Files stay on your computer. Only summaries and embeddings leave.*

</div>

---

## 🎯 What is Searchify?

Searchify is a **local-first AI-powered file discovery CLI** that helps you find files using natural language queries. It runs entirely on your machine — indexing, summarizing, and embedding locally — while optionally syncing only derived metadata (summaries + 768-dim embeddings) to Supabase for semantic search across devices.

**Core philosophy**: Your files never leave your computer. Extracted text exists only in memory. Supabase receives metadata, summaries, and embeddings — never source contents or chunks.

---

## ✨ Features Implemented

### 📁 **Directory Registration & Rules**
| Feature | Command |
|---------|---------|
| Register roots with profiles | `searchify dirs add ~/Projects --profile code` |
| List registered directories | `searchify dirs` |
| Configure includes/excludes/max-size | `searchify dirs configure ~/Docs --include '*.md' --max-size 1048576` |
| Per-directory sync toggle | `searchify dirs configure ~/Docs --sync off` |
| Remove registration (metadata only) | `searchify dirs remove ~/Docs --yes` |

**Smart defaults**: Android profile excludes `build/`, `.gradle/`, `generated/`; Document profile supports text, Markdown, CSV, reStructuredText.

---

### 🔄 **Incremental Synchronization**
| Feature | Command |
|---------|---------|
| Dry-run (preview only) | `searchify sync --dry-run` |
| Normal sync (batch size 5) | `searchify sync` |
| Custom batch size | `searchify sync --batch-size 10` |
| Retry failed files | `searchify sync --retry-failed` |
| Retry permanent errors (after fix) | `searchify sync --retry-failed --retry-permanent` |

**Guarantees**:
- ✅ Content-hash based change detection (inode + size + mtime + ctime)
- ✅ Unchanged files reuse completed summaries/embeddings
- ✅ Progress persisted after **every file** — interruption safe
- ✅ Workspace locking prevents duplicate processing
- ✅ Unknown/foreign claims never stolen

---

### 🤖 **Local AI Processing**
| Component | Model | Purpose |
|-----------|-------|---------|
| **Summarization** | `qwen2.5-coder:7b` | Generate concise file summaries |
| **Embeddings** | `embeddinggemma-2:270m` | 768-dim vectors for semantic search |
| **Runtime** | Ollama (local) | No cloud AI calls |

**Privacy**: Summaries checked for source/credential echoes before persistence. Chunks never written to SQLite or Supabase.

---

### 🔍 **Search & Discovery**
| Mode | Command | Description |
|------|---------|-------------|
| **Metadata** | `searchify search 'filename words'` | Local FTS5 index |
| **Semantic** | `searchify search 'auth helpers' --semantic --limit 10` | Vector similarity via Supabase pgvector |
| **Status** | `searchify status` | Local index health + sync counts |
| **Reports** | `searchify report [--history] [--verbose]` | Timestamped JSON sync reports |

**No extracted text ever uploaded** — semantic search uses stored summaries only.

---

### 🩺 **Health & Diagnostics**
| Command | Purpose |
|---------|---------|
| `searchify doctor` | Interpreter, dependencies, connectivity |
| `searchify doctor --auth` | Keyring session + owner-scoped metadata read |
| `searchify doctor --offline` | Skip network/keyring checks |

---

### 🛡️ **Privacy & Safety**
- **Source chunks never persisted** — not in SQLite, not in Supabase, not in reports
- **Summaries validated** — obvious source echoes rejected before write
- **Reports atomically published** — no history overwritten
- **Keyring for sessions** — no tokens in CLI args or `.env`
- **Explicit opt-in for live tests** — disposable local Supabase only

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        YOUR COMPUTER                             │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────────────┐ │
│  │  SQLite     │    │  Ollama     │    │  File System        │ │
│  │  (FTS5 +    │◄───│  (local)    │◄───│  (read-only)        │ │
│  │   metadata) │    │             │    │                     │ │
│  └──────┬──────┘    └──────┬──────┘    └─────────────────────┘ │
│         │                  │                                     │
│         ▼                  ▼                                     │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              Searchify Pipeline                          │   │
│  │  Register → Scan → Index → Summarize → Embed → Sync     │   │
│  └────────────────────────────┬────────────────────────────┘   │
│                               │                                 │
│              ┌────────────────┴────────────────┐               │
│              ▼                                 ▼               │
│  ┌─────────────────────┐            ┌─────────────────────┐   │
│  │   Supabase          │            │   Local Reports     │   │
│  │   (metadata +       │            │   (JSON, timestamped)│  │
│  │    summaries +      │            │                     │   │
│  │    embeddings)      │            │                     │   │
│  └─────────────────────┘            └─────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🚀 Quick Start

```bash
# 1. Install
python -m pip install -e .

# 2. Configure (create .env with your credentials)
cp .env.example .env
# Edit .env with SUPABASE_URL, SUPABASE_KEY

# 3. Start Ollama (if not running)
ollama serve

# 4. Pull models
ollama pull qwen2.5-coder:7b
ollama pull embeddinggemma-2:270m

# 5. Register directories
searchify dirs add ~/Projects --profile code
searchify dirs add ~/Documents --profile documents

# 6. First sync
searchify sync --dry-run   # preview
searchify sync             # run

# 7. Search!
searchify search 'authentication' --semantic --limit 5
```

---

## 🧪 Testing

```bash
# Offline E2E (no Ollama, no Supabase, no keyring)
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_pipeline_e2e.py -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p test_live_pipeline_guards.py -v
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v

# Live integration (requires disposable local Supabase stack)
# See docs/integration-readiness.md for exact setup
export SEARCHIFY_RUN_LIVE_E2E=1
export SEARCHIFY_LIVE_SUPABASE_URL=http://127.0.0.1:54321
export SEARCHIFY_LIVE_SUPABASE_ANON_KEY=<local-stack-anon>
export SEARCHIFY_LIVE_SUPABASE_SERVICE_KEY=<local-stack-service>
PYTHONDONTWRITEBYTECODE=1 python3 tests/live_pipeline.py --live
```

---

## 📋 Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `SUPABASE_URL` | — | Supabase project URL |
| `SUPABASE_KEY` | — | Supabase anon/service key |
| `OLLAMA_HOST` | `http://localhost:11434` | Local Ollama endpoint |
| `SUMMARY_MODEL` | `qwen2.5-coder:7b` | Summarization model |
| `EMBEDDING_MODEL` | `embeddinggemma-2:270m` | Embedding model (768-dim) |
| `SEARCHIFY_DB_PATH` | `~/.searchify/searchify.db` | Local SQLite DB |
| `SEARCHIFY_REPORTS_DIR` | `~/.searchify/reports/` | Sync reports |
| `SEARCHIFY_REVIEWS_DIR` | `~/.searchify/reviews/` | Review artifacts |
| `SEARCHIFY_PLANS_DIR` | `~/.searchify/plans/` | Organization plans |

---

## 🗺️ Roadmap — Features to Implement

### 🎯 **High Priority**

| Feature | Status | Notes |
|---------|--------|-------|
| **Interactive TUI** | 🔄 Planned | Rich-based dashboard for browse/search/review |
| **Multi-chunk summarization** | 🔄 Planned | Large files → chunked summaries with overlap |
| **Query history & analytics** | 🔄 Planned | Track search patterns, improve ranking |
| **Reranker integration** | 🔄 Planned | Cross-encoder rerank for top-k results |

### 🎯 **Medium Priority**

| Feature | Status | Notes |
|---------|--------|-------|
| **PDF/Office extraction** | 📋 Planned | `pymupdf`, `python-docx` for document profile |
| **Image OCR** | 📋 Planned | Tesseract integration for screenshots/scans |
| **Git-aware indexing** | 📋 Planned | Use `.gitignore`, track commits for freshness |
| **Web UI (read-only)** | 📋 Planned | FastAPI + HTMX for remote browsing |

### 🎯 **Future / Nice-to-Have**

| Feature | Status | Notes |
|---------|--------|-------|
| **Plugin system** | 💭 Idea | Custom extractors, summarizers, embedders |
| **Team/shared indexes** | 💭 Idea | Encrypted shared Supabase projects |
| **Mobile companion** | 💭 Idea | React Native / Tauri for phone access |
| **Code-aware search** | 💭 Idea | AST-based semantic code search |

---

## 📁 Project Structure

```
searchify/
├── __main__.py              # CLI entry point
├── config/
│   └── settings.py          # Configuration (dataclass + .env)
├── ai/
│   ├── ollama_client.py     # Local Ollama HTTP client
│   ├── summarizer.py        # Summary generation
│   └── embeddings.py        # 768-dim embedding generation
├── core/
│   └── extractor.py         # Text extraction (2MB limit)
├── database/
│   └── repositories/        # Supabase repository layer
├── services/
│   ├── summary_service.py   # Summarization pipeline
│   ├── sync_service.py      # Incremental sync orchestration
│   └── search_service.py    # Semantic search
├── retrieval/
│   └── semantic_search.py   # pgvector RPC wrapper
├── ui/
│   ├── mascot.py            # ANSI mascot renderer
│   └── searchify_mascot.ansi
├── cli/
│   ├── sync.py              # `searchify sync`
│   └── summarize.py         # `searchify summarize`
├── indexer.py               # Local SQLite + FTS5 indexer
├── migrate_directories.py   # Local → Supabase dir sync
├── migrate_files.py         # Local → Supabase file metadata sync
└── supabase_client.py       # Auth + session management
```

---

## 📜 License

MIT License — see [LICENSE](LICENSE) for details.

---

## 🤝 Contributing

1. Fork & clone
2. Create feature branch: `git checkout -b feat/amazing-feature`
3. Run tests: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v`
4. Commit & push
5. Open PR

---

<div align="center">

**Built with ❤️ for local-first privacy**

**Searchify** — *Your files. Your computer. Your search.*

</div>
