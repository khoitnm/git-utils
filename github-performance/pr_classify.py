"""What kind of change a pull request is: which parts of the system it touches
(area, from its file paths) and why it was made (type, from its title and branch).

Both are rule lists checked top to bottom, first match wins, so the order is the
priority: a test file under src/main/java/.../controller is a test, not an API.
Tune the regexes here; github_performance.py only calls classify_pr().
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

# --------------------------------------------------------------------------- #
# areas: one per changed file
# --------------------------------------------------------------------------- #
BACKEND_EXT = r"\.(java|kt|kts|scala|groovy|cs|vb|fs|py|go|rb|php|rs|c|cc|cpp|h|hpp|swift)$"
SCRIPT_EXT = r"\.(js|mjs|cjs|ts|mts|cts)$"
# Repos that are a UI as a whole: their plain .ts/.js files are front-end code.
FRONTEND_REPO_RE = re.compile(r"(^|[-_/])(ui|mfe|web|frontend|front-end|portal|spa|react|angular|vue)([-_/]|$)", re.I)

AREA_RULES: list[tuple[str, str, str]] = [
    ("AI & agent tooling",
     r"(^|/)(\.claude|\.cursor|\.windsurf|\.codex|\.gemini|\.continue)/|(^|/)(claude|agents|gemini)\.md$|"
     r"\.cursorrules$|(^|/)\.github/(copilot-instructions\.md|prompts/|instructions/|agents/|chatmodes/)|"
     r"(^|/)\.mcp\.json$|(^|/)(skills?|prompts?|agents?)/[^/]+\.(md|ya?ml|json|txt)$",
     "Claude / Copilot / Cursor config, skills, prompts, agent and MCP definitions."),
    ("Dependencies & build",
     r"(^|/)(package(-lock)?\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|\.npmrc|\.nvmrc|\.yarnrc(\.yml)?|"
     r"pom\.xml|build\.gradle(\.kts)?|settings\.gradle(\.kts)?|gradle\.properties|libs\.versions\.toml|"
     r"gradle-wrapper\.properties|mvnw(\.cmd)?|maven-wrapper\.properties|"
     r"requirements[^/]*\.txt|pipfile(\.lock)?|poetry\.lock|pyproject\.toml|setup\.(py|cfg)|uv\.lock|"
     r"go\.(mod|sum)|cargo\.(toml|lock)|gemfile(\.lock)?|composer\.(json|lock)|"
     r"[^/]+\.(csproj|fsproj|vbproj|sln)|packages\.config|directory\.(packages|build)\.props|nuget\.config|"
     r"\.tool-versions|\.sdkmanrc|\.java-version|\.python-version|renovate\.json|\.github/dependabot\.ya?ml)$",
     "Manifests, lock files and build-tool versions (npm, Maven, Gradle, pip, Go, NuGet...)."),
    ("CI/CD pipelines",
     r"(^|/)\.github/(workflows|actions)/|(^|/)jenkinsfile[^/]*$|\.jenkinsfile$|(^|/)\.jenkins/|"
     r"azure-pipelines[^/]*\.ya?ml$|(^|/)\.azure-pipelines/|(^|/)\.gitlab-ci\.ya?ml$|(^|/)\.circleci/|"
     r"(^|/)\.buildkite/|bitbucket-pipelines\.ya?ml$|buildspec[^/]*\.ya?ml$|(^|/)\.harness/|"
     r"(^|/)\.tekton/|cloudbuild[^/]*\.ya?ml$|(^|/)\.drone\.ya?ml$|(^|/)sonar-project\.properties$",
     "GitHub Actions, Jenkins, Azure Pipelines and other build / deploy pipelines."),
    ("Scheduled jobs",
     r"(^|/)(cron|crons|cronjobs?|jobs?|batch|quartz|schedulers?)/|(^|/)[^/]+[-_](jobs?|cron|batch)/|"
     r"cronjob[^/]*\.ya?ml$|"
     r"[^/]*(cronjob|cron|job|batchjob)\.(java|kt|cs|py|go|ts|js)$",
     "Cron jobs, batch and scheduled jobs (by folder or file name)."),
    ("Infrastructure",
     r"\.(tf|tfvars|hcl|bicep)$|(^|/)(terraform|helm|charts?|k8s|kubernetes|kustomize|kustomization|"
     r"deploy|deployment|deployments|infra|infrastructure|ansible|cloudformation|cdk|argocd|manifests|"
     r"pulumi|environments?)/|(^|/)dockerfile[^/]*$|(^|/)[^/]*\.dockerfile$|(^|/)docker-compose[^/]*\.ya?ml$|"
     r"(^|/)\.dockerignore$|(^|/)chart\.ya?ml$|(^|/)values[^/]*\.ya?ml$|(^|/)serverless\.ya?ml$|"
     r"(^|/)template\.ya?ml$",
     "Terraform, Helm / Kubernetes, Docker, cloud templates and environment definitions."),
    ("Database",
     r"\.sql$|(^|/)(migrations?|migration|db|database|liquibase|flyway|changelogs?|schema|ddl|dml)/|"
     r"(^|/)db\.changelog[^/]*$|changelog[^/]*\.(xml|ya?ml|json)$|\.prisma$",
     "SQL, migrations (Liquibase / Flyway), schemas."),
    ("Tests",
     r"(^|/)(tests?|__tests__|__mocks__|spec|specs|e2e|cypress|playwright|integration-tests?|it|"
     r"testdata|test-data|fixtures|testing)/|(^|/)src/(test|it|integrationtest)/|"
     r"\.(test|spec|e2e|cy)\.[jt]sx?$|(^|/)test_[^/]+\.py$|_test\.(py|go)$|[^/]*tests?\.(java|kt|cs|scala|groovy)$|"
     r"(^|/)(jest|vitest|karma|cypress|playwright)\.config\.[^/]+$|\.snap$",
     "Unit, integration and end-to-end tests, fixtures, test configuration."),
    ("APIs",
     r"(openapi|swagger)[^/]*\.(ya?ml|json)$|(^|/)(openapi|swagger|api-?specs?|contracts?|proto|protos|"
     r"graphql|schemas?/api)/|\.(proto|graphqls?|gql|avsc|raml|wsdl)$|"
     r"[^/]*(controller|resource|endpoint|endpoints|router|routes|handler|api|apis|client|grpc)"
     r"[^/]*\.(java|kt|cs|py|go|scala|groovy)$",
     "API contracts (OpenAPI, proto, GraphQL) and the API layer: controllers, resources, handlers, clients."),
    ("Docs",
     r"\.(md|mdx|rst|adoc|asciidoc|txt)$|(^|/)(docs?|documentation|wiki|adr|adrs)/|(^|/)(license|changelog|"
     r"codeowners)$",
     "README, docs, ADRs, changelogs, CODEOWNERS."),
    ("Support tools & scripts",
     r"(^|/)(scripts?|tools?|tooling|bin|hack|utils?/scripts|devtools|local)/|\.(sh|bash|zsh|ps1|psm1|bat|cmd)$|"
     r"(^|/)makefile$|(^|/)justfile$|(^|/)taskfile\.ya?ml$|(^|/)\.devcontainer/|(^|/)\.husky/",
     "Developer scripts, local tooling, Makefiles, dev containers, git hooks."),
    ("Frontend",
     r"\.(tsx|jsx|vue|svelte|astro|css|scss|sass|less|styl|html?|svg|png|jpe?g|gif|webp|ico|woff2?|ttf|eot)$|"
     r"(^|/)(public|assets|static|styles?|themes?|components|pages|views|layouts|hooks|store|redux|"
     r"frontend|client|webapp|web|ui|mfe|i18n|locales?)/|(^|/)src/app/",
     "UI code, styles, assets, components (and .ts/.js in UI repositories)."),
    ("Configuration",
     r"(^|/)application[^/]*\.(ya?ml|properties)$|\.(properties|ya?ml|toml|ini|conf|cfg|env|json|xml)$|"
     r"(^|/)(config|configs|conf|settings|resources)/|(^|/)\.env[^/]*$|(^|/)\.(editorconfig|gitignore|"
     r"gitattributes|prettierrc[^/]*|eslintrc[^/]*|stylelintrc[^/]*|babelrc)$",
     "Application and tool configuration (YAML, properties, JSON, lint / format settings)."),
    ("Backend",
     BACKEND_EXT + "|" + SCRIPT_EXT,
     "Server-side code (Java, Kotlin, C#, Python, Go...; .ts/.js outside UI repositories)."),
]
AREA_RES = [(name, re.compile(rx, re.I)) for name, rx, _ in AREA_RULES]
AREAS = [name for name, _, _ in AREA_RULES] + ["Other"]
AREA_DESCRIPTIONS = {name: desc for name, _, desc in AREA_RULES} | {"Other": "Files no rule recognises."}

# Generated files count as a few lines, so one lock-file refresh does not make a
# feature PR look like a dependency PR.
GENERATED_RE = re.compile(r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|"
                          r"uv\.lock|pipfile\.lock|go\.sum|cargo\.lock|gemfile\.lock|composer\.lock|"
                          r"packages\.lock\.json)$|\.snap$|\.min\.(js|css)$|(^|/)dist/", re.I)
GENERATED_WEIGHT = 5


def file_area(path: str, repo: str | None) -> str:
    p = path.replace("\\", "/")
    for name, rx in AREA_RES:
        if name == "Backend" and re.search(SCRIPT_EXT, p, re.I) and repo and FRONTEND_REPO_RE.search(repo.split("/")[-1]):
            return "Frontend"
        if rx.search(p):
            return name
    return "Other"


def file_weight(path: str, additions: int, deletions: int) -> int:
    lines = (additions or 0) + (deletions or 0)
    if GENERATED_RE.search(path):
        return min(lines, GENERATED_WEIGHT) or 1
    return lines or 1  # renames / binary files still count


# --------------------------------------------------------------------------- #
# change types: one per PR
# --------------------------------------------------------------------------- #
CONVENTIONAL_RE = re.compile(r"^\s*(\w+)(\([^)]*\))?!?:\s*", re.I)
CONVENTIONAL_TYPES = {
    "feat": "Feature", "feature": "Feature", "fix": "Bug fix", "bugfix": "Bug fix", "hotfix": "Bug fix",
    "perf": "Performance", "refactor": "Refactor & cleanup", "style": "Refactor & cleanup",
    "test": "Tests", "tests": "Tests", "docs": "Docs", "doc": "Docs", "ci": "CI/CD & build", "build": "CI/CD & build",
    "revert": "Revert", "deps": "Dependency & platform upgrade", "release": "Release & merge",
}
# JIRA keys, also the lower-case "Scs 5075" form branch-named PRs get.
TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b[:\s-]*|^\s*[A-Za-z]{2,10}[ _-]\d{2,6}\b[:\s-]*")

TYPE_RULES: list[tuple[str, str, str]] = [
    ("Revert", r"^\s*revert\b", "Reverts an earlier change."),
    ("Release & merge", r"^\s*(release\b|merge (branch|remote|pull)|back-?merge|sync (main|master|develop|dev)\b)|"
                        r"\bcut release\b|\brelease v?\d|^\s*(\w+\s+)?(to\s+)?(master|main|develop|dev)\s*$|"
                        r"\bresolve (merge )?conflicts?\b",
     "Releases, version cuts, branch syncs and conflict merges."),
    ("Dependency & platform upgrade",
     r"\b(bump|bumps|bumped|dependabot|renovate|deps?|dependenc(y|ies)|upgrade[sd]?|cve-\d+|vulnerab\w*|"
     r"snyk|security patch|java ?\d{2}|jdk ?\d+|node(js)? ?\d{2}|spring ?boot ?\d|angular ?\d+|react ?\d+|"
     r"python ?3\.\d+|\.net ?\d|zulu|eol|sdk)\b",
     "Library, framework, runtime, SDK or security upgrades."),
    ("Bug fix", r"\b(fix(es|ed|ing)?|bug(fix)?|hot-?fix|defect|broken|crash\w*|npe|null ?pointer|regression|"
                r"incorrect|wrong|issue with|prevent\w*|guard\w*|null[- ]?safe\w*|tolerant\w*|don'?t fail|"
                r"misleading|stop \w+ failing|failing|handle (missing|null|empty))\b",
     "Fixes for defects and production issues."),
    ("Performance", r"\b(perf(ormance)?|optimi[sz]\w*|speed ?up|faster|slow(ness)?|latency|caching|cache|"
                    r"timeouts?|memory|throughput|in parallel|parallel\w*|eager\w*|lazy[- ]?load\w*|n\+1|"
                    r"connection ?pool\w*|max ?connections\w*|db calls?|virtual ?threads?)\b",
     "Speed, memory, timeouts, caching, parallelism, fewer DB calls."),
    ("Tooling & AI", r"\b(scripts?|claude|skills?|copilot|cursor|mcp|prompts?|kibana|appd\w*|appdynamics|"
                     r"dashboards?|grafana|datadog|splunk|monitoring|alerts?|daily error check)\b",
     "Developer tooling: AI skills and agent config, scripts, monitoring helpers (title or files)."),
    ("Tests", r"\b(tests?|testing|unit tests?|coverage|e2e|contract tests?|junit|cypress|playwright)\b",
     "Adds or fixes tests only."),
    ("Docs", r"\b(docs?|readme|documentation|adr|changelog|comments?|notes?)\b", "Documentation changes."),
    ("CI/CD & build", r"\b(ci|cd|pipelines?|workflows?|github actions?|jenkins|build|deploy(ment)?s?|helm|"
                      r"docker(file)?|sonar|lint(ing|er)?)\b", "Pipelines, build, deployment, lint setup."),
    ("Refactor & cleanup", r"\b(refactor\w*|clean ?up|cleanup|tidy|renam\w*|remov(e|es|ed|ing)|delet(e|es|ed)|"
                           r"deprecat\w*|move[sd]?|extract(ed)?|simplif\w*|restructur\w*|reorgani[sz]\w*|"
                           r"merged? .* into|drop\w*|reduce log\w*|todo)\b",
     "Restructures or removes code without new behaviour."),
    ("Configuration", r"\b(config(uration|ure|urable)?|properties|settings|feature ?flags?|flag|toggle|"
                      r"env(ironment)? vars?|every \d+ (mins?|minutes|hours?)|cron|schedule)\b",
     "Configuration, feature-flag and schedule changes."),
    ("Feature", r"\b(feat(ure)?|add(s|ed|ing)?|implement\w*|introduc\w*|support(s|ed)?|new|create[sd]?|"
                r"enable[sd]?|allow(s|ed)?|expose[sd]?|integrat\w*|api|endpoint|show\w*|surface\w*|"
                r"email\w*|notif\w*|sync\w*|find|get|read indicator|indicator)\b",
     "New behaviour or capability."),
    ("Enhancement", r"\b(improv\w*|enhanc\w*|updat(e|es|ed|ing)|tweak\w*|adjust\w*|chang(e|es|ed|ing)|convert\w*|"
                    r"tun(e|es|ed|ing)|polish\w*|treat\w*|apply|applies|point\w*|switch\w*|replac\w*|use|"
                    r"skip\w*|propagat\w*|separat\w*|validat\w*|make|requir\w*|reset\w*|disabl\w*|compar\w*|"
                    r"trust|let|parse\w*|pars(e|ing)|report\w*|logs?|logging|debug\w*|open\w*|handl\w*|"
                    r"scop\w*|keep|trim\w*|accept\w*|option|group\w*|clarif\w*|string to|to uuid|logic)\b",
     "Changes to existing behaviour that are neither a fix nor a new capability."),
]
TYPE_RES = [(name, re.compile(rx, re.I)) for name, rx, _ in TYPE_RULES]
TOOLING_TYPE = "Tooling & AI"
PRODUCT_TYPE = "Product change"
TYPES = [name for name, _, _ in TYPE_RULES] + [PRODUCT_TYPE, "Other change"]
TYPE_DESCRIPTIONS = {name: desc for name, _, desc in TYPE_RULES} | {
    PRODUCT_TYPE: "Code change whose title gives no hint (often a ticket number alone); decided by the files.",
    "Other change": "Nothing in the title, branch or files says what kind of change it is."}

# PRs that are mostly tooling are tooling work whatever verb the title uses.
TOOLING_AREAS = {"AI & agent tooling", "Support tools & scripts"}
CODE_AREAS = {"Backend", "Frontend", "APIs", "Scheduled jobs", "Database"}

# When the title says nothing, the files decide.
AREA_DEFAULT_TYPE = {
    "Dependencies & build": "Dependency & platform upgrade", "Tests": "Tests", "Docs": "Docs",
    "CI/CD pipelines": "CI/CD & build", "Infrastructure": "CI/CD & build", "Configuration": "Configuration",
    "AI & agent tooling": TOOLING_TYPE, "Support tools & scripts": TOOLING_TYPE,
}
# Areas that support a change rather than being the change itself.
SUPPORTING_AREAS = {"Tests", "Docs", "Configuration", "Other"}
MAIN_AREA_MIN_SHARE = 0.10


def change_type(title: str, branch: str | None, primary_area: str, areas: dict[str, int]) -> tuple[str, str]:
    """(type, what decided it)."""
    match = CONVENTIONAL_RE.match(title or "")
    if match and match.group(1).lower() in CONVENTIONAL_TYPES:
        kind = CONVENTIONAL_TYPES[match.group(1).lower()]
        if kind != "Feature" or primary_area != "Dependencies & build":
            return kind, "conventional prefix"
    # A PR that only touches manifests is an upgrade, whatever its title says.
    total = sum(areas.values()) or 1
    if areas.get("Dependencies & build", 0) / total >= 0.8:
        return "Dependency & platform upgrade", "files"
    if primary_area in TOOLING_AREAS and sum(areas.get(a, 0) for a in TOOLING_AREAS) / total >= 0.6:
        return TOOLING_TYPE, "files"
    text = TICKET_RE.sub(" ", title or "")
    branch_text = re.sub(r"[/_.-]+", " ", TICKET_RE.sub(" ", branch or ""))
    for source, value in (("title", text), ("branch", branch_text)):
        for name, rx in TYPE_RES:
            if rx.search(value):
                return name, source
    if primary_area in AREA_DEFAULT_TYPE:
        return AREA_DEFAULT_TYPE[primary_area], "files"
    if primary_area in CODE_AREAS:
        return PRODUCT_TYPE, "files"
    return "Other change", "none"


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def classify_pr(repo: str | None, title: str, branch: str | None, files: list[dict],
                changed_files: int | None = None) -> dict[str, Any]:
    """Area weights (lines, generated files damped), primary area, change type.

    `files` are {path, additions, deletions}; GitHub returns at most the first
    100, so a huge PR is classified from those (files_seen < changed_files)."""
    areas: Counter = Counter()
    for f in files:
        areas[file_area(f["path"], repo)] += file_weight(f["path"], f.get("additions", 0), f.get("deletions", 0))
    if not areas:  # no file list (deleted repo, or a PR with nothing in it)
        areas["Other"] = 1
    primary = areas.most_common(1)[0][0]
    if primary in SUPPORTING_AREAS:
        # Tests, docs and config usually outweigh the code they go with: a PR with
        # 300 test lines and 40 backend lines is a backend change.
        total = sum(areas.values())
        code = [(a, w) for a, w in areas.most_common() if a not in SUPPORTING_AREAS]
        if code and code[0][1] >= MAIN_AREA_MIN_SHARE * total:
            primary = code[0][0]
    kind, decided_by = change_type(title, branch, primary, areas)
    return {
        "area": primary,
        "areas": dict(areas.most_common()),
        "type": kind,
        "type_source": decided_by,
        "touches_tests": areas.get("Tests", 0) > 0,
        "files_seen": len(files),
        "files_partial": bool(changed_files and changed_files > len(files)),
    }


def category_defs() -> dict[str, list[dict[str, str]]]:
    return {
        "areas": [{"name": a, "desc": AREA_DESCRIPTIONS[a]} for a in AREAS],
        "types": [{"name": t, "desc": TYPE_DESCRIPTIONS[t]} for t in TYPES],
    }
