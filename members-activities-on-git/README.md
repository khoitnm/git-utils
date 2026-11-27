# members-activities-on-git

Analyse one or more people's GitHub activity over the last year inside the company
organisation, compare them with their peers and with everyone else in the org, and
open the result as an HTML report in the browser.

```bash
pip install -r requirements.txt
cp .env.example .env          # set GITHUB_ORG and GITHUB_USERS

python members-activities-on-git.py                                   # people from GITHUB_USERS
python members-activities-on-git.py https://github.com/<login> <login2>,<login3>
python members-activities-on-git.py --users-file people.txt --team <team-slug>
python members-activities-on-git.py <login> --peers <a>,<b> --days 180
python members-activities-on-git.py --view ../reports/members-activities-on-git/<stamp>   # re-open a report
```

Authentication: the script tries these sources in order and uses the first one GitHub accepts:

1. `GITHUB_TOKEN`
2. Git Credential Manager: the `git:https://github.com` secret in Windows Credential Manager, read
   with `git credential fill` (no prompts). Set `GITHUB_CREDENTIAL_USER` to the account, because
   the store can hold several github.com accounts.
3. `GITHUB_PAT` / `GH_TOKEN`
4. the GitHub CLI token (`gh auth token`)

Only the source name is printed, never the token.

## What it collects

All data comes from GitHub search, scoped to `org:<GITHUB_ORG>` and the window:

| Data | Source |
|---|---|
| PRs opened: size, files, commits, state, merged by, every review received | GraphQL search `is:pr author:<login> created:<window>` |
| Reviews given: state, inline comments, the PR they were on | GraphQL search `is:pr reviewed-by:<login> -author:<login>`, then the person's reviews on each PR, kept if submitted in the window |
| Commits: repo, date, first line of the message | REST commit search `author:<login> merge:false` (default branches only) |
| Counts for the org comparison | 5 searches per person: PRs opened, merged, reviewed, commented on, issues opened |

GitHub's contribution graph API (`contributionsCollection`) is not used. In an
enterprise org it returns zeros for other users when their private contributions
are hidden.

Searches over 1000 results are split by date automatically.

## Comparison

* **Cohort**: the people asked for plus `--peers` / `--team`. Every metric gets a percentile.
* **Organisation**: the 5 search counts for every org member (about 1000 members ≈ 255
  batched requests ≈ 5 minutes), cached in `.cache/` for 7 days. Members with no PR
  activity at all are left out. `--baseline-sample N` uses N random members, and
  `--no-baseline` skips the step.
* **New joiners**: when a person's GitHub account was created more than 14 days into the
  window (enterprise accounts are created on joining), everything is compared over
  *their* window only: the org baseline is counted from their join date for the members who
  were already in the company by then, and cohort peers are re-measured over the same days
  from their raw data. Everyone else is compared with the members who were there for the
  whole window.
* **Similar tenure**: a separate section compares the person with the org members whose
  time in the company (from account creation) is 0.5x to 1.5x theirs, e.g. 1 to 3 months for
  someone who joined 2 months ago (`TENURE_LOW` / `TENURE_HIGH`). Because a longer tenure
  means more time to accumulate activity, everyone is measured **per 30 days of their own time
  in the window**. Members there for the whole window reuse the org baseline counts; members
  who joined inside it get their own search counts, cached for 7 days in
  `.cache/member_counts_<org>.json`.
* **Score** = the percentile turned so that 100 is always the good end (lower cycle time
  is better, more reviews is better). Group scores are the average over a group.
  Context-only metrics (lines added, weekend share, ...) are shown but never scored.

## Metrics

| Group | Metrics |
|---|---|
| Delivery | PRs opened / merged / still open, merge rate, commits on default branches, commits in own PRs, repos contributed to, lines added / deleted, median PR size, share of small PRs (≤200 lines), cycle time median and p75 |
| Quality & process | wait for first review, reviews received per PR, PRs with changes requested, self-merged without approval, PRs with a description, commits per PR |
| Code review | reviews given, PRs reviewed, approvals, change requests, inline review comments, comments per review, silent-approval share, review turnaround, lines reviewed, review/PR ratio |
| Collaboration | teammates reviewed, repos reviewed, others' PRs commented on, distinct reviewers received, GitHub issues opened |
| Consistency | total activity, active days, active weeks, longest streak, weekend share |

Full definitions are in `METRIC_DEFS` in the script and at the bottom of every report.
Every metric also has a short "how to read it" line (`METRIC_READ`) that says when a value is
good, bad or only context. In the report, hovering a metric shows its definition and that
line, and hovering any table column header shows what the column means.

## What kind of work (`pr_classify.py`, `tech_stack.py`)

Every PR is classified twice, by rules you can tune in `pr_classify.py`:

* **Area**, from its changed files, as a parent and its parts:

  | Parent | Parts |
  |---|---|
  | Backend | Core logic, APIs, Scheduled jobs, Database, Configuration, Tests |
  | Frontend | UI code, Configuration, Tests |
  | Infrastructure & delivery | Infrastructure, CI/CD pipelines |
  | Dependencies & build | (manifests, lock files, build-tool versions) |
  | Developer tooling | AI & agent tooling, Support tools & scripts |
  | Docs & other | Docs, Other |

  Tests and configuration go to Backend or Frontend by file and repository (`.java` tests,
  `application.yml` to Backend; `.spec.tsx`, Cypress, UI-repo config to Frontend). A PR's main
  parent is where its real changes are: tests, config and docs decide only when nothing else
  reaches 10% of the changed lines. Lock files count as only a few lines.
* **Change type**, from a conventional-commit prefix, then the title, then the branch name,
  then the files: Feature, Enhancement, Bug fix, Performance, Refactor & cleanup,
  Dependency & platform upgrade, Tooling & AI, Tests, Docs, CI/CD & build, Configuration,
  Release & merge, Revert, Product change.

The report shows, per parent and part, and per change type: PRs, PRs touching it, line share,
merged, lines added / deleted, median size, median time to merge, repos, share with tests.
It also shows area by month, type × area, the most-changed directories, file types, and the
areas and types of the PRs the person reviews.

**Languages & frameworks** (`tech_stack.py`), per parent and part:

* languages from file extensions, as a share of changed lines (also for the PRs they review);
* tools seen in the changed files (Docker, Helm, GitHub Actions, Jenkins, Liquibase, Cypress,
  Claude Code, Copilot...);
* frameworks and libraries from the dependency manifests (`pom.xml`, `build.gradle`,
  `package.json`, `requirements.txt`...) at the root and at the top of the folders the
  person changed most, read from GitHub and cached for 7 days in
  `.cache/repo_manifests_<org>.json`. A framework is credited to an area only when the person
  changed that area's files in a repository that uses it, and the number shown is how many
  of their PRs it applies to.

GitHub returns the first 100 files of a PR (60 for reviewed PRs), so very large PRs are
classified from those.

## Stored data and versions

The raw data of every person is kept in `.cache/people/<org>/<login>.json` (gitignored),
tagged with `data_version` and the date ranges it covers. On the next run with the same
`DATA_VERSION`, the script fetches only:

* the days of the requested window that the store does not cover yet;
* the last 14 days before the previous fetch (`REFRESH_DAYS`), because PRs merge and get
  reviewed after they are first seen;
* still-open PRs from earlier fetches, by ID, to pick up merges and closes.

Profile and the 5 search counts are always fetched (2 calls). A same-day rerun therefore
costs a handful of calls instead of dozens. `--refresh-data` ignores the store.

Metrics and PR classification are recomputed from the raw data on every run, so changes to
`pr_classify.py` or the metrics need no new fetch. Bump `DATA_VERSION` in the script only
when the GraphQL fields or the stored layout change: stores with another version are then
fetched again from scratch. Org baselines have their own `BASELINE_VERSION` and a 7-day cache.

Every JSON file and the report folder name carry the data version.

## Cost

The report ends with a **Run cost** section: GitHub API calls (total and per person), which
date ranges were fetched and which came from the store, and AI usage. No AI model is used,
because PR classification is rule-based, so tokens and cost are always 0. The fields are
in `manifest.json` → `run_cost`.

## Output

```
reports/members-activities-on-git/<stamp>_v<DATA_VERSION>/      (gitignored)
  index.html                 copy of report_template.html
  data/manifest.json         window, people, files, problems
  data/summary.json          metric definitions, metrics, ranks, scores for everyone
  data/org_baseline.json     anonymous org distributions
  data/users/<login>.json    raw per person: profile, PRs, reviews, commits, calendar, chart series
  data/report-data.js        all of the above in one script, so index.html opens from file://
```

`index.html` can be **opened straight from disk** (double-click, or copy the folder and send
it). Browsers block `fetch()` on `file://` pages but still run a `<script src>` from the same
folder, so every run also writes `data/report-data.js` with all the JSON in one file, and the
page reads it from there. The script additionally serves the folder on `127.0.0.1` and opens
the browser (Ctrl+C stops the server; `--no-serve` skips it). For reports made before
`report-data.js` existed, `--view <folder>` adds it once.

## Caveats

Every report opens with this note:

* **Don't rely on these numbers alone to evaluate a person.** They are a starting point for a
  conversation, alongside what teammates, leads and the work itself say.
* **They are not enough to judge someone's contribution.** Much of what matters can't be
  measured correctly, even when there is a number for it: design, mentoring, unblocking
  others, incidents and on-call, JIRA and requirements work, and the difficulty and impact of
  a change. Volume metrics (PRs, commits, lines) are easy to inflate.
* **They say nothing about a person's character**: teamwork, curiosity, listening,
  presentation, transparency, open-mindedness, adaptability and many other important
  qualities can't be seen in GitHub activity.
* **What the numbers mean will change over time.** Review counts and comments mean
  something different once AI generates review comments. Approval counts mean something
  different in teams that let people approve and merge their own PRs, or need no approval.
  Compare people only within the same period and similar team practices.

Also: commit search only sees default branches and commits whose author email is linked to
the account, and the report flags people whose first activity is well into the window.
