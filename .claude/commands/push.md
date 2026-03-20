Push current changes as a PR to GitHub.

## Steps

1. Run `git status` and `git diff --stat` to see what changed.
2. Check the current branch. If on `main`, create a new descriptive branch name based on the changes (e.g. `fix/description` or `feat/description`).
3. Stage all modified/new source files (never stage `.env`, `*.mp4`, `*.mkv`, `streamer.db`, or other media/secrets).
4. Commit with a concise message summarizing the changes. Include `Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>` trailer.
5. Push to origin with `-u` flag.
6. Create a pull request using `curl` against the GitHub API:
   - Repo: detect from `git remote get-url origin` (extract `owner/repo`)
   - Auth: use the git credential helper or `GITHUB_TOKEN` env var
   - Base branch: `main`
   - Title: short, under 70 chars
   - Body: include `## Summary` (bullet points) and `## Test plan` (checklist) sections, plus the Claude Code attribution line

If `gh` CLI is available, prefer using it over curl.

Print the PR URL when done.
