#!/usr/bin/env python3
"""Group the per-PR report from github_pr_jira_report.py into themes of work.

The per-PR CSV answers "which PRs did this person merge"; 200 rows of it does
not answer "what did they actually work on". This script folds those rows into
one row per *theme* - a piece of work someone would name in a review - and keeps
only the columns needed to read that, plus the links back to every JIRA ticket
and pull request the theme was built from.

Grouping happens in two steps, neither of which needs the network:

  * PRs that share a JIRA ticket are the same work, so they are grouped first.
    A key only counts when it came from the PR title or the branch name
    (``ticket_source``): a long-lived branch mentions every key it merged in, so
    a commit-message key is trusted only when it is the single key on the PR;
  * those ticket groups (and the PRs with no ticket at all) are then merged into
    themes by text similarity - idf-weighted cosine over the ticket summary, PR
    title and branch name, so that "cross org - Phase 1" and "Phase 2", or five
    PRs each called "update claude skill", land in one row.

Merging compares whole groups, never single PRs, and every pair of parts in a
theme has to clear the threshold, not just the pair that merged last. Both rules
exist to stop chaining: one vague title ("resolve conflict"), or a repo where
every PR is called "update the API", otherwise pulls one theme after another
into a single row that means nothing.

Examples
--------
  python pr_group_report.py                      # newest CSV in reports/
  python pr_group_report.py reports/<file>.csv --since 2026-04-01
  python pr_group_report.py reports/<file>.csv --similarity 0.45 --sort recent

Anything that can make the report wrong is printed on the console: WARNING for
recoverable trouble, ERROR for problems that probably invalidate the output
(exit code 2 = the run could not start, 1 = the report was written but has
problems). Add --debug for the grouping decisions.
"""

from __future__ import annotations

import argparse
import csv
import heapq
import math
import re
import sys
import traceback
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_REPORTS_DIR = SCRIPT_DIR / "reports"

# How close two groups must be to become one theme. Tuned on a 200-PR report:
# much lower merges "read indicator" with "mention indicator" work, much higher
# splits "cross org - Phase 1" from "Phase 2".
DEFAULT_SIMILARITY = 0.55
# Two groups also need this many shared meaningful words, so a single rare word
# in common cannot merge them on its own.
MIN_SHARED_TOKENS = 2
# Words on more than this share of the PRs carry no signal ("update", "api").
COMMON_TOKEN_SHARE = 0.30
DEFAULT_ITEM_CHARS = 500
MAX_ITEMS = 8

KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,6}\b")
# Where github_pr_jira_report.py found the key. Anything else is a weak match.
TRUSTED_SOURCES = ("title", "branch")

OUT_FIELDS = ["theme", "category", "work_items", "prs", "commits",
              "first_merged", "last_merged", "jira_tickets", "jira_links", "pr_links"]

# First rule that matches a PR's text wins, so the order matters: the narrow,
# unambiguous words come before the ones every title contains. The theme takes
# the majority vote of its PRs, and a JIRA issue type wins over the text.
CATEGORY_RULES = [
    ("Tooling & monitoring", r"(\bscripts?\b|\bskill\b|\bclaude\b|\bcopilot\b|\bkibana\b|"
                             r"\bappd\b|appdynamics|\bgrafana\b|\bdashboard\b|\bmonitoring\b|"
                             r"\balerts?\b|\bpipelines?\b|\bjenkins\b|\bdocker|\bhelm\b|"
                             r"\bdeploy(ment)?\b|\breadme\b|\bdocs\b|documentation)"),
    ("Testing", r"(\btests?\b|\btesting\b|contract[- ]test|\bcoverage\b|\bjunit\b|\bmocks?\b)"),
    ("Bug fix", r"(bug ?fix|\bbug\b|\bfix(es|ed|ing)?\b|\bnpe\b|null ?pointer|null[- ]?safe|"
                r"\bexception\b|\bcrash|\bregression\b|\bhotfix\b|\bdefect\b|\bguard\b)"),
    ("Performance & resilience", r"(\bperformance\b|\bslow(ness)?\b|\btimeouts?\b|\blatency\b|"
                                 r"circuit[- ]?breaker|\bretr(y|ies)\b|\bthrottl|\bcach(e|ing)\b|"
                                 r"\bindex(es|ing)?\b|optimi[sz]|\bmemory\b|\bheap\b|"
                                 r"\bconnection ?pool\b|\bconfig(uration|ure)?\b)"),
    ("Refactoring & cleanup", r"(\brefactor|\bclean[- ]?up\b|\bcleanup\b|\brename\b|\bremove\b|"
                              r"\bdeletes?\b|\bdeprecat|\bmigrat(e|ion)\b|\bupgrade\b)"),
]
CATEGORY_ORDER = [name for name, _ in CATEGORY_RULES] + ["Feature"]
CATEGORY_RES = [(name, re.compile(pattern, re.I)) for name, pattern in CATEGORY_RULES]
# A defect ticket is a defect whatever its title says.
TYPE_CATEGORIES = {
    "bug": "Bug fix", "production defect": "Bug fix", "pre-production defect": "Bug fix",
    "defect": "Bug fix", "root cause action item": "Bug fix",
}

STOP = set("""
a about after all also an and any are as at be because been before being but by can cannot could
did do does doing done for from get gets got had has have having he her him his how i if in into is
it its just make makes made may me might more most must my no nor not now of off on once one only
or our out over own per put putting same set sets she should so some such than that the their
them then there these they this those to too two under until up us use used uses using very via
was way we well were what when where which while who why will with within would you your
""".split())
# Version-control noise: real words, but they never describe the work.
NOISE = set("""
branch bump cherry conflict conflicts develop dev jira main master merge merged merging pick pr prs
rebase resolve resolved revert reverted snapshot squash story temp ticket tmp version wip minor misc
""".split())


# --------------------------------------------------------------------------- #
# helpers (same reporting contract as github_pr_jira_report.py)
# --------------------------------------------------------------------------- #
PROBLEMS: list[str] = []
DEBUG = False
# The JIRA browse base is read back from the input CSV, so this script needs no
# credentials and no configuration of its own.
JIRA_BASE = ""


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
    log(f"WARNING: {msg}")


def problem(msg: str, *, hint: str = "") -> None:
    PROBLEMS.append(msg)
    log(f"ERROR: {msg}")
    if hint:
        log(f"  -> {hint}")


def debug(msg: str) -> None:
    if DEBUG:
        log(f"debug: {msg}")


def err(message: str, *, hint: str = "") -> int:
    log(f"ERROR: {message}")
    if hint:
        log(f"  -> {hint}")
    return 2


def truncate(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[: limit - 3]
    if " " in cut[limit // 2:]:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:-|") + "..."


# --------------------------------------------------------------------------- #
# text
# --------------------------------------------------------------------------- #
CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
# "Scs 5075 read indicator": what a branch name looks like once GitHub has
# turned it into a PR title.
SPACED_KEY_RE = re.compile(r"\b[A-Za-z]{2,10}[ _-]\d{3,6}\b")
NON_WORD_RE = re.compile(r"[^A-Za-z0-9]+")


def tokenize(text: str) -> list[str]:
    """-> the meaningful words of a title / branch / summary, first-seen order."""
    text = KEY_RE.sub(" ", text or "")
    text = SPACED_KEY_RE.sub(" ", text)
    text = CAMEL_RE.sub(" ", text)
    out: list[str] = []
    for raw in NON_WORD_RE.split(text.lower()):
        word = raw.strip()
        if len(word) < 3 or word.isdigit() or word in STOP or word in NOISE:
            continue
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]  # participants and participant are the same word here
        if word not in out:
            out.append(word)
    return out


def fingerprint(text: str) -> str:
    """A key for spotting two titles that differ only in punctuation or a key."""
    return " ".join(sorted(tokenize(text)))


# --------------------------------------------------------------------------- #
# input
# --------------------------------------------------------------------------- #
class PR:
    __slots__ = ("number", "url", "title", "branch", "merged", "commits",
                 "keys", "sources", "primary", "summaries", "types", "tokens")

    def __init__(self, row: dict[str, str]):
        self.url = (row.get("pr_url") or "").strip()
        self.number = self.url.rstrip("/").rpartition("/")[2]
        self.title = (row.get("pr_title") or row.get("summary") or "").strip()
        self.branch = (row.get("branch") or "").strip()
        self.merged = (row.get("merged_date") or "").strip()
        try:
            self.commits = int(row.get("commits_by_author") or 0)
        except ValueError:
            self.commits = 0

        links = row.get("jira_links") or row.get("jira_link") or ""
        self.keys = list(dict.fromkeys(KEY_RE.findall(links) or KEY_RE.findall(self.title)))
        self.sources = [s.strip() for s in (row.get("ticket_source") or "").split(",") if s.strip()]
        self.primary = self._primary()

        raw = row.get("jira_summaries") or row.get("jira_summary") or ""
        parts = [p.strip() for p in raw.split(" | ") if p.strip()]
        # The summaries column only holds the non-empty ones, so it lines up with
        # the keys exactly when every key resolved.
        self.summaries = dict(zip(self.keys, parts)) if len(parts) == len(self.keys) else {}
        self.types = [t.partition("=")[2].strip().lower()
                      for t in (row.get("jira_types") or "").split(",") if "=" in t]
        if not self.types and (row.get("jira_type") or "").strip():
            self.types = [row["jira_type"].strip().lower()]

        self.tokens = self._tokens()

    def _tokens(self) -> list[str]:
        return tokenize(" ".join([" | ".join(self.summaries.values()), self.title, self.branch]))

    def _primary(self) -> list[str]:
        """The keys that say what this PR was about.

        A key found only in the commit messages is trustworthy when it is the
        only one; when a long-lived branch dragged in five, none of them says
        what the PR was about, so the text has to decide instead. The count is
        taken over the whole PR, not one --explode-tickets row of it."""
        trusted = any(source in TRUSTED_SOURCES for source in self.sources)
        return self.keys if (trusted or len(self.keys) == 1) else []

    def absorb(self, other: PR) -> None:
        """Fold an --explode-tickets sibling row back into this PR."""
        self.keys = list(dict.fromkeys(self.keys + other.keys))
        self.sources = list(dict.fromkeys(self.sources + other.sources))
        self.summaries = {**other.summaries, **self.summaries}
        self.types = list(dict.fromkeys(self.types + other.types))
        self.commits = max(self.commits, other.commits)  # repeated, not additive
        self.primary = self._primary()
        self.tokens = self._tokens()

    def jira_links(self) -> list[str]:
        return [f"{JIRA_BASE}/browse/{key}" for key in self.primary] if JIRA_BASE else []


def load_prs(path: Path, args: argparse.Namespace) -> list[PR]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("the file has no data row")
    missing = [c for c in ("pr_title", "merged_date", "pr_url") if c not in rows[0]]
    if missing:
        raise ValueError(f"missing the column(s) {', '.join(missing)} - this is not a "
                         f"github_pr_jira_report.py CSV")

    prs: dict[str, PR] = {}
    dropped = 0
    for row in rows:
        pr = PR(row)
        if (args.since and pr.merged and pr.merged < args.since) or \
           (args.until and pr.merged and pr.merged > args.until):
            dropped += 1
            continue
        # --explode-tickets writes one row per ticket: fold them back per PR.
        identity = pr.url or f"{pr.title}@{pr.merged}"
        if identity in prs:
            prs[identity].absorb(pr)
        else:
            prs[identity] = pr
    if dropped:
        log(f"  {dropped} PR(s) merged outside {args.since or '...'}..{args.until or '...'} "
            f"were skipped", quiet=args.quiet)
    return list(prs.values())


# --------------------------------------------------------------------------- #
# grouping
# --------------------------------------------------------------------------- #
class Group:
    """A candidate theme: its PRs and the idf-weighted centroid of their words."""

    def __init__(self, prs: list[PR], weights: dict[str, float]):
        self.prs = prs
        self.vector = self._vector(weights)

    def _vector(self, weights: dict[str, float]) -> dict[str, float]:
        vector: dict[str, float] = defaultdict(float)
        for pr in self.prs:
            for token in pr.tokens:
                vector[token] += weights.get(token, 0.0)
        norm = math.sqrt(sum(value * value for value in vector.values())) or 1.0
        return {token: value / norm for token, value in vector.items() if value}

    def absorb(self, other: "Group", weights: dict[str, float]) -> None:
        self.prs.extend(other.prs)
        self.vector = self._vector(weights)


def token_weights(prs: list[PR]) -> dict[str, float]:
    """idf: a word on every second PR ("update", "api") must not make two
    unrelated themes look alike, so it is worth nothing."""
    df = Counter(token for pr in prs for token in pr.tokens)
    total = len(prs) or 1
    weights: dict[str, float] = {}
    for token, count in df.items():
        too_common = count > 2 and count / total > COMMON_TOKEN_SHARE
        weights[token] = 0.0 if too_common else math.log(total / count) + 1.0
    debug("ignored as too common: "
          + ", ".join(sorted(t for t, w in weights.items() if not w)))
    return weights


def similarity(left: Group, right: Group) -> float:
    shared = [token for token in left.vector if token in right.vector]
    if len(shared) < MIN_SHARED_TOKENS:
        return 0.0
    return sum(left.vector[token] * right.vector[token] for token in shared)


def seed_groups(prs: list[PR], weights: dict[str, float]) -> list[Group]:
    """One group per JIRA ticket, plus one per PR with no trustworthy ticket.
    Tickets closed by the same PR are one group: they are the same work."""
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        while parent.setdefault(key, key) != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for pr in prs:
        for key in pr.primary[1:]:
            root_a, root_b = find(pr.primary[0]), find(key)
            if root_a != root_b:
                parent[root_a] = root_b

    buckets: dict[str, list[PR]] = defaultdict(list)
    for index, pr in enumerate(prs):
        buckets[find(pr.primary[0]) if pr.primary else f"pr#{index}"].append(pr)
    return [Group(members, weights) for members in buckets.values()]


def merge_groups(groups: list[Group], weights: dict[str, float],
                 threshold: float) -> list[Group]:
    """Agglomerate the seed groups while any two are closer than the threshold.

    Whole seed groups are compared, never single PRs, and a theme only forms when
    *every* pair of parts in it clears the threshold (complete linkage): with the
    looser rule, a repo whose PR titles all look alike ends up with one theme
    holding everything, each merge dragged in by its predecessor.

    The similarity of a merged theme to everything else is therefore the *worst*
    of the two it came from, which needs no new vector arithmetic - so the pair
    scores are computed once, kept in a heap, and stale entries are skipped when
    they surface."""
    live = {id(group): group for group in groups}
    scores: dict[tuple[int, int], float] = {}
    for index, left in enumerate(groups):
        for right in groups[index + 1:]:
            score = similarity(left, right)
            if score >= threshold:  # a pair below it can never rise
                scores[pair_key(left, right)] = score
    heap = [(-score, key) for key, score in scores.items()]
    heapq.heapify(heap)

    while heap:
        negated, key = heapq.heappop(heap)
        if scores.get(key) != -negated:
            continue  # a stale score, superseded by a merge
        left, right = live.get(key[0]), live.get(key[1])
        if left is None or right is None:
            continue  # one side has already been merged away
        if -negated < threshold:
            break
        debug(f"merge (sim {-negated:.2f}) {theme_label(left)!r} <- {theme_label(right)!r}")
        for other in live.values():
            if other is left or other is right:
                continue
            worst = min(scores.pop(pair_key(left, other), 0.0),
                        scores.pop(pair_key(right, other), 0.0))
            if worst >= threshold:
                scores[pair_key(left, other)] = worst
                heapq.heappush(heap, (-worst, pair_key(left, other)))
        left.absorb(right, weights)
        # Dropped from live and from every score in the same step, so its id can
        # never be reused while something still refers to it.
        del live[id(right)]
        scores = {k: v for k, v in scores.items() if id(right) not in k}
    return list(live.values())


def pair_key(left: Group, right: Group) -> tuple[int, int]:
    return (id(left), id(right)) if id(left) < id(right) else (id(right), id(left))


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def theme_label(group: Group) -> str:
    """The name of the theme: the JIRA summary of the ticket with the most PRs,
    else the PR title closest to the middle of the group."""
    per_ticket: Counter[str] = Counter()
    summaries: dict[str, str] = {}
    for pr in group.prs:
        for key in pr.primary:
            if pr.summaries.get(key):
                per_ticket[key] += 1
                summaries[key] = pr.summaries[key]
    if per_ticket:
        return summaries[per_ticket.most_common(1)[0][0]]

    best, label = -1.0, ""
    for pr in group.prs:
        score = sum(group.vector.get(token, 0.0) for token in pr.tokens)
        if score > best and (pr.title or pr.branch):
            best, label = score, pr.title or pr.branch
    return label


def pr_category(pr: PR) -> str:
    for issue_type in pr.types:
        if issue_type in TYPE_CATEGORIES:
            return TYPE_CATEGORIES[issue_type]
    text = " ".join([" | ".join(pr.summaries.values()), pr.title, pr.branch])
    for name, pattern in CATEGORY_RES:
        if pattern.search(text):
            return name
    return "Feature"


def theme_category(group: Group) -> str:
    votes = Counter(pr_category(pr) for pr in group.prs)
    top = max(votes.values())
    return min((name for name, count in votes.items() if count == top),
               key=CATEGORY_ORDER.index)


def distinct(texts: Iterable[str]) -> list[str]:
    """Deduplicated on words, so the near-copies a stack of follow-up PRs
    produces ("...19c minor improvement" twice) collapse into one item."""
    items: list[str] = []
    seen: set[str] = set()
    for text in texts:
        text = " ".join((text or "").split())
        mark = fingerprint(text)
        if not text or not mark or mark in seen:
            continue
        seen.add(mark)
        items.append(text)
    return items


def work_items(group: Group, limit: int) -> str:
    """What the theme is made of, oldest first.

    A theme covering several tickets is described by those tickets - that is its
    scope. A theme that is one ticket (or none) is described by its PR titles
    instead, because repeating the theme's own name says nothing."""
    by_date = sorted(group.prs, key=lambda p: (p.merged, p.number))
    tickets = distinct(f"{key}: {pr.summaries[key]}"
                       for pr in by_date for key in pr.primary if pr.summaries.get(key))
    titles = distinct(strip_key(pr.title) or pr.branch for pr in by_date)
    # The last fallback is the raw titles: a title made only of stop words
    # ("merge dev") leaves no fingerprint, so distinct() drops it, and this
    # column must never come out empty when there is anything at all to say.
    items = ((tickets if len(tickets) > 1 else titles) or tickets or titles
             or [pr.title or pr.branch for pr in by_date if pr.title or pr.branch])
    extra = len(items) - MAX_ITEMS
    text = truncate(" | ".join(items[:MAX_ITEMS]), limit)
    return f"{text} (+{extra} more)" if extra > 0 else text


def strip_key(title: str) -> str:
    """Drop the ticket key a PR title starts with: the row already reports it."""
    return re.sub(r"^\s*(" + KEY_RE.pattern + r"|" + SPACED_KEY_RE.pattern + r")[\s:+.-]*",
                  "", title or "", count=1, flags=re.IGNORECASE).strip()


def group_row(group: Group, args: argparse.Namespace, *, theme: str = "",
              category: str = "") -> dict:
    by_date = sorted(group.prs, key=lambda p: (p.merged, p.number))
    dates = [pr.merged for pr in by_date if pr.merged]
    return {
        "theme": theme or truncate(theme_label(group), 200) or "(untitled)",
        "category": category or theme_category(group),
        "work_items": work_items(group, args.item_chars),
        "prs": len(group.prs),
        "commits": sum(pr.commits for pr in group.prs),
        "first_merged": dates[0] if dates else "",
        "last_merged": dates[-1] if dates else "",
        "jira_tickets": ", ".join(dict.fromkeys(k for pr in by_date for k in pr.primary)),
        "jira_links": " ".join(dict.fromkeys(l for pr in by_date for l in pr.jira_links())),
        "pr_links": " ".join(pr.url for pr in by_date if pr.url),
    }


def build_rows(groups: list[Group], args: argparse.Namespace,
               weights: dict[str, float]) -> list[dict]:
    keep, tail = groups, []
    if args.fold_below > 1:
        keep = [g for g in groups if len(g.prs) >= args.fold_below]
        tail = [g for g in groups if len(g.prs) < args.fold_below]

    rows = [group_row(group, args) for group in keep]
    if tail:
        # One row for the long tail, so the small stuff stays visible - and its
        # links stay complete - without one line per drive-by change.
        folded = Group([pr for group in tail for pr in group.prs], weights)
        row = group_row(folded, args,
                        theme=f"Other, smaller changes ({len(tail)} separate items)",
                        category="Mixed")
        row["_last"] = True  # leftovers belong at the bottom, whatever the sort
        rows.append(row)
    sorters = {
        "prs": lambda r: (-r["prs"], -r["commits"], r["last_merged"]),
        "commits": lambda r: (-r["commits"], -r["prs"], r["last_merged"]),
        "recent": lambda r: (r["last_merged"], r["first_merged"]),
    }
    rows.sort(key=sorters[args.sort], reverse=args.sort == "recent")
    rows.sort(key=lambda r: r.get("_last", False))
    return rows


def write_csv(rows: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=OUT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def sniff_jira_base(path: Path) -> str:
    """-> the JIRA base URL of the input's links, so the output can rebuild them."""
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            for value in (row.get("jira_links") or row.get("jira_link") or "").split():
                base, sep, _ = value.partition("/browse/")
                if sep:
                    return base
    return ""


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
THEMES_SUFFIX = "_themes.csv"


def newest_report() -> Path | None:
    candidates = [p for p in DEFAULT_REPORTS_DIR.glob("*.csv")
                  if not p.name.endswith(THEMES_SUFFIX)]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Group a github_pr_jira_report.py CSV into one row per theme of work.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("csv_path", nargs="?", default="",
                        help="the per-PR CSV to analyze (default: the newest one in reports/)")
    parser.add_argument("--out", default="", help="output CSV path (default: <input>_themes.csv)")
    parser.add_argument("--since", default="", metavar="YYYY-MM-DD",
                        help="only PRs merged on or after this date")
    parser.add_argument("--until", default="", metavar="YYYY-MM-DD",
                        help="only PRs merged on or before this date")
    parser.add_argument("--similarity", type=float, default=DEFAULT_SIMILARITY,
                        help="0-1: how alike two groups must be to become one theme "
                             "(lower = fewer, broader themes)")
    parser.add_argument("--no-text-grouping", action="store_true",
                        help="group by JIRA ticket only, never by text similarity")
    parser.add_argument("--sort", choices=["prs", "commits", "recent"], default="prs",
                        help="row order: most PRs, most commits, or latest activity first")
    parser.add_argument("--fold-below", type=int, default=0, metavar="N",
                        help="collect every theme with fewer than N PRs into one "
                             "'Other, smaller changes' row (0 = keep them all)")
    parser.add_argument("--item-chars", type=int, default=DEFAULT_ITEM_CHARS,
                        help="max characters of the work_items column")
    parser.add_argument("--top", type=int, default=15,
                        help="how many themes to print on the console (0 = none)")
    parser.add_argument("--quiet", action="store_true", help="suppress progress output")
    parser.add_argument("--debug", action="store_true", help="log every grouping decision")
    return parser.parse_args(argv)


def pick_input(args: argparse.Namespace) -> Path | None:
    if args.csv_path:
        path = Path(args.csv_path).expanduser()
        if not path.is_file():
            err(f"{path} does not exist")
            return None
        return path
    found = newest_report()
    if found is None:
        err(f"no per-PR CSV found in {DEFAULT_REPORTS_DIR}",
            hint="run github_pr_jira_report.py first, or pass the CSV path")
        return None
    log(f"no CSV given, using the newest report: {found.name}", quiet=args.quiet)
    return found


def check_args(args: argparse.Namespace) -> str:
    for name, value in (("--since", args.since), ("--until", args.until)):
        if value:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                return f"{name} must be a date like 2026-04-01, not {value!r}"
    if args.since and args.until and args.since > args.until:
        return f"--since {args.since} is after --until {args.until}"
    if not 0.0 < args.similarity <= 1.0:
        return f"--similarity must be between 0 and 1, not {args.similarity}"
    return ""


def run(argv: list[str] | None = None) -> int:
    global DEBUG, JIRA_BASE
    args = parse_args(argv)
    DEBUG = args.debug

    complaint = check_args(args)
    if complaint:
        return err(complaint)
    path = pick_input(args)
    if path is None:
        return 2

    JIRA_BASE = sniff_jira_base(path)
    try:
        prs = load_prs(path, args)
    except (OSError, ValueError, csv.Error) as exc:
        return err(f"cannot read {path}: {exc}",
                   hint="pass a CSV written by github_pr_jira_report.py")
    if not prs:
        return err(f"{path.name} has no PR merged in "
                   f"{args.since or 'the start'}..{args.until or 'today'}",
                   hint="widen --since/--until, or re-run the per-PR report with more --days")

    dates = sorted(pr.merged for pr in prs if pr.merged)
    log(f"{len(prs)} PR(s) from {path.name} | merged {dates[0]}..{dates[-1]}", quiet=args.quiet)
    if not JIRA_BASE:
        warn("no JIRA link in the input CSV: the jira_links column will be empty "
             "(the ticket keys are still reported)")
    weak = sum(1 for pr in prs if pr.keys and not pr.primary)
    if weak:
        warn(f"{weak} PR(s) mention several JIRA keys but only in their commit messages: "
             f"none of the keys identifies the PR, so they were grouped by text instead")

    weights = token_weights(prs)
    groups = seed_groups(prs, weights)
    log(f"  {len(groups)} group(s) after grouping by JIRA ticket", quiet=args.quiet)
    if not args.no_text_grouping:
        groups = merge_groups(groups, weights, args.similarity)
        log(f"  {len(groups)} theme(s) after merging similar groups "
            f"(--similarity {args.similarity})", quiet=args.quiet)

    rows = build_rows(groups, args, weights)
    unnamed = sum(1 for row in rows if row["theme"] == "(untitled)")
    if unnamed:
        problem(f"{unnamed} theme(s) could not be named: their PRs have no JIRA summary, "
                f"no title and no branch")

    out_path = (Path(args.out).expanduser() if args.out
                else path.with_name(path.stem + THEMES_SUFFIX))
    try:
        write_csv(rows, out_path)
    except OSError as exc:
        return err(f"cannot write {out_path}: {exc}",
                   hint="close the file if it is open in Excel, or pass a different --out path")

    log("", quiet=args.quiet)
    singles = sum(1 for row in rows if row["prs"] == 1)
    log(f"{len(rows)} theme(s) from {len(prs)} PR(s) | {singles} theme(s) are a single PR | "
        + ", ".join(f"{n} {c}" for c, n in
                    Counter(row["category"] for row in rows).most_common()),
        quiet=args.quiet)
    if not args.fold_below and singles > len(rows) / 3:
        log(f"  -> {singles} one-off PR(s) each got their own row: add --fold-below 2 to "
            f"collect them into one row and leave only the real workstreams",
            quiet=args.quiet)
    if args.top > 0:
        log("", quiet=args.quiet)
        log(f"top {min(args.top, len(rows))} theme(s) by {args.sort}:", quiet=args.quiet)
        for row in rows[:args.top]:
            log(f"  {row['prs']:3d} PR {row['commits']:4d} commit(s)  "
                f"{row['first_merged']}..{row['last_merged']}  {row['category']:24s} "
                f"{truncate(row['theme'], 80)}", quiet=args.quiet)

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
            log("  -> re-run with --debug for the traceback")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
