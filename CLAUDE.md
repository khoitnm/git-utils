# git-utils

Personal scripts that read git and GitHub data (`all-tasks-in-github-history/`, `members-activities-on-git/`). Every script must work for any organization: the org, the people and the tokens come from the user's gitignored `.env` or the command line, never from the code.

## Never commit anything org- or project-specific

This applies to every committed file: scripts, templates, READMEs, `.env.example`, examples, test data, and commit messages. None of them may contain:

- an employer's or customer's name, or its GitHub org, repos, modules, package prefixes, internal apps, or services
- ticket keys, tracker or wiki URLs, internal hostnames, Slack channels, or people's names, logins and emails
- one team's conventions written as if they were the script's own rules

Use the made-up org `acme` (`https://github.com/acme/invoicing`, `example.com`, `<your-jira-host>`) wherever an example needs a name. Placeholder keys like `PROJ-1234` are fine.

Never block a private name by writing it into a committed file, not even into a check or a gitignore: that publishes it. It goes in `.skill-lint-private-terms` at the repo root (gitignored, one case-insensitive regex per line), which `scripts/check_portability.py` reads.

Data about real people stays local: `.env`, `.cache/` and `reports/` are gitignored. Never move it anywhere tracked.

## Before every commit

1. **Run `python scripts/check_portability.py`** and fix every hit. It checks every file git would commit (tracked, plus untracked files that are not gitignored) for ticket keys, private wiki/tracker links, real email addresses, and the terms in `.skill-lint-private-terms`. The `.githooks/pre-commit` hook runs it automatically once a clone has run `git config core.hooksPath .githooks`.
2. **Read your own diff once more for things the script can't catch**: a real product, team or person's name with no recognizable shape, a real value copied into `.env.example` or a README example. If a name is private and keeps coming up, add it to `.skill-lint-private-terms`.
3. **Check the commit message too.** The hook doesn't read it.
