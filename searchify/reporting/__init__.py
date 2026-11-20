"""
Reporting Package

This package handles local synchronization report generation and storage:
- sync_report.py: Report data structures and rendering
- history.py: Report loading and history management
- artifacts.py: Review/organization artifact persistence

Key privacy guarantee: Reports NEVER store source file contents,
credentials, or provider error bodies. Only sanitized diagnostics
and outcome metadata are persisted.

Used by: sync_service.py, CLI report/review/organize commands
"""
