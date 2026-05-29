# Hermes → Atlaz Rebrand Audit Report

**Repo:** `/Users/azriel/dev/atlaz` (branch: `main`)
**Scanned:** May 30, 2026
**Total matches found:** ~3,476 references to "hermes" across the codebase

---

## Category 1: Package / Build System (HIGH Impact)

These control what name the project publishes as, how it's imported, and its dependency graph. Miss any of these and `pip install` / `uv sync` / imports break.

| File | Line(s) | Match | Notes |
|------|---------|-------|-------|
| `pyproject.toml` | 6 | `name = "hermes-agent"` | **PyPI package name** — must change for publishing |
| `pyproject.toml` | 141-211 | `"hermes-agent[cron]"`, `"hermes-agent[cli]"`, etc. | 19 references to `hermes-agent[extra]` in optional-deps and `[all]` |
| `pyproject.toml` | 215-217 | `hermes = "hermes_cli.main:main"`, `hermes-agent = "run_agent:main"`, `hermes-acp = "acp_adapter.entry:main"` | **CLI entry points / console_scripts** — what `hermes` command runs |
| `pyproject.toml` | 220 | `py-modules = ["run_agent", ..., "hermes_bootstrap", "hermes_constants", "hermes_state", "hermes_time", "hermes_logging"]` | Module list for setuptools |
| `pyproject.toml` | 223 | `hermes_cli = ["web_dist/**/*", ...]` | Package data for hermes_cli |
| `pyproject.toml` | 240 | `include = [..., "hermes_cli", "gateway", ...]` | Package include pattern |
| `package.json` | 2 | `"name": "hermes-agent"` | npm package name (TUI) |
| `package-lock.json` | 2, 8 | `"name": "hermes-agent"` (twice) | npm lockfile |
| `acp_registry/agent.json` | 2, 6, 7, 12 | `"id": "hermes-agent"`, `"package": "hermes-agent[acp]==0.15.1"`, repo/website URLs | ACP registry metadata |

**Impact**: HIGH — `import hermes_*` statements, pip installs, and CLI entry points all depend on these names.

---

## Category 2: Python Module / Package Names (HIGH Impact)

The following modules/packages are named `hermes_*` and can be imported as such. Renaming them means updating every file that imports from them, plus the directory structure.

### Top-level modules (files in `/Users/azriel/dev/atlaz/`):

| File | Usage pattern |
|------|---------------|
| `hermes_constants.py` | `from hermes_constants import ...` — used in ~60+ files |
| `hermes_state.py` | `from hermes_state import SessionDB` — used in 10+ files |
| `hermes_logging.py` | `from hermes_logging import ...` — import at module level |
| `hermes_bootstrap.py` | `import hermes_bootstrap` — 4 entry points |
| `hermes_time.py` | `from hermes_time import now` — used in cron files |
| `hermes_cli/` | `from hermes_cli.xxx import ...` — the main CLI package, used in 100+ files |
| `hermes-already-has-routines.md` | Doc file, not imported |

### Imports from `hermes_cli` (representative sample — too numerous to list all):

Files importing from `hermes_cli`: `run_agent.py`, `cli.py`, `batch_runner.py`, `gateway/run.py`, `cron/scheduler.py`, `cron/jobs.py`, `model_tools.py`, `tools/*.py`, `mcp_serve.py`, `agent/*.py`, `tests/*.py`, and many more.

**Sub-packages within `hermes_cli/`:**
- `hermes_cli.main` (entry point)
- `hermes_cli.config`
- `hermes_cli.env_loader`
- `hermes_cli.commands`
- `hermes_cli.plugins`
- `hermes_cli.profiles`
- `hermes_cli.models`
- `hermes_cli.providers`
- `hermes_cli.tools_config`
- `hermes_cli.kanban`, `hermes_cli.kanban_db`, `hermes_cli.kanban_decompose`
- `hermes_cli.auth`
- `hermes_cli.runtime_provider`
- `hermes_cli.goals`
- `hermes_cli.skin_engine`
- `hermes_cli.web_server`, `hermes_cli.tips`
- `hermes_cli.session_recap`
- `hermes_cli.model_switch`
- `hermes_cli.security_advisories`
- `hermes_cli.build_info`
- `hermes_cli.fallback_config`
- `hermes_cli._subprocess_compat`
- `hermes_cli.tui_dist`, `hermes_cli.web_dist`, `hermes_cli.scripts`
- `hermes_cli.banner` (referenced in code)

### Tests directory:

| File | Pattern |
|------|---------|
| `tests/test_hermes_state.py` | Test file named `test_hermes_state` |
| `tests/test_hermes_state_wal_fallback.py` | Named after `hermes_state` |
| `tests/test_hermes_state_compression_locks.py` | Named after `hermes_state` |
| `tests/test_hermes_logging.py` | Named after `hermes_logging` |
| `tests/test_hermes_home_profile_warning.py` | Named after `hermes_home` |
| `tests/test_hermes_constants.py` | Named after `hermes_constants` |
| `tests/test_hermes_bootstrap.py` | Named after `hermes_bootstrap` |
| `tests/hermes_cli/` | Directory named `hermes_cli` |
| `tests/hermes_cli/test_setup_hermes_script.py` | Test in hermes_cli dir |
| `tests/hermes_cli/test_nous_hermes_non_agentic.py` | In hermes_cli dir |
| `tests/agent/transports/test_hermes_tools_mcp_server.py` | Named after transport module |

### Agent transports:

| File | Pattern |
|------|---------|
| `agent/transports/hermes_tools_mcp_server.py` | Module `hermes_tools_mcp_server` — docstring mentions "Hermes-tools-as-MCP server" |

**Impact**: HIGH — every Python file that does `from hermes_* import ...` or `from hermes_cli import ...` (hundreds of files) needs updating.

---

## Category 3: CLI Commands & Entry Points (HIGH Impact)

| Location | Reference | Notes |
|----------|-----------|-------|
| `pyproject.toml:215` | `hermes = "hermes_cli.main:main"` | The `hermes` CLI command |
| `pyproject.toml:216` | `hermes-agent = "run_agent:main"` | The `hermes-agent` command |
| `pyproject.toml:217` | `hermes-acp = "acp_adapter.entry:main"` | The `hermes-acp` command |
| `hermes` (file) | `from hermes_cli.main import main` | Entry wrapper script at repo root |
| `nix/hermes-agent.nix:172-175` | `"hermes"`, `"hermes-agent"`, `"hermes-acp"` | Wrapper targets |
| `packaging/homebrew/hermes-agent.rb:29` | `%w[hermes hermes-agent hermes-acp]` | Homebrew install symlinks |
| `docker/hermes-exec-shim.sh:43` | `REAL=/opt/hermes/.venv/bin/hermes` | Docker exec shim |
| `scripts/hermes-gateway` | `SERVICE_NAME = "hermes-gateway"`, docstrings, comments | Gateway service script |
| `plugins/kanban/systemd/hermes-kanban-dispatcher.service` | Service named `hermes-kanban` | Systemd unit |

---

## Category 4: Configuration Paths & Environment Variables (HIGH Impact)

### HERMES_HOME and ~/.hermes paths

The fundamental data directory. Changing this affects every file that reads/writes under this path.

| Location | Pattern | Notes |
|----------|---------|-------|
| `hermes_constants.py:46` | `~/.hermes` (default) | The core path constant |
| `hermes_constants.py:63` | `os.environ.get("HERMES_HOME", "")` | Env var check |
| `hermes_constants.py:63,75,88,101` | `HERMES_HOME` (env var name) | Multiple references |
| `hermes_constants.py:75` | `Path.home() / ".hermes" / "active_profile"` | Profile path |
| `hermes_constants.py:88-93` | `[HERMES_HOME fallback]` warning message | Log message |
| `hermes_constants.py:101` | `Path.home() / ".hermes"` | Fallback path |
| 30+ files | Various `HERMES_HOME` usage | See `search_files` output for full list |

### All HERMES_* environment variables

These are used throughout the codebase and would need renaming for consistency:

| Env var | Used in |
|---------|---------|
| `HERMES_HOME` | 30+ files (core path) |
| `HERMES_OPTIONAL_SKILLS` | hermes_constants.py |
| `HERMES_OPTIONAL_MCPS` | hermes_constants.py |
| `HERMES_BUNDLED_SKILLS` | hermes_constants.py, nix file, Dockerfile |
| `HERMES_BUNDLED_PLUGINS` | nix/hermes-agent.nix |
| `HERMES_WEB_DIST` | Dockerfile, nix file |
| `HERMES_TUI_DIR` | nix file |
| `HERMES_PYTHON` | nix file |
| `HERMES_NODE` | nix file |
| `HERMES_REVISION` | nix file |
| `HERMES_MANAGED` | Homebrew formula |
| `HERMES_UID` / `HERMES_GID` | docker-compose.yml |
| `HERMES_DOCKER_EXEC_AS_ROOT` | hermes-exec-shim.sh, Dockerfile |
| `HERMES_DASHBOARD_INSECURE` | Docker env |
| `HERMES_DASHBOARD_HOST` | docker-compose.windows.yml |
| `HERMES_GIT_SHA` | Dockerfile build arg |
| `HERMES_GATEWAY_LOCK_DIR` | gateway/status.py |
| `HERMES_GATEWAY_NO_SUPERVISE` | Release notes |
| `HERMES_CRON_SCRIPT_TIMEOUT` | cron/scheduler.py |
| `HERMES_CRON_SESSION` | cron/scheduler.py |
| `HERMES_CRON_TIMEOUT` | cron/scheduler.py |
| `HERMES_CRON_MAX_PARALLEL` | cron/scheduler.py |
| `HERMES_CRON_AUTO_DELIVER_PLATFORM` | cron/scheduler.py |
| `HERMES_CRON_AUTO_DELIVER_CHAT_ID` | cron/scheduler.py |
| `HERMES_CRON_AUTO_DELIVER_THREAD_ID` | cron/scheduler.py |
| `HERMES_SESSION_PLATFORM` | gateway/session_context.py |
| `HERMES_SESSION_CHAT_ID` | gateway/session_context.py |
| `HERMES_SESSION_CHAT_NAME` | gateway/session_context.py |
| `HERMES_SESSION_THREAD_ID` | gateway/session_context.py |
| `HERMES_MODEL` | cron/scheduler.py |
| `HERMES_PREFILL_MESSAGES_FILE` | cron/scheduler.py |
| `HERMES_TIMEZONE` | hermes_time.py |
| `HERMES_PORTAL_BASE_URL` | RELEASE_v0.8.0.md |
| `HERMES_INFERENCE_PROVIDER` | cron/scheduler.py (comment) |

### Gateway PID file references

| File | Pattern |
|------|---------|
| `gateway/status.py:7` | `{HERMES_HOME}/gateway.pid` |
| `gateway/status.py:9` | `HERMES_HOME` directories |
| `gateway/status.py:45` | Gateway PID file path respecting HERMES_HOME |

---

## Category 5: URLs & External Links (MEDIUM Impact)

### GitHub repository URLs

| File | URL | Notes |
|------|-----|-------|
| `hermes_constants.py:57` | `https://github.com/NousResearch/hermes-agent/issues/18594` | Issue link in comment |
| `hermes_constants.py:265` | `https://github.com/NousResearch/hermes-agent/issues/25821` | Issue link in comment |
| `SECURITY.md:9` | `https://github.com/NousResearch/hermes-agent/security/advisories/new` | Security policy |
| `AGENTS.md` (many) | `NousResearch/hermes-agent` | Throughout dev guide |
| `acp_registry/agent.json:6` | `https://github.com/NousResearch/hermes-agent` | ACP registry |
| `skilss/autonomous-ai-agents/hermes-agent/SKILL.md:11` | `https://github.com/NousResearch/hermes-agent` | Skill metadata |
| `.github/ISSUE_TEMPLATE/*.yml` (multiple) | `https://github.com/NousResearch/hermes-agent/...` | Issue templates |
| `.github/workflows/skills-index.yml:21` | `NousResearch/hermes-agent` | CI workflow |
| `.github/workflows/skills-index-freshness.yml:21` | `NousResearch/hermes-agent` | CI workflow |
| `.github/workflows/deploy-site.yml:37` | `NousResearch/hermes-agent` | CI workflow |
| `.github/workflows/docker-publish.yml:49,185,284` | `NousResearch/hermes-agent` | CI workflow |
| `.github/workflows/deploy-site.yml:88` | `hermes-agent.nousresearch.com` | Docs URL |
| `.github/workflows/skills-index-freshness.yml:28` | `https://hermes-agent.nousresearch.com/docs/api/skills-index.json` | API URL |
| `.github/dependabot.yml:1` | `# Dependabot configuration for hermes-agent.` | Comment |
| `nix/hermes-agent.nix:215` | `https://github.com/NousResearch/hermes-agent` | Nix meta.homepage |
| `AGENTS.md` | `https://github.com/NousResearch/hermes-agent` | Multiple references |

### Documentation site URLs

| File | URL |
|------|-----|
| `README.md:8` | `https://hermes-agent.nousresearch.com/docs/` |
| `README.zh-CN.md:8,39,66,83,105,111-129,164` | Multiple `hermes-agent.nousresearch.com` links |
| `skilss/autonomous-ai-agents/hermes-agent/SKILL.md:32,156,235,368,398,658,679,708` | Multiple docs links |
| `packaging/homebrew/hermes-agent.rb:5` | `https://hermes-agent.nousresearch.com` |
| `acp_registry/agent.json:7` | `https://hermes-agent.nousresearch.com/docs/user-guide/features/acp` |
| `website/static/api/model-catalog.json:6` | `https://hermes-agent.nousresearch.com/docs/reference/model-catalog` |

### Install script URLs

| File | URL |
|------|-----|
| `skilss/autonomous-ai-agents/hermes-agent/SKILL.md:38` | `https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh` |

---

## Category 6: Docker References (HIGH Impact)

| File | Reference | Notes |
|------|-----------|-------|
| `docker-compose.yml:32,64` | `image: hermes-agent` | Docker image name |
| `docker-compose.yml:33` | `container_name: hermes` | Container name |
| `docker-compose.yml:37` | `~/.hermes:/opt/data` | Volume mount |
| `docker-compose.yml:39-40` | `HERMES_UID`, `HERMES_GID` | Env vars |
| `docker-compose.windows.yml:14,25` | `image: nousresearch/hermes-agent:latest` | Docker Hub image |
| `docker-compose.windows.yml:15,26` | `container_name: hermes` / `hermes-dashboard` | Container names |
| `docker-compose.windows.yml:20-21,33-35` | `HERMES_UID`, `HERMES_GID`, `HERMES_DASHBOARD_HOST` | Env vars |
| `Dockerfile:79` | `useradd -u 10000 -m -d /opt/data hermes` | User named `hermes` |
| `Dockerfile:179` | `chown -R hermes:hermes ...` | User ownership |
| `Dockerfile:185-188` | `# Link hermes-agent itself` | Comment |
| `Dockerfile:207` | `HERMES_GIT_SHA` build arg | Build arg |
| `Dockerfile:209` | `.hermes_build_sha` | Build info file |
| `Dockerfile:237` | `ENV HERMES_WEB_DIST=...` | Env var |
| `Dockerfile:238` | `ENV HERMES_HOME=/opt/data` | Env var |
| `Dockerfile:251` | `COPY ... docker/hermes-exec-shim.sh /opt/hermes/bin/hermes` | Shim binary |
| `Dockerfile:264` | `ENV PATH="/opt/hermes/bin:/opt/hermes/.venv/bin:..."` | PATH entries |
| `docker/hermes-exec-shim.sh:3,8,15,36,43,48,59,76-77,87` | Multiple `hermes` references | Shim script |
| `docker/s6-rc.d/user/contents.d/main-hermes` | File named `main-hermes` | s6 service |
| `.github/actions/hermes-smoke-test/action.yml` | `nousresearch/hermes-agent:test`, `hermes --help`, etc. | CI smoke test |
| `.github/workflows/docker-publish.yml:39` | `IMAGE_NAME: nousresearch/hermes-agent` | CI image build |
| `.github/workflows/docker-publish.yml:80,232` | `uses: ./.github/actions/hermes-smoke-test` | CI action |

---

## Category 7: GitHub Actions & CI (MEDIUM Impact)

| File | Reference |
|------|-----------|
| `.github/workflows/docker-publish.yml:39` | `IMAGE_NAME: nousresearch/hermes-agent` |
| `.github/workflows/docker-publish.yml:49,185,284` | `github.repository == 'NousResearch/hermes-agent'` |
| `.github/workflows/deploy-site.yml:37` | `github.repository == 'NousResearch/hermes-agent'` |
| `.github/workflows/skills-index.yml:21` | `github.repository == 'NousResearch/hermes-agent'` |
| `.github/workflows/skills-index-freshness.yml:21,28` | repo check + URL |
| `.github/workflows/upload_to_pypi.yml:64-67,71-72,76-78,95` | `hermes_cli/tui_dist`, `hermes_cli/web_dist`, `hermes_cli/scripts`, `pypi.org/p/hermes-agent` |
| `.github/actions/hermes-smoke-test/action.yml` | Action file named `hermes-smoke-test` |
| `.github/actions/nix-setup/action.yml:16` | `name: hermes-agent` |
| `.github/ISSUE_TEMPLATE/*.yml` (4 files) | Multiple `hermes` command and repo references |
| `.github/dependabot.yml:1` | `# Dependabot configuration for hermes-agent.` |

---

## Category 8: File & Directory Names (HIGH Impact)

### Python package directories that need renaming:

| Current path | Type |
|--------------|------|
| `/Users/azriel/dev/atlaz/hermes_cli/` | **Main Python package** — must rename to `atlaz_cli/` |
| `/Users/azriel/dev/atlaz/tests/hermes_cli/` | Test directory mirroring package |

### Top-level Python modules that need renaming:

| Current file | Import name |
|--------------|-------------|
| `hermes_constants.py` | `hermes_constants` |
| `hermes_state.py` | `hermes_state` |
| `hermes_logging.py` | `hermes_logging` |
| `hermes_bootstrap.py` | `hermes_bootstrap` |
| `hermes_time.py` | `hermes_time` |
| `hermes` (executable wrapper) | N/A |

### Other named files:

| File | Notes |
|------|-------|
| `scripts/hermes-gateway` | Gateway management script |
| `setup-hermes.sh` | Setup script |
| `skilss/autonomous-ai-agents/hermes-agent/` | Skill directory name `hermes-agent` |
| `agent/transports/hermes_tools_mcp_server.py` | MCP transport module |
| `nix/hermes-agent.nix` | Nix derivation file |
| `packaging/homebrew/hermes-agent.rb` | Homebrew formula |
| `plugins/kanban/systemd/hermes-kanban-dispatcher.service` | Systemd unit |
| `docker/s6-rc.d/user/contents.d/main-hermes` | s6 service file |
| `docker/hermes-exec-shim.sh` | Docker shim script |
| `docs/hermes-kanban-v1-spec.pdf` | Kanban spec document |
| `hermes-already-has-routines.md` | Documentation |
| `optional-mcps/n8n/manifest.yaml:6` | References `hermes` in MCP manifest |
| `website/docs/guides/*-hermes*.md` (7 files) | Website doc files |
| `website/i18n/zh-Hans/.../*-hermes*.md` (7 files) | Chinese translated docs |
| `website/static/img/hermes-agent-banner.png` | Banner image |
| `skilss/productivity/google-workspace/scripts/_hermes_home.py` | Helper script named `_hermes_home` |

---

## Category 9: Code Comments & Docstrings (LOW-MEDIUM Impact)

These don't break functionality but create branding confusion. Hundreds of occurrences across the codebase. Representative samples:

| File | Pattern |
|------|---------|
| `run_agent.py:3` | `AI Agent Runner with Tool Calling` (docstring mentions Hermes elsewhere) |
| `cli.py:3-5` | `Hermes Agent CLI - Interactive Terminal Interface` |
| `hermes_constants.py:1` | `Shared constants for Hermes Agent.` |
| `hermes_bootstrap.py:1` | `Windows UTF-8 bootstrap for Hermes entry points.` |
| `hermes_bootstrap.py:16` | `at the very top of every Hermes entry point (hermes, hermes-agent, hermes-acp, ...)` |
| `hermes_state.py:3` | `SQLite State Store for Hermes Agent.` |
| `hermes_logging.py:1` | `Centralized logging setup for Hermes Agent.` |
| `hermes_time.py:2` | `Timezone-aware clock for Hermes.` |
| `model_tools.py` | Multiple docstrings |
| `toolsets.py:3` | `Toolsets Module` (docstring mentions toolsets) |
| `gateway/run.py` | Hundreds of docstrings/comments mentioning hermes |
| `cron/scheduler.py` | Dozens of comments mentioning hermes |
| `tools/*.py` | Multiple tools have docstrings with hermes references |
| `hermes_cli/` package | Every file has Hermes references in docstrings |
| ~900 test files | Many reference Hermes in docstrings |

---

## Category 10: Documentation Files (LOW-MEDIUM Impact)

### Top-level docs:

| File | Notes |
|------|-------|
| `README.md` | Full rebrand needed: title, description, badges, links |
| `README.zh-CN.md` | Full rebrand in Chinese |
| `AGENTS.md` | ~95 references — used for AI coding assistants |
| `SECURITY.md:9` | GitHub security advisory URL |
| `CONTRIBUTING.md` | Likely needs updating |

### RELEASE notes (all need rebranding):

| File | Count |
|------|-------|
| `RELEASE_v0.2.0.md` | 183 references |
| `RELEASE_v0.4.0.md` | 286 references |
| `RELEASE_v0.5.0.md` | 216 references |
| `RELEASE_v0.8.0.md` | 215 references |
| `RELEASE_v0.9.0.md` | 199 references |
| `RELEASE_v0.14.0.md` | 219 references |
| `RELEASE_v0.15.0.md` | 362 references |
| `RELEASE_v0.15.1.md` | 28 references |

### Website docs:

| Path | Notes |
|------|-------|
| `website/docs/guides/*-hermes*.md` | 5 files |
| `website/docs/user-guide/skills/bundled/*/autonomous-ai-agents-hermes-agent.md` | Skill docs |
| `website/i18n/zh-Hans/...` | 7 Chinese translated files |
| `website/static/img/hermes-agent-banner.png` | Banner image asset |
| `website/static/api/model-catalog.json` | References `hermes-agent` |

### Skill documentation files:

| File | Notes |
|------|-------|
| `skilss/autonomous-ai-agents/hermes-agent/SKILL.md` | ~180 references — the Hermes skill itself |
| `skilss/productivity/google-workspace/SKILL.md` | ~9 references |
| `skilss/red-teaming/godmode/SKILL.md` | ~11 references |
| `skilss/media/spotify/SKILL.md` | ~3 references |
| Many other SKILL.md files | 1-21 references each |
| `gateway/platforms/ADDING_A_PLATFORM.md` | ~10 references |

---

## Category 11: Skills & Plugins (LOW-MEDIUM Impact)

### The hermes-agent skill:

| File | Notes |
|------|-------|
| `skilss/autonomous-ai-agents/hermes-agent/SKILL.md` | Metadata: `name: hermes-agent`, tags: `[hermes, setup, ...]`, homepage URL, all content references Hermes |

### Skill helper scripts:

| File | Notes |
|------|-------|
| `skilss/productivity/google-workspace/scripts/_hermes_home.py` | Helper named `_hermes_home.py` |
| `skilss/productivity/google-workspace/scripts/gws_bridge.py:3` | References hermes |
| `skilss/productivity/google-workspace/scripts/setup.py:5` | References hermes |
| `skilss/productivity/google-workspace/scripts/google_api.py:3` | References hermes |
| `skilss/red-teaming/godmode/scripts/*.py` | Multiple scripts reference hermes |

### Plugins:

| File | Notes |
|------|-------|
| `plugins/kanban/systemd/hermes-kanban-dispatcher.service` | Systemd unit for kanban |
| `plugins/hermes-achievements/` | Plugin directory name |
| `plugins/observability/` (likely has references) | |

---

## Category 12: Nix / Homebrew / Packaging (HIGH Impact)

| File | Notes |
|------|-------|
| `nix/hermes-agent.nix` | Package name `hermes-agent`, all `hermesVenv`, `hermesTui`, `hermesWeb`, `hermesNpmLib` references, wrapper binaries, env vars |
| `nix/python.nix` | May reference hermes |
| `nix/tui.nix` | May reference hermes |
| `nix/web.nix` | May reference hermes |
| `nix/lib.nix` | May reference hermes |
| `packaging/homebrew/hermes-agent.rb` | Class `HermesAgent`, homepage, URL, brew formula |
| `optional-skills/migration/openclaw-migration/scripts/openclaw_to_hermes.py` | Migration script named `openclaw_to_hermes` — ~3k lines |

---

## Category 13: Internal Code Attributes & Markers (LOW Impact)

These are internal markers used for function attributes and ContextVars:

| File | Reference |
|------|-----------|
| `hermes_constants.py:15` | `_HERMES_HOME_OVERRIDE: ContextVar` |
| `hermes_constants.py:434` | `_hermes_ipv4_patched` function attribute |
| `hermes_logging.py:104` | `_hermes_session_injector` function attribute |
| `hermes_logging.py:144` | `hermes_plugins` in COMPONENT_PREFIXES |
| `hermes_logging.py:274,280` | `_hermes_verbose` handler attribute |
| `gateway/session_context.py:51-53` | ContextVars named `HERMES_SESSION_*` |

---

## Summary by Impact Level

| Impact | Description | Approximate count |
|--------|-------------|-------------------|
| **HIGH** | Breaks functionality (imports, CLI, package names, env vars) | ~500 files touched |
| **MEDIUM** | Cosmetic/confusing (URLs, docs, CI, Docker image names) | ~100 files touched |
| **LOW** | Comments/docstrings/internal names | ~2,800+ occurrences |

### Rename Priority Order (highest risk first):

1. **`hermes_constants.py`** — lowest-level import, everything depends on it
2. **`hermes_cli/`** package — rename to `atlaz_cli/`
3. **`pyproject.toml`** — package name, entry points, extras
4. **`hermes_bootstrap.py`** — imported at entry points
5. **`hermes_state.py`**, **`hermes_logging.py`**, **`hermes_time.py`** — top-level modules
6. **`Dockerfile`** + **`docker-compose.yml`** — image names, build args
7. **`.github/workflows/*`** — CI repo checks, image tags
8. **`nix/hermes-agent.nix`** + **`packaging/homebrew/hermes-agent.rb`** — packaging
9. **All import statements** across the codebase (~hundreds of files)
10. **Documentation** and skill files (lower risk, purely cosmetic)
