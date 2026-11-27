"""Which languages, frameworks and tools a person's PRs use, per area.

* Languages come from file extensions, weighted by changed lines.
* Tools that show in file paths (Docker, Helm, GitHub Actions, Liquibase,
  Cypress, Claude Code...) come from the changed files themselves.
* Frameworks and libraries (Spring Boot, React, JUnit, Kafka...) come from the
  dependency manifests of the repositories (pom.xml, package.json, build.gradle,
  requirements.txt...), read by members-activities-on-git.py.

A framework is credited to an area only when the person changed files of that
area in a repository that uses it: React counts for their Frontend work only if
they touched UI code in a React repository. Tune the catalogues below; nothing
needs fetching again when they change (manifests are cached as text).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Iterable

import pr_classify

# --------------------------------------------------------------------------- #
# languages
# --------------------------------------------------------------------------- #
LANG_BY_EXT = {
    "java": "Java", "kt": "Kotlin", "kts": "Kotlin", "scala": "Scala", "groovy": "Groovy", "gradle": "Groovy",
    "cs": "C#", "vb": "VB.NET", "fs": "F#", "py": "Python", "go": "Go", "rb": "Ruby", "php": "PHP", "rs": "Rust",
    "c": "C", "h": "C", "cpp": "C++", "cc": "C++", "hpp": "C++", "swift": "Swift",
    "ts": "TypeScript", "mts": "TypeScript", "cts": "TypeScript", "tsx": "TypeScript (TSX)",
    "js": "JavaScript", "mjs": "JavaScript", "cjs": "JavaScript", "jsx": "JavaScript (JSX)",
    "vue": "Vue", "svelte": "Svelte", "html": "HTML", "htm": "HTML",
    "css": "CSS", "scss": "SCSS", "sass": "Sass", "less": "Less",
    "sql": "SQL", "sh": "Shell", "bash": "Shell", "zsh": "Shell", "ps1": "PowerShell", "psm1": "PowerShell",
    "bat": "Batch", "cmd": "Batch", "tf": "Terraform (HCL)", "tfvars": "Terraform (HCL)", "hcl": "Terraform (HCL)",
    "bicep": "Bicep", "yaml": "YAML", "yml": "YAML", "json": "JSON", "json5": "JSON", "xml": "XML",
    "properties": "Properties", "toml": "TOML", "ini": "INI", "md": "Markdown", "mdx": "Markdown",
    "proto": "Protobuf", "graphql": "GraphQL", "graphqls": "GraphQL", "gql": "GraphQL", "jq": "jq",
    "txt": "Text", "csv": "CSV", "svg": "SVG", "png": "Image", "jpg": "Image", "jpeg": "Image", "gif": "Image",
}
LANG_BY_NAME = {"dockerfile": "Dockerfile", "makefile": "Makefile", "jenkinsfile": "Groovy (Jenkinsfile)",
                "gemfile": "Ruby", "justfile": "Just"}
# Not worth listing as "the language they code in"
NON_CODE = {"Text", "CSV", "Image", "SVG"}


def language_of(path: str) -> str:
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name in LANG_BY_NAME or name.startswith("dockerfile") or name.endswith(".dockerfile"):
        return LANG_BY_NAME.get(name, "Dockerfile")
    if "." in name:
        return LANG_BY_EXT.get(name.rsplit(".", 1)[-1], "Other")
    return "Other"


# --------------------------------------------------------------------------- #
# tools visible in file paths: (name, regex on the path)
# --------------------------------------------------------------------------- #
PATH_TOOLS: list[tuple[str, str]] = [
    ("GitHub Actions", r"(^|/)\.github/(workflows|actions)/"),
    ("Jenkins", r"(^|/)jenkinsfile|\.jenkinsfile$"),
    ("Azure Pipelines", r"azure-pipelines[^/]*\.ya?ml$"),
    ("Docker", r"(^|/)dockerfile|\.dockerfile$|docker-compose[^/]*\.ya?ml$"),
    ("Helm", r"(^|/)(helm|helm_chart|charts?)/|(^|/)chart\.ya?ml$"),
    ("Kubernetes", r"(^|/)(k8s|kubernetes|kustomize)/|kustomization\.ya?ml$"),
    ("Terraform", r"\.(tf|tfvars)$"),
    ("Maven", r"(^|/)pom\.xml$|(^|/)mvnw"),
    ("Gradle", r"\.gradle(\.kts)?$|(^|/)gradlew"),
    ("npm", r"(^|/)package(-lock)?\.json$"),
    ("Yarn", r"(^|/)yarn\.lock$|\.yarnrc"),
    ("pnpm", r"(^|/)pnpm-lock\.yaml$"),
    ("Liquibase", r"liquibase|db\.changelog|(^|/)changelogs?/"),
    ("Flyway", r"(^|/)V\d+(_\d+)*__[^/]+\.sql$|flyway"),
    ("OpenAPI", r"(openapi|swagger)[^/]*\.(ya?ml|json)$"),
    ("Cypress", r"(^|/)cypress[^/]*/|\.cy\.[jt]sx?$"),
    ("Playwright", r"playwright"),
    ("Storybook", r"\.stories\.[jt]sx?$|(^|/)\.storybook/"),
    ("Dependabot", r"dependabot"),
    ("Renovate", r"renovate\.json"),
    ("SonarQube", r"sonar-project\.properties$"),
    ("Lombok", r"(^|/)lombok\.config$"),
    ("Claude Code", r"(^|/)\.claude/|(^|/)claude\.md$"),
    ("GitHub Copilot", r"copilot-instructions\.md$|(^|/)\.github/(prompts|instructions|agents|chatmodes)/"),
    ("Cursor", r"(^|/)\.cursor(/|rules)"),
    ("MCP", r"(^|/)\.?mcp\.json$|(^|/)mcp/"),
]
PATH_TOOL_RES = [(name, re.compile(rx, re.I)) for name, rx in PATH_TOOLS]


def path_tools(path: str) -> list[str]:
    return [name for name, rx in PATH_TOOL_RES if rx.search(path)]


# --------------------------------------------------------------------------- #
# frameworks from dependency manifests: (name, area it belongs to, regex)
# The area is a parent ("Backend") or a child ("Backend > Tests").
# --------------------------------------------------------------------------- #
SEP = pr_classify.SEP
BT, FT = "Backend" + SEP + "Tests", "Frontend" + SEP + "Tests"
FRAMEWORKS: list[tuple[str, str, str]] = [
    # backend: JVM
    ("Spring Boot", "Backend", r"spring-boot"),
    ("Spring Cloud", "Backend", r"spring-cloud"),
    ("Spring WebFlux", "Backend", r"spring-boot-starter-webflux|spring-webflux"),
    ("Spring Security", "Backend", r"spring-boot-starter-security|spring-security"),
    ("Spring Data JPA / Hibernate", "Backend", r"spring-boot-starter-data-jpa|hibernate-core|<artifactId>hibernate"),
    ("Spring Batch", "Backend", r"spring-batch|spring-boot-starter-batch"),
    ("Quartz", "Backend", r"quartz"),
    ("ShedLock", "Backend", r"shedlock"),
    ("Kafka", "Backend", r"kafka"),
    ("RabbitMQ", "Backend", r"spring-boot-starter-amqp|rabbitmq|amqp-client"),
    ("Redis", "Backend", r"redis|lettuce|jedis|redisson"),
    ("gRPC", "Backend", r"grpc"),
    ("OpenFeign", "Backend", r"openfeign|feign-core"),
    ("Resilience4j", "Backend", r"resilience4j"),
    ("MapStruct", "Backend", r"mapstruct"),
    ("Lombok", "Backend", r"<artifactId>lombok|lombok['\"]|io\.freefair\.lombok"),
    ("Micronaut", "Backend", r"micronaut"),
    ("Quarkus", "Backend", r"quarkus"),
    ("Liquibase", "Backend", r"liquibase"),
    ("Flyway", "Backend", r"flyway"),
    ("PostgreSQL", "Backend", r"postgresql|\"pg\"\s*:"),
    ("SQL Server", "Backend", r"mssql-jdbc|sqlserver|\"mssql\"\s*:|\"tedious\"\s*:"),
    ("MySQL", "Backend", r"mysql"),
    ("MongoDB", "Backend", r"mongodb|mongoose"),
    ("Elasticsearch", "Backend", r"elasticsearch"),
    ("AWS SDK", "Backend", r"software\.amazon\.awssdk|aws-java-sdk|\"aws-sdk\"|@aws-sdk/"),
    ("Azure SDK", "Backend", r"<groupId>com\.azure|\"@azure/(?!msal)"),
    ("Twilio", "Backend", r"twilio"),
    ("OpenTelemetry", "Backend", r"opentelemetry"),
    ("Micrometer", "Backend", r"micrometer"),
    ("Datadog", "Backend", r"datadog|dd-trace"),
    ("AppDynamics", "Backend", r"appdynamics"),
    ("Jackson", "Backend", r"jackson-databind"),
    # backend: Node / Python / Go / .NET
    ("Express", "Backend", r"\"express\"\s*:"),
    ("NestJS", "Backend", r"@nestjs/core"),
    ("Fastify", "Backend", r"\"fastify\"\s*:"),
    ("Django", "Backend", r"(^|\W)django"),
    ("Flask", "Backend", r"(^|\W)flask"),
    ("FastAPI", "Backend", r"fastapi"),
    ("SQLAlchemy", "Backend", r"sqlalchemy"),
    ("Gin", "Backend", r"gin-gonic"),
    ("ASP.NET Core", "Backend", r"microsoft\.aspnetcore"),
    # frontend
    ("React", "Frontend", r"\"react\"\s*:"),
    ("Next.js", "Frontend", r"\"next\"\s*:"),
    ("Angular", "Frontend", r"@angular/core"),
    ("Vue", "Frontend", r"\"vue\"\s*:"),
    ("Svelte", "Frontend", r"\"svelte\"\s*:"),
    ("Redux", "Frontend", r"\"(redux|@reduxjs/toolkit)\"\s*:"),
    ("TanStack Query", "Frontend", r"@tanstack/react-query|\"react-query\"\s*:"),
    ("React Router", "Frontend", r"\"react-router(-dom)?\"\s*:"),
    ("MUI", "Frontend", r"@mui/"),
    ("Tailwind CSS", "Frontend", r"\"tailwindcss\"\s*:"),
    ("styled-components", "Frontend", r"\"styled-components\"\s*:"),
    ("Vite", "Frontend", r"\"vite\"\s*:"),
    ("Webpack", "Frontend", r"\"webpack\"\s*:"),
    ("Module Federation", "Frontend", r"module-federation"),
    ("single-spa", "Frontend", r"single-spa"),
    ("i18next", "Frontend", r"i18next"),
    ("Axios", "Frontend", r"\"axios\"\s*:"),
    ("Zod", "Frontend", r"\"zod\"\s*:"),
    ("React Hook Form", "Frontend", r"\"react-hook-form\"\s*:"),
    ("Formik", "Frontend", r"\"formik\"\s*:"),
    ("date-fns", "Frontend", r"\"date-fns\"\s*:"),
    ("Storybook", "Frontend", r"@storybook/"),
    # tests
    ("JUnit", BT, r"junit"),
    ("Mockito", BT, r"mockito"),
    ("AssertJ", BT, r"assertj"),
    ("Testcontainers", BT, r"testcontainers"),
    ("WireMock", BT, r"wiremock"),
    ("REST Assured", BT, r"rest-assured"),
    ("Spock", BT, r"spock-core"),
    ("ArchUnit", BT, r"archunit"),
    ("pytest", BT, r"(^|\W)pytest"),
    ("Jest", FT, r"\"jest\"\s*:"),
    ("Vitest", FT, r"\"vitest\"\s*:"),
    ("Testing Library", FT, r"@testing-library/"),
    ("Cypress", FT, r"\"cypress\"\s*:"),
    ("Playwright", FT, r"@playwright/test"),
    ("MSW", FT, r"\"msw\"\s*:"),
]
FRAMEWORK_RES = [(name, area, re.compile(rx, re.I)) for name, area, rx in FRAMEWORKS]

# Manifests read from each repository's default branch: these at the root, plus
# any manifest the person changed (multi-module repos keep them in sub-folders).
ROOT_MANIFESTS = ["pom.xml", "build.gradle", "build.gradle.kts", "package.json", "requirements.txt",
                  "pyproject.toml", "go.mod", "Directory.Packages.props"]
MANIFEST_RE = re.compile(r"(^|/)(pom\.xml|build\.gradle(\.kts)?|package\.json|requirements[^/]*\.txt|"
                         r"pyproject\.toml|go\.mod|[^/]+\.csproj|Directory\.Packages\.props)$", re.I)
MODULE_MANIFESTS = ["package.json", "pom.xml", "build.gradle", "build.gradle.kts", "requirements.txt",
                    "pyproject.toml"]
MAX_MODULES_PER_REPO = 4
MAX_MANIFESTS_PER_REPO = 40


def manifest_paths(repo_files: Iterable[str]) -> list[str]:
    """Where to look for dependency manifests in one repository: the root, the
    manifests the person changed, and the top of the folders they changed most
    (monorepos keep each module's manifest there: web-app/package.json)."""
    files = list(repo_files)
    seen = list(ROOT_MANIFESTS)
    for path in files:
        if MANIFEST_RE.search(path) and path not in seen and "node_modules/" not in path:
            seen.append(path)
    folders = Counter(p.split("/", 1)[0] for p in files if "/" in p and not p.startswith("."))
    for folder, _ in folders.most_common(MAX_MODULES_PER_REPO):
        seen += [f"{folder}/{m}" for m in MODULE_MANIFESTS if f"{folder}/{m}" not in seen]
    return seen[:MAX_MANIFESTS_PER_REPO]


def frameworks_in(texts: Iterable[str]) -> dict[str, str]:
    """framework -> area, from manifest texts."""
    found: dict[str, str] = {}
    blob = "\n".join(t for t in texts if t)
    for name, area, rx in FRAMEWORK_RES:
        if name not in found and rx.search(blob):
            found[name] = area
    return found


# --------------------------------------------------------------------------- #
# aggregation for one person
# --------------------------------------------------------------------------- #
def _share_list(counter: Counter, files: Counter, total: float, limit: int = 8) -> list[dict[str, Any]]:
    return [{"name": name, "lines": lines, "files": files[name], "share": round(lines / total, 3) if total else 0}
            for name, lines in counter.most_common(limit)]


def summarize(prs: list[dict], reviewed: list[dict], repo_frameworks: dict[str, dict[str, str]]) -> dict[str, Any]:
    """Languages and frameworks per parent and child area, overall, and for the
    PRs the person reviews. `repo_frameworks`: repo -> {framework: area}."""
    lang_lines: dict[str, Counter] = defaultdict(Counter)   # area -> language -> weighted lines
    lang_files: dict[str, Counter] = defaultdict(Counter)
    tool_prs: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))  # area -> tool -> PR urls
    area_repos: dict[str, set] = defaultdict(set)            # area -> repos with changes in it
    area_repo_prs: dict[tuple[str, str], set] = defaultdict(set)
    for p in prs:
        for f in p.get("files") or []:
            area = pr_classify.file_area(f["path"], p["repository"])
            parent = pr_classify.area_parent(area)
            lang = language_of(f["path"])
            weight = pr_classify.file_weight(f["path"], f.get("additions", 0), f.get("deletions", 0))
            for key in (area, parent, "*"):
                lang_lines[key][lang] += weight
                lang_files[key][lang] += 1
            for tool in path_tools(f["path"]):
                tool_prs[area][tool].add(p["url"])
                tool_prs[parent][tool].add(p["url"])
            for key in (area, parent):
                area_repos[key].add(p["repository"])
                area_repo_prs[(key, p["repository"])].add(p["url"])

    def frameworks_for(area: str) -> list[dict[str, Any]]:
        """Manifest frameworks of the repos where the person worked in `area`,
        restricted to frameworks that belong to that area, plus path tools."""
        prs_by_fw: dict[str, set] = defaultdict(set)
        for repo in area_repos.get(area, ()):
            for fw, fw_area in (repo_frameworks.get(repo) or {}).items():
                # parent row: every framework of that parent (its tests included);
                # child row: only the frameworks of that child (JUnit for Backend > Tests)
                matches = fw_area == area if SEP in area else pr_classify.area_parent(fw_area) == area
                if matches:
                    prs_by_fw[fw] |= area_repo_prs[(area, repo)]
        for tool, urls in (tool_prs.get(area) or {}).items():
            prs_by_fw[tool] |= urls
        rows = [{"name": fw, "prs": len(urls), "source": "files" if fw in dict(PATH_TOOLS) else "manifest"}
                for fw, urls in prs_by_fw.items()]
        return sorted(rows, key=lambda r: (-r["prs"], r["name"]))

    by_area = []
    for parent in pr_classify.PARENTS:
        if parent not in lang_lines:
            continue
        total = sum(lang_lines[parent].values())
        by_area.append({"area": parent, "label": parent, "level": 0,
                        "languages": _share_list(lang_lines[parent], lang_files[parent], total),
                        "frameworks": frameworks_for(parent)})
        for area in pr_classify.AREAS:
            if pr_classify.area_parent(area) != parent or area not in lang_lines \
                    or pr_classify.area_child(area) == parent:
                continue
            total = sum(lang_lines[area].values())
            by_area.append({"area": area, "label": pr_classify.area_child(area), "level": 1, "parent": parent,
                            "languages": _share_list(lang_lines[area], lang_files[area], total, 5),
                            "frameworks": frameworks_for(area)})

    overall_total = sum(lang_lines["*"].values())
    code_langs = Counter({k: v for k, v in lang_lines["*"].items() if k not in NON_CODE})

    review_lines: Counter = Counter()
    review_files: Counter = Counter()
    for pr in reviewed:
        for f in pr.get("files") or []:
            lang = language_of(f["path"])
            review_lines[lang] += pr_classify.file_weight(f["path"], f.get("additions", 0), f.get("deletions", 0))
            review_files[lang] += 1
    review_total = sum(review_lines.values())

    all_fw: dict[str, set] = defaultdict(set)
    for row in by_area:
        if row["level"] == 0:
            for fw in row["frameworks"]:
                all_fw[fw["name"]].add(row["area"])
    return {
        "languages": _share_list(code_langs, lang_files["*"], overall_total, 12),
        "by_area": by_area,
        "review_languages": _share_list(review_lines, review_files, review_total, 10),
        "frameworks_seen": sorted(all_fw),
        "repos_scanned": sorted(r for r in repo_frameworks if repo_frameworks[r] is not None),
    }
