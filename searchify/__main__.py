"""
Searchify CLI Entry Point

This module serves as the primary entry point for the Searchify application.
It can be invoked via:
    - `searchify` command (installed via pip)
    - `python -m searchify` module execution

The actual CLI application logic resides in `searchify.cli.app.main()`.
This module simply delegates to that function and exits with its return code.
"""

from searchify.cli.app import main

if __name__ == "__main__":
    raise SystemExit(main())
