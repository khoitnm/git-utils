#!/usr/bin/env python3
"""Build a CSV report of the JIRA tickets / pull requests a GitHub user worked on.

Given a GitHub repo URL, an author username and a look-back window, the script
finds every pull request in that window that the user opened and/or committed
to, pulls the JIRA ticket keys out of the branch name / PR title / commit
messages / PR body, and writes one CSV row per PR (or per ticket with
--explode-tickets).

Examples
--------
  python github_pr_jira_report.py https://github.com/acme/webapp --author trank --days 360
  python github_pr_jira_report.py acme/webapp --author trank --days 90 --summarizer anthropic
  python github_pr_jira_report.py https://github.com/acme/webapp --author trank --no-jira

Credentials are read from a .env file found next to this script (or in any
parent folder): GIT_PAT (or GITHUB_PAT / GITHUB_TOKEN), JIRA_PAT, JIRA_BASE_URL.

Anything that can make the report wrong is printed on the console: WARNING for
recoverable trouble, ERROR for problems that probably invalidate the output.
Errors are repeated in a summary at the end and make the exit code non-zero
(2 = the run could not start, 1 = the report was produced but has problems).
Add --debug for the HTTP request log and tracebacks.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
import traceback
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency: pip install requests")

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DAYS = 360
SEARCH_RESULT_CAP = 1000  # hard cap of the GitHub search API

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

# PR-template boilerplate that should never end up in a summary.
BOILERPLATE_RE = re.compile(
    r"^\s*(#+\s*)?("
    r"description|summary|what( does this pr do)?\??|why\??|how (was this|to) test(ed)?|"
    r"testing|test plan|checklist|screenshots?|notes?|jira|ticket|type of change|"
    r"related (issues?|tickets?|prs?)|reviewers?|risk|rollback|definition of done"
    r")\s*:?\s*$",
    re.IGNORECASE,
)


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


def load_env() -> None:
    """Load .env files, nearest first (nearest wins). Never clobbers real env vars."""
    paths: list[Path] = []
    for base in (SCRIPT_DIR, Path.cwd().resolve()):
        for folder in (base, *base.parents):
            candidate = folder / ".env"
            if candidate.is_file() and candidate not in paths:
                paths.append(candidate)
    try:
        from dotenv import load_dotenv
    except ImportError:
        load_dotenv = None
    for path in paths:
        if load_dotenv is not None:
            load_dotenv(path, override=False)
            continue
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.removeprefix("export ").partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def env_first(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def parse_repo(raw: str) -> tuple[str, str, str, str]:
    """-> (owner, repo, api_base, web_base). Accepts URL, SSH remote or owner/repo."""
    text = raw.strip().rstrip("/")
    host = "github.com"
    if text.startswith("git@"):
        host, _, path = text[4:].partition(":")
    elif "://" in text:
        parsed = urlparse(text)
        host, path = parsed.netloc, parsed.path.lstrip("/")
    else:
        path = text
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise SystemExit(f"Cannot parse owner/repo out of {raw!r}")
    owner, repo = parts[0], parts[1].removesuffix(".git")
    host = host.split("@")[-1]
    api_base = ("https://api.github.com" if host in ("github.com", "www.github.com")
                else f"https://{host}/api/v3")
    return owner, repo, api_base, f"https://{host}"


def iso_date(value: str | None) -> str:
    return (value or "")[:10]


def truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 3]
    if " " in cut[limit // 2:]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:-") + "..."


# --------------------------------------------------------------------------- #
# GitHub
# --------------------------------------------------------------------------- #
DIAG_HEADERS = ("x-ratelimit-remaining", "x-ratelimit-reset", "x-github-sso",
                "x-accepted-github-permissions", "x-oauth-scopes", "x-github-request-id")


class GitHubError(RuntimeError):
    """A GitHub response we cannot use, with the details needed to explain why."""

    def __init__(self, response: requests.Response, url: str):
        self.status = response.status_code
        self.url = url
        self.body = truncate(response.text, 400)
        self.headers = {name: response.headers[name]
                        for name in DIAG_HEADERS if name in response.headers}
        detail = " ".join(f"{k}={v}" for k, v in self.headers.items())
        super().__init__(f"GitHub {self.status} for {url}: {self.body}"
                         + (f" [{detail}]" if detail else ""))


class GitHubClient:
    def __init__(self, token: str, api_base: str, *, timeout: int = 30, quiet: bool = False):
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.quiet = quiet
        self.calls = 0
        self.last_total_count: int | None = None
        self.token_login = ""
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "github-pr-jira-report",
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def _sleep_for(self, response: requests.Response) -> float:
        retry_after = response.headers.get("Retry-After", "")
        if retry_after.isdigit():
            return min(int(retry_after), 300)
        reset = response.headers.get("X-RateLimit-Reset", "")
        if reset.isdigit():
            return max(1.0, min(int(reset) - time.time() + 2, 300))
        return 30.0

    @staticmethod
    def _rate_limited(response: requests.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.status_code != 403:
            return False
        if response.headers.get("X-RateLimit-Remaining") == "0":
            return True
        body = response.text.lower()
        return "rate limit" in body or "abuse" in body

    def request(self, url: str, params: dict | None = None) -> requests.Response:
        if not url.startswith("http"):
            url = f"{self.api_base}{url}"
        last: requests.Response | None = None
        last_exc: Exception | None = None
        for attempt in range(6):
            self.calls += 1
            try:
                last = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                warn(f"GitHub request to {url} failed ({exc}); retry {attempt + 1}/6")
                time.sleep(2 ** attempt)
                continue
            debug(f"GET {last.url} -> {last.status_code}")
            if self._rate_limited(last):
                wait = self._sleep_for(last)
                warn(f"rate limited by GitHub ({last.status_code}), sleeping {wait:.0f}s "
                     f"(retry {attempt + 1}/6)")
                time.sleep(wait)
                continue
            if last.status_code >= 500:
                warn(f"GitHub {last.status_code} for {url}; retry {attempt + 1}/6")
                time.sleep(2 ** attempt)
                continue
            return last
        if last is None:
            raise RuntimeError(f"GitHub unreachable: {url} ({last_exc})")
        raise GitHubError(last, url)  # exhausted retries on 5xx / rate limit

    def get(self, url: str, params: dict | None = None) -> Any:
        response = self.request(url, params)
        if not response.ok:
            raise GitHubError(response, url)
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"GitHub returned non-JSON for {url}: {exc}: "
                               f"{truncate(response.text, 200)}") from exc

    def paginate(self, path: str, params: dict | None = None, *, key: str | None = None,
                 max_items: int | None = None) -> Iterator[dict]:
        query: dict | None = dict(params or {})
        query.setdefault("per_page", 100)
        url, sent = path, 0
        self.last_total_count = None
        while url:
            response = self.request(url, query)
            if not response.ok:
                raise GitHubError(response, url)
            payload = response.json()
            if isinstance(payload, dict) and "total_count" in payload:
                self.last_total_count = payload.get("total_count")
                if payload.get("incomplete_results"):
                    warn(f"GitHub search timed out and returned incomplete results for {url} "
                         f"- the counts below are lower than reality")
            items = payload.get(key, []) if key else payload
            if key and isinstance(payload, dict) and key not in payload:
                raise RuntimeError(f"GitHub response for {url} has no {key!r} field: "
                                   f"{truncate(response.text, 200)}")
            for item in items:
                yield item
                sent += 1
                if max_items and sent >= max_items:
                    return
            if not items:
                return
            # The next link already carries the query string.
            url, query = self._next_link(response), None

    @staticmethod
    def _next_link(response: requests.Response) -> str | None:
        for chunk in response.headers.get("Link", "").split(","):
            if 'rel="next"' in chunk:
                return chunk.split(";")[0].strip().strip("<>")
        return None


def commit_matches_author(commit: dict, aliases: set[str]) -> bool:
    login = ((commit.get("author") or {}).get("login") or "").lower()
    if login and login in aliases:
        return True
    meta = (commit.get("commit") or {}).get("author") or {}
    name = (meta.get("name") or "").lower()
    local = (meta.get("email") or "").lower().split("@")[0]
    for alias in aliases:
        if alias and (alias == name or alias == local
                      or alias in name.replace(" ", "") or alias in local):
            return True
    message = ((commit.get("commit") or {}).get("message") or "").lower()
    for line in message.splitlines():
        if line.strip().startswith("co-authored-by:") and any(a in line for a in aliases if a):
            return True
    return False


def fetch_pr(gh: GitHubClient, owner: str, repo: str, number: int) -> dict:
    pr = gh.get(f"/repos/{owner}/{repo}/pulls/{number}")
    pr["_commits"] = list(gh.paginate(f"/repos/{owner}/{repo}/pulls/{number}/commits"))
    return pr


def pr_window_date(pr: dict, field: str) -> str | None:
    return {"created": pr.get("created_at"),
            "merged": pr.get("merged_at"),
            "updated": pr.get("updated_at")}.get(field)


def state_of(pr: dict) -> str:
    if pr.get("merged_at"):
        return "merged"
    return "open" if pr.get("state") == "open" else "closed"


# --------------------------------------------------------------------------- #
# JIRA
# --------------------------------------------------------------------------- #
def extract_tickets(sources: list[tuple[str, str]], project_keys: set[str] | None) -> dict[str, str]:
    """-> {'ABC-123': 'branch'} keeping first-seen order and where it was found."""
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


class JiraClient:
    def __init__(self, base_url: str, token: str, *, email: str = "", timeout: int = 20, quiet: bool = False):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.quiet = quiet
        self.cache: dict[str, dict[str, str]] = {}
        self.reported: set[str] = set()
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json",
                                     "User-Agent": "github-pr-jira-report"})
        if email:  # Atlassian Cloud: email + API token
            self.session.auth = (email, token)
        else:  # Jira Server / Data Center personal access token
            self.session.headers["Authorization"] = f"Bearer {token}"

    def issue(self, key: str) -> dict[str, str]:
        if key in self.cache:
            return self.cache[key]
        info = {"summary": "", "status": "", "type": ""}
        try:
            response = self.session.get(
                f"{self.base_url}/rest/api/2/issue/{key}",
                params={"fields": "summary,status,issuetype"},
                timeout=self.timeout,
            )
            if response.status_code == 404:
                info["status"] = "NOT_FOUND"
                warn(f"jira {key}: not found (moved, deleted, or a false-positive key)")
            elif response.status_code in (401, 403):
                info["status"] = "NO_ACCESS"
                self._report_once(
                    f"jira rejected the credentials with HTTP {response.status_code} "
                    f"(first seen on {key}): all ticket details will be blank",
                    hint="check JIRA_PAT / JIRA_BASE_URL, and set JIRA_EMAIL too if this is "
                         "Atlassian Cloud")
            elif response.ok:
                fields = response.json().get("fields", {})
                info = {
                    "summary": fields.get("summary") or "",
                    "status": ((fields.get("status") or {}).get("name")) or "",
                    "type": ((fields.get("issuetype") or {}).get("name")) or "",
                }
            else:
                info["status"] = f"HTTP_{response.status_code}"
                self._report_once(f"jira returned HTTP {response.status_code} for {key}: "
                                  f"{truncate(response.text, 200)}")
        except requests.RequestException as exc:
            self._report_once(f"jira is unreachable at {self.base_url} ({exc}): "
                              f"ticket details will be blank")
            info["status"] = "ERROR"
        self.cache[key] = info
        return info

    def _report_once(self, message: str, *, hint: str = "") -> None:
        """One error per failure kind, not one per ticket."""
        kind = message.split(":")[0]
        if kind not in self.reported:
            self.reported.add(kind)
            problem(message, hint=hint)


# --------------------------------------------------------------------------- #
# summarizers
# --------------------------------------------------------------------------- #
def clean_markdown(text: str) -> str:
    text = text or ""
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.DOTALL)     # html comments
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)      # fenced code
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)            # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)         # links
    text = re.sub(r"<[^>]+>", " ", text)                         # html tags
    kept = []
    for raw in text.splitlines():
        if BOILERPLATE_RE.match(raw.strip()):
            continue
        line = re.sub(r"^\[[ xX]\]\s*", "", raw.strip().lstrip("#>*-+ ").strip())
        if not line or set(line) <= set("-=|_ "):
            continue
        kept.append(line)
    return " ".join(kept)


def heuristic_summary(pr: dict, limit: int) -> str:
    title = " ".join((pr.get("title") or "").split())
    body = clean_markdown(pr.get("body") or "")
    if title and body.lower().startswith(title.lower()[:40]):  # body just repeats the title
        body = body[len(title):].strip(" -:.")
    text = f"{title} - {body}".strip(" -") if body else title
    return truncate(text, limit)


def ollama_summary(pr: dict, limit: int, model: str, host: str, timeout: int) -> str:
    prompt = (
        f"Summarize this pull request in one sentence of at most {limit} characters. "
        "Plain text only, no preamble, no markdown.\n\n"
        f"Title: {pr.get('title') or ''}\n"
        f"Branch: {(pr.get('head') or {}).get('ref') or ''}\n"
        f"Description:\n{truncate(clean_markdown(pr.get('body') or ''), 3000)}"
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
    commit_lines = "; ".join(
        truncate((c.get("commit") or {}).get("message") or "", 90) for c in pr.get("_commits", [])[:10]
    )
    user_text = (
        f"Title: {pr.get('title') or ''}\n"
        f"Branch: {(pr.get('head') or {}).get('ref') or ''}\n"
        f"Commits: {commit_lines}\n"
        f"Description:\n{truncate(clean_markdown(pr.get('body') or ''), 4000)}"
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
                text = ollama_summary(pr, self.limit, self.args.ollama_model,
                                      self.args.ollama_host, self.args.ai_timeout)
                if text:
                    return text, f"ollama:{self.args.ollama_model}"
            except Exception as exc:  # noqa: BLE001
                self._warn(f"ollama summary failed ({exc}); falling back to heuristic")
        elif self.mode == "anthropic":
            try:
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
# discovery
# --------------------------------------------------------------------------- #
def preflight(gh: GitHubClient, args: argparse.Namespace, owner: str, repo: str) -> None:
    """Check token, repo and author before searching so a misconfiguration is
    reported as an error instead of showing up as an empty report."""
    response = gh.request(f"/repos/{owner}/{repo}")
    if not response.ok:
        raise GitHubError(response, f"/repos/{owner}/{repo}")
    info = response.json()
    scopes = response.headers.get("X-OAuth-Scopes")
    log(f"repo ok: {info.get('full_name')} (private={info.get('private')}, "
        f"archived={info.get('archived')}) | token scopes: "
        f"{scopes if scopes else 'none reported (fine-grained token?)'} | "
        f"rate limit left: {response.headers.get('X-RateLimit-Remaining', '?')}", quiet=args.quiet)
    if response.headers.get("X-GitHub-SSO"):
        problem(f"the token is not SSO-authorized for this org: "
                f"{response.headers['X-GitHub-SSO']}",
                hint="authorize the PAT for the org under GitHub > Settings > Developer settings > "
                     "Personal access tokens > Configure SSO")

    try:
        me = gh.get("/user")
        gh.token_login = me.get("login") or ""
        log(f"token user: {gh.token_login}", quiet=args.quiet)
    except RuntimeError as exc:
        warn(f"cannot read the token owner (/user): {exc}")

    try:
        user = gh.get(f"/users/{args.author}")
        if (user.get("login") or "").lower() != args.author.lower():
            warn(f"--author {args.author!r} resolves to login {user.get('login')!r}; "
                 f"using the exact login is safer")
        log(f"author ok: {user.get('login')} ({user.get('type')})", quiet=args.quiet)
    except GitHubError as exc:
        if exc.status == 404:
            problem(f"GitHub user {args.author!r} does not exist (or is invisible to this token)",
                    hint="--author must be the GitHub login, not an email address or AD id; "
                         f"check https://github.com/{args.author}")
        else:
            problem(f"cannot verify --author {args.author!r}: {exc}")


def diagnose_no_results(gh: GitHubClient, args: argparse.Namespace, owner: str, repo: str) -> None:
    """Nothing matched: prove whether the repo has PRs at all and, if it does,
    show who authored the recent ones so the real problem is visible."""
    log("nothing matched - checking whether the repo has PRs at all", quiet=args.quiet)
    try:
        recent = list(gh.paginate(f"/repos/{owner}/{repo}/pulls",
                                  {"state": "all", "sort": "created", "direction": "desc"},
                                  max_items=100))
    except RuntimeError as exc:
        problem(f"cannot list the PRs of {owner}/{repo} directly either: {exc}",
                hint="the token cannot read pull requests: a fine-grained PAT needs "
                     "'Pull requests: read' and 'Contents: read' on this repo")
        return

    if not recent:
        log(f"  {owner}/{repo} really has no pull requests - nothing to report", quiet=args.quiet)
        return

    newest = recent[0]
    counts = Counter((pr.get("user") or {}).get("login") or "?" for pr in recent)
    problem(f"the searches returned 0 results, but {owner}/{repo} does have PRs "
            f"(newest: #{newest.get('number')} by "
            f"{(newest.get('user') or {}).get('login')} created {iso_date(newest.get('created_at'))})")
    log("  authors of the " + f"{len(recent)} most recent PRs: "
        + ", ".join(f"{login}({n})" for login, n in counts.most_common(15)))
    near = [login for login in counts
            if args.author.lower() in login.lower() or login.lower() in args.author.lower()]
    if gh.token_login in counts and gh.token_login.lower() != args.author.lower():
        near.insert(0, gh.token_login)  # the token owner authored PRs here: almost certainly them
    if near:
        log(f"  -> did you mean --author {near[0]} ? "
            f"(--author takes the GitHub login, not the corporate/AD username)")
    else:
        log(f"  -> no recent PR is authored by {args.author!r}. Check the login spelling, "
            f"--days ({args.days}), --date-field ({args.date_field}) and --state ({args.state})")


def discover(gh: GitHubClient, args: argparse.Namespace, owner: str, repo: str, since: str) -> dict[int, dict]:
    """-> {pr_number: pr json enriched with _commits and _reasons}"""
    prs: dict[int, dict] = {}
    reasons: dict[int, set[str]] = {}

    if args.match in ("author", "both"):
        query = f"repo:{owner}/{repo} type:pr author:{args.author} {args.date_field}:>={since}"
        log(f"searching PRs opened by {args.author} ({args.date_field} >= {since})", quiet=args.quiet)
        debug(f"pr search query: {query}")
        try:
            numbers = [item["number"] for item in gh.paginate(
                "/search/issues", {"q": query, "sort": "created", "order": "desc"},
                key="items", max_items=SEARCH_RESULT_CAP)]
        except GitHubError as exc:
            if exc.status == 422:
                problem(f"GitHub rejected the PR search query {query!r}: {exc.body}",
                        hint="a 422 here usually means the --author login does not exist or is "
                             "not visible to this token")
            else:
                problem(f"PR search failed: {exc}")
            numbers = []
        except RuntimeError as exc:
            problem(f"PR search failed: {exc}")
            numbers = []
        total = gh.last_total_count
        log(f"  {len(numbers)} PR(s) opened by {args.author}"
            + (f" (search total_count={total})" if total is not None else ""), quiet=args.quiet)
        if total is not None and total > SEARCH_RESULT_CAP:
            warn(f"the PR search matched {total} PRs but the GitHub search API only returns "
                 f"{SEARCH_RESULT_CAP}: narrow --days to see them all")
        for index, number in enumerate(numbers, 1):
            if args.limit and len(prs) >= args.limit:
                break
            log(f"  [{index}/{len(numbers)}] loading PR #{number}", quiet=args.quiet)
            try:
                prs[number] = fetch_pr(gh, owner, repo, number)
            except RuntimeError as exc:
                problem(f"cannot load PR #{number}: {exc}")
                continue
            reasons.setdefault(number, set()).add("pr-author")

    if args.match in ("commits", "both"):
        sha_owner = {c["sha"]: number for number, pr in prs.items() for c in pr["_commits"]}
        query = f"repo:{owner}/{repo} author:{args.author} author-date:>={since}"
        log(f"searching commits authored by {args.author} since {since}", quiet=args.quiet)
        debug(f"commit search query: {query}")
        try:
            shas = [item["sha"] for item in gh.paginate(
                "/search/commits", {"q": query, "sort": "author-date", "order": "desc"},
                key="items", max_items=SEARCH_RESULT_CAP)]
        except GitHubError as exc:
            problem(f"commit search failed: {exc}",
                    hint="/search/commits needs a token that can read the repo contents; "
                         "a 422 usually means the --author login is unknown to GitHub")
            shas = []
        except RuntimeError as exc:
            problem(f"commit search failed: {exc}")
            shas = []
        orphans = []
        for sha in shas:
            if sha in sha_owner:  # already covered by a PR we loaded, no extra API call needed
                reasons.setdefault(sha_owner[sha], set()).add("commit-author")
            else:
                orphans.append(sha)
        total = gh.last_total_count
        log(f"  {len(shas)} commit(s)"
            + (f" (search total_count={total})" if total is not None else "")
            + f", {len(orphans)} not in the PRs found so far", quiet=args.quiet)
        for index, sha in enumerate(orphans, 1):
            if args.limit and len(prs) >= args.limit:
                break
            try:
                linked = gh.get(f"/repos/{owner}/{repo}/commits/{sha}/pulls")
            except RuntimeError as exc:
                warn(f"cannot resolve commit {sha[:8]} to a PR: {exc}")
                continue
            for item in linked:
                number = item["number"]
                if number in prs:
                    reasons.setdefault(number, set()).add("commit-author")
                    continue
                log(f"  [{index}/{len(orphans)}] {sha[:8]} -> PR #{number}", quiet=args.quiet)
                try:
                    prs[number] = fetch_pr(gh, owner, repo, number)
                except RuntimeError as exc:
                    problem(f"cannot load PR #{number}: {exc}")
                    continue
                reasons.setdefault(number, set()).add("commit-author")

    for number, pr in prs.items():
        pr["_reasons"] = sorted(reasons.get(number, set()))
    if not prs:
        diagnose_no_results(gh, args, owner, repo)
    return prs


def build_rows(prs: dict[int, dict], args: argparse.Namespace, aliases: set[str], web_base: str,
               owner: str, repo: str, since: str, jira: JiraClient | None, jira_base: str,
               summarizer: Summarizer) -> list[dict]:
    project_keys = ({k.strip().upper() for k in args.project_keys.split(",") if k.strip()}
                    if args.project_keys else None)
    rows: list[dict] = []
    dropped: Counter[str] = Counter()

    for number, pr in sorted(prs.items(), reverse=True):
        commits = pr["_commits"]
        mine = [c for c in commits if commit_matches_author(c, aliases)]
        if not mine and "pr-author" not in pr["_reasons"]:
            dropped["no commit matched the author aliases"] += 1
            continue
        if args.only_author_commits and not mine:
            dropped["--only-author-commits and no commit of their own"] += 1
            continue

        # In the window if the PR date is, or if one of the author's own commits is:
        # a long-lived PR opened before the window still counts for work done inside it.
        pr_date = iso_date(pr_window_date(pr, args.date_field))
        mine_dates = sorted(iso_date(((c.get("commit") or {}).get("author") or {}).get("date")) for c in mine)
        if not (pr_date >= since or (mine_dates and mine_dates[-1] >= since)):
            dropped[f"{args.date_field} date outside the --days window"] += 1
            continue

        state = state_of(pr)
        if args.state != "all" and state != args.state:
            dropped[f"state is not --state {args.state}"] += 1
            continue

        branch = (pr.get("head") or {}).get("ref") or ""
        commit_text = "\n".join((c.get("commit") or {}).get("message") or "" for c in commits)
        tickets = extract_tickets(
            [("branch", branch), ("title", pr.get("title") or ""),
             ("commit-message", commit_text), ("pr-body", pr.get("body") or "")],
            project_keys,
        )

        summary, summary_source = "", ""
        if args.summarize == "always" or (args.summarize == "no-ticket" and not tickets):
            summary, summary_source = summarizer.summarize(pr)

        details = {key: (jira.issue(key) if jira else {"summary": "", "status": "", "type": ""})
                   for key in tickets}
        commit_dates = sorted(iso_date(((c.get("commit") or {}).get("author") or {}).get("date"))
                              for c in commits)

        rows.append({
            "jira_tickets": ", ".join(tickets),
            "jira_links": " ".join(f"{jira_base}/browse/{key}" for key in tickets) if jira_base else "",
            "jira_summaries": " | ".join(details[k]["summary"] for k in tickets if details[k]["summary"]),
            "jira_statuses": ", ".join(f"{k}={details[k]['status']}" for k in tickets if details[k]["status"]),
            "jira_types": ", ".join(f"{k}={details[k]['type']}" for k in tickets if details[k]["type"]),
            "ticket_source": ", ".join(sorted(set(tickets.values()))),
            "pr_number": number,
            "pr_title": pr.get("title") or "",
            "pr_url": pr.get("html_url") or f"{web_base}/{owner}/{repo}/pull/{number}",
            "branch": branch,
            "pr_author": (pr.get("user") or {}).get("login") or "",
            "pr_state": state,
            "created_date": iso_date(pr.get("created_at")),
            "merged_date": iso_date(pr.get("merged_at")),
            "closed_date": iso_date(pr.get("closed_at")),
            "first_commit_date": commit_dates[0] if commit_dates else "",
            "last_commit_date": commit_dates[-1] if commit_dates else "",
            "author_first_commit_date": mine_dates[0] if mine_dates else "",
            "author_last_commit_date": mine_dates[-1] if mine_dates else "",
            "commits_total": pr.get("commits", len(commits)),
            "commits_by_author": len(mine),
            "files_changed": pr.get("changed_files", ""),
            "additions": pr.get("additions", ""),
            "deletions": pr.get("deletions", ""),
            "match_reason": ", ".join(pr["_reasons"]),
            "summary_200": summary,
            "summary_source": summary_source,
        })

    if dropped:
        log("filtered out " + ", ".join(f"{n} PR(s): {why}" for why, n in dropped.most_common()),
            quiet=args.quiet)
    if prs and not rows:
        problem(f"found {len(prs)} PR(s) for {args.author} but every one was filtered out",
                hint="loosen the filters above (--days / --date-field / --state / "
                     "--only-author-commits) or add --alias for the name/email used in the commits")
    return rows


def explode(rows: list[dict], jira_base: str) -> list[dict]:
    """One row per JIRA ticket; PRs without a ticket keep a single blank-ticket row."""
    out: list[dict] = []
    for row in rows:
        keys = [k.strip() for k in row["jira_tickets"].split(",") if k.strip()] or [""]
        for key in keys:
            new = {"jira_ticket": key,
                   "jira_link": f"{jira_base}/browse/{key}" if (key and jira_base) else "",
                   **{k: v for k, v in row.items() if k not in ("jira_tickets", "jira_links")}}
            out.append(new)
    return out


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CSV report of the JIRA tickets / PRs a GitHub user worked on.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("repo_url", help="GitHub repo URL, SSH remote, or owner/repo")
    parser.add_argument("--author", required=True, help="GitHub username to report on, e.g. trank")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, help="look-back window in days")
    parser.add_argument("--alias", action="append", default=[],
                        help="extra name/email fragment identifying the author in commits (repeatable)")
    parser.add_argument("--match", choices=["author", "commits", "both"], default="both",
                        help="PRs opened by the author, PRs containing their commits, or both")
    parser.add_argument("--date-field", choices=["created", "merged", "updated"], default="created",
                        help="which PR date the window applies to")
    parser.add_argument("--state", choices=["all", "merged", "open", "closed"], default="all")
    parser.add_argument("--only-author-commits", action="store_true",
                        help="drop PRs where the author has no commit of their own")
    parser.add_argument("--project-keys", default="",
                        help="comma-separated JIRA project keys to accept, e.g. ABC,DEF (default: heuristic)")
    parser.add_argument("--jira-base-url", default="", help="overrides JIRA_BASE_URL from .env")
    parser.add_argument("--no-jira", action="store_true", help="skip JIRA lookups (links only)")
    parser.add_argument("--summarizer", choices=["heuristic", "ollama", "anthropic", "none"],
                        default="heuristic", help="how to build the short summary column")
    parser.add_argument("--summarize", choices=["no-ticket", "always"], default="no-ticket",
                        help="summarize only PRs without a ticket, or every PR")
    parser.add_argument("--summary-chars", type=int, default=200, help="max characters of the summary column")
    parser.add_argument("--ollama-model", default="llama3.2:1b", help="model for --summarizer ollama")
    parser.add_argument("--ollama-host", default=os.environ.get("OLLAMA_HOST", "http://localhost:11434"))
    parser.add_argument("--anthropic-model", default="claude-haiku-4-5", help="model for --summarizer anthropic")
    parser.add_argument("--ai-timeout", type=int, default=60, help="timeout in seconds for AI summary calls")
    parser.add_argument("--explode-tickets", action="store_true", help="one row per JIRA ticket instead of per PR")
    parser.add_argument("--out", default="", help="output CSV path (default: reports/<repo>_<author>_<date>.csv)")
    parser.add_argument("--limit", type=int, default=0, help="stop after N PRs (for testing)")
    parser.add_argument("--quiet", action="store_true",
                        help="suppress progress output (warnings and errors are still printed)")
    parser.add_argument("--debug", action="store_true",
                        help="log every HTTP request and print tracebacks")
    return parser.parse_args(argv)


def err(message: str, *, hint: str = "") -> int:
    log(f"ERROR: {message}")
    if hint:
        log(f"  -> {hint}")
    return 2


def run(argv: list[str] | None = None) -> int:
    global DEBUG
    args = parse_args(argv)
    DEBUG = args.debug
    load_env()

    token = env_first("GIT_PAT", "GITHUB_PAT", "GITHUB_TOKEN", "GH_TOKEN")
    if not token:
        return err("no GitHub token found",
                   hint="set GIT_PAT (or GITHUB_PAT / GITHUB_TOKEN) in a .env file next to "
                        f"{SCRIPT_DIR}\\{Path(__file__).name} or in the environment")

    owner, repo, api_base, web_base = parse_repo(args.repo_url)
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    aliases = {args.author.lower(), *(a.lower() for a in args.alias)}

    gh = GitHubClient(token, api_base, quiet=args.quiet)
    try:
        preflight(gh, args, owner, repo)
    except GitHubError as exc:
        hints = {
            401: "the token is invalid or expired",
            403: "the token cannot read this repo: check the org's SSO authorization and, for a "
                 "fine-grained PAT, 'Pull requests: read' + 'Contents: read'",
            404: "either the repo does not exist under that name, or the token cannot see it "
                 "(private repo without access, or missing SSO authorization)",
        }
        return err(f"cannot read {owner}/{repo}: {exc}", hint=hints.get(exc.status, ""))
    except RuntimeError as exc:
        return err(f"cannot reach {api_base}: {exc}")

    jira_base = (args.jira_base_url or env_first("JIRA_BASE_URL")).rstrip("/")
    jira_token = env_first("JIRA_PAT", "JIRA_TOKEN", "JIRA_API_TOKEN")
    jira: JiraClient | None = None
    if not args.no_jira:
        if jira_base and jira_token:
            jira = JiraClient(jira_base, jira_token, email=env_first("JIRA_EMAIL"), quiet=args.quiet)
        elif not jira_base:
            warn("JIRA_BASE_URL not set: ticket links and details will be blank "
                 "(pass --no-jira to silence this)")
        else:
            warn("JIRA_PAT not set: ticket links only, no summary/status "
                 "(pass --no-jira to silence this)")

    summarizer = Summarizer(args)

    log(f"repo {owner}/{repo} | author {args.author} | window {since}..today ({args.days}d)", quiet=args.quiet)
    prs = discover(gh, args, owner, repo, since)
    pr_rows = build_rows(prs, args, aliases, web_base, owner, repo, since, jira, jira_base, summarizer)
    rows = explode(pr_rows, jira_base) if args.explode_tickets else pr_rows

    out_path = Path(args.out) if args.out else (
        SCRIPT_DIR / "reports" / f"{owner}-{repo}_{args.author}_{datetime.now():%Y%m%d}.csv")
    fieldnames = list(rows[0].keys()) if rows else ["jira_tickets", "pr_number", "pr_url"]
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    except OSError as exc:
        return err(f"cannot write {out_path}: {exc}",
                   hint="close the file if it is open in Excel, or pass a different --out path")

    tickets = {k.strip() for row in pr_rows for k in row["jira_tickets"].split(",") if k.strip()}
    commits = sum(int(row["commits_by_author"] or 0) for row in pr_rows)
    log("", quiet=args.quiet)
    log(f"PRs: {len(pr_rows)} | rows: {len(rows)} | distinct JIRA tickets: {len(tickets)} | "
        f"commits by {args.author}: {commits} | GitHub API calls: {gh.calls}", quiet=args.quiet)
    if not rows:
        log(f"the report is EMPTY: {out_path} contains headers only")

    if PROBLEMS:
        log("")
        log(f"{len(PROBLEMS)} problem(s) detected during this run:")
        for index, message in enumerate(PROBLEMS, 1):
            log(f"  {index}. {message}")
    print(out_path)
    return 1 if PROBLEMS else 0


def main(argv: list[str] | None = None) -> int:
    """Wraps run() so no failure can end as a bare traceback with no explanation."""
    try:
        return run(argv)
    except KeyboardInterrupt:
        log("interrupted")
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - report anything unexpected, then exit non-zero
        log("")
        log(f"ERROR: unexpected failure: {type(exc).__name__}: {exc}")
        if DEBUG:
            traceback.print_exc()
        else:
            log("  -> re-run with --debug for the traceback and the HTTP request log")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
