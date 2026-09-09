# all-tasks-in-github-history

`github_pr_jira_report.py` builds a CSV of every JIRA ticket / pull request a
GitHub user worked on in a repo over a look-back window (default 360 days).

For each PR it reports the JIRA ticket(s) with full links, the PR link, the
dates, and the commit counts. When a PR carries no ticket, a short (200 char)
summary is generated instead.

## Setup

```powershell
pip install -r requirements.txt
```

Credentials come from the `.env` in the repo root (already git-ignored):

| Variable | Used for |
| --- | --- |
| `GIT_PAT` (or `GITHUB_PAT` / `GITHUB_TOKEN`) | GitHub REST + search API. Needs `repo` (read) scope. |
| `JIRA_PAT` | Jira ticket summary / status / type. Optional. |
| `JIRA_BASE_URL` | Builds the `…/browse/KEY-1` links, e.g. `https://jira.pointclickcare.com`. |
| `JIRA_EMAIL` | Only for Atlassian **Cloud** (email + API token). Leave unset for Jira Server/DC PATs. |
| `ANTHROPIC_API_KEY` | Only for `--summarizer anthropic`. |

## Usage

```powershell
python github_pr_jira_report.py https://github.com/OWNER/REPO --author trank --days 360
```

The report lands in `all-tasks-in-github-history/reports/OWNER-REPO_author_YYYYMMDD.csv`
(`reports/` is git-ignored) and the path is printed on stdout.

`--author` is the **GitHub login**, not the local git username. To check yours:

```powershell
python -c "import github_pr_jira_report as m; m.load_env(); print(m.GitHubClient(m.env_first('GIT_PAT','GITHUB_PAT'),'https://api.github.com').get('/user')['login'])"
```

### Common options

| Option | Meaning |
| --- | --- |
| `--days 360` | Look-back window. |
| `--match both` | `author` = PRs the user opened, `commits` = PRs containing their commits, `both` (default). |
| `--date-field created` | Which PR date the window applies to (`created` / `merged` / `updated`). A PR opened before the window is still included if the user's own commits fall inside it. |
| `--state merged` | Only merged (or `open` / `closed` / `all`) PRs. |
| `--alias k.tran --alias "Kevin Tran"` | Extra name/email fragments that identify the author in commit metadata (repeatable). Useful when commits were made with a different git identity. |
| `--project-keys ABC,DEF` | Only accept these JIRA project keys (avoids false positives). |
| `--only-author-commits` | Drop PRs the user opened but did not commit to. |
| `--explode-tickets` | One row per JIRA ticket instead of one row per PR. |
| `--no-jira` | Skip Jira calls; ticket links are still built from `JIRA_BASE_URL`. |
| `--limit 5 --quiet` | Handy for a quick trial run. |

### Columns

`jira_tickets`, `jira_links`, `jira_summaries`, `jira_statuses`, `jira_types`,
`ticket_source` (branch / title / commit-message / pr-body), `pr_number`,
`pr_title`, `pr_url`, `branch`, `pr_author`, `pr_state`, `created_date`,
`merged_date`, `closed_date`, `first_commit_date`, `last_commit_date`,
`author_first_commit_date`, `author_last_commit_date`, `commits_total`,
`commits_by_author`, `files_changed`, `additions`, `deletions`,
`match_reason`, `summary_200`, `summary_source`.

The CSV is written as UTF-8 with BOM so Excel opens it correctly.

## The 200-character summary

By default only ticket-less PRs get a summary (`--summarize always` does every
PR). Three backends, all capped at `--summary-chars` (default 200):

| `--summarizer` | Cost | Notes |
| --- | --- | --- |
| `heuristic` (default) | free, no network | Strips HTML comments, code fences, links and PR-template headings, then takes `title - first meaningful body text`. Good enough for most rows. |
| `ollama` | free, local | Needs [Ollama](https://ollama.com) running: `ollama pull llama3.2:1b`. Tune with `--ollama-model` / `--ollama-host`. |
| `anthropic` | paid API | Uses Claude Haiku 4.5 (`--anthropic-model`), ~200 output tokens per PR. Needs `ANTHROPIC_API_KEY` and `pip install anthropic`. |
| `none` | — | Leaves the column empty. |

Both AI backends fall back to the heuristic summary (with one warning) if the
model or server is unavailable, so a run never fails because of summarization.

## How PRs are found

1. **Search issues** — `repo:O/R type:pr author:USER created:>=DATE` finds PRs the user opened.
2. **Search commits** — `repo:O/R author:USER author-date:>=DATE` finds their commits; any sha not already inside a PR from step 1 is resolved to its PR via `/commits/{sha}/pulls`. This catches commits pushed to other people's PRs.
3. Each PR's full commit list is fetched, and commits are attributed to the user by GitHub login, commit author name/email, or a `Co-authored-by:` trailer.
4. A PR is dropped if the user neither opened it nor has a commit in it — so PRs the user merely merged do not pollute the report.

JIRA keys are matched as `[A-Z][A-Z0-9]+-\d+` against the branch name, PR
title, commit messages and PR body (in that order). A blocklist filters
look-alikes (`UTF-8`, `SHA-1`, `CVE-2021-…`, `RELEASE-2`, …); use
`--project-keys` when you want a strict allow-list.

## Limits worth knowing

- GitHub's search API caps a query at 1000 results and 30 searches/minute; the script sleeps and retries when rate-limited.
- `commits_total` comes from the PR object, but the fetched commit list stops at 250 commits, so `commits_by_author` can undercount on very large PRs.
- Tickets referenced only in review comments are not picked up.
