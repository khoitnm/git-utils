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
     # name *ends* with the role (UserController.java, CoreClient.java, api.py), so
     # CoreApiConfig.java or RapidSync.java stay Backend
     r"(controller|controllers|resource|resources|endpoint|endpoints|router|routes|handler|handlers|"
     r"api|apis|client|grpc|grpcservice)\.(java|kt|cs|py|go|scala|groovy)$",
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
     "Application and tool configuration (YAML, properties, JSON, lint / format settings). "
     "Source code in a config/ package is Backend, not configuration."),
    ("Backend",
     BACKEND_EXT + "|" + SCRIPT_EXT,
     "Server-side code (Java, Kotlin, C#, Python, Go...; .ts/.js outside UI repositories)."),
]
AREA_RES = [(name, re.compile(rx, re.I)) for name, rx, _ in AREA_RULES]
KIND_DESCRIPTIONS = {name: desc for name, _, desc in AREA_RULES} | {"Other": "Files no rule recognises."}

# The rules above decide what kind of file it is; the report groups those into a
# parent / child tree. Tests and Configuration belong to Backend or Frontend,
# depending on the file and the repository (see file_area).
SEP = " \u203a "  # "Backend > Tests"
AREA_TREE: list[tuple[str, str, list[str]]] = [
    ("Backend", "Server-side work: business logic, APIs, jobs, database, its config and tests.",
     ["Core logic", "APIs", "Scheduled jobs", "Database", "Configuration", "Tests"]),
    ("Frontend", "UI work: components, styles, assets, UI config and UI tests.",
     ["UI code", "Configuration", "Tests"]),
    ("Infrastructure & delivery", "How the software is built, deployed and run.",
     ["Infrastructure", "CI/CD pipelines"]),
    ("Dependencies & build", "Library, framework and build-tool versions.", ["Dependencies & build"]),
    ("Developer tooling", "Tools for developers rather than the product.",
     ["AI & agent tooling", "Support tools & scripts"]),
    ("Docs & other", "Documentation and files no rule recognises.", ["Docs", "Other"]),
]
PARENTS = [parent for parent, _, _ in AREA_TREE]
PARENT_DESCRIPTIONS = {parent: desc for parent, desc, _ in AREA_TREE}
AREAS = [parent + SEP + child for parent, _, children in AREA_TREE for child in children]
# rule name -> (parent, child) when the parent does not depend on context
KIND_TO_AREA = {
    "AI & agent tooling": ("Developer tooling", "AI & agent tooling"),
    "Support tools & scripts": ("Developer tooling", "Support tools & scripts"),
    "Dependencies & build": ("Dependencies & build", "Dependencies & build"),
    "CI/CD pipelines": ("Infrastructure & delivery", "CI/CD pipelines"),
    "Infrastructure": ("Infrastructure & delivery", "Infrastructure"),
    "Scheduled jobs": ("Backend", "Scheduled jobs"),
    "Database": ("Backend", "Database"),
    "APIs": ("Backend", "APIs"),
    "Backend": ("Backend", "Core logic"),
    "Frontend": ("Frontend", "UI code"),
    "Docs": ("Docs & other", "Docs"),
    "Other": ("Docs & other", "Other"),
}
CHILD_KIND = {"Core logic": "Backend", "UI code": "Frontend"}  # child -> rule whose description it uses
# Tests / configuration that are front-end even outside a UI repository
FRONTEND_TEST_RE = re.compile(r"\.(test|spec|cy)\.[jt]sx$|\.cy\.[jt]s$|(^|/)(cypress|playwright)[^/]*/|\.snap$|"
                              r"(^|/)(jest|vitest|karma|cypress|playwright)\.config\.[^/]+$", re.I)
FRONTEND_CONFIG_RE = re.compile(r"(^|/)\.(eslintrc|prettierrc|stylelintrc|babelrc|browserslistrc)[^/]*$|"
                                r"(^|/)(tsconfig|vite\.config|webpack\.config|next\.config|tailwind\.config|"
                                r"postcss\.config|angular|module-federation\.config)[^/]*$", re.I)


def area_parent(area: str) -> str:
    return area.split(SEP, 1)[0]


def area_child(area: str) -> str:
    return area.split(SEP, 1)[-1]


def area_description(area: str) -> str:
    if area in PARENT_DESCRIPTIONS:
        return PARENT_DESCRIPTIONS[area]
    parent, child = area_parent(area), area_child(area)
    if child in ("Tests", "Configuration"):
        return f"{parent} {child.lower()}: " + KIND_DESCRIPTIONS[child]
    return KIND_DESCRIPTIONS[CHILD_KIND.get(child, child)]

# Generated files count as a few lines, so one lock-file refresh does not make a
# feature PR look like a dependency PR.
GENERATED_RE = re.compile(r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|"
                          r"uv\.lock|pipfile\.lock|go\.sum|cargo\.lock|gemfile\.lock|composer\.lock|"
                          r"packages\.lock\.json)$|\.snap$|\.min\.(js|css)$|(^|/)dist/", re.I)
GENERATED_WEIGHT = 5


# Source code is never "configuration", even in a config/ package (SomethingConfig.java).
CODE_FILE_RE = re.compile(BACKEND_EXT + "|" + SCRIPT_EXT + r"|\.(tsx|jsx|vue|svelte)$", re.I)


def is_frontend_repo(repo: str | None) -> bool:
    return bool(repo and FRONTEND_REPO_RE.search(repo.split("/")[-1]))


def file_area(path: str, repo: str | None) -> str:
    """The file's "Parent > Child" area."""
    kind = file_kind(path, repo)
    if kind == "Tests":
        parent = "Frontend" if is_frontend_repo(repo) or FRONTEND_TEST_RE.search(path) else "Backend"
        return parent + SEP + "Tests"
    if kind == "Configuration":
        parent = "Frontend" if is_frontend_repo(repo) or FRONTEND_CONFIG_RE.search(path) else "Backend"
        return parent + SEP + "Configuration"
    parent, child = KIND_TO_AREA[kind]
    return parent + SEP + child


def file_kind(path: str, repo: str | None) -> str:
    """Which AREA_RULES rule the file matches (first one wins)."""
    p = path.replace("\\", "/")
    for name, rx in AREA_RES:
        if name == "Backend" and re.search(SCRIPT_EXT, p, re.I) and is_frontend_repo(repo):
            return "Frontend"
        if name == "Configuration" and CODE_FILE_RE.search(p):
            continue
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

# Parents whose work is the product itself: an unlabelled PR there is a product change.
PRODUCT_PARENTS = {"Backend", "Frontend"}

# When the title says nothing, the files decide (keyed by child area).
AREA_DEFAULT_TYPE = {
    "Dependencies & build": "Dependency & platform upgrade", "Tests": "Tests", "Docs": "Docs",
    "CI/CD pipelines": "CI/CD & build", "Infrastructure": "CI/CD & build", "Configuration": "Configuration",
    "AI & agent tooling": TOOLING_TYPE, "Support tools & scripts": TOOLING_TYPE,
}
# Areas that support a change rather than being the change itself.
SUPPORTING_CHILDREN = {"Tests", "Docs", "Configuration", "Other"}
MAIN_AREA_MIN_SHARE = 0.10


def change_type(title: str, branch: str | None, main_area: str, areas: dict[str, int]) -> tuple[str, str]:
    """(type, what decided it). main_area / areas use "Parent > Child" names."""
    parent, child = area_parent(main_area), area_child(main_area)
    match = CONVENTIONAL_RE.match(title or "")
    if match and match.group(1).lower() in CONVENTIONAL_TYPES:
        kind = CONVENTIONAL_TYPES[match.group(1).lower()]
        if kind != "Feature" or parent != "Dependencies & build":
            return kind, "conventional prefix"
    total = sum(areas.values()) or 1
    share = lambda p: sum(w for a, w in areas.items() if area_parent(a) == p) / total  # noqa: E731
    # A PR that only touches manifests is an upgrade, whatever its title says.
    if share("Dependencies & build") >= 0.8:
        return "Dependency & platform upgrade", "files"
    # A PR that is mostly tooling is tooling work, whatever verb its title uses.
    if parent == "Developer tooling" and share("Developer tooling") >= 0.6:
        return TOOLING_TYPE, "files"
    text = TICKET_RE.sub(" ", title or "")
    branch_text = re.sub(r"[/_.-]+", " ", TICKET_RE.sub(" ", branch or ""))
    for source, value in (("title", text), ("branch", branch_text)):
        for name, rx in TYPE_RES:
            if rx.search(value):
                return name, source
    if child in AREA_DEFAULT_TYPE:
        return AREA_DEFAULT_TYPE[child], "files"
    if parent in PRODUCT_PARENTS:
        return PRODUCT_TYPE, "files"
    return "Other change", "none"


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def classify_pr(repo: str | None, title: str, branch: str | None, files: list[dict],
                changed_files: int | None = None) -> dict[str, Any]:
    """Area weights per "Parent > Child" (lines, generated files damped), the main
    parent and child area, and the change type.

    The main parent is where the PR's real changes are: tests, config, docs and
    unknown files only decide when nothing else reaches MAIN_AREA_MIN_SHARE of
    the lines (a PR with 300 test lines and 40 backend lines is Backend > Core
    logic; an upgrade that mostly adjusts tests is Dependencies & build).
    `files` are {path, additions, deletions}; GitHub returns at most the first
    100, so a huge PR is classified from those (files_partial)."""
    areas: Counter = Counter()
    for f in files:
        areas[file_area(f["path"], repo)] += file_weight(f["path"], f.get("additions", 0), f.get("deletions", 0))
    if not areas:  # no file list (deleted repo, or a PR with nothing in it)
        areas["Docs & other" + SEP + "Other"] = 1
    total = sum(areas.values())
    parents: Counter = Counter()
    code: Counter = Counter()
    for area, weight in areas.items():
        parents[area_parent(area)] += weight
        if area_child(area) not in SUPPORTING_CHILDREN:
            code[area_parent(area)] += weight
    if code and code.most_common(1)[0][1] >= MAIN_AREA_MIN_SHARE * total:
        main_parent = code.most_common(1)[0][0]
        candidates = [(a, w) for a, w in areas.most_common()
                      if area_parent(a) == main_parent and area_child(a) not in SUPPORTING_CHILDREN]
    else:
        main_parent = parents.most_common(1)[0][0]
        candidates = [(a, w) for a, w in areas.most_common() if area_parent(a) == main_parent]
    main = candidates[0][0]
    kind, decided_by = change_type(title, branch, main, areas)
    return {
        "area": main,
        "area_parent": main_parent,
        "areas": dict(areas.most_common()),
        "area_parents": dict(parents.most_common()),
        "type": kind,
        "type_source": decided_by,
        "touches_tests": any(area_child(a) == "Tests" for a in areas),
        "files_seen": len(files),
        "files_partial": bool(changed_files and changed_files > len(files)),
    }


def category_defs() -> dict[str, list[dict[str, Any]]]:
    return {
        "areas": [{"name": parent, "desc": desc,
                   "children": [{"name": parent + SEP + child, "short": child,
                                 "desc": area_description(parent + SEP + child)} for child in children]}
                  for parent, desc, children in AREA_TREE],
        "types": [{"name": t, "desc": TYPE_DESCRIPTIONS[t]} for t in TYPES],
    }
