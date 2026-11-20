"""
Database Repositories Package

This package contains base Supabase data access repositories:
- files.py: File metadata CRUD and query operations
- summaries.py: Summary and embedding CRUD operations
- queries.py: Query utilities (placeholder)

Base repositories provide owner-scoped access via auth.uid()
and are extended by the higher-level repositories in searchify.repositories.
"""