# Code Style & Conventions
- Follows PEP 8 style with type hints for function signatures; docstrings used sparingly for module entrypoints.
- Logging uses standard `logging` module with structured messages; INFO level is the norm.
- Database access centralized in `storage.py`; prefer helper methods rather than raw SQL in other modules.
- Threading/event coordination via `threading.Event` and locks; background workers should respect these primitives instead of spawning ad-hoc threads.
- JSON APIs respond with camelCase keys for front-end consumption; keep responses consistent when adding endpoints.
- Front-end templates are Jinja2 strings embedded in Python; maintain inline `<script>` structure and reuse existing helper functions for dynamic updates.
