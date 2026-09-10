# all-tasks-in-github-history

*What did somebody actually work on in this repo over the last N months?*

Point `run_pr_reports.py` at a local clone. It writes two CSVs: **one row per
theme of work**, and the per-PR detail behind it.

Everything comes from the clone's **git history** — no GitHub API at all, so it
works behind an org IP allow list. The only network call is the JIRA lookup,
batched 100 keys per request.

## Quick start

```powershell
pip install -r requirements.txt
python run_pr_reports.py C:\path\to\clone --authors trank --days 300
```

Both files land in `reports/` (git-ignored) and both paths are printed, the
themes one last:

| File | One row per | Read it for |
| --- | --- | --- |
| `<stamp>_<repo>_<author>_<days>d_themes.csv` | **theme** | what the person spent the period on |
| `<stamp>_<repo>_<author>_<days>d.csv` | **pull request** | the detail behind any theme |

Two things worth knowing before the first run:

- **`git fetch` in the clone first** — the report only sees what your local refs
  know. It warns when the mainline branch looks stale.
- **`--authors` takes git author names, emails or fragments, not GitHub
  logins.** It is comma-separated and repeatable, and a PR counts when *any* of
  them matches the commit author, its email or a `Co-authored-by:` trailer.
  Passing the shared email is usually enough — it catches every identity at
  once, including web-UI commits stamped with your GitHub profile name. Use
  `--list-authors` to see the candidates; with no `--authors`, the clone's own
  `git config user.email` is used.

## The three scripts

| Script | Answers |
| --- | --- |
| `run_pr_reports.py` | Both steps in one command. **Start here.** |
| `github_pr_jira_report.py` | *Which PRs and JIRA tickets did this person ship?* Step 1, one row per PR. |
| `pr_group_report.py` | *What did they work on?* Step 2, one row per theme, built from step 1's CSV. |

`run_pr_reports.py` takes **exactly the arguments of
`github_pr_jira_report.py`** — same names, same defaults, same `--help`.
`--quiet` and `--debug` are passed on to the grouping step, `--list-authors`
stops after step 1, and the exit code is the worse of the two steps.

It has no grouping options of its own on purpose: to re-cut the themes, run
step 2 again on the same CSV. That step needs no network — it even recovers the
Jira base URL from the input's links — and overwrites its own output, so it
costs nothing to repeat:

```powershell
python pr_group_report.py                                     # newest CSV in reports/
python pr_group_report.py reports\<file>.csv --since 2026-04-01 --until 2026-06-30
python pr_group_report.py reports\<file>.csv --fold-below 2 --sort recent
```

## Credentials

From the `.env` in the repo root (already git-ignored). No GitHub token needed.

| Variable | Used for |
| --- | --- |
| `JIRA_PAT` | Jira ticket summary / status / type. Optional (`--no-jira`). |
| `JIRA_BASE_URL` | The base of the Jira **REST API**, including any context path, e.g. `https://jira.pointclickcare.com/jira`. Also builds the `…/browse/KEY-1` links. |
| `JIRA_EMAIL` | Only for Atlassian **Cloud** (email + API token). Leave unset for Jira Server/DC PATs. |
| `ANTHROPIC_API_KEY` | Only for `--summarizer anthropic`. |

The nearest `.env` **wins over variables already set in your environment** — it
is the file you edit, so a stale `JIRA_PAT` in your Windows user environment
must not silently shadow a freshly issued one. Any value it replaces is reported
with the command to delete the stale variable.

## Options

Step 1 (also accepted by `run_pr_reports.py`):

| Option | Meaning |
| --- | --- |
| `--authors a,b,c` | Git author names / emails / fragments, comma-separated and repeatable. `--author` and `--alias` are synonyms. |
| `--days 365` | Look-back window, applied to the merge date. |
| `--branch dev` | Mainline branch PRs are merged into. Default: the first of `dev` / `develop` / `main` / `master` / `trunk` that exists, preferring `origin/<name>`. |
| `--list-authors` | Print the git authors in the window and exit. |
| `--project-keys ABC,DEF` | Only accept these JIRA project keys (avoids false positives). |
| `--summary-from jira` | Where `summary` comes from: `jira` (default) prefers the ticket title, `commits` always summarizes the commit messages. |
| `--explode-tickets` | One row per JIRA ticket instead of one per PR. |
| `--no-jira` | Skip Jira calls; ticket links are still built from `JIRA_BASE_URL`. |
| `--quiet` / `--debug` | Suppress progress / log every git and HTTP call. |

Step 2 (`pr_group_report.py` only):

| Option | Meaning |
| --- | --- |
| `--since` / `--until` | Restrict to a period (on the merge date). Can only narrow the input's own window. |
| `--similarity 0.55` | 0-1. Lower = fewer, broader themes; higher = more, narrower ones. |
| `--fold-below N` | Collect every theme with fewer than N PRs into one *Other, smaller changes* row, links kept in full. |
| `--no-text-grouping` | Group by JIRA ticket only: one row per ticket, plus one per ticketless PR. |
| `--sort prs\|commits\|recent` | Row order. |
| `--top N` | How many themes to print on the console (`0` = none). |
| `--out` / `--quiet` / `--debug` | Output path / silence progress / log every merge decision with its score. |

## How it works

### Finding the PRs

Four git commands, then everything else happens in memory:

1. **One `git log --all --since=…`** loads the window (plus 180 days of slack, so
   long-lived feature branches keep their commit messages).
2. The **first-parent chain** of the mainline branch is walked: exactly what
   landed on the branch, in merge order.
3. A commit on that chain is a PR when its subject is `Merge pull request #N
   from owner/branch` (the PR title is in the merge commit body) or a squash
   merge's `title (#N)`. Branch-sync merges (`master` → `dev`, `release/*`, …)
   are skipped.
4. A PR is **attributed by its branch commits**, reachable from the merge's
   second parent and not already on the mainline. This matters: the merge commit
   is authored by whoever clicked *Merge*, so it says nothing about who wrote the
   code. A squash merge is the exception — it carries the PR author itself.

JIRA keys are matched as `[A-Z][A-Z0-9]+-\d+` with a strict precedence: **PR
title, then branch name, then commit messages**. The first source that yields a
key wins, because a PR that merged other branches in mentions every key those
branches touched; that last-resort commit-message scan is capped at 5 keys. A
blocklist filters look-alikes (`UTF-8`, `SHA-1`, `CVE-2021-…`); use
`--project-keys` for a strict allow-list.

### The `summary` column

Never empty. The first of these that yields something, capped at
`--summary-chars` (default 200):

1. the **JIRA ticket title(s)**, joined with ` | ` when the PR has several
   (`summary_source` = `jira:ABC-1`). `--summary-from commits` skips this step;
2. a summary of the PR's **commit messages** (`summary_source` = the backend
   below), also the fallback when Jira is unreachable or the key was not a real
   ticket;
3. the **PR title** (`summary_source` = `pr-title`), as a last resort.

| `--summarizer` | Cost | Notes |
| --- | --- | --- |
| `heuristic` (default) | free, no network | Strips code fences, links, merge/PR-template boilerplate and duplicate lines, then takes `title - first meaningful commit text`. |
| `ollama` | free, local | Needs [Ollama](https://ollama.com) running: `ollama pull llama3.2:1b`. Tune with `--ollama-model` / `--ollama-host`. |
| `anthropic` | paid API | Claude Haiku 4.5 (`--anthropic-model`), ~200 output tokens per PR. Needs `ANTHROPIC_API_KEY` and `pip install anthropic`. |
| `none` | — | Leaves the column empty. |

Both AI backends fall back to the heuristic summary (with one warning) if the
model or server is unavailable, so a run never fails because of summarization.

### Grouping into themes

1. **By JIRA ticket.** PRs sharing a ticket are the same work; two tickets closed
   by one PR become one theme. A key only counts when `ticket_source` says it
   came from the PR **title** or **branch** — a long-lived branch mentions every
   key it merged in, so a commit-message key is trusted only when it is the only
   one on the PR. PRs whose keys are all untrustworthy get a warning and are
   grouped by text instead.
2. **By text similarity**, merging those groups into themes: idf-weighted cosine
   over the ticket summary, PR title and branch name, after ticket keys,
   `camelCase`, stop words and version-control noise (`merge`, `revert`, `wip`)
   are stripped. Words on more than 30% of the PRs are worth nothing, so
   "update" and "api" cannot make two themes look alike.

Only whole groups are ever compared, and **every** pair of parts in a theme has
to clear `--similarity` — not just the pair that merged last. Both rules exist
to stop chaining: without them, one vague title (`resolve conflict`), or a repo
where every PR is called "update the API", collapses everything into one
meaningless row. On a synthetic 2000-PR repo of near-identical titles the loose
rule produced a single theme of 1994 PRs; the strict one produces 191, the
largest of 24.

## When something is wrong

Nothing fails silently. `WARNING:` marks recoverable trouble, `ERROR:` marks a
problem that probably invalidates the report; errors are repeated in a summary
at the end and set the exit code (`2` = the run could not start, `1` = the report
was written but has problems, `0` = clean). `--debug` adds the git/HTTP call log
and tracebacks.

Checks that run every time: the folder is a git clone, the mainline branch exists
and is not stale, the author matches somebody (otherwise the real PR authors are
listed with a *did you mean* hint), the Jira URL and token work before any ticket
is looked up, and every filter that dropped merges is counted.

## Limits worth knowing

- Only merged PRs exist in git history. Open PRs, and PRs merged with GitHub's
  *Rebase and merge*, leave no merge commit — the latter are counted and
  reported as a warning, but have no PR number to report.
- A PR is attributed by name matching, so an unusually short `--authors`
  fragment can match a colleague: prefer a full email.
- The report only sees commits your local refs have: `git fetch` first.
- PRs whose branch is older than the window + 180 days keep their merge commit
  but lose their branch commit messages (reported as a warning).
- Tickets referenced only in the PR description or review comments are not
  picked up: GitHub keeps those, git does not.
