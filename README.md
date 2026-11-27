# git-utils

Scripts that read git and GitHub data:

- [`all-tasks-in-github-history/`](all-tasks-in-github-history/README.md) - PR and Jira task reports from a repo's history.
- [`members-activities-on-git/`](members-activities-on-git/README.md) - one or more people's GitHub activity over a year, compared with the rest of the org.

## Setup once per clone

Nothing org- or project-specific may be committed (see [CLAUDE.md](CLAUDE.md)). Turn on the pre-commit check:

```
git config core.hooksPath .githooks
```

Then list your employer's private names (org, repos, internal apps, people's logins) in `.skill-lint-private-terms` at the repo root, one case-insensitive regex per line. That file is gitignored. Run the check by hand with `python scripts/check_portability.py`.
