"""
Lexical Search Module

Reserved module for a future lexical retrieval adapter; no implementation here.

Current filename search is implemented by searchify.indexer.search_files using
parameterized SQLite substring matching, followed by local availability and
directory-rule checks. The current index does not retain source-text FTS data.
"""
