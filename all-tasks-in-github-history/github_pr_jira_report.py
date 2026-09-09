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
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
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
def log(msg: str, *, quiet: bool = False) -> None:
    """stderr progress output, safe on a legacy (cp1252) Windows console."""
    if quiet:
        return
    try:
        print(msg, file=sys.stderr, flush=True)
    except UnicodeEncodeError:
        encoding = sys.stderr.encoding or "ascii"
        print(msg.encode(encoding, "replace").decode(encoding), file=sys.stderr, flush=True)


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
class GitHubClient:
    def __init__(self, token: str, api_base: str, *, timeout: int = 30, quiet: bool = False):
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout
        self.quiet = quiet
        self.calls = 0
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
        for attempt in range(6):
            self.calls += 1
            last = self.session.get(url, params=params, timeout=self.timeout)
            if self._rate_limited(last):
                wait = self._sleep_for(last)
                log(f"  rate limited by GitHub, sleeping {wait:.0f}s", quiet=self.quiet)
                time.sleep(wait)
                continue
            if last.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            return last
        return last  # type: ignore[return-value]

    def get(self, url: str, params: dict | None = None) -> Any:
        response = self.request(url, params)
        if not response.ok:
            raise RuntimeError(f"GitHub {response.status_code} for {url}: {truncate(response.text, 300)}")
        return response.json()

    def paginate(self, path: str, params: dict | None = None, *, key: str | None = None,
                 max_items: int | None = None) -> Iterator[dict]:
        query: dict | None = dict(params or {})
        query.setdefault("per_page", 100)
        url, sent = path, 0
        while url:
            response = self.request(url, query)
            if not response.ok:
                raise RuntimeError(f"GitHub {response.status_code} for {url}: {truncate(response.text, 300)}")
            payload = response.json()
            items = payload.get(key, []) if key else payload
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
            elif response.status_code in (401, 403):
                info["status"] = "NO_ACCESS"
            elif response.ok:
                fields = response.json().get("fields", {})
                info = {
                    "summary": fields.get("summary") or "",
                    "status": ((fields.get("status") or {}).get("name")) or "",
                    "type": ((fields.get("issuetype") or {}).get("name")) or "",
                }
            else:
                info["status"] = f"HTTP_{response.status_code}"
        except requests.RequestException as exc:
            log(f"  jira lookup failed for {key}: {exc}", quiet=self.quiet)
            info["status"] = "ERROR"
        self.cache[key] = info
        return info


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
                log("anthropic SDK not installed (pip install anthropic); using heuristic summaries")
                self.mode = "heuristic"
                return
            key = env_first("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
            try:
                self.client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
            except Exception as exc:  # noqa: BLE001 - any init failure degrades gracefully
                log(f"anthropic client unavailable ({exc}); using heuristic summaries")
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
            log(message)
            self.warned = True


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
def discover(gh: GitHubClient, args: argparse.Namespace, owner: str, repo: str, since: str) -> dict[int, dict]:
    """-> {pr_number: pr json enriched with _commits and _reasons}"""
    prs: dict[int, dict] = {}
    reasons: dict[int, set[str]] = {}

    if args.match in ("author", "both"):
        query = f"repo:{owner}/{repo} type:pr author:{args.author} {args.date_field}:>={since}"
        log(f"searching PRs opened by {args.author} ({args.date_field} >= {since})", quiet=args.quiet)
        numbers = [item["number"] for item in gh.paginate(
            "/search/issues", {"q": query, "sort": "created", "order": "desc"},
            key="items", max_items=SEARCH_RESULT_CAP)]
        log(f"  {len(numbers)} PR(s) opened by {args.author}", quiet=args.quiet)
        for index, number in enumerate(numbers, 1):
            if args.limit and len(prs) >= args.limit:
                break
            log(f"  [{index}/{len(numbers)}] loading PR #{number}", quiet=args.quiet)
            prs[number] = fetch_pr(gh, owner, repo, number)
            reasons.setdefault(number, set()).add("pr-author")

    if args.match in ("commits", "both"):
        sha_owner = {c["sha"]: number for number, pr in prs.items() for c in pr["_commits"]}
        query = f"repo:{owner}/{repo} author:{args.author} author-date:>={since}"
        log(f"searching commits authored by {args.author} since {since}", quiet=args.quiet)
        try:
            shas = [item["sha"] for item in gh.paginate(
                "/search/commits", {"q": query, "sort": "author-date", "order": "desc"},
                key="items", max_items=SEARCH_RESULT_CAP)]
        except RuntimeError as exc:
            log(f"  commit search unavailable: {exc}", quiet=args.quiet)
            shas = []
        orphans = []
        for sha in shas:
            if sha in sha_owner:  # already covered by a PR we loaded, no extra API call needed
                reasons.setdefault(sha_owner[sha], set()).add("commit-author")
            else:
                orphans.append(sha)
        log(f"  {len(shas)} commit(s), {len(orphans)} not in the PRs found so far", quiet=args.quiet)
        for index, sha in enumerate(orphans, 1):
            if args.limit and len(prs) >= args.limit:
                break
            try:
                linked = gh.get(f"/repos/{owner}/{repo}/commits/{sha}/pulls")
            except RuntimeError as exc:
                log(f"  {sha[:8]}: {exc}", quiet=args.quiet)
                continue
            for item in linked:
                number = item["number"]
                if number in prs:
                    reasons.setdefault(number, set()).add("commit-author")
                    continue
                log(f"  [{index}/{len(orphans)}] {sha[:8]} -> PR #{number}", quiet=args.quiet)
                prs[number] = fetch_pr(gh, owner, repo, number)
                reasons.setdefault(number, set()).add("commit-author")

    for number, pr in prs.items():
        pr["_reasons"] = sorted(reasons.get(number, set()))
    return prs


def build_rows(prs: dict[int, dict], args: argparse.Namespace, aliases: set[str], web_base: str,
               owner: str, repo: str, since: str, jira: JiraClient | None, jira_base: str,
               summarizer: Summarizer) -> list[dict]:
    project_keys = ({k.strip().upper() for k in args.project_keys.split(",") if k.strip()}
                    if args.project_keys else None)
    rows: list[dict] = []

    for number, pr in sorted(prs.items(), reverse=True):
        commits = pr["_commits"]
        mine = [c for c in commits if commit_matches_author(c, aliases)]
        if not mine and "pr-author" not in pr["_reasons"]:
            continue
        if args.only_author_commits and not mine:
            continue

        # In the window if the PR date is, or if one of the author's own commits is:
        # a long-lived PR opened before the window still counts for work done inside it.
        pr_date = iso_date(pr_window_date(pr, args.date_field))
        mine_dates = sorted(iso_date(((c.get("commit") or {}).get("author") or {}).get("date")) for c in mine)
        if not (pr_date >= since or (mine_dates and mine_dates[-1] >= since)):
            continue

        state = state_of(pr)
        if args.state != "all" and state != args.state:
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
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    return parser.parse_args(argv)


def err(message: str) -> int:
    log(f"error: {message}")
    return 2


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    load_env()

    token = env_first("GIT_PAT", "GITHUB_PAT", "GITHUB_TOKEN", "GH_TOKEN")
    if not token:
        return err("No GitHub token found. Set GIT_PAT (or GITHUB_PAT) in .env")

    owner, repo, api_base, web_base = parse_repo(args.repo_url)
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
    aliases = {args.author.lower(), *(a.lower() for a in args.alias)}

    gh = GitHubClient(token, api_base, quiet=args.quiet)
    try:
        gh.get(f"/repos/{owner}/{repo}")
    except RuntimeError as exc:
        return err(f"Cannot read {owner}/{repo}: {exc}")

    jira_base = (args.jira_base_url or env_first("JIRA_BASE_URL")).rstrip("/")
    jira_token = env_first("JIRA_PAT", "JIRA_TOKEN", "JIRA_API_TOKEN")
    jira: JiraClient | None = None
    if not args.no_jira:
        if jira_base and jira_token:
            jira = JiraClient(jira_base, jira_token, email=env_first("JIRA_EMAIL"), quiet=args.quiet)
        elif not jira_base:
            log("JIRA_BASE_URL not set: ticket links and details will be blank", quiet=args.quiet)
        else:
            log("JIRA_PAT not set: ticket links only, no summary/status", quiet=args.quiet)

    summarizer = Summarizer(args)

    log(f"repo {owner}/{repo} | author {args.author} | window {since}..today ({args.days}d)", quiet=args.quiet)
    prs = discover(gh, args, owner, repo, since)
    pr_rows = build_rows(prs, args, aliases, web_base, owner, repo, since, jira, jira_base, summarizer)
    rows = explode(pr_rows, jira_base) if args.explode_tickets else pr_rows

    out_path = Path(args.out) if args.out else (
        SCRIPT_DIR / "reports" / f"{owner}-{repo}_{args.author}_{datetime.now():%Y%m%d}.csv")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) if rows else ["jira_tickets", "pr_number", "pr_url"]
    with out_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    tickets = {k.strip() for row in pr_rows for k in row["jira_tickets"].split(",") if k.strip()}
    commits = sum(int(row["commits_by_author"] or 0) for row in pr_rows)
    log("", quiet=args.quiet)
    log(f"PRs: {len(pr_rows)} | rows: {len(rows)} | distinct JIRA tickets: {len(tickets)} | "
        f"commits by {args.author}: {commits} | GitHub API calls: {gh.calls}", quiet=args.quiet)
    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
