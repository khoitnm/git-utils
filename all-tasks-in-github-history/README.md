# all-tasks-in-github-history

`github_pr_jira_report.py` builds a CSV of every JIRA ticket / pull request a
developer shipped in a repo over a look-back window (default 365 days).

It reads a **local clone's git history** — no GitHub API calls at all, so it
works behind an org IP allow list. The only network traffic is the JIRA lookup,
which is batched (100 keys per request).

For each PR it reports the JIRA ticket(s) with full links, the PR link, the
merge date, who merged it, and the commit counts. The `summary` column is
**never empty**: it holds the JIRA ticket title, or a summary of the PR's commit
messages when there is no ticket.

## Setup

```powershell
pip install -r requirements.txt
```

Credentials come from the `.env` in the repo root (already git-ignored):

| Variable | Used for |
| --- | --- |
| `JIRA_PAT` | Jira ticket summary / status / type. Optional (`--no-jira`). |
| `JIRA_BASE_URL` | The base of the Jira **REST API**, including any context path, e.g. `https://jira.pointclickcare.com/jira`. Also builds the `…/browse/KEY-1` links. |
| `JIRA_EMAIL` | Only for Atlassian **Cloud** (email + API token). Leave unset for Jira Server/DC PATs. |
| `ANTHROPIC_API_KEY` | Only for `--summarizer anthropic`. |

The nearest `.env` **wins over variables already set in your environment** — it is
the file you edit, so a stale `JIRA_PAT` left in your Windows user environment must
not silently shadow a freshly issued one. Any value the `.env` replaces is reported
with the command to delete the stale variable.

No GitHub token is needed any more.

## Usage

```powershell
python github_pr_jira_report.py C:\path\to\clone --authors trank --days 365
```

The first argument is the folder of a local clone (anywhere inside it works).
The report lands in
`all-tasks-in-github-history/reports/YYYYMMDD-HHMMSS_REPO_author_<days>d.csv`
(`reports/` is git-ignored) and the path is printed on stdout. The date-time
prefix keeps the folder in chronological order and stops two runs overwriting
each other; the window is in the name too, so a quick `--days 10` trial cannot be
mistaken for the full report — always open the path the run printed.

`--authors` takes **git author names, emails or fragments** — not GitHub logins.
One person usually has several identities, so the flag is comma-separated and
repeatable; a PR counts when *any* of them matches:

```powershell
python github_pr_jira_report.py C:\path\to\clone --authors Kevin-Tran_PCC,trank --days 300
python github_pr_jira_report.py C:\path\to\clone --authors trank --authors "Kevin Tran"
```

A name matches when it appears in the commit's author name, its email, or a
`Co-authored-by:` trailer. **Passing the shared email is usually enough** — it
catches every identity at once, including commits made through the GitHub web UI
(which stamp your GitHub profile name rather than your local `user.name`).

To see the candidates:

```powershell
python github_pr_jira_report.py C:\path\to\clone --list-authors --days 365
```

With no `--authors`, the clone's own `git config user.email` is used. The report
file is named after the first name given.

> Run `git fetch` in the clone first: the report can only see what your local
> refs know about. The script warns when the mainline branch looks stale.

### Common options

| Option | Meaning |
| --- | --- |
| `--authors a,b,c` | Git author names / emails / fragments, comma-separated and repeatable. `--author` and `--alias` are accepted as synonyms. |
| `--days 365` | Look-back window, applied to the merge date. |
| `--branch dev` | Mainline branch PRs are merged into. Default: the first of `dev` / `develop` / `main` / `master` / `trunk` that exists, preferring `origin/<name>`. |
| `--list-authors` | Print the git authors in the window and exit. |
| `--project-keys ABC,DEF` | Only accept these JIRA project keys (avoids false positives). |
| `--summary-from jira` | Where the `summary` column comes from: `jira` (default) prefers the ticket title, `commits` always summarizes the commit messages. |
| `--explode-tickets` | One row per JIRA ticket instead of one row per PR. |
| `--no-jira` | Skip Jira calls; ticket links are still built from `JIRA_BASE_URL`. |
| `--quiet` / `--debug` | Suppress progress / log every git and HTTP call. |

### Columns

The six you actually read come first:

`summary` (never empty), `jira_summaries`, `pr_title`, `branch`, `merged_date`,
`commits_by_author`

then the provenance:

`jira_links`, `ticket_source` (title / branch / commit-message), `pr_url`,
`merged_by`, `authors`, `first_commit_date`, `last_commit_date`,
`summary_source`, `merge_sha`

and the JIRA field dumps last, where they stay out of the way:

`jira_statuses`, `jira_types`

`--explode-tickets` swaps the aggregate `jira_summaries` column for a per-ticket
`jira_summary` / `jira_ticket` / `jira_link`, and `jira_statuses` / `jira_types`
for the single-value `jira_status` / `jira_type`, keeping the same lead order.

The order lives in `LEAD_FIELDS` / `REST_FIELDS` at the top of the script — edit
those to rearrange. The CSV is written as UTF-8 with BOM so Excel opens it
correctly.

## How PRs are found

Four git commands, then everything else happens in memory:

1. **One `git log --all --since=…`** loads the window (plus 180 days of slack, so
   long-lived feature branches keep their commit messages).
2. The **first-parent chain** of the mainline branch is walked: that is exactly
   what landed on the branch, in merge order.
3. A commit on that chain is a PR when its subject is `Merge pull request #N
   from owner/branch` (the PR title is in the merge commit body) or a squash
   merge's `title (#N)`. Branch-sync merges (`master` → `dev`, `release/*`, …)
   are skipped.
4. A PR is **attributed by its branch commits**, reachable from the merge's
   second parent and not already on the mainline. This matters: the merge commit
   is authored by whoever clicked *Merge*, so it says nothing about who wrote the
   code. A squash merge is the exception — it carries the PR author itself.
   Any of the `--authors` names counts, including in a `Co-authored-by:` trailer.

JIRA keys are matched as `[A-Z][A-Z0-9]+-\d+` with a strict precedence: **PR
title, then branch name, then commit messages**. The first source that yields a
key wins, because a PR that merged other branches in mentions every key those
branches touched; that last-resort commit-message scan is also capped at 5 keys.
A blocklist filters look-alikes (`UTF-8`, `SHA-1`, `CVE-2021-…`, `RELEASE-2`, …);
use `--project-keys` when you want a strict allow-list.

## The 200-character summary

The `summary` column always has content, from the first of these that yields
something (all capped at `--summary-chars`, default 200):

1. the **JIRA ticket title(s)** of the PR, joined with ` | ` when it has several
   (`summary_source` = `jira:ABC-1`). `--summary-from commits` skips this step;
2. a summary of the PR's **commit messages** (`summary_source` = the backend
   below), which is also the fallback when Jira is unreachable or the key turned
   out not to be a real ticket;
3. the **PR title** (`summary_source` = `pr-title`), as a last resort.

The commit-message backends:

| `--summarizer` | Cost | Notes |
| --- | --- | --- |
| `heuristic` (default) | free, no network | Strips code fences, links, merge/PR-template boilerplate and duplicate lines, then takes `title - first meaningful commit text`. |
| `ollama` | free, local | Needs [Ollama](https://ollama.com) running: `ollama pull llama3.2:1b`. Tune with `--ollama-model` / `--ollama-host`. |
| `anthropic` | paid API | Uses Claude Haiku 4.5 (`--anthropic-model`), ~200 output tokens per PR. Needs `ANTHROPIC_API_KEY` and `pip install anthropic`. |
| `none` | — | Leaves the column empty. |

Both AI backends fall back to the heuristic summary (with one warning) if the
model or server is unavailable, so a run never fails because of summarization.

## When something is wrong

Nothing fails silently. `WARNING:` marks recoverable trouble, `ERROR:` marks a
problem that probably invalidates the report; errors are repeated in a summary
at the end and set the exit code (`2` = the run could not start, `1` = the report
was written but has problems, `0` = clean). `--debug` adds the git/HTTP call log
and tracebacks.

Checks that run on every report: the folder is a git clone, the mainline branch
exists and is not stale, the author matches somebody (otherwise the real PR
authors are listed with a *did you mean* hint), the Jira URL and token work
before any ticket is looked up, and every filter that dropped merges is counted.

## Limits worth knowing

- Only merged PRs exist in git history. Open PRs, and PRs merged with GitHub's
  *Rebase and merge*, leave no merge commit — the latter are counted and
  reported as a warning, but they have no PR number to report.
- A PR is attributed by name matching, so an unusually short `--authors`
  fragment can match a colleague: prefer a full email or login.
- The report can only see commits your local refs have: `git fetch` first.
- PRs whose branch is older than the window + 180 days keep their merge commit
  but lose their branch commit messages (reported as a warning).
- Tickets referenced only in the PR description or review comments are not
  picked up: GitHub keeps those, git does not.
