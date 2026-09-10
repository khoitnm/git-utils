#!/usr/bin/env python3
"""Summarize the features a developer shipped, from a local clone's git history.

Point the script at a local working copy. It reads the merge history of the
mainline branch (dev / develop / main / master) with plain git commands, so it
needs no GitHub API access at all - handy behind an org IP allow list:

  * every pull request merged into the mainline in the window is recovered from
    the merge commits ("Merge pull request #N from ..." and squash-merge
    "title (#N)" subjects);
  * a PR belongs to the author when one of the commits *on the branch* is
    theirs - the merge commit itself is authored by whoever clicked Merge, so it
    cannot be used for that;
  * JIRA keys are taken from the PR title, the branch name, then the commit
    messages, and every key is resolved in batches of 100 with one JIRA request;
  * PRs with no JIRA key anywhere get a summary built from their commit
    messages (--summarizer heuristic|ollama|anthropic).

The only network traffic is the JIRA lookup.

Examples
--------
  python github_pr_jira_report.py <path-to-clone> --authors trank
  python github_pr_jira_report.py <path-to-clone> --authors jdoe,"Jane Doe" --days 90
  python github_pr_jira_report.py . --list-authors

Credentials come from a .env file next to this script (or any parent folder):
JIRA_PAT and JIRA_BASE_URL (plus JIRA_EMAIL on Atlassian Cloud).

Anything that can make the report wrong is printed on the console: WARNING for
recoverable trouble, ERROR for problems that probably invalidate the output.
Errors are repeated in a summary at the end and make the exit code non-zero
(2 = the run could not start, 1 = the report was produced but has problems).
Add --debug for the git/HTTP call log and tracebacks.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency: pip install requests")

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DAYS = 365
# Feature branches can be older than the window: load some history before it so
# their commit messages are available for the summaries.
GRAPH_BUFFER_DAYS = 180
JIRA_BATCH = 100
# A PR with no key in its title/branch falls back to its commit messages, where a
# long-lived branch can drag in a dozen unrelated keys: keep only the first few.
COMMIT_TICKET_CAP = 5

MAINLINE_CANDIDATES = ("dev", "develop", "main", "master", "trunk")
# A merge from one of these is a branch sync (master -> dev), not a feature PR.
LONG_LIVED_RE = re.compile(r"^(dev|develop|main|master|trunk|(release|hotfix|support)[/-].*)$", re.I)

MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+) from (\S+)")
BARE_PATH_RE = re.compile(r"^\S*[/\\]\S*\.\w{1,6}$")
SQUASH_PR_RE = re.compile(r"^(?P<title>.+?)\s*\(#(?P<number>\d+)\)$")

JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d{1,6})\b")

# Prefixes that look like a JIRA key but are not. Use --project-keys for an
# explicit allow-list when your org has an unusual key.
NON_JIRA_PREFIXES = {
    "AES", "API", "ASCII", "BASE64", "BUGFIX", "CD", "CI", "CVE", "COVID", "DB",
    "DEV", "EC2", "EOL", "EU", "FEATURE", "FIX", "GB", "HOTFIX", "HTTP", "HTTPS",
    "ID", "IE", "ISO", "JDK", "JRE", "JSON", "K8S", "KB", "LOG4J", "MAIN",
    "MASTER", "MB", "MD5", "MR", "MS", "NET", "NOTE", "OWASP", "PART", "PR",
    "PROD", "QA", "RC", "RELEASE", "REVERT", "RFC", "RSA", "S3", "SHA", "SHA1",
    "SHA256", "SHA512", "SQL", "SSL", "STEP", "TLS", "TODO", "TOP", "US", "UTF",
    "UI", "UX", "V", "WIP", "XML",
}

# PR-template / merge boilerplate that should never end up in a summary.
BOILERPLATE_RE = re.compile(
    r"^\s*(#+\s*)?("
    r"description|summary|what( does this pr do)?\??|why\??|how (was this|to) test(ed)?|"
    r"testing|test plan|checklist|screenshots?|notes?|jira|ticket|type of change|"
    r"related (issues?|tickets?|prs?)|reviewers?|risk|rollback|definition of done|"
    r"co-authored-by:.*|signed-off-by:.*|merge branch .*|merge remote-tracking .*|conflicts:.*|# conflicts:.*"
    r")\s*:?\s*$",
    re.IGNORECASE,
)

# CSV column order. The lead columns are the ones you actually read in Excel;
# everything else is provenance kept to the right of them.
LEAD_FIELDS = ["summary", "jira_summaries", "pr_title", "branch", "merged_date",
               "commits_by_author"]
REST_FIELDS = ["jira_links", "ticket_source", "pr_url", "merged_by", "authors",
               "first_commit_date", "last_commit_date", "summary_source", "merge_sha",
               "jira_statuses", "jira_types"]
ROW_FIELDS = LEAD_FIELDS + REST_FIELDS


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
PROBLEMS: list[str] = []
DEBUG = False


def log(msg: str, *, quiet: bool = False) -> None:
    """stderr progress output, safe on a legacy (cp1252) Windows console."""
    if quiet:
        return
    try:
        print(msg, file=sys.stderr, flush=True)
    except UnicodeEncodeError:
        encoding = sys.stderr.encoding or "ascii"
        print(msg.encode(encoding, "replace").decode(encoding), file=sys.stderr, flush=True)


def warn(msg: str) -> None:
    """Something went wrong but the report can still be built. Always printed."""
    log(f"WARNING: {msg}")


def problem(msg: str, *, hint: str = "") -> None:
    """A problem that probably makes the report wrong: printed now, repeated in the
    summary at the end, and makes the process exit non-zero. Ignores --quiet."""
    PROBLEMS.append(msg)
    log(f"ERROR: {msg}")
    if hint:
        log(f"  -> {hint}")


def debug(msg: str) -> None:
    if DEBUG:
        log(f"debug: {msg}")


SECRET_RE = re.compile(r"PAT|TOKEN|KEY|SECRET|PASSWORD", re.IGNORECASE)


def mask(key: str, value: str) -> str:
    """Never print a credential: enough to tell two values apart, not to use one."""
    if not SECRET_RE.search(key):
        return value
    return f"<{len(value)} chars, starts {value[:4]!r}>" if value else "<empty>"


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def load_env() -> None:
    """Load .env files, nearest first (nearest wins).

    The .env *overrides* the process environment on purpose: it is the file the
    user edits, and a stale JIRA_PAT left in the machine environment silently
    shadowing a freshly issued one is a long afternoon. Any value it replaces is
    reported, so the shadowing is never invisible either way."""
    paths: list[Path] = []
    for base in (SCRIPT_DIR, Path.cwd().resolve()):
        for folder in (base, *base.parents):
            candidate = folder / ".env"
            if candidate.is_file() and candidate not in paths:
                paths.append(candidate)
    debug(f".env files, nearest first: {paths or 'none found'}")

    chosen: dict[str, tuple[str, Path]] = {}
    for path in paths:
        try:
            for key, value in read_env_file(path).items():
                chosen.setdefault(key, (value, path))  # nearest .env wins
        except OSError as exc:
            warn(f"cannot read {path}: {exc}")

    for key, (value, path) in chosen.items():
        previous = os.environ.get(key)
        if previous is not None and previous.strip() != value:
            warn(f"{key} is set in your environment ({mask(key, previous.strip())}) and "
                 f"differs from {path} ({mask(key, value)}): using the .env value")
            log(f"  -> the environment variable is stale; remove it with "
                f"`[Environment]::SetEnvironmentVariable('{key}', $null, 'User')` "
                f"and reopen the terminal")
        os.environ[key] = value
        debug(f"{key} <- {path} ({mask(key, value)})")


def env_first(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 3]
    if " " in cut[limit // 2:]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:-") + "..."


def parse_remote(url: str) -> tuple[str, str, str]:
    """-> (owner, repo, web_base) from an origin URL, all blank if unrecognizable."""
    text = (url or "").strip().rstrip("/")
    if not text:
        return "", "", ""
    if "://" in text:
        rest = text.split("://", 1)[1]
        host, _, path = rest.partition("/")
        host = host.split("@")[-1]
    elif "@" in text:  # git@github.com:owner/repo.git
        host, _, path = text.partition("@")[2].partition(":")
    else:
        host, path = "github.com", text
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return "", "", f"https://{host}" if host else ""
    return parts[0], parts[1].removesuffix(".git"), f"https://{host}"


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #
FIELD, RECORD = "\x1f", "\x1e"
LOG_FORMAT = RECORD + FIELD.join(["%H", "%P", "%an", "%ae", "%ad", "%cd", "%s", "%B"])


class GitError(RuntimeError):
    pass


class Commit:
    __slots__ = ("sha", "parents", "name", "email", "date", "merge_date", "subject", "message")

    def __init__(self, sha: str, parents: str, name: str, email: str,
                 date: str, merge_date: str, subject: str, message: str):
        self.sha = sha
        self.parents = parents.split() if parents else []
        self.name = name
        self.email = email
        self.date = date
        self.merge_date = merge_date
        self.subject = subject
        self.message = message

    @property
    def body(self) -> str:
        """The commit message without its subject line."""
        return self.message.partition("\n")[2].strip()

    def matches(self, aliases: set[str]) -> bool:
        """True when this commit was authored (or co-authored) by the target user."""
        name = self.name.lower()
        email = self.email.lower()
        local = email.split("@")[0]
        for alias in aliases:
            if not alias:
                continue
            if alias in (name, email, local) or alias in name.replace(" ", "") or alias in local:
                return True
        for line in self.message.lower().splitlines():
            line = line.strip()
            if line.startswith("co-authored-by:") and any(a and a in line for a in aliases):
                return True
        return False

    def who(self) -> str:
        return f"{self.name} <{self.email}>"


class Git:
    def __init__(self, path: Path, *, quiet: bool = False):
        self.path = path
        self.quiet = quiet
        self.calls = 0

    def run(self, *args: str, check: bool = True) -> str:
        self.calls += 1
        started = time.time()
        debug(f"git {' '.join(args)}")
        try:
            proc = subprocess.run(["git", "-C", str(self.path), *args],
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
        except FileNotFoundError as exc:
            raise GitError(f"git is not on PATH: {exc}") from exc
        debug(f"  -> exit {proc.returncode} in {time.time() - started:.2f}s, "
              f"{len(proc.stdout)} bytes")
        if proc.returncode != 0:
            message = truncate(proc.stderr or proc.stdout, 300)
            if check:
                raise GitError(f"`git {' '.join(args)}` failed ({proc.returncode}): {message}")
            debug(f"  (ignored) {message}")
            return ""
        return proc.stdout

    def rev(self, ref: str) -> str:
        return self.run("rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False).strip()


def parse_log(text: str) -> Iterable[Commit]:
    for record in text.split(RECORD):
        if not record.strip():
            continue
        fields = record.split(FIELD)
        if len(fields) < 8:
            debug(f"skipping malformed log record: {truncate(record, 120)}")
            continue
        yield Commit(*(f.strip("\n") for f in fields[:8]))


def open_repo(path_arg: str, quiet: bool) -> tuple[Git, Path]:
    path = Path(path_arg).expanduser()
    if not path.exists():
        raise GitError(f"{path} does not exist")
    git = Git(path, quiet=quiet)
    top = git.run("rev-parse", "--show-toplevel", check=False).strip()
    if not top:
        raise GitError(f"{path} is not inside a git working copy "
                       f"(no .git found from there upwards)")
    git.path = Path(top)
    return git, Path(top)


def pick_mainline(git: Git, requested: str) -> tuple[str, str]:
    """-> (ref, sha). Prefers the remote-tracking ref so a stale local branch is not used."""
    names = [requested] if requested else list(MAINLINE_CANDIDATES)
    for name in names:
        refs = (name,) if "/" in name else (f"origin/{name}", name)
        for ref in refs:
            sha = git.rev(ref)
            if sha:
                return ref, sha
    raise GitError(f"none of these branches exist in this clone: {', '.join(names)} "
                   f"(pass --branch with the right name)")


def load_history(git: Git, buffer_since: str) -> dict[str, Commit]:
    """Every commit reachable from any ref since buffer_since, in one git call."""
    text = git.run("log", "--all", "--no-notes", f"--since={buffer_since}",
                   "--date=short", f"--format={LOG_FORMAT}")
    commits = {c.sha: c for c in parse_log(text)}
    debug(f"loaded {len(commits)} commit(s) since {buffer_since}")
    return commits


def mainline_chain(commits: dict[str, Commit], tip: str) -> list[Commit]:
    """The first-parent chain of the mainline: what actually landed on the branch."""
    chain: list[Commit] = []
    seen: set[str] = set()
    sha: str | None = tip
    while sha and sha not in seen:
        seen.add(sha)
        commit = commits.get(sha)
        if commit is None:  # older than the loaded window
            break
        chain.append(commit)
        sha = commit.parents[0] if commit.parents else None
    return chain


def branch_side(merge: Commit, commits: dict[str, Commit], mainline: set[str]) -> list[Commit]:
    """The commits a merge brought in: reachable from its non-first parents and not
    already on the mainline. Walks the graph we loaded, so it costs no git calls."""
    found: dict[str, Commit] = {}
    stack = list(merge.parents[1:])
    while stack:
        sha = stack.pop()
        if sha in mainline or sha in found:
            continue
        commit = commits.get(sha)
        if commit is None:  # outside the loaded window: stop walking here
            continue
        found[sha] = commit
        stack.extend(commit.parents)
    return sorted(found.values(), key=lambda c: (c.date, c.sha))


# --------------------------------------------------------------------------- #
# pull requests
# --------------------------------------------------------------------------- #
def pr_from_merge(commit: Commit) -> dict | None:
    """-> {'number', 'title', 'branch', 'kind'} for a PR merge, else None."""
    match = MERGE_PR_RE.match(commit.subject)
    if match:
        head = match.group(2)
        branch = head.partition("/")[2] or head  # drop the owner prefix
        # GitHub puts the PR title in the body of the merge commit.
        title = next((line.strip() for line in commit.body.splitlines() if line.strip()), "")
        return {"number": int(match.group(1)), "title": title or branch,
                "branch": branch, "kind": "merge"}
    match = SQUASH_PR_RE.match(commit.subject)
    if match:
        return {"number": int(match.group("number")), "title": match.group("title").strip(),
                "branch": "", "kind": "squash"}
    return None


def collect_prs(chain: list[Commit], commits: dict[str, Commit], mainline: set[str],
                since: str, args: argparse.Namespace) -> tuple[list[dict], Counter, Counter]:
    """Walk the mainline and build one record per pull request merged in the window."""
    prs: list[dict] = []
    skipped: Counter[str] = Counter()
    contributors: Counter[str] = Counter()

    for commit in chain:
        if commit.merge_date < since:
            skipped["merged before the --days window"] += 1
            continue

        info = pr_from_merge(commit)
        if info is None:
            skipped["not a pull request merge (direct commit or plain merge)"] += 1
            if len(commit.parents) < 2 and commit.matches(args.aliases):
                skipped["_direct_by_author"] += 1
            continue
        if info["kind"] == "merge" and LONG_LIVED_RE.match(info["branch"]):
            skipped[f"branch sync merge from {info['branch']}"] += 1
            continue

        # A squash merge carries the PR author itself; a real merge commit is
        # authored by whoever clicked Merge, so the branch side is what counts.
        members = [commit] if info["kind"] == "squash" else branch_side(commit, commits, mainline)
        if not members:
            members = [commit]
            skipped["_shallow"] += 1

        for member in members:
            contributors[member.who()] += 1

        mine = [c for c in members if c.matches(args.aliases)]
        if not mine:
            skipped["authored by someone else"] += 1
            continue

        dates = sorted(c.date for c in members)
        prs.append({
            **info,
            "merge_sha": commit.sha,
            "merged_date": commit.merge_date,
            "merged_by": commit.who(),
            "authors": sorted({c.who() for c in members}),
            "commits": members,
            "commits_by_author": len(mine),
            "first_commit_date": dates[0],
            "last_commit_date": dates[-1],
        })

    return prs, skipped, contributors


def extract_tickets(sources: list[tuple[str, str]], project_keys: set[str] | None) -> dict[str, str]:
    """-> {'ABC-123': 'title'} keeping first-seen order and where it was found."""
    found: dict[str, str] = {}
    for label, text in sources:
        for match in JIRA_KEY_RE.finditer(text or ""):
            prefix, number = match.group(1), match.group(2)
            if project_keys is not None:
                if prefix not in project_keys:
                    continue
            elif prefix in NON_JIRA_PREFIXES or not any(c.isalpha() for c in prefix):
                continue
            found.setdefault(f"{prefix}-{number}", label)
    return found


def find_tickets(pr: dict, project_keys: set[str] | None) -> tuple[dict[str, str], bool]:
    """-> ({key: where it was found}, hit_the_cap).

    The title is where the team puts the key, so it wins outright; the branch name
    comes next. Commit messages are only a fallback, because a PR that merged other
    branches in mentions every key those branches touched."""
    for label, text in (("title", pr["title"]), ("branch", pr["branch"])):
        found = extract_tickets([(label, text)], project_keys)
        if found:
            return found, False
    found = extract_tickets([("commit-message", pr_text(pr))], project_keys)
    if len(found) > COMMIT_TICKET_CAP:
        return dict(list(found.items())[:COMMIT_TICKET_CAP]), True
    return found, False


def pr_text(pr: dict) -> str:
    """Every commit message of the PR, for ticket extraction and summarizing."""
    return "\n".join(c.message for c in pr["commits"])


# --------------------------------------------------------------------------- #
# JIRA
# --------------------------------------------------------------------------- #
BLANK_ISSUE = {"summary": "", "status": "", "type": ""}


class JiraClient:
    """Resolves issue keys in batches: one request per 100 keys instead of one each."""

    def __init__(self, base_url: str, token: str, *, email: str = "",
                 timeout: int = 30, quiet: bool = False):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.quiet = quiet
        self.cache: dict[str, dict[str, str]] = {}
        self.reported: set[str] = set()
        self.disabled = False
        self.calls = 0
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json",
                                     "Content-Type": "application/json",
                                     "User-Agent": "github-pr-jira-report"})
        if email:  # Atlassian Cloud: email + API token
            self.session.auth = (email, token)
        else:  # Jira Server / Data Center personal access token
            self.session.headers["Authorization"] = f"Bearer {token}"

    def issue(self, key: str) -> dict[str, str]:
        return self.cache.get(key, dict(BLANK_ISSUE))

    def preflight(self) -> bool:
        """Check the URL and the token once, instead of discovering per ticket that
        every lookup returns nothing."""
        response = self._probe(self.base_url)
        if response is not None and not self._is_jira(response):
            # Jira Data Center is often mounted under a context path (/jira). The
            # root redirects to it, so follow that before giving up.
            fixed = self._context_path()
            probe = self._probe(fixed) if fixed and fixed != self.base_url else None
            if probe is not None and self._is_jira(probe):
                log(f"jira: {self.base_url} redirects to its context path, using {fixed}",
                    quiet=self.quiet)
                self.base_url, response = fixed, probe
            else:
                self._fatal(f"no jira REST API at {self.base_url}/rest/api/2/myself "
                            f"(HTTP {response.status_code}, not JSON): every ticket "
                            f"would come back blank",
                            hint="JIRA_BASE_URL must be the base of the REST API, e.g. "
                                 "https://jira.example.com/jira")
                return False
        if response is None:
            return False
        if response.status_code in (401, 403):
            self._fatal(f"jira rejected the token (HTTP {response.status_code}): every ticket "
                        f"would come back blank",
                        hint="JIRA_PAT is expired or revoked - create a new personal access "
                             f"token at {self.base_url}/secure/ViewProfile.jspa "
                             "(Personal Access Tokens), or set JIRA_EMAIL too for Atlassian Cloud")
            return False
        if not response.ok:
            self._fatal(f"jira preflight failed with HTTP {response.status_code}: "
                        f"{truncate(response.text, 200)}")
            return False
        try:
            me = response.json()
        except ValueError:
            me = {}
        log(f"jira ok: {self.base_url} as {me.get('name') or me.get('displayName') or '?'}",
            quiet=self.quiet)
        return True

    @staticmethod
    def _is_jira(response: requests.Response) -> bool:
        """A JSON body means we reached the REST API, even when it says 401."""
        return response.headers.get("Content-Type", "").startswith("application/json")

    def _probe(self, base: str) -> requests.Response | None:
        self.calls += 1
        try:
            return self.session.get(f"{base}/rest/api/2/myself", timeout=self.timeout)
        except requests.RequestException as exc:
            self._fatal(f"jira is unreachable at {base} ({exc}): ticket details will be blank",
                        hint="check JIRA_BASE_URL, your VPN and any proxy settings")
            return None

    def _context_path(self) -> str:
        """-> the base URL with the context path the site redirects to, or ''."""
        self.calls += 1
        try:
            response = self.session.get(self.base_url, timeout=self.timeout,
                                        allow_redirects=False)
        except requests.RequestException:
            return ""
        location = response.headers.get("Location", "")
        if not location:
            return ""
        if location.startswith("http"):
            return location.rstrip("/")
        return f"{self.base_url}/{location.strip('/')}"

    def prefetch(self, keys: Iterable[str]) -> None:
        pending = sorted({k for k in keys if k not in self.cache})
        if not pending or self.disabled:
            return
        batches = (len(pending) + JIRA_BATCH - 1) // JIRA_BATCH
        log(f"jira: resolving {len(pending)} ticket(s) in {batches} request(s)", quiet=self.quiet)
        for start in range(0, len(pending), JIRA_BATCH):
            if self.disabled:
                break
            chunk = pending[start:start + JIRA_BATCH]
            if not self._search(chunk):
                # One unknown key makes the whole JQL batch fail, so fall back to
                # single lookups for that chunk rather than losing all of it.
                warn(f"jira: batch lookup failed for {len(chunk)} key(s), "
                     f"falling back to one request per key")
                for key in chunk:
                    if self.disabled:
                        break
                    self._single(key)

    def _search(self, keys: list[str]) -> bool:
        self.calls += 1
        try:
            response = self.session.post(f"{self.base_url}/rest/api/2/search", timeout=self.timeout,
                                         json={"jql": f"key in ({','.join(keys)})",
                                               "fields": ["summary", "status", "issuetype"],
                                               "maxResults": len(keys)})
        except requests.RequestException as exc:
            self._fatal(f"jira is unreachable at {self.base_url} ({exc}): "
                        f"ticket details will be blank")
            return True  # already reported, no point retrying key by key
        if response.status_code in (401, 403):
            self._fatal(f"jira rejected the credentials (HTTP {response.status_code}): "
                        f"all ticket details will be blank",
                        hint="check JIRA_PAT and JIRA_BASE_URL, and set JIRA_EMAIL too if this "
                             "is Atlassian Cloud")
            return True
        if not response.ok:
            debug(f"jira search {response.status_code}: {truncate(response.text, 300)}")
            return False
        try:
            issues = response.json().get("issues", [])
        except ValueError as exc:
            debug(f"jira search returned non-JSON: {exc}")
            return False
        for issue in issues:
            fields = issue.get("fields") or {}
            self.cache[issue.get("key", "")] = {
                "summary": fields.get("summary") or "",
                "status": ((fields.get("status") or {}).get("name")) or "",
                "type": ((fields.get("issuetype") or {}).get("name")) or "",
            }
        missing = [k for k in keys if k not in self.cache]
        if missing:
            for key in missing:
                self.cache[key] = {**BLANK_ISSUE, "status": "NOT_FOUND"}
            if len(missing) == len(keys) and len(keys) > 3:
                self._fatal(f"jira returned nothing for all {len(keys)} key(s) in this batch: "
                            f"the report has no ticket details at all",
                            hint="a 200 with no issues usually means the request was not "
                                 "authenticated - check JIRA_PAT")
            else:
                warn(f"jira: {len(missing)} key(s) not found, probably not real tickets: "
                     f"{truncate(', '.join(missing), 200)}")
        return True

    def _single(self, key: str) -> None:
        self.calls += 1
        try:
            response = self.session.get(f"{self.base_url}/rest/api/2/issue/{key}",
                                        params={"fields": "summary,status,issuetype"},
                                        timeout=self.timeout)
        except requests.RequestException as exc:
            self._fatal(f"jira is unreachable at {self.base_url} ({exc})")
            return
        info = dict(BLANK_ISSUE)
        if response.status_code == 404:
            info["status"] = "NOT_FOUND"
        elif response.status_code in (401, 403):
            self._fatal(f"jira rejected the credentials (HTTP {response.status_code}) on {key}")
            info["status"] = "NO_ACCESS"
        elif response.ok:
            fields = response.json().get("fields", {})
            info = {"summary": fields.get("summary") or "",
                    "status": ((fields.get("status") or {}).get("name")) or "",
                    "type": ((fields.get("issuetype") or {}).get("name")) or ""}
        else:
            info["status"] = f"HTTP_{response.status_code}"
            self._report_once(f"jira returned HTTP {response.status_code} for {key}: "
                              f"{truncate(response.text, 200)}")
        self.cache[key] = info

    def _fatal(self, message: str, *, hint: str = "") -> None:
        self.disabled = True
        self._report_once(message, hint=hint)

    def _report_once(self, message: str, *, hint: str = "") -> None:
        """One error per failure kind, not one per ticket."""
        kind = message.split(":")[0]
        if kind not in self.reported:
            self.reported.add(kind)
            problem(message, hint=hint)


# --------------------------------------------------------------------------- #
# summarizers
# --------------------------------------------------------------------------- #
def clean_messages(text: str) -> str:
    text = text or ""
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)     # html comments
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)      # fenced code
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)            # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)         # links
    text = re.sub(r"<[^>]+>", " ", text)                         # html tags
    kept: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if BOILERPLATE_RE.match(stripped) or MERGE_PR_RE.match(stripped):
            continue
        line = re.sub(r"^\[[ xX]\]\s*", "", stripped.lstrip("#>*-+ ").strip())
        if not line or set(line) <= set("-=|_ "):
            continue
        if BARE_PATH_RE.match(line):  # the file list under a "Conflicts:" block
            continue
        if line not in kept:  # commit messages repeat themselves a lot
            kept.append(line)
    return " ".join(kept)


def heuristic_summary(pr: dict, limit: int) -> str:
    title = " ".join((pr.get("title") or "").split())
    body = clean_messages(pr_text(pr))
    if title and body.lower().startswith(title.lower()[:40]):  # body just repeats the title
        body = body[len(title):].strip(" -:.")
    text = f"{title} - {body}".strip(" -") if body else title
    return truncate(text, limit)


def ollama_summary(pr: dict, limit: int, model: str, host: str, timeout: int) -> str:
    prompt = (
        f"Summarize this pull request in one sentence of at most {limit} characters. "
        "Plain text only, no preamble, no markdown.\n\n"
        f"Title: {pr.get('title') or ''}\n"
        f"Branch: {pr.get('branch') or ''}\n"
        f"Commit messages:\n{truncate(clean_messages(pr_text(pr)), 3000)}"
    )
    response = requests.post(
        f"{host.rstrip('/')}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False,
              "options": {"temperature": 0.2, "num_predict": 120}},
        timeout=timeout,
    )
    response.raise_for_status()
    return truncate(response.json().get("response", ""), limit)


def anthropic_summary(pr: dict, limit: int, model: str, client: Any) -> str:
    user_text = (
        f"Title: {pr.get('title') or ''}\n"
        f"Branch: {pr.get('branch') or ''}\n"
        f"Commit messages:\n{truncate(clean_messages(pr_text(pr)), 4000)}"
    )
    response = client.messages.create(
        model=model,
        max_tokens=200,
        system=(
            f"You summarize pull requests for a status report. Reply with ONE sentence of at most "
            f"{limit} characters describing what the change does. Plain text, no preamble, no "
            "markdown, no quotes."
        ),
        messages=[{"role": "user", "content": user_text}],
    )
    text = " ".join(block.text for block in response.content if block.type == "text")
    return truncate(text, limit)


class Summarizer:
    """Picks a summary backend and always degrades to the heuristic one."""

    def __init__(self, args: argparse.Namespace):
        self.mode = args.summarizer
        self.limit = args.summary_chars
        self.args = args
        self.client: Any = None
        self.warned = False
        self.calls = 0
        if self.mode == "anthropic":
            try:
                import anthropic
            except ImportError:
                warn("anthropic SDK not installed (pip install anthropic); using heuristic summaries")
                self.mode = "heuristic"
                return
            key = env_first("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
            try:
                self.client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
            except Exception as exc:  # noqa: BLE001 - any init failure degrades gracefully
                warn(f"anthropic client unavailable ({exc}); using heuristic summaries")
                self.mode = "heuristic"

    def summarize(self, pr: dict) -> tuple[str, str]:
        if self.mode == "none":
            return "", "none"
        if self.mode == "ollama":
            try:
                self.calls += 1
                text = ollama_summary(pr, self.limit, self.args.ollama_model,
                                      self.args.ollama_host, self.args.ai_timeout)
                if text:
                    return text, f"ollama:{self.args.ollama_model}"
            except Exception as exc:  # noqa: BLE001
                self._warn(f"ollama summary failed ({exc}); falling back to heuristic")
        elif self.mode == "anthropic":
            try:
                self.calls += 1
                text = anthropic_summary(pr, self.limit, self.args.anthropic_model, self.client)
                if text:
                    return text, f"anthropic:{self.args.anthropic_model}"
            except Exception as exc:  # noqa: BLE001
                self._warn(f"anthropic summary failed ({exc}); falling back to heuristic")
        return heuristic_summary(pr, self.limit), "heuristic"

    def _warn(self, message: str) -> None:
        if not self.warned:
            warn(message)
            self.warned = True


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def build_rows(prs: list[dict], args: argparse.Namespace, jira: JiraClient | None,
               jira_base: str, web_base: str, summarizer: Summarizer) -> list[dict]:
    project_keys = ({k.strip().upper() for k in args.project_keys.split(",") if k.strip()}
                    if args.project_keys else None)

    # Pass 1: find the ticket keys, title > branch > commit messages.
    capped = 0
    for pr in prs:
        pr["tickets"], hit_cap = find_tickets(pr, project_keys)
        capped += hit_cap
    if capped:
        warn(f"{capped} PR(s) mention more than {COMMIT_TICKET_CAP} JIRA keys in their commit "
             f"messages and have none in the title or branch: only the first "
             f"{COMMIT_TICKET_CAP} were kept")

    # Pass 2: one batched JIRA round-trip for every key in the whole report.
    if jira:
        jira.prefetch(key for pr in prs for key in pr["tickets"])

    # Pass 3: fill the summary column. It is never empty.
    rows: list[dict] = []
    for pr in sorted(prs, key=lambda p: p["number"], reverse=True):
        tickets = pr["tickets"]
        details = {key: (jira.issue(key) if jira else dict(BLANK_ISSUE)) for key in tickets}

        # The JIRA ticket title if we have one, else a summary of the commit
        # messages, else the PR title: this column can never come out empty.
        titled = [key for key in tickets if details[key]["summary"]]
        if args.summary_from == "jira" and titled:
            summary = truncate(" | ".join(details[key]["summary"] for key in titled),
                               args.summary_chars)
            summary_source = "jira:" + ",".join(titled)
        else:
            summary, summary_source = summarizer.summarize(pr)
        if not summary.strip():
            summary = truncate(pr["title"], args.summary_chars)
            summary_source = "pr-title"
        url = (f"{web_base}/{args.owner}/{args.repo}/pull/{pr['number']}"
               if web_base and args.owner and args.repo else "")
        rows.append({
            "jira_links": " ".join(f"{jira_base}/browse/{k}" for k in tickets) if jira_base else "",
            "jira_summaries": " | ".join(details[k]["summary"] for k in tickets if details[k]["summary"]),
            "jira_statuses": ", ".join(f"{k}={details[k]['status']}" for k in tickets if details[k]["status"]),
            "jira_types": ", ".join(f"{k}={details[k]['type']}" for k in tickets if details[k]["type"]),
            "ticket_source": ", ".join(sorted(set(tickets.values()))),
            "pr_title": pr["title"],
            "pr_url": url,
            "branch": pr["branch"],
            "merged_date": pr["merged_date"],
            "merged_by": pr["merged_by"],
            "authors": "; ".join(pr["authors"]),
            "commits_by_author": pr["commits_by_author"],
            "first_commit_date": pr["first_commit_date"],
            "last_commit_date": pr["last_commit_date"],
            "summary": summary,
            "summary_source": summary_source,
            "merge_sha": pr["merge_sha"][:12],
            "_details": details,
        })
    return rows


def explode(rows: list[dict], jira_base: str) -> list[dict]:
    """One row per JIRA ticket; PRs without a ticket keep a single blank-ticket row."""
    out: list[dict] = []
    for row in rows:
        keys = list(row["_details"]) or [""]
        for key in keys:
            info = row["_details"].get(key, BLANK_ISSUE)
            out.append({
                "jira_ticket": key,
                "jira_link": f"{jira_base}/browse/{key}" if (key and jira_base) else "",
                "jira_summary": info["summary"],
                "jira_status": info["status"],
                "jira_type": info["type"],
                **{k: v for k, v in row.items()
                   if k not in ("jira_links", "jira_summaries",
                                "jira_statuses", "jira_types")},
            })
    return out


EXPLODED_FIELDS = (["summary", "jira_summary", "pr_title", "branch", "merged_date",
                    "commits_by_author", "jira_ticket", "jira_link"]
                   + [f for f in REST_FIELDS if not f.startswith("jira_")]
                   + ["jira_status", "jira_type"])


def write_csv(rows: list[dict], out_path: Path, fieldnames: list[str]) -> None:
    present = {key for row in rows for key in row if not key.startswith("_")}
    extra = sorted(present - set(fieldnames))
    if extra:  # a new row key would otherwise be dropped silently
        warn(f"column(s) not in the CSV layout, appended at the end: {', '.join(extra)}")
        fieldnames = fieldnames + extra
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CSV report of the JIRA tickets / PRs a developer shipped, "
                    "built from a local clone's git history.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("repo_path", help="path to a local clone (the folder containing .git)")
    parser.add_argument("--authors", "--author", "--alias", dest="authors", action="append",
                        default=[], metavar="NAMES",
                        help="git author names, emails or fragments - one person often has "
                             "several. Comma-separated and repeatable, e.g. "
                             "--authors jdoe,'Jane Doe',jane.doe@corp.com "
                             "(default: this clone's user.email)")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="look-back window in days")
    parser.add_argument("--branch", default="",
                        help=f"mainline branch PRs are merged into (default: first of "
                             f"{'/'.join(MAINLINE_CANDIDATES)} that exists)")
    parser.add_argument("--list-authors", action="store_true",
                        help="print the git authors in the window and exit")
    parser.add_argument("--project-keys", default="",
                        help="comma-separated JIRA project keys to accept, e.g. ABC,DEF (default: heuristic)")
    parser.add_argument("--jira-base-url", default="", help="overrides JIRA_BASE_URL from .env")
    parser.add_argument("--no-jira", action="store_true", help="skip JIRA lookups (links only)")
    parser.add_argument("--summarizer", choices=["heuristic", "ollama", "anthropic", "none"],
                        default="heuristic", help="how to summarize PRs from their commit messages")
    parser.add_argument("--summary-from", choices=["jira", "commits"], default="jira",
                        help="fill the summary column from the JIRA ticket title when the PR has "
                             "one (falling back to the commit messages), or always from the "
                             "commit messages")
    parser.add_argument("--summarize", dest="legacy_summarize",
                        choices=["no-ticket", "always"], help=argparse.SUPPRESS)
    parser.add_argument("--summary-chars", type=int, default=200, help="max characters of the summary column")
    parser.add_argument("--ollama-model", default="llama3.2:1b", help="model for --summarizer ollama")
    parser.add_argument("--ollama-host", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    parser.add_argument("--anthropic-model", default="claude-haiku-4-5", help="model for --summarizer anthropic")
    parser.add_argument("--ai-timeout", type=int, default=60, help="timeout in seconds for AI summary calls")
    parser.add_argument("--explode-tickets", action="store_true", help="one row per JIRA ticket instead of per PR")
    parser.add_argument("--out", default="", help="output CSV path (default: reports/<repo>_<author>_<date>.csv)")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress progress output (warnings and errors are still printed)")
    parser.add_argument("--debug", action="store_true",
                        help="log every git/HTTP call and print tracebacks")
    return parser.parse_args(argv)


def err(message: str, *, hint: str = "") -> int:
    log(f"ERROR: {message}")
    if hint:
        log(f"  -> {hint}")
    return 2


def report_authors(contributors: Counter, args: argparse.Namespace) -> None:
    """Nothing matched: show who did author the PRs so --authors can be fixed."""
    if not contributors:
        log("  no PR merge in this window had any branch commit to attribute")
        return
    log("  authors of the PRs merged in this window:")
    for who, count in contributors.most_common(20):
        log(f"    {count:5d} commit(s)  {who}")
    near = [who for who in contributors if any(a and a in who.lower() for a in args.aliases)]
    if near:
        log(f"  -> {args.who} does match {near[0]}, so their PRs are outside this "
            f"window or this branch: try --days / --branch")
    else:
        log(f"  -> nothing matches --authors {args.who!r}: use names or emails from the list "
            f"above (git author names are not GitHub logins), or --list-authors")


def run(argv: list[str] | None = None) -> int:
    global DEBUG
    args = parse_args(argv)
    DEBUG = args.debug
    load_env()

    try:
        git, top = open_repo(args.repo_path, args.quiet)
    except GitError as exc:
        return err(str(exc), hint="pass the folder of a local clone, the one containing .git")

    # --authors takes a comma-separated list and is repeatable: flatten both forms.
    args.authors = [name.strip() for entry in args.authors
                    for name in entry.split(",") if name.strip()]
    if not args.authors:
        fallback = git.run("config", "user.email", check=False).strip()
        if not fallback:
            return err("no --authors given and this clone has no user.email configured",
                       hint="pass --authors with git author names or emails, or "
                            "--list-authors to see the candidates")
        args.authors = [fallback]
        log(f"no --authors given, using this clone's user.email: {fallback}", quiet=args.quiet)
    args.aliases = {name.lower() for name in args.authors}
    args.who = " / ".join(args.authors)
    if args.legacy_summarize:  # --summarize was renamed to --summary-from
        args.summary_from = "commits" if args.legacy_summarize == "always" else "jira"
        warn(f"--summarize {args.legacy_summarize} is deprecated: "
             f"use --summary-from {args.summary_from}")

    try:
        origin = git.run("remote", "get-url", "origin", check=False).strip()
        args.owner, args.repo, web_base = parse_remote(origin)
        if not args.owner:
            warn(f"cannot read an owner/repo from origin ({origin or 'no origin remote'}): "
                 f"the pr_url column will be empty")
        ref, tip = pick_mainline(git, args.branch)
    except GitError as exc:
        return err(str(exc))

    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    buffer_since = (datetime.now(timezone.utc)
                    - timedelta(days=args.days + GRAPH_BUFFER_DAYS)).strftime("%Y-%m-%d")
    log(f"repo {top} ({args.owner}/{args.repo}) | mainline {ref} | authors {args.who} | "
        f"window {since}..today ({args.days}d)", quiet=args.quiet)

    try:
        commits = load_history(git, buffer_since)
    except GitError as exc:
        return err(f"cannot read the git history: {exc}")
    if not commits:
        return err(f"no commit in this clone is newer than {buffer_since}",
                   hint="run git fetch in the clone, or widen --days")

    chain = mainline_chain(commits, tip)
    if not chain:
        return err(f"{ref} points at {tip[:12]}, which is not in the history loaded since "
                   f"{buffer_since}",
                   hint="the branch tip is older than the window: widen --days")
    log(f"  {len(commits)} commit(s) loaded since {buffer_since}, "
        f"{len(chain)} on the {ref} first-parent chain", quiet=args.quiet)
    stale_before = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    if chain[0].merge_date < stale_before:
        warn(f"{ref} has had nothing new since {chain[0].merge_date}: run `git fetch` in "
             f"{top} or recent PRs will be missing")

    mainline = {c.sha for c in chain}
    prs, skipped, contributors = collect_prs(chain, commits, mainline, since, args)
    direct = skipped.pop("_direct_by_author", 0)
    shallow = skipped.pop("_shallow", 0)
    if skipped:
        log("  skipped " + ", ".join(f"{n} merge(s): {why}" for why, n in skipped.most_common()),
            quiet=args.quiet)
    log(f"  {len(prs)} PR(s) merged into {ref} with a commit by {args.who}", quiet=args.quiet)
    if direct:
        warn(f"{direct} commit(s) by {args.who} landed on {ref} without a PR merge "
             f"(rebase merge or direct push): they have no PR number and are not in the report")
    if shallow:
        warn(f"{shallow} PR(s) had no branch commit inside the loaded history, so only their "
             f"merge commit was used")
    if not prs:
        problem(f"no PR merged into {ref} in the last {args.days} day(s) has a commit "
                f"by {args.who}")
        report_authors(contributors, args)

    jira_base = (args.jira_base_url or env_first("JIRA_BASE_URL")).rstrip("/")
    jira_token = env_first("JIRA_PAT", "JIRA_TOKEN", "JIRA_API_TOKEN")
    jira: JiraClient | None = None
    if not args.no_jira:
        if jira_base and jira_token:
            jira = JiraClient(jira_base, jira_token, email=env_first("JIRA_EMAIL"), quiet=args.quiet)
            jira.preflight()          # records its own problem, keeps going with blank details
            jira_base = jira.base_url  # may have gained the /context-path
        elif not jira_base:
            warn("JIRA_BASE_URL not set: ticket links and details will be blank "
                 "(pass --no-jira to silence this)")
        else:
            warn("JIRA_PAT not set: ticket links only, no summary/status "
                 "(pass --no-jira to silence this)")

    summarizer = Summarizer(args)
    pr_rows = build_rows(prs, args, jira, jira_base, web_base, summarizer)
    rows = explode(pr_rows, jira_base) if args.explode_tickets else pr_rows

    # The window is part of the name: a quick --days 10 trial must never be
    # mistaken for the full report.
    # Date-time first so the folder sorts chronologically, and two runs in the
    # same minute cannot overwrite each other. The window is in the name too: a
    # quick --days 10 trial must never be mistaken for the full report.
    stem = (f"{datetime.now():%Y%m%d-%H%M%S}_{args.repo or top.name}"
            f"_{re.sub(r'[^A-Za-z0-9._-]', '-', args.authors[0])}_{args.days}d")
    out_path = Path(args.out) if args.out else (SCRIPT_DIR / "reports" / f"{stem}.csv")
    try:
        write_csv(rows, out_path, EXPLODED_FIELDS if args.explode_tickets else ROW_FIELDS)
    except OSError as exc:
        return err(f"cannot write {out_path}: {exc}",
                   hint="close the file if it is open in Excel, or pass a different --out path")

    tickets = {k for row in pr_rows for k in row["_details"]}
    with_ticket = sum(1 for row in pr_rows if row["_details"])
    sources = Counter(row["summary_source"].split(":")[0] for row in pr_rows)
    blank = sum(1 for row in pr_rows if not row["summary"].strip())
    commits_mine = sum(int(row["commits_by_author"] or 0) for row in pr_rows)
    log("", quiet=args.quiet)
    log(f"PRs: {len(pr_rows)} ({with_ticket} with a JIRA key) | rows: {len(rows)} | "
        f"distinct JIRA tickets: {len(tickets)} | commits by {args.who}: {commits_mine}",
        quiet=args.quiet)
    log("summary column: " + ", ".join(f"{n} from {src}" for src, n in sources.most_common()),
        quiet=args.quiet)
    if blank:
        problem(f"{blank} row(s) ended up with an empty summary")
    log(f"calls: git {git.calls} | jira {jira.calls if jira else 0} | "
        f"summarizer {summarizer.calls} | github 0", quiet=args.quiet)
    if not rows:
        log(f"the report is EMPTY: {out_path} contains headers only")

    if PROBLEMS:
        log("")
        log(f"{len(PROBLEMS)} problem(s) detected during this run:")
        for index, message in enumerate(PROBLEMS, 1):
            log(f"  {index}. {message}")
    print(out_path)
    return 1 if PROBLEMS else 0


def list_authors(argv: list[str] | None = None) -> int:
    """--list-authors: who committed in the window, so --authors can be picked correctly."""
    args = parse_args(argv)
    git, top = open_repo(args.repo_path, args.quiet)
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    log(f"git authors in {top} since {since} (count, name, email):")
    print(git.run("shortlog", "-sne", "--all", f"--since={since}").rstrip())
    return 0


def main(argv: list[str] | None = None) -> int:
    """Wraps run() so no failure can end as a bare traceback with no explanation."""
    global DEBUG
    try:
        args = parse_args(argv)
        DEBUG = args.debug
        return list_authors(argv) if args.list_authors else run(argv)
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    except SystemExit:
        raise
    except GitError as exc:
        return err(str(exc))
    except Exception as exc:  # noqa: BLE001 - report anything unexpected, then exit non-zero
        log("")
        log(f"ERROR: unexpected failure: {type(exc).__name__}: {exc}")
        if DEBUG:
            traceback.print_exc()
        else:
            log("  -> re-run with --debug for the traceback and the git/HTTP call log")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
