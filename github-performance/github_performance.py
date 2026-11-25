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
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
import pr_classify  # noqa: E402 - needs the sys.path line above
TEMPLATE = SCRIPT_DIR / "report_template.html"
DEFAULT_REPORTS_DIR = SCRIPT_DIR.parent / "reports" / "github-performance"
CACHE_DIR = SCRIPT_DIR / ".cache"
API = "https://api.github.com"

# Version of the raw data this script fetches and stores. Bump it whenever the
# queries or the stored layout change: stores and reports with another version
# are then fetched again instead of reused. Metrics and PR classification are
# recomputed from the raw data on every run, so changing those needs no bump.
DATA_VERSION = 2
BASELINE_VERSION = 1        # layout of the cached org baselines
PEOPLE_CACHE_DIR = CACHE_DIR / "people"
# Data fetched earlier can be stale near its fetch time (PRs merge later, reviews
# arrive): re-fetch this many days before the last fetch on top of the new days.
REFRESH_DAYS = 14
DEFAULT_DAYS = 365
MAX_DAYS = 730
SEARCH_CAP = 1000           # GitHub search never returns more than this per query
PR_PAGE = 25                # PRs per page; each carries up to 50 reviews
BASELINE_BATCH = 4          # people per aliased baseline query (x5 searches each)
BASELINE_WORKERS = 4
BASELINE_CACHE_DAYS = 7
# Someone whose account was created more than this far into the window is a new
# joiner: they are compared over [join date, end] only, with members who were
# already in the org by then (enterprise accounts are created on joining).
JOIN_GRACE_DAYS = 14
# "Similar tenure" = members whose time in the company is within these factors of
# the person's (2 months -> 1 to 3 months). They are compared per 30 days of
# their own time in the window, so a longer tenure does not mean bigger totals.
TENURE_LOW, TENURE_HIGH = 0.5, 1.5
RATE_DAYS = 30
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
  id number title url state isDraft createdAt mergedAt closedAt
  additions deletions changedFiles baseRefName headRefName body
  repository { nameWithOwner }
  files(first:100) { nodes { path additions deletions } }
  author { login }
  mergedBy { login }
  reviewDecision
  commits { totalCount }
  comments { totalCount }
  reviews(first:50) { totalCount nodes { author { login __typename } state submittedAt comments { totalCount } } }
"""

REVIEWED_FIELDS = """
  number url title createdAt additions deletions changedFiles headRefName
  author { login }
  repository { nameWithOwner }
  files(first:60) { nodes { path additions deletions } }
  reviews(first:100, author:$login) { nodes { state submittedAt body comments { totalCount } } }
"""

OPEN_PRS_QUERY = "query($ids:[ID!]!){ nodes(ids:$ids){ ... on PullRequest { %s } } }" % PR_FIELDS

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
    membersWithRole(first:100, after:$after) { pageInfo { hasNextPage endCursor } nodes { login createdAt } }
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
    return count_batch_ranges(gh, [(login, start, end) for login in logins], org)


def count_batch_ranges(gh: GitHub, items: list[tuple[str, date, date]], org: str) -> dict[str, dict]:
    """Like count_batch, but every login has its own date range."""
    logins = [login for login, _, _ in items]
    parts = []
    for i, (login, start, end) in enumerate(items):
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
    {"key": "areas_worked", "group": "Delivery", "label": "Areas worked in", "unit": "count", "better": None,
     "desc": "Distinct areas (frontend, backend, infrastructure...) that are the main area of at least one PR."},
    {"key": "feature_pr_share", "group": "Delivery", "label": "Feature PRs", "unit": "pct", "better": None,
     "desc": "Share of PRs classified as a feature (new behaviour). See 'What kind of work'."},
    {"key": "bugfix_pr_share", "group": "Delivery", "label": "Bug-fix PRs", "unit": "pct", "better": None,
     "desc": "Share of PRs classified as a bug fix."},
    {"key": "dependency_pr_share", "group": "Delivery", "label": "Dependency / upgrade PRs", "unit": "pct",
     "better": None, "desc": "Share of PRs that upgrade libraries, frameworks or runtimes."},
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
    {"key": "tests_included_share", "group": "Quality & process", "label": "PRs that include tests",
     "unit": "pct", "better": "up",
     "desc": "Share of code PRs (feature, bug fix, refactor, performance) that also change test files."},
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
     "better": "down", "desc": "Approvals on PRs where the person wrote no comment in any of their reviews."},
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


# How to read each metric: when a value is good, bad, or only context. Shown in
# the report's tooltips next to the definition.
METRIC_READ = {
    "prs_opened": "Higher usually means more shipped work, but many tiny PRs inflate it. Read it with PR size.",
    "prs_merged": "Higher is better: work that actually landed. Far below 'PRs opened' means work is stalling.",
    "merge_rate": "Near 90-100% is healthy. Low means many PRs are abandoned or rejected; ask why.",
    "prs_open_now": "Context only. A few is normal; many old open PRs suggest blocked or forgotten work.",
    "commits": "More is not better on its own: commit habits differ (squash merges count as one).",
    "commits_in_prs": "Context only. Shows iteration before merge; very high per PR can mean rework.",
    "repos_contributed": "Higher means broader reach. Low is fine for someone focused on one service.",
    "areas_worked": "Context only. High = generalist, low = specialist. Neither is better.",
    "feature_pr_share": "Context only. Depends on the team's roadmap phase.",
    "bugfix_pr_share": "Context only. High can mean owning support / stability work, not poor quality.",
    "dependency_pr_share": "Context only. High often means owning upgrades and security fixes: needed but less visible.",
    "lines_added": "Volume only. Never read more lines as better work.",
    "lines_deleted": "Volume only. Deleting code is often a good thing.",
    "pr_size_median": "Lower is better: small PRs get reviewed faster and break less. Above ~400 lines is large.",
    "small_pr_share": "Higher is better. Above ~60% is a good habit.",
    "cycle_time_median_h": "Lower is better: work reaches users sooner. Days, not weeks, is healthy; it also depends on reviewers.",
    "cycle_time_p75_h": "Lower is better. A long tail means some PRs wait a long time; look at which ones.",
    "time_to_first_review_median_h": "Context only: mostly how fast the team reviews, not this person.",
    "reviews_received_per_pr": "Context only. 1-2 is typical; 0 means PRs merge without review.",
    "changes_requested_share": "Lower is usually better, but some requested changes are normal on hard work.",
    "self_merged_unapproved": "Lower is better. Should be near 0 unless the team allows self-approval.",
    "described_pr_share": "Higher is better: reviewers and future readers know why the change exists.",
    "tests_included_share": "Higher is better: code changes come with tests. Below ~50% is worth a look.",
    "commits_per_pr_median": "Context only. 1-5 is typical; very high can mean rework or long-lived branches.",
    "reviews_given": "Higher means more help to teammates. Compare with 'Comments per review' for depth.",
    "prs_reviewed": "Higher is better: carrying a share of the team's review load.",
    "approvals": "Context only. Teams that self-approve or need no approval make this meaningless.",
    "changes_requested": "Context only. Some change requests show careful review; none at all can mean rubber-stamping.",
    "review_comments": "Higher usually means deeper reviews. AI-generated comments can inflate it.",
    "comments_per_review": "Higher means more thorough reviews. Near 0 with many reviews suggests quick approvals.",
    "silent_approval_share": "Lower is better. Near 100% means approving without ever commenting.",
    "review_turnaround_median_h": "Lower is better: teammates are unblocked sooner. Under a day is good.",
    "lines_reviewed": "Higher means more code read for others. Volume only, not depth.",
    "review_ratio": "Around 1 or above is healthy: they review at least as much as they ship. Far below 1 means others carry their reviews.",
    "authors_helped": "Higher means helping more people, not only one close teammate.",
    "repos_reviewed": "Higher means wider knowledge of the codebase.",
    "prs_commented": "Higher means more engagement in others' work.",
    "reviewers_received": "Context only. Very low can mean one person reviews everything (a bottleneck).",
    "issues_opened": "Context only. Most teams track work in JIRA, so 0 is normal.",
    "total_activity": "Higher means more visible GitHub activity. Says nothing about value.",
    "active_days": "Higher means steady activity. Low can mean leave, meetings, design or on-call work.",
    "active_weeks": "Higher is steadier. Gaps are normal for holidays and non-coding work.",
    "longest_streak_days": "Context only. Long streaks without weekends can mean overwork.",
    "weekend_share": "Context only. Persistently high can mean too much load; a few % is normal.",
}
for _m in METRIC_DEFS:
    _m["read"] = METRIC_READ.get(_m["key"], "")

# --------------------------------------------------------------------------- #
# per-person collection
# --------------------------------------------------------------------------- #
def store_path(org: str, login: str) -> Path:
    return PEOPLE_CACHE_DIR / org.lower() / f"{safe_name(login.lower())}.json"


def load_store(org: str, login: str, quiet: bool) -> dict | None:
    path = store_path(org, login)
    if not path.is_file():
        return None
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        warn(f"{login}: unreadable data store {path.name} ({exc}); fetching everything again")
        return None
    if store.get("data_version") != DATA_VERSION:
        log(f"  {login}: stored data is version {store.get('data_version')}, this script needs "
            f"{DATA_VERSION}: fetching everything again", quiet=quiet)
        return None
    return store


def day_ranges(days: set[date]) -> list[tuple[date, date]]:
    """Sorted days -> contiguous (first, last) ranges."""
    out: list[list[date]] = []
    for d in sorted(days):
        if out and d == out[-1][1] + timedelta(days=1):
            out[-1][1] = d
        else:
            out.append([d, d])
    return [(x, y) for x, y in out]


def days_in(first: date, last: date) -> set[date]:
    return {first + timedelta(days=i) for i in range((last - first).days + 1)}


def plan_fetch(store: dict | None, start: datetime, end: datetime) -> list[tuple[date, date]]:
    """The date ranges still to fetch: the requested days no store covers, plus
    the last REFRESH_DAYS before the previous fetch when time has passed since."""
    wanted = days_in(start.date(), end.date())
    if not store:
        return day_ranges(wanted)
    covered: set[date] = set()
    for first, last in store["covered"]:
        covered |= days_in(date.fromisoformat(first), date.fromisoformat(last))
    needed = wanted - covered
    fetched_at = ts(store["fetched_at"])
    if end - fetched_at > timedelta(hours=1):
        needed |= wanted & days_in(fetched_at.date() - timedelta(days=REFRESH_DAYS), end.date())
    return day_ranges(needed)


def merge_by(old: list[dict], new: list[dict], key) -> list[dict]:
    merged = {key(x): x for x in old}
    merged.update({key(x): x for x in new})  # the newer copy wins
    return list(merged.values())


def fetch_person(gh: GitHub, login: str, org: str, start: datetime, end: datetime, quiet: bool,
                 refresh: bool = False) -> dict | None:
    user = gh.query(PROFILE_QUERY, {"login": login}).get("user")
    if not user:
        error(f"{login}: no such GitHub user (or not visible to this token)")
        return None
    login = user["login"]  # canonical casing
    calls_before = gh.calls
    store = None if refresh else load_store(org, login, quiet)
    ranges = plan_fetch(store, start, end)
    had_store = store is not None
    if store:
        covered = ", ".join(f"{x}..{y}" for x, y in store["covered"])
        todo = ", ".join(f"{x}..{y}" for x, y in ranges) or "nothing"
        log(f"  {login}: stored data v{DATA_VERSION} covers {covered}; fetching {todo}", quiet=quiet)
    store = store or {"data_version": DATA_VERSION, "org": org, "login": login, "covered": [],
                      "pull_requests": [], "reviewed_prs": [], "commits": []}

    for a, b in ranges:
        authored = search_prs(gh, f"org:{org} is:pr author:{login}", "created", a, b, PR_FIELDS)
        reviewed = search_prs(gh, f"org:{org} is:pr reviewed-by:{login} -author:{login}", "updated", a, b,
                              REVIEWED_FIELDS, {"login": login}, page_size=50)
        try:
            commits = search_commits(gh, login, org, a, b)
        except GitHubError as exc:
            error(f"{login}: commit search {a}..{b} failed, so commit metrics are incomplete: {exc}")
            commits = []
        store["pull_requests"] = merge_by(store["pull_requests"], [flatten_pr(n) for n in authored],
                                          lambda p: p["url"])
        store["reviewed_prs"] = merge_by(store["reviewed_prs"], [flatten_reviewed(n) for n in reviewed],
                                         lambda p: p["url"])
        store["commits"] = merge_by(store["commits"], commits, lambda c: c["sha"])
        debug(f"{login} {a}..{b}: {len(authored)} PRs, {len(reviewed)} reviewed PRs, {len(commits)} commits")

    # Open PRs from earlier fetches may have merged or closed since: refresh them by id.
    first_fetched = min((a for a, _ in ranges), default=end.date() + timedelta(days=1))
    stale = [p["id"] for p in store["pull_requests"]
             if p["state"] == "OPEN" and p.get("id") and date.fromisoformat(p["createdAt"][:10]) < first_fetched]
    for i in range(0, len(stale), 50):
        nodes = gh.query(OPEN_PRS_QUERY, {"ids": stale[i:i + 50]}).get("nodes") or []
        store["pull_requests"] = merge_by(store["pull_requests"], [flatten_pr(n) for n in nodes if n],
                                          lambda p: p["url"])

    covered = set()
    for first, last in store["covered"]:
        covered |= days_in(date.fromisoformat(first), date.fromisoformat(last))
    for a, b in ranges:
        covered |= days_in(a, b)
    store["covered"] = [[x.isoformat(), y.isoformat()] for x, y in day_ranges(covered)]
    if ranges or stale:
        store["fetched_at"] = iso(end)
    store.setdefault("fetched_at", iso(end))
    path = store_path(org, login)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store), encoding="utf-8")

    counts = count_batch(gh, [login], org, start.date(), end.date())[login]
    person = person_from_store(store, user, counts, start, end)
    person["fetch"] = {"ranges": [[x.isoformat(), y.isoformat()] for x, y in ranges],
                       "open_prs_refreshed": len(stale), "api_calls": gh.calls - calls_before,
                       "reused_store": had_store}
    log(f"  {login}: {len(person['pull_requests'])} PR(s) opened, {len(person['reviews'])} review(s) on "
        f"{len({r['pr_url'] for r in person['reviews']})} PR(s) by others, {len(person['commits'])} commit(s) "
        f"({person['fetch']['api_calls']} API calls)", quiet=quiet)
    return person


def person_from_store(store: dict, profile: dict, counts: dict, start: datetime, end: datetime) -> dict:
    """The window's slice of the stored raw data, with every PR classified afresh
    (so pr_classify.py changes apply without fetching again)."""
    lo, hi = iso(start), iso(end)
    prs = [dict(p) for p in store["pull_requests"] if lo <= p["createdAt"] <= hi]
    for p in prs:
        p.update(pr_classify.classify_pr(p["repository"], p.get("title") or "", p.get("headRefName"),
                                         p.get("files") or [], p.get("changedFiles")))
    prs.sort(key=lambda p: p["createdAt"], reverse=True)
    commits = sorted((c for c in store["commits"] if c.get("date") and start <= ts(c["date"]) <= end),
                     key=lambda c: c["date"], reverse=True)
    return {
        "data_version": DATA_VERSION,
        "profile": profile,
        "counts": counts,
        "pull_requests": prs,
        "reviews": review_rows(store["reviewed_prs"], start, end),
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
    pr["files"] = [{"path": f["path"], "additions": f.get("additions", 0), "deletions": f.get("deletions", 0)}
                   for f in (pr.pop("files", None) or {}).get("nodes") or [] if f]
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


def flatten_reviewed(pr: dict) -> dict:
    """A PR by someone else with every review the person left on it (any date:
    the window is applied when the report is built)."""
    return {
        "url": pr.get("url"), "number": pr.get("number"), "title": pr.get("title"),
        "createdAt": pr.get("createdAt"), "additions": pr.get("additions") or 0,
        "deletions": pr.get("deletions") or 0, "changedFiles": pr.get("changedFiles"),
        "headRefName": pr.get("headRefName"),
        "author": (pr.get("author") or {}).get("login"),
        "repository": (pr.get("repository") or {}).get("nameWithOwner"),
        "files": [{"path": f["path"], "additions": f.get("additions", 0), "deletions": f.get("deletions", 0)}
                  for f in (pr.get("files") or {}).get("nodes") or [] if f],
        "reviews": [{"state": r.get("state"), "submittedAt": r.get("submittedAt"),
                     "body_chars": len((r.get("body") or "").strip()),
                     "comments": (r.get("comments") or {}).get("totalCount", 0)}
                    for r in (pr.get("reviews") or {}).get("nodes") or [] if r],
    }


def review_rows(reviewed_prs: list[dict], start: datetime, end: datetime) -> list[dict]:
    """One row per review the person submitted inside the window."""
    rows = []
    for pr in reviewed_prs:
        kind = None
        for review in pr["reviews"]:
            submitted = ts(review.get("submittedAt"))
            if not submitted or not (start <= submitted <= end):
                continue
            kind = kind or pr_classify.classify_pr(pr["repository"], pr.get("title") or "", pr.get("headRefName"),
                                                   pr.get("files") or [], pr.get("changedFiles"))
            rows.append({
                "state": review.get("state"),
                "submittedAt": review.get("submittedAt"),
                "body_chars": review.get("body_chars", 0),
                "comments": review.get("comments", 0),
                "pr_number": pr.get("number"),
                "pr_url": pr.get("url"),
                "pr_title": pr.get("title"),
                "pr_createdAt": pr.get("createdAt"),
                "pr_author": pr.get("author"),
                "pr_size": pr["additions"] + pr["deletions"],
                "repository": pr.get("repository"),
                "pr_area": kind["area"],
                "pr_type": kind["type"],
            })
    rows.sort(key=lambda r: r["submittedAt"], reverse=True)
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


CODE_TYPES = {"Feature", "Bug fix", "Refactor & cleanup", "Performance"}


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
    # GitHub records "comment, then approve" as two reviews: what counts is whether
    # the person wrote anything on the PR, in any of their reviews.
    talked_on = {r["pr_url"] for r in given if r["comments"] or r["body_chars"]}
    first_by_pr: dict[str, dict] = {}
    for r in sorted(given, key=lambda r: r["submittedAt"]):
        first_by_pr.setdefault(r["pr_url"], r)
    repos = {p["repository"] for p in prs} | {c["repository"] for c in person["commits"]}

    code_prs = [p for p in prs if p.get("type") in CODE_TYPES]
    kinds = Counter(p.get("type") for p in prs)
    m = calendar_metrics(person["calendar"])
    m.update({
        "areas_worked": len({p.get("area") for p in prs if p.get("area") not in (None, "Other")}),
        "feature_pr_share": ratio(kinds.get("Feature", 0), len(prs)),
        "bugfix_pr_share": ratio(kinds.get("Bug fix", 0), len(prs)),
        "dependency_pr_share": ratio(kinds.get("Dependency & platform upgrade", 0), len(prs)),
        "tests_included_share": ratio(sum(1 for p in code_prs if p.get("touches_tests")), len(code_prs)),
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
        "silent_approval_share": ratio(sum(1 for r in approvals if r["pr_url"] not in talked_on), len(approvals)),
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
        "work_mix": work_mix(person, months),
        "first_activity": active_dates[0] if active_dates else None,
        "last_activity": active_dates[-1] if active_dates else None,
        "monthly": list(monthly.values()),
        "pr_sizes": [{"bucket": label, "prs": size_counts.get(label, 0)} for label, _ in buckets],
        "repos": repo_rows,
        "review_states": [{"state": s, "count": c} for s, c in states.most_common()],
        "top_authors_reviewed": [{"login": a, "reviews": c} for a, c in authors.most_common(15)],
        "top_reviewers": [{"login": a, "reviews": c} for a, c in reviewers.most_common(15)],
    }


def work_mix(person: dict, months: list[str]) -> dict[str, Any]:
    """Detailed statistics on what kind of PRs the person opens and reviews."""
    prs = person["pull_requests"]
    total_weight: Counter = Counter()
    for p in prs:
        total_weight.update(p.get("areas") or {})

    def stats(group: list[dict]) -> dict[str, Any]:
        merged = [p for p in group if p["state"] == "MERGED"]
        return {
            "prs": len(group), "merged": len(merged),
            "additions": sum(p["additions"] for p in group), "deletions": sum(p["deletions"] for p in group),
            "files": sum(p.get("changedFiles") or 0 for p in group),
            "size_median": median(p["additions"] + p["deletions"] for p in group),
            "cycle_median_h": median(hours(p["createdAt"], p["mergedAt"]) for p in merged),
            "repos": len({p["repository"] for p in group}),
            "with_tests": sum(1 for p in group if p.get("touches_tests")),
        }

    by_area: dict[str, list[dict]] = defaultdict(list)
    by_type: dict[str, list[dict]] = defaultdict(list)
    for p in prs:
        by_area[p.get("area", "Other")].append(p)
        by_type[p.get("type", "Other change")].append(p)
    touching = Counter(a for p in prs for a in (p.get("areas") or {}))
    weight_sum = sum(total_weight.values()) or 1
    areas = [{"area": a, **stats(by_area[a]), "prs_touching": touching.get(a, 0),
              "line_share": round(total_weight.get(a, 0) / weight_sum, 3)}
             for a in pr_classify.AREAS if by_area.get(a) or touching.get(a)]
    areas.sort(key=lambda r: (-r["prs"], -r["prs_touching"]))
    types = [{"type": t, **stats(by_type[t])} for t in pr_classify.TYPES if by_type.get(t)]
    types.sort(key=lambda r: -r["prs"])

    area_monthly = {m: Counter() for m in months}
    type_monthly = {m: Counter() for m in months}
    for p in prs:
        month = p["createdAt"][:7]
        if month in area_monthly:
            area_monthly[month][p.get("area", "Other")] += 1
            type_monthly[month][p.get("type", "Other change")] += 1
    cross = Counter((p.get("type", "Other change"), p.get("area", "Other")) for p in prs)

    dirs: Counter = Counter()
    dir_prs: dict[str, set] = defaultdict(set)
    for p in prs:
        for f in p.get("files") or []:
            parts = f["path"].split("/")
            key = f"{(p['repository'] or '').split('/')[-1]}/" + "/".join(parts[:2] if len(parts) > 2 else parts[:1])
            dirs[key] += pr_classify.file_weight(f["path"], f["additions"], f["deletions"])
            dir_prs[key].add(p["url"])
    exts: Counter = Counter()
    ext_areas: dict[str, Counter] = defaultdict(Counter)
    for p in prs:
        for f in p.get("files") or []:
            name = f["path"].rsplit("/", 1)[-1]
            ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else name
            exts[ext] += 1
            ext_areas[ext][pr_classify.file_area(f["path"], p["repository"])] += 1

    reviewed: dict[str, dict] = {}
    for r in person["reviews"]:
        reviewed.setdefault(r["pr_url"], r)
    review_areas = Counter(r.get("pr_area", "Other") for r in reviewed.values())
    review_types = Counter(r.get("pr_type", "Other change") for r in reviewed.values())
    review_lines = Counter()
    for r in reviewed.values():
        review_lines[r.get("pr_area", "Other")] += r["pr_size"]

    return {
        "areas": areas,
        "types": types,
        "area_monthly": [{"month": m, **dict(c)} for m, c in area_monthly.items()],
        "type_monthly": [{"month": m, **dict(c)} for m, c in type_monthly.items()],
        "type_by_area": [{"type": t, "area": a, "prs": n} for (t, a), n in cross.most_common()],
        "top_dirs": [{"dir": d, "lines": w, "prs": len(dir_prs[d])} for d, w in dirs.most_common(15)],
        "file_types": [{"ext": e, "files": n, "areas": dict(ext_areas[e].most_common())}
                       for e, n in exts.most_common(15)],
        "review_areas": [{"area": a, "prs": n, "lines": review_lines[a]} for a, n in review_areas.most_common()],
        "review_types": [{"type": t, "prs": n} for t, n in review_types.most_common()],
        "partial_prs": sum(1 for p in prs if p.get("files_partial")),
        "type_sources": dict(Counter(p.get("type_source", "none") for p in prs)),
    }


# --------------------------------------------------------------------------- #
# organisation baseline
# --------------------------------------------------------------------------- #
def org_members(gh: GitHub, org: str) -> dict[str, str]:
    """login -> account creation time, which is the join date for enterprise accounts."""
    nodes = paginate(gh, MEMBERS_QUERY, {"org": org}, ["organization", "membersWithRole"])
    return {n["login"]: n.get("createdAt") for n in nodes}


def team_members(gh: GitHub, org: str, team: str) -> list[str]:
    data = gh.query(TEAM_QUERY, {"org": org, "team": team, "after": None})
    if not (data.get("organization") or {}).get("team"):
        error(f"team {team!r} not found in {org} (use the slug from the team's URL)")
        return []
    return [n["login"] for n in paginate(gh, TEAM_QUERY, {"org": org, "team": team},
                                         ["organization", "team", "members"])]


def build_baseline(gh: GitHub, org: str, since: datetime, end: datetime, label: str, sample: int | None,
                   refresh: bool, quiet: bool) -> dict[str, Any]:
    """COUNT_SEARCHES over [since, end] for every org member, plus their join dates.
    Cached per window (label) for BASELINE_CACHE_DAYS."""
    CACHE_DIR.mkdir(exist_ok=True)
    cache = CACHE_DIR / f"org_baseline_{org}_{label}_{sample or 'all'}.json"
    if cache.is_file() and not refresh:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        age = datetime.now(timezone.utc) - ts(cached["generated_at"])
        if age < timedelta(days=BASELINE_CACHE_DAYS) and cached.get("baseline_version", 1) == BASELINE_VERSION:
            if "joined" not in cached:  # cache from before join dates were kept: add them (cheap)
                log("org baseline: adding member join dates to the cached baseline...", quiet=quiet)
                cached["joined"] = org_members(gh, org)
                cache.write_text(json.dumps(cached), encoding="utf-8")
            log(f"org baseline ({label}): using the cache from {cached['generated_at'][:10]} "
                f"({len(cached['members'])} members; --refresh-baseline rebuilds it)", quiet=quiet)
            return cached

    log(f"org baseline ({label}): listing members of {org}...", quiet=quiet)
    joined = org_members(gh, org)
    members = list(joined)
    if sample and sample < len(members):
        members = random.Random(42).sample(members, sample)
    batches = [members[i:i + BASELINE_BATCH] for i in range(0, len(members), BASELINE_BATCH)]
    log(f"org baseline ({label}): counting activity of {len(members)} member(s) from {since.date()} "
        f"in {len(batches)} request(s)...", quiet=quiet)
    results: dict[str, dict] = {}
    failed = 0
    a, b = since.date(), end.date()
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
    baseline = {"generated_at": iso(datetime.now(timezone.utc)), "org": org, "from": iso(since), "to": iso(end),
                "sampled": bool(sample), "members": results, "baseline_version": BASELINE_VERSION,
                "joined": {login: joined.get(login) for login in members}}
    cache.write_text(json.dumps(baseline), encoding="utf-8")
    return baseline


def baseline_stats(baseline: dict, since: datetime) -> dict[str, Any]:
    """Anonymous distributions over the members who were already in the org at
    `since` (so they had the whole window too) and had any PR activity in it."""
    members = baseline.get("members") or {}
    joined = baseline.get("joined") or {}
    eligible = [v for login, v in members.items()
                if not joined.get(login) or ts(joined[login]) <= since + timedelta(days=JOIN_GRACE_DAYS)]
    active = [v for v in eligible if v["prs_opened"] + v["prs_reviewed"] + v["prs_commented"] > 0]
    stats: dict[str, Any] = {"generated_at": baseline["generated_at"], "sampled": baseline["sampled"],
                             "from": iso(since), "to": baseline["to"], "members": len(members),
                             "eligible_members": len(eligible), "active_members": len(active), "metrics": {}}
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
def comparison_start(person: dict, start: datetime) -> datetime:
    """Where this person's comparison window starts: the window start, or the day
    their account was created when that is well inside the window."""
    created = ts(person["profile"].get("createdAt"))
    if created and created > start + timedelta(days=JOIN_GRACE_DAYS):
        return created.replace(hour=0, minute=0, second=0, microsecond=0)
    return start


def slice_person(person: dict, since: datetime, end: datetime, counts: dict) -> dict:
    """The person's raw data cut down to [since, end], to compare them with a
    newer colleague over the same days."""
    cut = iso(since)
    sliced = {**person,
              "pull_requests": [p for p in person["pull_requests"] if p["createdAt"] >= cut],
              "reviews": [r for r in person["reviews"] if r["submittedAt"] >= cut],
              "commits": [c for c in person["commits"] if c["date"] and ts(c["date"]) >= since],
              "counts": counts}
    sliced["calendar"] = build_calendar(sliced, since, end)
    return sliced


def build_windows(gh: GitHub, org: str, people: dict[str, dict], start: datetime, end: datetime,
                  args: argparse.Namespace) -> dict[datetime, dict]:
    """For every distinct comparison start: the cohort members who were around for
    the whole window, with their metrics over it, and the org distribution."""
    windows: dict[datetime, dict] = {}
    for since in sorted({p["comparison_since"] for p in people.values()}):
        eligible = [p for p in people.values() if p["comparison_since"] <= since]
        logins = [p["profile"]["login"] for p in eligible]
        if since == start:
            counts = {p["profile"]["login"]: p["counts"] for p in eligible}
        else:
            log(f"comparison window from {since.date()}: {len(eligible)} cohort member(s)", quiet=args.quiet)
            counts = {}
            for i in range(0, len(logins), BASELINE_BATCH):
                counts.update(count_batch(gh, logins[i:i + BASELINE_BATCH], org, since.date(), end.date()))
        cohort = {}
        for p in eligible:
            login = p["profile"]["login"]
            metrics = p["metrics"] if since == start else person_metrics(slice_person(p, since, end, counts[login]))
            cohort[login] = {"metrics": metrics, "counts": counts[login]}

        org_stats = None
        if not args.no_baseline:
            label = f"{(end - start).days}d" if since == start else f"since-{since.date()}"
            try:
                baseline = build_baseline(gh, org, since, end, label, args.baseline_sample,
                                          args.refresh_baseline, args.quiet)
                org_stats = baseline_stats(baseline, since)
                org_stats["file"] = "org_baseline.json" if since == start else f"org_baseline_{since.date()}.json"
            except GitHubError as exc:
                error(f"org baseline from {since.date()} failed, so org percentiles are missing: {exc}")
        windows[since] = {"cohort": cohort, "org": org_stats}
    return windows


def compare(people: dict[str, dict], windows: dict[datetime, dict], start: datetime, end: datetime) -> None:
    for login, person in people.items():
        since = person["comparison_since"]
        cohort, org_stats = windows[since]["cohort"], windows[since]["org"]
        own = cohort[login]
        ranks: dict[str, Any] = {}
        groups: dict[str, list[float]] = defaultdict(list)
        cohort_median = {}
        for mdef in METRIC_DEFS:
            key = mdef["key"]
            population = [c["metrics"].get(key) for c in cohort.values()]
            cohort_median[key] = median(population)
            entry: dict[str, Any] = {}
            if len(cohort) > 1:
                entry["cohort_pct"] = percentile_rank(own["metrics"].get(key), population)
            if org_stats and key in org_stats["metrics"]:
                entry["org_value"] = own["counts"].get(key)
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
        person["comparison"] = {
            "from": iso(since), "to": iso(end), "days": (end - since).days,
            "reason": "full window" if since == start else "joined during the window",
            "joined": person["profile"].get("createdAt"),
            "cohort": sorted(cohort), "cohort_median": cohort_median,
            "org_file": org_stats["file"] if org_stats else None,
            "org_eligible": org_stats["eligible_members"] if org_stats else None,
            "org_active": org_stats["active_members"] if org_stats else None,
        }


def tenure_window(created: datetime | None, start: datetime, end: datetime) -> tuple[date, date, int]:
    """A member's own part of the report window: from joining (or the window
    start) to the end, and its length in days."""
    first = max(created, start) if created else start
    return first.date(), end.date(), max((end - first).days, 1)


def as_rates(counts: dict, days: int) -> dict[str, float | None]:
    rates = {k: round(counts[k] * RATE_DAYS / days, 2) for k in COUNT_SEARCHES}
    rates["review_ratio"] = counts.get("review_ratio")  # already a ratio
    return rates


def tenure_compare(gh: GitHub, org: str, people: dict[str, dict], start: datetime, end: datetime,
                   args: argparse.Namespace) -> None:
    """For every person: the org members with a similar tenure (TENURE_LOW..HIGH x
    theirs), everyone measured per RATE_DAYS over their own time in the window."""
    label = f"{(end - start).days}d"
    full = build_baseline(gh, org, start, end, label, args.baseline_sample, False, args.quiet)
    joined, full_counts = full.get("joined") or {}, full.get("members") or {}
    cache_path = CACHE_DIR / f"member_counts_{org}.json"
    cache = {}
    if cache_path.is_file() and not args.refresh_baseline:
        cache = json.loads(cache_path.read_text(encoding="utf-8")).get("entries", {})

    def fresh(login: str, first: date) -> dict | None:
        entry = cache.get(login)
        if entry and entry["from"] == first.isoformat() and \
                datetime.now(timezone.utc) - ts(entry["fetched_at"]) < timedelta(days=BASELINE_CACHE_DAYS):
            return entry
        return None

    plans: dict[str, dict] = {}
    todo: dict[str, tuple[date, date]] = {}
    for login, person in people.items():
        created = ts(person["profile"].get("createdAt"))
        if not created:
            continue
        tenure = (end - created).days
        lo, hi = tenure * TENURE_LOW, tenure * TENURE_HIGH
        peers = [m for m, j in joined.items()
                 if j and lo <= (end - ts(j)).days <= hi and not same(m, login)]
        plans[login] = {"tenure_days": tenure, "range_days": [round(lo), round(hi)], "peers": peers}
        for m in peers + [login]:
            first, last, _ = tenure_window(ts(joined.get(m)) if m != login else created, start, end)
            if first == start.date() and m in full_counts:
                continue  # in the company for the whole window: the baseline counts are exact
            if not fresh(m, first):
                todo[m] = (first, last)

    if todo:
        log(f"similar tenure: counting {len(todo)} member(s) over their own time in the window...",
            quiet=args.quiet)
        items = [(m, a, b) for m, (a, b) in todo.items()]
        batches = [items[i:i + BASELINE_BATCH] for i in range(0, len(items), BASELINE_BATCH)]
        now = iso(datetime.now(timezone.utc))
        with ThreadPoolExecutor(BASELINE_WORKERS) as pool:
            futures = {pool.submit(count_batch_ranges, gh, batch, org): batch for batch in batches}
            for fut in as_completed(futures):
                try:
                    for m, counts in fut.result().items():
                        a, b = todo[m]
                        cache[m] = {"from": a.isoformat(), "to": b.isoformat(), "fetched_at": now, "counts": counts}
                except GitHubError as exc:
                    warn(f"similar tenure: a batch of {len(futures[fut])} member(s) failed and is left out ({exc})")
        cache_path.write_text(json.dumps({"baseline_version": BASELINE_VERSION, "entries": cache}), encoding="utf-8")

    def member_rates(m: str, created: datetime | None) -> dict | None:
        first, _, days = tenure_window(created, start, end)
        if first == start.date() and m in full_counts:
            counts = full_counts[m]
        else:
            entry = cache.get(m)
            if not entry:
                return None
            counts = entry["counts"]
            days = max((date.fromisoformat(entry["to"]) - date.fromisoformat(entry["from"])).days, 1)
        return {"active": counts["prs_opened"] + counts["prs_reviewed"] + counts["prs_commented"] > 0,
                "days": days, "rates": as_rates(counts, days)}

    for login, plan in plans.items():
        person = people[login]
        own = member_rates(login, ts(person["profile"].get("createdAt")))
        rows = [r for r in (member_rates(m, ts(joined.get(m))) for m in plan["peers"]) if r]
        active = [r for r in rows if r["active"]]
        metrics = {}
        for mdef in METRIC_DEFS:
            key = mdef["key"]
            if not mdef.get("org") or not own:
                continue
            values = sorted(r["rates"][key] for r in active if r["rates"].get(key) is not None)
            value = own["rates"].get(key)
            pct = percentile_rank(value, values)
            metrics[key] = {
                "value": value, "pct": pct,
                "score": None if pct is None or not mdef["better"] else
                         (pct if mdef["better"] == "up" else round(100 - pct, 1)),
                "values": values,
                "p25": quantile(values, 0.25), "median": quantile(values, 0.5), "p75": quantile(values, 0.75),
                "p90": quantile(values, 0.9), "max": values[-1] if values else None,
            }
        person["tenure"] = {
            "tenure_days": plan["tenure_days"], "range_days": plan["range_days"],
            "low": TENURE_LOW, "high": TENURE_HIGH, "rate_days": RATE_DAYS,
            "own_days": own["days"] if own else None,
            "members": len(rows), "active_members": len(active), "metrics": metrics,
        }
        log(f"{login}: tenure {plan['tenure_days']} days -> {len(active)} active member(s) with "
            f"{plan['range_days'][0]}-{plan['range_days'][1]} days", quiet=args.quiet)


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
    p.add_argument("--refresh-data", action="store_true",
                   help="ignore the stored per-person data and fetch the whole window again")
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
                    person = fetch_person(gh, login, org, start, end, args.quiet, args.refresh_data)
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

        for person in people.values():
            person["comparison_since"] = comparison_start(person, start)
            if person["comparison_since"] != start:
                log(f"{person['profile']['login']} joined on {person['comparison_since'].date()}: compared over "
                    f"the same {(end - person['comparison_since']).days} days only", quiet=args.quiet)
        windows = build_windows(gh, org, people, start, end, args)
        if not args.no_baseline:
            try:
                tenure_compare(gh, org, people, start, end, args)
            except GitHubError as exc:
                error(f"similar-tenure comparison failed: {exc}")
    except GitHubError as exc:
        log(f"ERROR: {exc}")
        if DEBUG:
            traceback.print_exc()
        return 2

    compare(people, windows, start, end)

    folder = args.out / f"{end.strftime('%Y%m%d-%H%M%S')}_v{DATA_VERSION}"
    data_dir = folder / "data"
    people_index = []
    for login, person in people.items():
        person.pop("comparison_since", None)
        name = f"users/{safe_name(login)}.json"
        write_json(data_dir / name, person)
        people_index.append({"login": login, "name": person["profile"].get("name"), "role": person["role"],
                             "file": name})
    write_json(data_dir / "summary.json", {
        "data_version": DATA_VERSION,
        "org": org_data, "from": iso(start), "to": iso(end), "days": days, "metric_defs": METRIC_DEFS,
        "categories": pr_classify.category_defs(),
        "people": [{"login": l, "name": p["profile"].get("name"), "role": p["role"], "metrics": p["metrics"],
                    "counts": p["counts"], "ranks": p["ranks"], "group_scores": p["group_scores"],
                    "comparison": p["comparison"], "tenure": p.get("tenure")} for l, p in people.items()],
    })
    org_files = []
    for window in windows.values():
        if window["org"]:
            write_json(data_dir / window["org"]["file"], {"data_version": DATA_VERSION, **window["org"]})
            org_files.append(window["org"]["file"])
    write_json(data_dir / "manifest.json", {
        "generated_at": iso(datetime.now(timezone.utc)), "org": org, "from": iso(start), "to": iso(end),
        "days": days, "people": people_index, "summary": "summary.json", "org_baselines": org_files,
        "problems": PROBLEMS, "api_calls": gh.calls, "data_version": DATA_VERSION,
        "run_cost": {
            "github_api_calls": gh.calls,
            "per_person": {l: p.get("fetch") for l, p in people.items()},
            # Nothing here calls an LLM: PR areas and types come from the regex rules
            # in pr_classify.py. Kept explicit so the report can say so.
            "ai": {"used": False, "model": None, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
                   "note": "PR classification is rule-based (pr_classify.py); no AI model is called."},
        },
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
