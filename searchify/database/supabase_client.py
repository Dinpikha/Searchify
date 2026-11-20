"""
Database Supabase Client - Shared Authenticated Client

This module re-exports the shared authenticated Supabase client
from the main supabase_client module for use by database repositories.

All database repositories use this client which provides:
- Authenticated Supabase client with service/anon key
- Owner ID extraction from JWT session
- Automatic token refresh via keyring

The client is configured in searchify.supabase_client and uses
the system keyring for secure session storage.

Used by: All database repositories
"""

from searchify.supabase_client import get_client, get_owner_id

__all__ = ["get_client", "get_owner_id"]
