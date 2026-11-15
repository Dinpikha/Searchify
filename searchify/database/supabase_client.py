"""Shared authenticated client used by database repositories."""
from searchify.supabase_client import get_client, get_owner_id

__all__ = ['get_client', 'get_owner_id']
