#!/usr/bin/env python3
"""Measure what people did on GitHub in the last year and compare them with the company.

Give it one or more people, as profile links or plain logins:

  python github_performance.py https://github.com/<login> <login2>,<login3>
  python github_performance.py --users-file people.txt --team my-team
  python github_performance.py                  # people from GITHUB_USERS in .env

For each person it pulls, inside the company organisation and for the window
(default: the last 365 days):

  * every pull request they opened, with size, commits, the reviews it received,
    who merged it and how long it took;
  * every review they submitted on someone else's PR, with its state, inline
    comment count and the PR it was on;
  * every non-merge commit of theirs on a default branch (repo + date);
  * search counts: PRs opened / merged / reviewed / commented on, issues opened.

Everything comes from GitHub search. The contribution graph API
(contributionsCollection) is not used: GitHub returns zeros for other users when
their private contributions are hidden, which is the norm in an enterprise org.

From that it computes delivery, quality, code-review, collaboration and
consistency metrics and ranks every person against two populations:

  * the *cohort*: the people asked for plus any --peers / --team members (every
    metric, same detail);
  * the *organisation*: the search counts of every org member (or a
    --baseline-sample of them), cached for a week, so the headline numbers get a
    company-wide percentile. Members with no PR activity at all are left out.

Raw data lands as JSON in reports/github-performance/<stamp>/data, next to a copy
of report_template.html, and the report opens in the browser through a small
local web server (Ctrl+C stops it). Re-open an old report with --view <dir>.

Configuration (.env next to this script or in any parent folder; real environment
variables win): GITHUB_ORG, GITHUB_USERS, optional GITHUB_PEERS / GITHUB_TEAM, and
GITHUB_TOKEN or GITHUB_PAT (falls back to `gh auth token`).

Exit code: 0 clean, 1 report written with problems, 2 could not run.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency: pip install requests")

SCRIPT_DIR = Path(__file__).resolve().parent
TEMPLATE = SCRIPT_DIR / "report_template.html"
DEFAULT_REPORTS_DIR = SCRIPT_DIR.parent / "reports" / "github-performance"
CACHE_DIR = SCRIPT_DIR / ".cache"
API = "https://api.github.com"

DEFAULT_DAYS = 365
MAX_DAYS = 730
SEARCH_CAP = 1000           # GitHub search never returns more than this per query
PR_PAGE = 25                # PRs per page; each carries up to 50 reviews
BASELINE_BATCH = 4          # people per aliased baseline query (x5 searches each)
BASELINE_WORKERS = 4
BASELINE_CACHE_DAYS = 7
SMALL_PR_LINES = 200        # "small PR" threshold, lines added + deleted
DESCRIBED_PR_CHARS = 30     # a PR body shorter than this counts as "no description"

PROBLEMS: list[str] = []
DEBUG = False


# --------------------------------------------------------------------------- #
# console helpers
# --------------------------------------------------------------------------- #
def log(msg: str, *, quiet: bool = False) -> None:
    if quiet:
        return
    enc = getattr(sys.stderr, "encoding", None) or "utf-8"
    print(msg.encode(enc, "replace").decode(enc, "replace"), file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    log(f"WARNING: {msg}")


def error(msg: str) -> None:
    PROBLEMS.append(msg)
    log(f"ERROR: {msg}")


def debug(msg: str) -> None:
    if DEBUG:
        log(f"  [debug] {msg}")


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def load_env() -> None:
    """Read KEY=VALUE lines from .env files, nearest folder first; the real
    environment always wins."""
    folder = SCRIPT_DIR
    seen: set[str] = set()
    while True:
        env_file = folder / ".env"
        if env_file.is_file():
            for raw in env_file.read_text(encoding="utf-8-sig").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip().removeprefix("export ").strip()
                value = value.strip().strip('"').strip("'")
                if key not in seen:
                    seen.add(key)
                    os.environ.setdefault(key, value)
        if folder.parent == folder:
            break
        folder = folder.parent


LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,98}$")
URL_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/([^/?#\s]+)", re.I)


def parse_people(values: Iterable[str]) -> list[str]:
    """Logins from profile links, @handles or bare logins (comma / space / newline
    separated), in first-seen order without duplicates."""
    out: list[str] = []
    for value in values:
        for token in re.split(r"[\s,;]+", value or ""):
            token = token.strip().rstrip("/")
            if not token or token.startswith("#"):
                continue
            match = URL_RE.match(token)
            login = match.group(1) if match else token.lstrip("@")
            if not LOGIN_RE.match(login):
                warn(f"ignoring {token!r}: not a GitHub login or profile link")
                continue
            if login.lower() not in {o.lower() for o in out}:
                out.append(login)
    return out


def token_candidates() -> Iterable[tuple[str, str]]:
    """(source, token) pairs, most explicit first. connect() uses the first one
    GitHub accepts, so a stale entry further up only costs one failed call."""
    if os.environ.get("GITHUB_TOKEN"):
        yield "GITHUB_TOKEN", os.environ["GITHUB_TOKEN"]
    token = git_credential_token()
    if token:
        yield "Git Credential Manager", token
    for key in ("GITHUB_PAT", "GH_TOKEN"):
        if os.environ.get(key):
            yield key, os.environ[key]
    token = gh_cli_token()
    if token:
        yield "GitHub CLI (gh auth token)", token


def git_credential_token() -> str:
    """The github.com secret Git keeps in its credential store (Windows
    Credential Manager via GCM, macOS keychain, libsecret...).

    The store can hold several github.com accounts; without a username GCM would
    then pop up an account picker, so the account comes from
    GITHUB_CREDENTIAL_USER (or git's credential.https://github.com.username) and
    prompts are switched off: no entry simply means no token."""
    user = os.environ.get("GITHUB_CREDENTIAL_USER", "").strip()
    if not user:
        try:
            user = subprocess.run(["git", "config", "--get", "credential.https://github.com.username"],
                                  capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""
    request = "protocol=https\nhost=github.com\n" + (f"username={user}\n" if user else "") + "\n"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    try:
        result = subprocess.run(["git", "credential", "fill"], input=request, capture_output=True,
                                text=True, timeout=30, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        debug(f"git credential fill found nothing{' for ' + user if user else ''}")
        return ""
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return fields.get("password", "")


def gh_cli_token() -> str:
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


# --------------------------------------------------------------------------- #
# GitHub client (GraphQL + the one REST endpoint GraphQL lacks: commit search)
# --------------------------------------------------------------------------- #
class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"bearer {token}",
                                     "Accept": "application/vnd.github+json",
                                     "User-Agent": "github-performance-report"})
        self.calls = 0
        self.lock = threading.Lock()

    def _send(self, method: str, url: str, tries: int = 6, **kwargs: Any) -> requests.Response:
        for attempt in range(1, tries + 1):
            with self.lock:
                self.calls += 1
            try:
                resp = self.session.request(method, url, timeout=90, **kwargs)
            except requests.RequestException as exc:
                if attempt == tries:
                    raise GitHubError(f"network error talking to GitHub: {exc}") from exc
                time.sleep(2 * attempt)
                continue
            if resp.status_code == 401:
                raise GitHubError("GitHub rejected the token (401): check GITHUB_TOKEN / GITHUB_PAT "
                                  "or run `gh auth login`")
            if resp.status_code in (403, 429):
                body = resp.text[:300]
                if "SAML" in body or "IP allow list" in body:
                    raise GitHubError(f"GitHub refused access to the organisation: {body}")
                wait = int(resp.headers.get("Retry-After") or 0)
                reset = resp.headers.get("X-RateLimit-Reset")
                if not wait and resp.headers.get("X-RateLimit-Remaining") == "0" and reset:
                    wait = max(int(reset) - int(time.time()), 1) + 1
                if attempt == tries:
                    raise GitHubError(f"GitHub rate limit / forbidden ({resp.status_code}): {body}")
                wait = wait or 20 * attempt
                warn(f"GitHub throttled a request ({resp.status_code}); waiting {wait}s")
                time.sleep(min(wait, 900))
                continue
            if resp.status_code >= 500:
                if attempt == tries:
                    raise GitHubError(f"GitHub server error {resp.status_code}: {resp.text[:300]}")
                debug(f"HTTP {resp.status_code}, retry {attempt}")
                time.sleep(3 * attempt)
                continue
            if resp.status_code != 200:
                raise GitHubError(f"GitHub HTTP {resp.status_code}: {resp.text[:300]}")
            return resp
        raise GitHubError("unreachable")

    def query(self, query: str, variables: dict[str, Any] | None = None, *, tries: int = 4) -> dict:
        for attempt in range(1, tries + 1):
            payload = self._send("POST", f"{API}/graphql",
                                 json={"query": query, "variables": variables or {}}).json()
            errors = payload.get("errors") or []
            if not errors:
                return payload["data"]
            messages = "; ".join(e.get("message", "?") for e in errors)
            if any(e.get("type") == "RATE_LIMITED" for e in errors) and attempt < tries:
                warn("GraphQL rate limited; waiting 60s")
                time.sleep(60)
                continue
            # "Something went wrong" / timeouts carry no type: worth a retry
            if all("type" not in e for e in errors) and attempt < tries:
                debug(f"GraphQL error, retry {attempt}: {messages}")
                time.sleep(3 * attempt)
                continue
            raise GitHubError(f"GraphQL error: {messages}")
        raise GitHubError("unreachable")

    def rest(self, path: str, params: dict[str, Any]) -> dict:
        return self._send("GET", f"{API}{path}", params=params).json()


# --------------------------------------------------------------------------- #
# queries
# --------------------------------------------------------------------------- #
ORG_QUERY = "query($org:String!){ organization(login:$org){ id login name } }"
PROFILE_QUERY = "query($login:String!){ user(login:$login){ login name company avatarUrl url createdAt } }"

PR_FIELDS = """
  number title url state isDraft createdAt mergedAt closedAt
  additions deletions changedFiles baseRefName body
  repository { nameWithOwner }
  author { login }
  mergedBy { login }
  reviewDecision
  commits { totalCount }
  comments { totalCount }
  reviews(first:50) { totalCount nodes { author { login __typename } state submittedAt comments { totalCount } } }
"""

REVIEWED_FIELDS = """
  number url title createdAt additions deletions
  author { login }
  repository { nameWithOwner }
  reviews(first:100, author:$login) { nodes { state submittedAt body comments { totalCount } } }
"""

SEARCH_PAGE_QUERY = """
query($q:String!, $after:String%s) {
  search(query:$q, type:ISSUE, first:%d, after:$after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { %s } }
  }
}"""

MEMBERS_QUERY = """
query($org:String!, $after:String) {
  organization(login:$org) {
    membersWithRole(first:100, after:$after) { pageInfo { hasNextPage endCursor } nodes { login } }
  }
}"""

TEAM_QUERY = """
query($org:String!, $team:String!, $after:String) {
  organization(login:$org) {
    team(slug:$team) {
      name
      members(first:100, after:$after) { pageInfo { hasNextPage endCursor } nodes { login } }
    }
  }
}"""

# The same five searches feed the org baseline and the people analysed, so the
# org percentile compares like with like.
COUNT_SEARCHES = {
    "prs_opened": "is:pr author:{l} created:{a}..{b}",
    "prs_merged": "is:pr is:merged author:{l} created:{a}..{b}",
    "prs_reviewed": "is:pr reviewed-by:{l} -author:{l} updated:{a}..{b}",
    "prs_commented": "is:pr commenter:{l} -author:{l} updated:{a}..{b}",
    "issues_opened": "is:issue author:{l} created:{a}..{b}",
}


def paginate(gh: GitHub, query: str, variables: dict, path: list[str]) -> list[dict]:
    nodes: list[dict] = []
    after = None
    while True:
        data = gh.query(query, {**variables, "after": after})
        for key in path:
            data = (data or {}).get(key) or {}
        nodes.extend(n for n in data.get("nodes") or [] if n)
        page = data.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            return nodes
        after = page["endCursor"]


def search_prs(gh: GitHub, base: str, qualifier: str, start: date, end: date, fields: str,
               extra_vars: dict[str, str] | None = None, page_size: int = PR_PAGE) -> list[dict]:
    """Every PR matching `base qualifier:start..end`. Search stops at 1000 hits,
    so a range over that is split in two and each half searched on its own."""
    extra_vars = extra_vars or {}
    var_defs = "".join(f", ${k}:String!" for k in extra_vars)
    query = SEARCH_PAGE_QUERY % (var_defs, page_size, fields)
    q = f"{base} {qualifier}:{start.isoformat()}..{end.isoformat()}"
    nodes: list[dict] = []
    after = None
    while True:
        result = gh.query(query, {"q": q, "after": after, **extra_vars})["search"]
        if after is None and result["issueCount"] > SEARCH_CAP and start < end:
            mid = start + (end - start) / 2
            debug(f"{result['issueCount']} hits for {q!r}: splitting at {mid}")
            return (search_prs(gh, base, qualifier, start, mid, fields, extra_vars, page_size)
                    + search_prs(gh, base, qualifier, mid + timedelta(days=1), end, fields, extra_vars,
                                 page_size))
        nodes.extend(n for n in result["nodes"] if n)
        if not result["pageInfo"]["hasNextPage"]:
            break
        after = result["pageInfo"]["endCursor"]
    if len(nodes) >= SEARCH_CAP:
        warn(f"search {q!r} hit the {SEARCH_CAP}-result cap; some PRs may be missing")
    return nodes


def search_commits(gh: GitHub, login: str, org: str, start: date, end: date) -> list[dict]:
    """Non-merge commits authored by login on the org's default branches (that is
    all commit search indexes), split like search_prs when over the cap."""
    q = f"author:{login} org:{org} merge:false author-date:{start.isoformat()}..{end.isoformat()}"
    items: list[dict] = []
    page = 1
    while True:
        data = gh.rest("/search/commits", {"q": q, "per_page": 100, "page": page, "sort": "author-date"})
        if page == 1 and data.get("total_count", 0) > SEARCH_CAP and start < end:
            mid = start + (end - start) / 2
            return (search_commits(gh, login, org, start, mid)
                    + search_commits(gh, login, org, mid + timedelta(days=1), end))
        batch = data.get("items") or []
        items.extend(batch)
        if len(batch) < 100 or page * 100 >= min(data.get("total_count", 0), SEARCH_CAP):
            break
        page += 1
    return [{
        "sha": it["sha"],
        "repository": (it.get("repository") or {}).get("full_name"),
        "date": ((it.get("commit") or {}).get("author") or {}).get("date"),
        "message": (((it.get("commit") or {}).get("message") or "").splitlines() or [""])[0][:200],
        "url": it.get("html_url"),
    } for it in items]


def count_batch(gh: GitHub, logins: list[str], org: str, start: date, end: date) -> dict[str, dict]:
    """The COUNT_SEARCHES for several people in one aliased GraphQL request."""
    parts = []
    for i, login in enumerate(logins):
        for key, template in COUNT_SEARCHES.items():
            q = f"org:{org} " + template.format(l=login, a=start.isoformat(), b=end.isoformat())
            parts.append(f'u{i}_{key}: search(query:{json.dumps(q)}, type:ISSUE, first:0) {{ issueCount }}')
    data = gh.query("query { " + "\n".join(parts) + " }")
    out = {}
    for i, login in enumerate(logins):
        counts = {key: data[f"u{i}_{key}"]["issueCount"] for key in COUNT_SEARCHES}
        counts["review_ratio"] = ratio(counts["prs_reviewed"], counts["prs_opened"], 2)
        out[login] = counts
    return out


# --------------------------------------------------------------------------- #
# small maths helpers
# --------------------------------------------------------------------------- #
def ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def hours(start: str | None, end: str | None) -> float | None:
    a, b = ts(start), ts(end)
    if not a or not b:
        return None
    return max((b - a).total_seconds() / 3600, 0.0)


def median(values: Iterable[float | None]) -> float | None:
    values = [v for v in values if v is not None]
    return round(statistics.median(values), 2) if values else None


def quantile(values: Iterable[float | None], q: float) -> float | None:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    idx = (len(values) - 1) * q
    lo, hi = int(idx), min(int(idx) + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (idx - lo), 2)


def ratio(num: float, den: float, digits: int = 3) -> float | None:
    return round(num / den, digits) if den else None


def is_bot(login: str | None, typename: str | None = None) -> bool:
    return not login or typename == "Bot" or login.endswith("[bot]")


def percentile_rank(value: float | None, population: list[float | None]) -> float | None:
    """Share of the population below value, ties counted half (0..100)."""
    pop = [p for p in population if p is not None]
    if value is None or not pop:
        return None
    below = sum(1 for p in pop if p < value)
    equal = sum(1 for p in pop if p == value)
    return round(100 * (below + 0.5 * equal) / len(pop), 1)


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def same(a: str | None, b: str | None) -> bool:
    return (a or "").lower() == (b or "").lower()


# --------------------------------------------------------------------------- #
# metric catalogue: one place that says what every number means
# --------------------------------------------------------------------------- #
# better: "up" = higher is better, "down" = lower is better, None = context only.
# org: True = also ranked against the whole organisation (from COUNT_SEARCHES).
METRIC_DEFS: list[dict[str, Any]] = [
    # Delivery
    {"key": "prs_opened", "group": "Delivery", "label": "PRs opened", "unit": "count", "better": "up", "org": True,
     "desc": "Pull requests the person opened in the window."},
    {"key": "prs_merged", "group": "Delivery", "label": "PRs merged", "unit": "count", "better": "up", "org": True,
     "desc": "Of the PRs opened in the window, how many are merged."},
    {"key": "merge_rate", "group": "Delivery", "label": "Merge rate", "unit": "pct", "better": "up",
     "desc": "Merged / (merged + closed without merge). Open PRs are left out."},
    {"key": "prs_open_now", "group": "Delivery", "label": "PRs still open", "unit": "count", "better": None,
     "desc": "PRs opened in the window that are still open: work in progress, or stalled."},
    {"key": "commits", "group": "Delivery", "label": "Commits on default branches", "unit": "count",
     "better": "up", "desc": "Non-merge commits authored by the person that reached a default branch "
                             "(squash merges count as one)."},
    {"key": "commits_in_prs", "group": "Delivery", "label": "Commits in own PRs", "unit": "count", "better": None,
     "desc": "Sum of commits on the person's PRs, before any squash."},
    {"key": "repos_contributed", "group": "Delivery", "label": "Repositories contributed to", "unit": "count",
     "better": "up", "desc": "Distinct repos with a commit or an opened PR."},
    {"key": "lines_added", "group": "Delivery", "label": "Lines added (merged PRs)", "unit": "count",
     "better": None, "desc": "Volume only - more lines is not better work."},
    {"key": "lines_deleted", "group": "Delivery", "label": "Lines deleted (merged PRs)", "unit": "count",
     "better": None, "desc": "Volume only; deleting code is often valuable."},
    {"key": "pr_size_median", "group": "Delivery", "label": "Median PR size (lines)", "unit": "count",
     "better": "down", "desc": "Median additions + deletions of merged PRs. Small PRs review faster and break less."},
    {"key": "small_pr_share", "group": "Delivery", "label": "Small PRs", "unit": "pct", "better": "up",
     "desc": f"Share of merged PRs with at most {SMALL_PR_LINES} changed lines."},
    {"key": "cycle_time_median_h", "group": "Delivery", "label": "PR cycle time (median)", "unit": "hours",
     "better": "down", "desc": "Hours from PR opened to merged, median over merged PRs."},
    {"key": "cycle_time_p75_h", "group": "Delivery", "label": "PR cycle time (p75)", "unit": "hours",
     "better": "down", "desc": "75th percentile of open-to-merge hours: the slow tail."},
    # Quality & process on own PRs
    {"key": "time_to_first_review_median_h", "group": "Quality & process",
     "label": "Wait for first review (median)", "unit": "hours", "better": None,
     "desc": "Hours from opening a PR to the first human review by someone else. Mostly reflects the team."},
    {"key": "reviews_received_per_pr", "group": "Quality & process", "label": "Reviews received per PR",
     "unit": "num", "better": None, "desc": "Human reviews by others, per PR opened."},
    {"key": "changes_requested_share", "group": "Quality & process", "label": "PRs with changes requested",
     "unit": "pct", "better": "down", "desc": "Share of the person's PRs where a reviewer requested changes."},
    {"key": "self_merged_unapproved", "group": "Quality & process", "label": "Self-merged without approval",
     "unit": "count", "better": "down",
     "desc": "Merged PRs the author merged themselves with no approving review from anyone else."},
    {"key": "described_pr_share", "group": "Quality & process", "label": "PRs with a description",
     "unit": "pct", "better": "up", "desc": f"Share of PRs whose body has at least {DESCRIBED_PR_CHARS} characters."},
    {"key": "commits_per_pr_median", "group": "Quality & process", "label": "Commits per PR (median)",
     "unit": "num", "better": None, "desc": "Iteration per PR. Very high values can mean rework."},
    # Code review
    {"key": "reviews_given", "group": "Code review", "label": "Reviews given", "unit": "count", "better": "up",
     "desc": "Reviews submitted on other people's PRs (approve, request changes or comment)."},
    {"key": "prs_reviewed", "group": "Code review", "label": "PRs reviewed", "unit": "count", "better": "up",
     "org": True, "desc": "Distinct PRs by others that the person reviewed in the window. Org rank uses the "
                          "search count (PRs updated in the window with a review by them)."},
    {"key": "approvals", "group": "Code review", "label": "Approvals", "unit": "count", "better": None,
     "desc": "Reviews with state APPROVED."},
    {"key": "changes_requested", "group": "Code review", "label": "Change requests", "unit": "count",
     "better": None, "desc": "Reviews with state CHANGES_REQUESTED: blocking feedback."},
    {"key": "review_comments", "group": "Code review", "label": "Inline review comments", "unit": "count",
     "better": "up", "desc": "Line comments written inside the person's reviews."},
    {"key": "comments_per_review", "group": "Code review", "label": "Comments per review", "unit": "num",
     "better": "up", "desc": "Inline comments per review given: depth of review."},
    {"key": "silent_approval_share", "group": "Code review", "label": "Silent approvals", "unit": "pct",
     "better": "down", "desc": "Approvals with no comment and no body. Very high values can mean rubber-stamping."},
    {"key": "review_turnaround_median_h", "group": "Code review", "label": "Review turnaround (median)",
     "unit": "hours", "better": "down",
     "desc": "Hours from a PR being opened to this person's first review on it (median)."},
    {"key": "lines_reviewed", "group": "Code review", "label": "Lines reviewed", "unit": "count", "better": "up",
     "desc": "Additions + deletions of the distinct PRs reviewed."},
    {"key": "review_ratio", "group": "Code review", "label": "Review / PR ratio", "unit": "num", "better": "up",
     "org": True, "desc": "PRs reviewed per PR opened. Above 1 means they review more than they ship."},
    # Collaboration
    {"key": "authors_helped", "group": "Collaboration", "label": "Teammates reviewed", "unit": "count",
     "better": "up", "desc": "Distinct people whose PRs this person reviewed."},
    {"key": "repos_reviewed", "group": "Collaboration", "label": "Repositories reviewed", "unit": "count",
     "better": "up", "desc": "Distinct repos the person reviewed in: breadth of knowledge."},
    {"key": "prs_commented", "group": "Collaboration", "label": "Others' PRs commented on", "unit": "count",
     "better": "up", "org": True,
     "desc": "PRs by others, updated in the window, with a comment or review comment from this person."},
    {"key": "reviewers_received", "group": "Collaboration", "label": "Distinct reviewers received",
     "unit": "count", "better": None, "desc": "How many different people reviewed this person's PRs."},
    {"key": "issues_opened", "group": "Collaboration", "label": "GitHub issues opened", "unit": "count",
     "better": None, "org": True, "desc": "GitHub issues opened (most teams track work in JIRA instead)."},
    # Consistency
    {"key": "total_activity", "group": "Consistency", "label": "Total activity", "unit": "count",
     "better": "up", "desc": "Commits + PRs opened + reviews given."},
    {"key": "active_days", "group": "Consistency", "label": "Active days", "unit": "count", "better": "up",
     "desc": "Days with at least one commit, PR opened or review given."},
    {"key": "active_weeks", "group": "Consistency", "label": "Active weeks", "unit": "count", "better": "up",
     "desc": "ISO weeks with any activity (about 52 in a year)."},
    {"key": "longest_streak_days", "group": "Consistency", "label": "Longest streak (days)", "unit": "count",
     "better": None, "desc": "Longest run of consecutive active days."},
    {"key": "weekend_share", "group": "Consistency", "label": "Weekend activity", "unit": "pct", "better": None,
     "desc": "Share of activity on Saturday/Sunday. A workload signal, not a goal."},
]


# --------------------------------------------------------------------------- #
# per-person collection
# --------------------------------------------------------------------------- #
def fetch_person(gh: GitHub, login: str, org: str, start: datetime, end: datetime, quiet: bool) -> dict | None:
    user = gh.query(PROFILE_QUERY, {"login": login}).get("user")
    if not user:
        error(f"{login}: no such GitHub user (or not visible to this token)")
        return None
    login = user["login"]  # canonical casing
    a, b = start.date(), end.date()

    counts = count_batch(gh, [login], org, a, b)[login]
    authored = search_prs(gh, f"org:{org} is:pr author:{login}", "created", a, b, PR_FIELDS)
    log(f"  {login}: {len(authored)} PR(s) opened", quiet=quiet)
    reviewed = search_prs(gh, f"org:{org} is:pr reviewed-by:{login} -author:{login}", "updated", a, b,
                          REVIEWED_FIELDS, {"login": login}, page_size=50)
    reviews = [r for n in reviewed for r in flatten_reviews(n, start, end)]
    log(f"  {login}: {len(reviews)} review(s) on {len(reviewed)} PR(s) by others", quiet=quiet)
    try:
        commits = search_commits(gh, login, org, a, b)
    except GitHubError as exc:
        error(f"{login}: commit search failed, commit metrics are empty: {exc}")
        commits = []
    log(f"  {login}: {len(commits)} commit(s) on default branches", quiet=quiet)

    return {
        "profile": user,
        "counts": counts,
        "pull_requests": [flatten_pr(n) for n in authored],
        "reviews": reviews,
        "commits": commits,
    }


def flatten_pr(node: dict) -> dict:
    pr = dict(node)
    pr["repository"] = (pr.get("repository") or {}).get("nameWithOwner")
    pr["author"] = (pr.get("author") or {}).get("login")
    pr["mergedBy"] = (pr.get("mergedBy") or {}).get("login")
    pr["commits"] = (pr.get("commits") or {}).get("totalCount", 0)
    pr["comments"] = (pr.get("comments") or {}).get("totalCount", 0)
    pr["body_chars"] = len((pr.pop("body", "") or "").strip())  # length only: bodies are not stored
    revs = pr.pop("reviews", {}) or {}
    pr["reviews_total"] = revs.get("totalCount", 0)
    pr["reviews"] = [{
        "author": (r.get("author") or {}).get("login"),
        "bot": is_bot((r.get("author") or {}).get("login"), (r.get("author") or {}).get("__typename")),
        "state": r.get("state"),
        "submittedAt": r.get("submittedAt"),
        "comments": (r.get("comments") or {}).get("totalCount", 0),
    } for r in revs.get("nodes") or []]
    return pr


def flatten_reviews(pr: dict, start: datetime, end: datetime) -> list[dict]:
    """One row per review the person submitted on this PR inside the window."""
    rows = []
    for review in (pr.get("reviews") or {}).get("nodes") or []:
        submitted = ts(review.get("submittedAt"))
        if not submitted or not (start <= submitted <= end):
            continue
        rows.append({
            "state": review.get("state"),
            "submittedAt": review.get("submittedAt"),
            "body_chars": len((review.get("body") or "").strip()),
            "comments": (review.get("comments") or {}).get("totalCount", 0),
            "pr_number": pr.get("number"),
            "pr_url": pr.get("url"),
            "pr_title": pr.get("title"),
            "pr_createdAt": pr.get("createdAt"),
            "pr_author": (pr.get("author") or {}).get("login"),
            "pr_size": (pr.get("additions") or 0) + (pr.get("deletions") or 0),
            "repository": (pr.get("repository") or {}).get("nameWithOwner"),
        })
    return rows


def build_calendar(person: dict, start: datetime, end: datetime) -> list[dict]:
    days: dict[str, dict] = {}
    cursor = start.date()
    while cursor <= end.date():
        days[cursor.isoformat()] = {"date": cursor.isoformat(), "commits": 0, "prs": 0, "reviews": 0}
        cursor += timedelta(days=1)
    for c in person["commits"]:
        if c["date"] and c["date"][:10] in days:  # author's local date
            days[c["date"][:10]]["commits"] += 1
    for p in person["pull_requests"]:
        if p["createdAt"][:10] in days:
            days[p["createdAt"][:10]]["prs"] += 1
    for r in person["reviews"]:
        if r["submittedAt"][:10] in days:
            days[r["submittedAt"][:10]]["reviews"] += 1
    for d in days.values():
        d["count"] = d["commits"] + d["prs"] + d["reviews"]
    return list(days.values())


def calendar_metrics(calendar: list[dict]) -> dict[str, Any]:
    active = [d for d in calendar if d["count"] > 0]
    total = sum(d["count"] for d in calendar)
    weekend = sum(d["count"] for d in calendar if date.fromisoformat(d["date"]).weekday() >= 5)
    streak = best = 0
    for d in calendar:
        streak = streak + 1 if d["count"] > 0 else 0
        best = max(best, streak)
    return {
        "total_activity": total,
        "active_days": len(active),
        "active_weeks": len({date.fromisoformat(d["date"]).isocalendar()[:2] for d in active}),
        "longest_streak_days": best,
        "weekend_share": ratio(weekend, total),
    }


def person_metrics(person: dict) -> dict[str, Any]:
    login = person["profile"]["login"]
    prs = person["pull_requests"]
    merged = [p for p in prs if p["state"] == "MERGED"]
    closed = [p for p in prs if p["state"] == "CLOSED"]
    sizes = [p["additions"] + p["deletions"] for p in merged]

    first_review_waits, reviewers, received = [], set(), 0
    changes_req = self_unapproved = 0
    for p in prs:
        human = [r for r in p["reviews"] if not r["bot"] and not same(r["author"], login)]
        received += len(human)
        reviewers.update(r["author"].lower() for r in human)
        times = sorted(r["submittedAt"] for r in human if r["submittedAt"])
        if times:
            first_review_waits.append(hours(p["createdAt"], times[0]))
        if any(r["state"] == "CHANGES_REQUESTED" for r in human):
            changes_req += 1
        if (p["state"] == "MERGED" and same(p["mergedBy"], login)
                and not any(r["state"] == "APPROVED" for r in human)):
            self_unapproved += 1

    given = person["reviews"]
    approvals = [r for r in given if r["state"] == "APPROVED"]
    first_by_pr: dict[str, dict] = {}
    for r in sorted(given, key=lambda r: r["submittedAt"]):
        first_by_pr.setdefault(r["pr_url"], r)
    repos = {p["repository"] for p in prs} | {c["repository"] for c in person["commits"]}

    m = calendar_metrics(person["calendar"])
    m.update({
        "prs_opened": len(prs),
        "prs_merged": len(merged),
        "prs_open_now": sum(1 for p in prs if p["state"] == "OPEN"),
        "merge_rate": ratio(len(merged), len(merged) + len(closed)),
        "commits": len(person["commits"]),
        "commits_in_prs": sum(p["commits"] for p in prs),
        "repos_contributed": len({r for r in repos if r}),
        "lines_added": sum(p["additions"] for p in merged),
        "lines_deleted": sum(p["deletions"] for p in merged),
        "pr_size_median": median(sizes),
        "small_pr_share": ratio(sum(1 for s in sizes if s <= SMALL_PR_LINES), len(sizes)),
        "cycle_time_median_h": median(hours(p["createdAt"], p["mergedAt"]) for p in merged),
        "cycle_time_p75_h": quantile((hours(p["createdAt"], p["mergedAt"]) for p in merged), 0.75),
        "time_to_first_review_median_h": median(first_review_waits),
        "reviews_received_per_pr": ratio(received, len(prs), 2),
        "changes_requested_share": ratio(changes_req, len(prs)),
        "self_merged_unapproved": self_unapproved,
        "described_pr_share": ratio(sum(1 for p in prs if p["body_chars"] >= DESCRIBED_PR_CHARS), len(prs)),
        "commits_per_pr_median": median(p["commits"] for p in prs),
        "reviews_given": len(given),
        "prs_reviewed": len(first_by_pr),
        "approvals": len(approvals),
        "changes_requested": sum(1 for r in given if r["state"] == "CHANGES_REQUESTED"),
        "review_comments": sum(r["comments"] for r in given),
        "comments_per_review": ratio(sum(r["comments"] for r in given), len(given), 2),
        "silent_approval_share": ratio(sum(1 for r in approvals if not r["comments"] and not r["body_chars"]),
                                       len(approvals)),
        "review_turnaround_median_h": median(hours(r["pr_createdAt"], r["submittedAt"])
                                             for r in first_by_pr.values()),
        "lines_reviewed": sum(r["pr_size"] for r in first_by_pr.values()),
        "review_ratio": ratio(len(first_by_pr), len(prs), 2),
        "authors_helped": len({r["pr_author"].lower() for r in given if not is_bot(r["pr_author"])}),
        "repos_reviewed": len({r["repository"] for r in given if r["repository"]}),
        "prs_commented": person["counts"]["prs_commented"],
        "reviewers_received": len(reviewers),
        "issues_opened": person["counts"]["issues_opened"],
    })
    return m


def person_series(person: dict, start: datetime, end: datetime) -> dict[str, Any]:
    """Pre-aggregated chart data so the template stays simple."""
    months = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end.date():
        months.append(cursor.strftime("%Y-%m"))
        cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    monthly = {mo: {"month": mo, "prs_opened": 0, "prs_merged": 0, "reviews_given": 0, "commits": 0}
               for mo in months}

    def bump(stamp: str | None, key: str) -> None:
        if stamp and stamp[:7] in monthly:
            monthly[stamp[:7]][key] += 1

    for p in person["pull_requests"]:
        bump(p["createdAt"], "prs_opened")
        bump(p["mergedAt"], "prs_merged")
    for r in person["reviews"]:
        bump(r["submittedAt"], "reviews_given")
    for c in person["commits"]:
        bump(c["date"], "commits")

    buckets = [("XS (<=10)", 10), ("S (<=50)", 50), ("M (<=200)", 200), ("L (<=500)", 500),
               ("XL (<=1000)", 1000), ("XXL (>1000)", None)]
    size_counts: Counter = Counter()
    for p in person["pull_requests"]:
        if p["state"] == "MERGED":
            size = p["additions"] + p["deletions"]
            size_counts[next(label for label, top in buckets if top is None or size <= top)] += 1

    repo_prs = Counter(p["repository"] for p in person["pull_requests"])
    repo_reviews = Counter(r["repository"] for r in person["reviews"])
    repo_commits = Counter(c["repository"] for c in person["commits"])
    repos = (set(repo_prs) | set(repo_reviews) | set(repo_commits)) - {None}
    repo_rows = sorted(({"repo": r, "prs": repo_prs.get(r, 0), "reviews": repo_reviews.get(r, 0),
                         "commits": repo_commits.get(r, 0)} for r in repos),
                       key=lambda x: -(x["prs"] + x["reviews"] + x["commits"]))

    login = person["profile"]["login"]
    authors = Counter(r["pr_author"] for r in person["reviews"] if not is_bot(r["pr_author"]))
    reviewers = Counter(r["author"] for p in person["pull_requests"] for r in p["reviews"]
                        if not r["bot"] and not same(r["author"], login))
    states = Counter(r["state"] for r in person["reviews"])
    active_dates = [d["date"] for d in person["calendar"] if d["count"]]
    return {
        "first_activity": active_dates[0] if active_dates else None,
        "last_activity": active_dates[-1] if active_dates else None,
        "monthly": list(monthly.values()),
        "pr_sizes": [{"bucket": label, "prs": size_counts.get(label, 0)} for label, _ in buckets],
        "repos": repo_rows,
        "review_states": [{"state": s, "count": c} for s, c in states.most_common()],
        "top_authors_reviewed": [{"login": a, "reviews": c} for a, c in authors.most_common(15)],
        "top_reviewers": [{"login": a, "reviews": c} for a, c in reviewers.most_common(15)],
    }


# --------------------------------------------------------------------------- #
# organisation baseline
# --------------------------------------------------------------------------- #
def org_members(gh: GitHub, org: str) -> list[str]:
    return [n["login"] for n in paginate(gh, MEMBERS_QUERY, {"org": org}, ["organization", "membersWithRole"])]


def team_members(gh: GitHub, org: str, team: str) -> list[str]:
    data = gh.query(TEAM_QUERY, {"org": org, "team": team, "after": None})
    if not (data.get("organization") or {}).get("team"):
        error(f"team {team!r} not found in {org} (use the slug from the team's URL)")
        return []
    return [n["login"] for n in paginate(gh, TEAM_QUERY, {"org": org, "team": team},
                                         ["organization", "team", "members"])]


def build_baseline(gh: GitHub, org: str, start: datetime, end: datetime, sample: int | None,
                   refresh: bool, quiet: bool) -> dict[str, Any]:
    CACHE_DIR.mkdir(exist_ok=True)
    cache = CACHE_DIR / f"org_baseline_{org}_{(end - start).days}d_{sample or 'all'}.json"
    if cache.is_file() and not refresh:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        age = datetime.now(timezone.utc) - ts(cached["generated_at"])
        if age < timedelta(days=BASELINE_CACHE_DAYS):
            log(f"org baseline: using the cache from {cached['generated_at'][:10]} "
                f"({len(cached['members'])} members; --refresh-baseline rebuilds it)", quiet=quiet)
            return cached

    log(f"org baseline: listing members of {org}...", quiet=quiet)
    members = org_members(gh, org)
    if sample and sample < len(members):
        members = random.Random(42).sample(members, sample)
    batches = [members[i:i + BASELINE_BATCH] for i in range(0, len(members), BASELINE_BATCH)]
    log(f"org baseline: counting activity of {len(members)} member(s) in {len(batches)} request(s)...",
        quiet=quiet)
    results: dict[str, dict] = {}
    failed = 0
    a, b = start.date(), end.date()
    with ThreadPoolExecutor(BASELINE_WORKERS) as pool:
        futures = {pool.submit(count_batch, gh, batch, org, a, b): batch for batch in batches}
        for done, fut in enumerate(as_completed(futures), 1):
            try:
                results.update(fut.result())
            except GitHubError as exc:
                failed += len(futures[fut])
                debug(f"baseline batch failed: {exc}")
            if done % 25 == 0 or done == len(batches):
                log(f"  baseline {done}/{len(batches)}", quiet=quiet)
    if failed:
        warn(f"org baseline: {failed} member(s) could not be counted and are left out")
    baseline = {"generated_at": iso(datetime.now(timezone.utc)), "org": org, "from": iso(start), "to": iso(end),
                "sampled": bool(sample), "members": results}
    cache.write_text(json.dumps(baseline), encoding="utf-8")
    return baseline


def baseline_stats(baseline: dict) -> dict[str, Any]:
    """Anonymous distributions of the org members with any PR activity."""
    members = baseline.get("members") or {}
    active = [v for v in members.values() if v["prs_opened"] + v["prs_reviewed"] + v["prs_commented"] > 0]
    stats: dict[str, Any] = {"members": len(members), "active_members": len(active), "metrics": {}}
    for mdef in METRIC_DEFS:
        if not mdef.get("org"):
            continue
        values = sorted(v[mdef["key"]] for v in active if v.get(mdef["key"]) is not None)
        if values:
            stats["metrics"][mdef["key"]] = {
                "values": values,
                "p25": quantile(values, 0.25), "median": quantile(values, 0.5),
                "p75": quantile(values, 0.75), "p90": quantile(values, 0.9), "max": values[-1],
            }
    return stats


# --------------------------------------------------------------------------- #
# comparison
# --------------------------------------------------------------------------- #
def compare(people: dict[str, dict], org_stats: dict | None) -> dict[str, Any]:
    cohort: dict[str, Any] = {}
    for mdef in METRIC_DEFS:
        values = [p["metrics"].get(mdef["key"]) for p in people.values()]
        clean = [v for v in values if v is not None]
        cohort[mdef["key"]] = {"median": median(clean), "min": min(clean, default=None),
                               "max": max(clean, default=None)}

    for person in people.values():
        ranks: dict[str, Any] = {}
        groups: dict[str, list[float]] = defaultdict(list)
        for mdef in METRIC_DEFS:
            key, value = mdef["key"], person["metrics"].get(mdef["key"])
            entry: dict[str, Any] = {}
            if len(people) > 1:
                entry["cohort_pct"] = percentile_rank(value, [p["metrics"].get(key) for p in people.values()])
            if org_stats and key in org_stats["metrics"]:
                entry["org_value"] = person["counts"].get(key)
                entry["org_pct"] = percentile_rank(entry["org_value"], org_stats["metrics"][key]["values"])
            # score = the percentile turned so that 100 is always the good end
            for scope in ("cohort", "org"):
                pct = entry.get(f"{scope}_pct")
                if pct is not None and mdef["better"]:
                    entry[f"{scope}_score"] = pct if mdef["better"] == "up" else round(100 - pct, 1)
            score = entry.get("cohort_score", entry.get("org_score"))
            if score is not None:
                groups[mdef["group"]].append(score)
            ranks[key] = entry
        person["ranks"] = ranks
        person["group_scores"] = {g: round(statistics.mean(v), 1) for g, v in groups.items()}
    return cohort


# --------------------------------------------------------------------------- #
# output + viewer
# --------------------------------------------------------------------------- #
def safe_name(login: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", login)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        if DEBUG:
            super().log_message(*args)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def serve(folder: Path, port: int, open_browser: bool) -> None:
    handler = functools.partial(QuietHandler, directory=str(folder))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        url = f"http://127.0.0.1:{httpd.server_address[1]}/index.html"
        log(f"report: {url}  (Ctrl+C stops the local server)")
        if open_browser:
            threading.Timer(0.5, webbrowser.open, args=(url,)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log("server stopped")


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("people", nargs="*",
                   help="profile links or logins, comma/space separated (default: GITHUB_USERS)")
    p.add_argument("--users-file", type=Path, help="file with profile links / logins, one per line")
    p.add_argument("--org", help="company GitHub organisation (default: GITHUB_ORG)")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"window length (default {DEFAULT_DAYS})")
    p.add_argument("--peers", help="extra people analysed in full for the comparison (default: GITHUB_PEERS)")
    p.add_argument("--team", help="org team slug whose members join the comparison (default: GITHUB_TEAM)")
    p.add_argument("--no-baseline", action="store_true", help="skip the company-wide baseline")
    p.add_argument("--baseline-sample", type=int, help="count only N random org members for the baseline")
    p.add_argument("--refresh-baseline", action="store_true", help="ignore the cached baseline")
    p.add_argument("--out", type=Path, default=DEFAULT_REPORTS_DIR, help="reports folder")
    p.add_argument("--no-serve", action="store_true", help="write the report and exit (no local server)")
    p.add_argument("--no-open", action="store_true", help="serve, but do not open a browser")
    p.add_argument("--port", type=int, default=0, help="local server port (default: any free port)")
    p.add_argument("--view", type=Path, help="serve an existing report folder and exit")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--debug", action="store_true")
    return p.parse_args(argv)


def connect(org: str) -> tuple[GitHub, dict]:
    tried: list[str] = []
    seen: set[str] = set()
    for source, token in token_candidates():
        if token in seen:
            continue
        seen.add(token)
        gh = GitHub(token)
        try:
            org_data = gh.query(ORG_QUERY, {"org": org})["organization"]
        except GitHubError as exc:
            tried.append(f"{source}: {exc}")
            warn(f"token from {source} failed; trying the next source")
            continue
        if not org_data:
            tried.append(f"{source}: organisation {org!r} not visible")
            continue
        log(f"authenticated with the token from {source}")
        return gh, org_data
    if not tried:
        raise GitHubError("no GitHub token: set GITHUB_CREDENTIAL_USER (Git Credential Manager), "
                          "GITHUB_TOKEN / GITHUB_PAT, or run `gh auth login`")
    raise GitHubError("no token could read the organisation:\n  " + "\n  ".join(tried))


def main(argv: list[str] | None = None) -> int:
    global DEBUG
    args = parse_args(argv)
    DEBUG = args.debug
    load_env()

    if args.view:
        folder = args.view if (args.view / "index.html").is_file() else args.view.parent
        if not (folder / "index.html").is_file():
            log(f"ERROR: {args.view} is not a report folder (no index.html)")
            return 2
        serve(folder.resolve(), args.port, not args.no_open)
        return 0

    org = args.org or os.environ.get("GITHUB_ORG", "").strip()
    if not org:
        log("ERROR: no organisation: pass --org or set GITHUB_ORG in .env")
        return 2
    raw_people = list(args.people)
    if args.users_file:
        raw_people.append(args.users_file.read_text(encoding="utf-8"))
    if not raw_people:
        raw_people.append(os.environ.get("GITHUB_USERS", ""))
    targets = parse_people(raw_people)
    if not targets:
        log("ERROR: nobody to analyse: pass profile links / logins or set GITHUB_USERS in .env")
        return 2
    peers = parse_people([args.peers or os.environ.get("GITHUB_PEERS", "")])
    team = args.team or os.environ.get("GITHUB_TEAM", "").strip() or None

    days = args.days
    if not 1 <= days <= MAX_DAYS:
        warn(f"--days {days} is outside 1..{MAX_DAYS}; using {DEFAULT_DAYS}")
        days = DEFAULT_DAYS
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(days=days)

    try:
        gh, org_data = connect(org)
        org = org_data["login"]
        if team:
            team_logins = team_members(gh, org, team)
            log(f"team {team}: {len(team_logins)} member(s) join the comparison", quiet=args.quiet)
            peers = parse_people(peers + team_logins)
        peers = [p for p in peers if p.lower() not in {t.lower() for t in targets}]

        log(f"window {start.date()} .. {end.date()} in {org}: {len(targets)} person(s), "
            f"{len(peers)} peer(s)", quiet=args.quiet)
        people: dict[str, dict] = {}
        for role, logins in (("target", targets), ("peer", peers)):
            for login in logins:
                try:
                    person = fetch_person(gh, login, org, start, end, args.quiet)
                except GitHubError as exc:
                    error(f"{login}: {exc}")
                    continue
                if not person:
                    continue
                person["role"] = role
                person["calendar"] = build_calendar(person, start, end)
                person["metrics"] = person_metrics(person)
                person["series"] = person_series(person, start, end)
                people[person["profile"]["login"]] = person
        if not any(p["role"] == "target" for p in people.values()):
            log("ERROR: none of the requested people could be analysed")
            return 2

        baseline = org_stats = None
        if not args.no_baseline:
            try:
                baseline = build_baseline(gh, org, start, end, args.baseline_sample,
                                          args.refresh_baseline, args.quiet)
                org_stats = baseline_stats(baseline)
            except GitHubError as exc:
                error(f"org baseline failed, so org percentiles are missing: {exc}")
    except GitHubError as exc:
        log(f"ERROR: {exc}")
        if DEBUG:
            traceback.print_exc()
        return 2

    cohort = compare(people, org_stats)

    folder = args.out / end.strftime("%Y%m%d-%H%M%S")
    data_dir = folder / "data"
    people_index = []
    for login, person in people.items():
        name = f"users/{safe_name(login)}.json"
        write_json(data_dir / name, person)
        people_index.append({"login": login, "name": person["profile"].get("name"), "role": person["role"],
                             "file": name})
    write_json(data_dir / "summary.json", {
        "org": org_data, "from": iso(start), "to": iso(end), "days": days,
        "metric_defs": METRIC_DEFS, "cohort": cohort,
        "people": [{"login": l, "name": p["profile"].get("name"), "role": p["role"], "metrics": p["metrics"],
                    "counts": p["counts"], "ranks": p["ranks"], "group_scores": p["group_scores"]}
                   for l, p in people.items()],
    })
    if org_stats:
        write_json(data_dir / "org_baseline.json", {"generated_at": baseline["generated_at"],
                                                    "sampled": baseline["sampled"], **org_stats})
    write_json(data_dir / "manifest.json", {
        "generated_at": iso(datetime.now(timezone.utc)), "org": org, "from": iso(start), "to": iso(end),
        "days": days, "people": people_index, "summary": "summary.json",
        "org_baseline": "org_baseline.json" if org_stats else None,
        "problems": PROBLEMS, "api_calls": gh.calls,
    })
    shutil.copyfile(TEMPLATE, folder / "index.html")
    print(folder.resolve(), flush=True)
    log(f"wrote {folder.resolve()} ({gh.calls} GitHub API calls)", quiet=args.quiet)

    if PROBLEMS:
        log(f"\n{len(PROBLEMS)} problem(s):")
        for problem in PROBLEMS:
            log(f"  - {problem}")
    if not args.no_serve:
        serve(folder.resolve(), args.port, not args.no_open)
    return 1 if PROBLEMS else 0


if __name__ == "__main__":
    raise SystemExit(main())
