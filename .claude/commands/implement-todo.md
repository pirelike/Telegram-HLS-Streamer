Pick the next unchecked item from todo.md and implement it.

1. Read `todo.md` and find the highest-priority unchecked item (P0 first, then P1, etc.)
2. Present the item to the user and confirm they want to proceed
3. Implement the fix/feature following the conventions in CLAUDE.md:
   - Edit existing files only (no new files unless absolutely necessary)
   - Follow the flat module structure
   - Use `database.py` for any DB changes
   - Respect the async/sync boundaries
4. Write or update tests in the appropriate `tests/` file
5. Run `pytest` to verify nothing is broken
6. Mark the item as checked in `todo.md`
7. Summarize what was changed and why
