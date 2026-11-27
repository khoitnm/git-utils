#!/usr/bin/env python3
"""Check that nothing org- or project-specific is about to be committed.

Every script here must work for any project in any organization.
This script checks the files git would commit: tracked files plus untracked ones
that are not gitignored. Gitignored files, real project presets included, are
never read.

    python scripts/check_portability.py

Checks:
  1. Every plugin with a project-presets/ folder has a .gitignore that keeps
     everything in it local except sample/.
  2. No file under project-presets/ other than sample/ is tracked or staged.
  3. No file contains a ticket key, a private wiki/tracker link, a real email
     address, or a term listed in the private-terms list.

The private-terms list holds names too private to commit: the employer, its
repos, its internal apps. Writing them into this script would publish them, so
they live in .skill-lint-private-terms at the repo root (gitignored), one
case-insensitive regex per line, `#` comments allowed. The env var
SKILL_LINT_PRIVATE_TERMS adds more, os.pathsep-separated.

Exit code 1 means something must be fixed before committing.
Python 3, standard library only.
"""

import os
import re
import subprocess
import sys

PRIVATE_TERMS_FILE = ".skill-lint-private-terms"
PRIVATE_TERMS_ENV = "SKILL_LINT_PRIVATE_TERMS"

# Shapes that give themselves away on any team.
SHAPES = {
    "ticket key": re.compile(
        # standards that share the shape, then placeholders (PROJ-1234, ACME-101)
        r"\b(?!(?:JSR|SHA|RFC|ISO|IEC|UTF|AES|RSA|TLS|SSL|CVE|JEP|JDK|HTTP|MD|SQL|"
        r"PROJ|ACME|ABC|KEY|XXX)-)"
        r"[A-Z]{2,5}-\d{3,6}\b"),
    "private wiki/tracker link": re.compile(
        # placeholder sites (acme, example, your-org) are fine in samples
        r"\b(?:confluence|jira|wiki)\.[\w.-]+/|"
        r"\b(?!(?:acme|example|your-[\w-]+)\.atlassian\.net)[\w.-]+\.atlassian\.net\b"),
    "email address": re.compile(r"\b[\w.+-]+@([\w-]+(?:\.[\w-]+)+)\b"),
}

# Email domains that name nobody.
SAFE_EMAIL_DOMAINS = re.compile(
    r"(?:^|\.)(?:example\.(?:com|org|net)|acme\.\w+|users\.noreply\.github\.com|"
    r"github\.com|anthropic\.com)$", re.IGNORECASE)

BINARY_EXT = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".pyc"}
SELF = os.path.normpath("scripts/check_portability.py")


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          check=True, encoding="utf-8").stdout


def committable_files():
    """Tracked files plus untracked files that are not gitignored."""
    out = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted({p for p in out.split("\0") if p and os.path.isfile(p)})


def private_terms(root):
    raw = os.environ.get(PRIVATE_TERMS_ENV, "").split(os.pathsep)
    path = os.path.join(root, PRIVATE_TERMS_FILE)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            raw += f.read().splitlines()
    terms = []
    for term in (t.strip() for t in raw):
        if not term or term.startswith("#"):
            continue
        try:
            terms.append(re.compile(term, re.IGNORECASE))
        except re.error as exc:
            print(f"{PRIVATE_TERMS_FILE}: skipping bad regex {term!r}: {exc}",
                  file=sys.stderr)
    return terms


def check_preset_gitignores(root):
    problems = []
    plugins = os.path.join(root, "plugins")
    if not os.path.isdir(plugins):
        return problems
    for name in sorted(os.listdir(plugins)):
        if not os.path.isdir(os.path.join(plugins, name, "project-presets")):
            continue
        gi = os.path.join(plugins, name, ".gitignore")
        lines = set()
        if os.path.isfile(gi):
            with open(gi, encoding="utf-8") as f:
                lines = {l.strip() for l in f}
        for needed in ("project-presets/*", "!project-presets/sample/"):
            if needed not in lines:
                problems.append(f"plugins/{name}/.gitignore: missing line `{needed}`")
    return problems


def check_preset_files(files):
    problems = []
    for p in files:
        parts = p.replace("\\", "/").split("/")
        if "project-presets" in parts:
            i = parts.index("project-presets")
            if i + 1 < len(parts) and parts[i + 1] != "sample":
                problems.append(f"{p}: real project preset would be committed "
                                "(only project-presets/sample/ may be)")
    return problems


def check_contents(files, terms):
    problems = []
    for p in files:
        if os.path.splitext(p)[1].lower() in BINARY_EXT or os.path.normpath(p) == SELF:
            continue
        try:
            with open(p, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        for n, line in enumerate(lines, 1):
            for label, rx in SHAPES.items():
                for m in rx.finditer(line):
                    if label == "email address" and SAFE_EMAIL_DOMAINS.search(m.group(1)):
                        continue
                    problems.append(f"{p}:{n}: {label}: {m.group(0)}")
            for rx in terms:
                m = rx.search(line)
                if m:
                    problems.append(f"{p}:{n}: private term: {m.group(0)}")
    return problems


def main():
    root = git("rev-parse", "--show-toplevel").strip()
    os.chdir(root)
    files = committable_files()
    terms = private_terms(root)
    problems = (check_preset_gitignores(root) + check_preset_files(files)
                + check_contents(files, terms))
    if not terms:
        print(f"note: no {PRIVATE_TERMS_FILE} found; only generic shapes were checked. "
              "Add your employer's name and repo prefixes there.", file=sys.stderr)
    if problems:
        print("Org- or project-specific content would be committed:")
        for p in problems:
            print(f"  {p}")
        print("Move it into the gitignored .env, replace it with a made-up "
              "name (acme), or drop it.")
        return 1
    print(f"OK: {len(files)} files checked, nothing org- or project-specific found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
