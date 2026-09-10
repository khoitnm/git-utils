#!/usr/bin/env python3
"""Run both reports in one go: the per-PR CSV, then the themes CSV built from it.

This is only a driver. It takes **exactly the arguments of
github_pr_jira_report.py** - same names, same defaults, same `--help` - runs
that script, then feeds the CSV it wrote to pr_group_report.py:

  step 1  github_pr_jira_report.py <args>   ->  reports/<stamp>_<repo>_<author>_<days>d.csv
  step 2  pr_group_report.py <that csv>     ->  ...<same name>_themes.csv

Both paths are printed on stdout, the per-PR one first, so the last line is
always the final artifact. `--quiet` and `--debug` are passed on to the grouping
step; everything else only concerns step 1. To tune the grouping itself
(`--similarity`, `--fold-below`, `--since`, `--sort`, ...) run pr_group_report.py
again on the same CSV - it is cheap, needs no network, and overwrites its own
output.

Examples
--------
  python run_pr_reports.py <path-to-clone> --authors trank --days 300
  python run_pr_reports.py <path-to-clone> --authors jdoe --days 90 --quiet
  python run_pr_reports.py . --list-authors        # step 1 only, nothing to group

The exit code is the worse of the two steps (2 = a step could not run, 1 = a
report was written but has problems, 0 = clean), so a failure anywhere is still
visible to whatever called this.
"""

from __future__ import annotations

import contextlib
import csv
import io
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
# Importing by name works when this file is run directly; the explicit path also
# covers being imported from somewhere else.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import github_pr_jira_report as per_pr  # noqa: E402 - needs the sys.path line above
import pr_group_report as themes        # noqa: E402


def log(msg: str, *, quiet: bool = False) -> None:
    per_pr.log(msg, quiet=quiet)


def csv_rows(path: Path) -> int:
    """How many PRs step 1 wrote, so an empty report is not handed on as if it
    were a real one."""
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            return max(sum(1 for _ in csv.reader(handle)) - 1, 0)  # minus the header
    except OSError:
        return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # Parsed with step 1's own parser, so --help, the option names and every
    # error message are exactly those of github_pr_jira_report.py.
    args = per_pr.parse_args(argv)
    if args.list_authors:  # prints the author list and stops: no CSV to group
        return per_pr.main(argv)

    log("step 1/2: the per-PR report (github_pr_jira_report.py)", quiet=args.quiet)
    # Step 1 prints its output path on stdout and everything else on stderr:
    # capture that one line, then hand it straight back to our own stdout.
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        first = per_pr.main(argv)
    lines = [line for line in captured.getvalue().splitlines() if line.strip()]
    for line in lines:
        # Flushed, or a piped stdout would hold this back and print it after
        # step 2's own log lines.
        print(line, flush=True)

    if first >= 2:
        log("step 2/2 skipped: the per-PR report could not be built", quiet=args.quiet)
        return first
    if not lines:
        log("ERROR: step 1 printed no output path, so there is nothing to group")
        log("  -> run github_pr_jira_report.py on its own to see what happened")
        return 2
    per_pr_csv = Path(lines[-1])
    if not per_pr_csv.is_file():
        log(f"ERROR: step 1 reported {per_pr_csv}, which does not exist")
        return 2

    rows = csv_rows(per_pr_csv)
    if not rows:
        log(f"step 2/2 skipped: {per_pr_csv.name} has a header and no PR "
            f"(nothing to group)")
        return max(first, 1)

    log("", quiet=args.quiet)
    log(f"step 2/2: grouping those {rows} PR(s) into themes (pr_group_report.py)",
        quiet=args.quiet)
    # Only the flags that mean the same thing in both scripts are forwarded.
    group_argv = [str(per_pr_csv)]
    if args.quiet:
        group_argv.append("--quiet")
    if args.debug:
        group_argv.append("--debug")
    second = themes.main(group_argv)

    return max(first, second)


if __name__ == "__main__":
    raise SystemExit(main())
