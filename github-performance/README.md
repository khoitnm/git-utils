# github-performance

Analyse one or more people's GitHub activity over the last year inside the company
organisation, compare them with their peers and with everyone else in the org, and
open the result as an HTML report in the browser.

```bash
pip install -r requirements.txt
cp .env.example .env          # set GITHUB_ORG and GITHUB_USERS

python github_performance.py                                   # people from GITHUB_USERS
python github_performance.py https://github.com/<login> <login2>,<login3>
python github_performance.py --users-file people.txt --team <team-slug>
python github_performance.py <login> --peers <a>,<b> --days 180
python github_performance.py --view ../reports/github-performance/<stamp>   # re-open a report
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

## Output

```
reports/github-performance/<stamp>/      (gitignored)
  index.html                 copy of report_template.html
  data/manifest.json         window, people, files, problems
  data/summary.json          metric definitions, metrics, ranks, scores for everyone
  data/org_baseline.json     anonymous org distributions
  data/users/<login>.json    raw per person: profile, PRs, reviews, commits, calendar, chart series
```

The page loads the JSON with `fetch`, which browsers block on `file://`, so the script
serves the folder on `127.0.0.1` and opens the browser. Ctrl+C stops the server. Use
`--no-serve` for batch runs.

## Caveats

These numbers measure GitHub activity, not the value of the work. Design, mentoring,
on-call, meetings and JIRA work don't show up. Compare people with similar roles
and tenure. The report flags people whose first activity is well into the window.
Commit search only sees default branches and commits whose author email is linked to
the account.
