"""Provisioning helpers for `agento install` / `agento upgrade`.

Materializes the in-package Docker build context into the project directory,
generates ``docker-compose.yml`` from the bundled template (substituting
extension mounts based on enabled PyPI modules), and writes/bumps the
project-level ``pyproject.toml``.

This is the Magento-like analogue of Composer's behaviour: the project owns
``pyproject.toml`` + ``uv.lock`` (composer.json + composer.lock equivalents),
the CLI provides the build context. Docker images are built locally — no
GHCR pulls.
"""
from __future__ import annotations

import importlib.resources as ires
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ..harness.descriptor import SandboxPackage
from ..module_status import read_module_status, resolve_module_source
from ._env import parse_env_file
from ._output import log_error, log_info
from ._templates import get_package_version, get_template

_AGENTO_REQUIRES_PYTHON = ">=3.12"


# ``SandboxPackage`` is imported above purely to re-export it: existing CLI callers
# import it from here, but the dataclass belongs to the harness contract.
__all__ = ["SandboxPackage"]

# Matches a uv.lock package source line:
#   source = { registry = "<path-or-url>" }
# Captures the path/URL so we can spot relative/absolute filesystem paths
# that won't resolve inside the cron container.
_LOCK_REGISTRY_LINE = re.compile(r'^source = \{ registry = "([^"]+)" \}', re.MULTILINE)

# Matches the floor of a semver range like "~2.1.142", "^2.1.0", "2.1.142", etc.
# Captures (major, minor, patch). Used to compare a customer's pinned range
# against a new default, and to extract the floor for stale-pin warnings.
_SEMVER_FLOOR = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse_semver_floor(value: str) -> tuple[int, int, int] | None:
    """Extract (major, minor, patch) from a semver string or range.

    Accepts plain semver ("2.1.142"), npm tilde ("~2.1.142"), caret ("^2.1.0"),
    and version output strings ("2.1.126 (Claude Code)"). Returns None when no
    semver triple is present, so callers can distinguish "unparseable" from a
    real comparison and skip silently instead of crashing on a malformed pin.
    """
    m = _SEMVER_FLOOR.search(value)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _iter_module_dirs(project_root: Path | None) -> list[Path]:
    """Deprecated shim — module discovery is public now.

    Kept so existing callers/tests keep working; the implementation moved to
    ``framework/module_discovery.py`` so core contracts (``framework/harness/``) can
    use it without depending on this private CLI helper.
    """
    from ..module_discovery import iter_module_dirs

    return iter_module_dirs(project_root)


def enumerate_sandbox_packages(project_root: Path | None = None) -> list[SandboxPackage]:
    """Enumerate agent CLI declarations across all reachable modules.

    Reads ``agent_harnesses[].sandbox_package`` plus the deprecated top-level
    ``sandbox_packages`` array (one more cycle). Scans core modules, local modules
    (``<project>/app/code/``) and PyPI extensions; filters modules disabled via
    ``app/etc/modules.json``. Raises ``RuntimeError`` on a duplicate
    ``version_env_key`` so a copy-paste collision surfaces immediately instead of one
    agent silently overwriting another's pin in ``docker/.env``.
    """
    from ..harness.manifest import enumerate_sandbox_packages as _enumerate

    return _enumerate(project_root)


def dev_sandbox_dockerfile_path() -> Path:
    """The in-package sandbox Dockerfile the DEV compose file builds directly.

    Deployment renders the template into ``.agento/docker/sandbox/``; the dev compose file
    builds this path instead, so it must be a **rendered artifact of the same template**
    rather than a hand-maintained copy with the agent CLIs hardcoded. Regenerate with
    ``render_dev_sandbox_dockerfile()``; ``TestSandboxDockerfileIsRendered`` fails if it
    drifts, so adding a harness cannot silently skip the dev image.
    """
    import importlib.resources as ires

    return Path(str(ires.files("agento.framework.docker") / "sandbox" / "Dockerfile"))


def render_dev_sandbox_dockerfile(project_root: Path | None = None) -> str:
    """Render the dev sandbox Dockerfile from the template + declared packages.

    Returns the rendered text (also written to :func:`dev_sandbox_dockerfile_path`).
    """
    rendered = render_sandbox_dockerfile(
        get_template("sandbox.Dockerfile"), enumerate_sandbox_packages(project_root),
    )
    dev_sandbox_dockerfile_path().write_text(rendered)
    return rendered


def render_sandbox_dockerfile(template: str, sandbox_packages: list[SandboxPackage]) -> str:
    """Substitute the agent-CLI markers in the sandbox Dockerfile template.

    Markers:
    - ``# {{ sandbox_package_args }}`` — one ``ARG <KEY>=<default>`` per package.
    - ``# {{ sandbox_package_install }}`` — one install RUN per package manager.
    - ``# {{ sandbox_package_chown }}`` — hands each CLI's package dir + binary to
      ``agent`` so it can self-update.

    Values are inserted into a FIXED, explicitly DOUBLE-quoted template, never through
    ``shlex.quote``: single quotes would stop Docker expanding ``${<KEY>}`` and the pin
    would break. Safety comes from ``SandboxPackage``'s validation instead — after those
    regexes a package/binary/ARG name cannot contain whitespace, ``$``, a backtick,
    ``;``, ``&``, ``|``, a quote or a newline, so there is nothing to escape. The install
    command is selected per ``manager`` from a closed set; it is never built out of the
    manager string.
    """
    arg_lines = "\n".join(
        f"ARG {p.version_env_key}={p.default_range}" for p in sandbox_packages
    )

    by_manager: dict[str, list[SandboxPackage]] = {}
    for p in sandbox_packages:
        by_manager.setdefault(p.manager, []).append(p)

    install_lines: list[str] = []
    for manager, packages in sorted(by_manager.items()):
        if manager == "npm":
            specs = " ".join(
                f'"{p.package}@${{{p.version_env_key}}}"' for p in packages
            )
            install_lines.append(f"RUN npm install -g {specs}")
        else:  # pragma: no cover - SandboxPackage rejects unknown managers first
            raise RuntimeError(
                f"No sandbox install template for manager {manager!r}; "
                f"add one here alongside its allowlist entry."
            )

    # Each CLI self-updates in place, so `agent` must own its node_modules dir and its
    # binary. `|| true` keeps the build green for a manager that installs elsewhere.
    chown_lines: list[str] = []
    for p in sandbox_packages:
        if p.manager != "npm":  # pragma: no cover - only npm is allowlisted today
            continue
        chown_lines.append(
            f"RUN chown -R agent /usr/local/lib/node_modules/{p.package} 2>/dev/null || true"
            f" && \\\n    chown agent /usr/local/bin/{p.binary} 2>/dev/null || true"
        )

    out = template
    out = _replace_marker_line(out, "# {{ sandbox_package_args }}", arg_lines)
    out = _replace_marker_line(out, "# {{ sandbox_package_install }}", "\n".join(install_lines))
    out = _replace_marker_line(out, "# {{ sandbox_package_chown }}", "\n".join(chown_lines))
    return out


def _replace_marker_line(text: str, marker: str, replacement: str) -> str:
    """Replace the whole marker line with ``replacement`` (dropping it when empty)."""
    lines = text.splitlines()
    out: list[str] = []
    for line in lines:
        if line.strip() == marker:
            if replacement:
                out.extend(replacement.splitlines())
            continue
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def find_links_for_local_install() -> list[str]:
    """Return --find-links args if agento-core was installed from a local wheel.

    When the CLI was installed via `uv tool install /path/to/wheel.whl` the
    dist-info contains a ``direct_url.json`` with a ``file://`` URL.  We pass
    the wheel's parent directory to `uv sync --find-links` so the project venv
    can resolve the same version without a PyPI lookup.  Returns [] when
    agento-core came from PyPI (no extra flags needed).
    """
    try:
        import json as _json
        from importlib.metadata import distribution

        dist = distribution("agento-core")
        text = dist.read_text("direct_url.json")
        if not text:
            return []
        data = _json.loads(text)
        url = data.get("url", "")
        if url.startswith("file://") and url.endswith(".whl"):
            wheel_path = Path(url[len("file://"):])
            if wheel_path.is_file():
                return ["--find-links", str(wheel_path.parent)]
    except Exception:
        pass
    return []


def write_project_pyproject(
    project_dir: Path,
    project_name: str,
    agento_version: str,
) -> None:
    """Write ``<project>/pyproject.toml`` pinned to a specific agento-core version.

    Magento composer.json equivalent. Pin uses ``==`` for deterministic builds.
    """
    content = (
        "[project]\n"
        f'name = "{project_name}"\n'
        'version = "0.1.0"\n'
        f'requires-python = "{_AGENTO_REQUIRES_PYTHON}"\n'
        f'dependencies = ["agento-core=={agento_version}"]\n'
    )
    (project_dir / "pyproject.toml").write_text(content)


def bump_agento_version(pyproject: Path, new_version: str) -> None:
    """Update the ``agento-core==X.Y.Z`` pin in a project's pyproject.toml."""
    text = pyproject.read_text()
    text = re.sub(
        r'agento-core\s*==\s*[^"\'\s,\]]+',
        f"agento-core=={new_version}",
        text,
    )
    pyproject.write_text(text)


def detect_python_version(venv: Path) -> str:
    """Return the venv's Python version as ``major.minor`` (e.g. ``"3.12"``).

    Reads ``pyvenv.cfg`` from the venv root. Falls back to ``"3.12"`` on
    parse failure or missing file — the cron Docker image is pinned to 3.12,
    so that's the safe default that keeps mount paths consistent.
    """
    cfg = venv / "pyvenv.cfg"
    if cfg.is_file():
        for line in cfg.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("version_info") or stripped.startswith("version "):
                _, _, value = stripped.partition("=")
                parts = value.strip().split(".")
                if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                    return f"{parts[0]}.{parts[1]}"
    return "3.12"


def enumerate_enabled_extensions(project_dir: Path) -> list[str]:
    """Enabled PyPI extensions to mount into containers.

    Returns module names from ``app/etc/modules.json`` where:
    - the entry is enabled (``True``),
    - the module resolves to source ``"pypi"`` (importable from ``.venv``),
    - and is *not* a local module (``app/code/<name>/`` takes precedence).

    Local modules are already mounted through ``app/code/`` and don't need
    per-extension mounts.
    """
    status_path = project_dir / "app" / "etc" / "modules.json"
    status = read_module_status(status_path)
    venv = project_dir / ".venv"
    return sorted(
        name
        for name, enabled in status.items()
        if enabled and resolve_module_source(name, project_dir, venv) == "pypi"
    )


def _copy_tree(src, dst: Path) -> None:
    """Copy a importlib.resources Traversable tree to ``dst`` recursively."""
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        target = dst / entry.name
        if entry.is_dir():
            _copy_tree(entry, target)
        else:
            with ires.as_file(entry) as f:
                shutil.copy2(f, target)


def localize_lockfile_for_container(
    project_dir: Path,
    cron_ctx: Path,
) -> None:
    """Inline local-wheel registries from the project's ``uv.lock`` into the cron context.

    When the CLI is installed from a local wheel (``uv tool install /path/to/*.whl``),
    ``find_links_for_local_install()`` adds ``--find-links`` to ``uv sync``. uv records
    the wheel's directory as a path-style ``source = { registry = "..." }`` in the
    project's ``uv.lock`` — relative to the project directory.

    Inside the cron container, that relative path navigates above ``/`` and uv refuses
    to normalize it. Fix by copying every locally-sourced wheel/sdist into
    ``cron/_local_dist/`` and rewriting the registry path in the cron context's
    ``uv.lock`` to ``_local_dist`` (relative to ``/opt/cron-agent/uv.lock``).

    Always creates ``cron/_local_dist/`` (with a ``.gitkeep``) so the Dockerfile's
    ``COPY ./_local_dist`` step succeeds even when no rewrites are needed.
    """
    local_dist = cron_ctx / "_local_dist"
    local_dist.mkdir(parents=True, exist_ok=True)
    (local_dist / ".gitkeep").touch()

    src_lock = project_dir / "uv.lock"
    dst_lock = cron_ctx / "uv.lock"
    if not src_lock.is_file() or not dst_lock.is_file():
        return

    text = dst_lock.read_text()
    rewrote = False
    for match in _LOCK_REGISTRY_LINE.finditer(text):
        registry = match.group(1)
        if registry.startswith(("http://", "https://")):
            continue

        # Filesystem registry — resolve relative to the project's uv.lock location.
        registry_path = (project_dir / registry).resolve()
        if not registry_path.is_dir():
            continue

        for entry in registry_path.iterdir():
            if entry.is_file() and entry.suffix in (".whl", ".gz", ".zip"):
                shutil.copy2(entry, local_dist / entry.name)

        text = text.replace(match.group(0), 'source = { registry = "_local_dist" }')
        rewrote = True

    if rewrote:
        dst_lock.write_text(text)


def materialize_docker_context(
    project_dir: Path,
    *,
    force: bool = False,
) -> None:
    """Copy the in-package Docker build context to ``<project>/.agento/docker/``.

    Idempotent: stamps ``.agento/docker/version`` with the current agento-core
    version. Skips re-copy when stamp matches and ``force`` is not set.

    The cron build context additionally needs ``pyproject.toml`` + ``uv.lock``
    from the project (so ``uv sync --frozen`` inside the image produces deps
    matching the host venv). The toolbox build context needs
    ``package.json`` + ``package-lock.json`` from the agento.toolbox package.
    """
    target = project_dir / ".agento" / "docker"
    stamp = target / "version"
    current = get_package_version()

    if not force and stamp.is_file() and stamp.read_text().strip() == current:
        return

    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    ctx = ires.files("agento.framework.docker")
    _copy_tree(ctx / "sandbox", target / "sandbox")

    # The sandbox Dockerfile's agent-CLI ARGs/installs are rendered from the enabled
    # harness declarations, so adding a harness needs no framework edit (and disabling
    # one stops installing its CLI).
    (target / "sandbox" / "Dockerfile").write_text(
        render_sandbox_dockerfile(
            get_template("sandbox.Dockerfile"),
            enumerate_sandbox_packages(project_dir),
        )
    )
    _copy_tree(ctx / "cron", target / "cron")
    _copy_tree(ctx / "toolbox", target / "toolbox")

    # Cron context needs the project's lockfile + pyproject for `uv sync`
    # (deps only — agento itself comes from the bind-mounted host venv).
    project_pyproject = project_dir / "pyproject.toml"
    project_lock = project_dir / "uv.lock"
    if project_pyproject.is_file():
        shutil.copy2(project_pyproject, target / "cron" / "pyproject.toml")
    if project_lock.is_file():
        shutil.copy2(project_lock, target / "cron" / "uv.lock")

    # If the project venv was hydrated from a local wheel (developer build),
    # uv.lock contains a path-style registry that won't resolve inside the
    # cron container. Inline those wheels into the cron context.
    localize_lockfile_for_container(project_dir, target / "cron")

    # Toolbox context needs npm manifests bundled with the wheel.
    # Wheel build force-includes them under agento.framework.docker.toolbox/,
    # so `_copy_tree(ctx / "toolbox", ...)` already brought them in for
    # installed packages. For source-tree dev runs, fall back to the toolbox
    # package directly.
    toolbox_target = target / "toolbox"
    for name in ("package.json", "package-lock.json"):
        if not (toolbox_target / name).is_file():
            try:
                src = ires.files("agento.toolbox") / name
                with ires.as_file(src) as p:
                    if p.is_file():
                        shutil.copy2(p, toolbox_target / name)
            except (FileNotFoundError, ModuleNotFoundError):
                pass

    stamp.write_text(current + "\n")


def render_compose(
    template: str,
    *,
    python_version: str,
    extensions: list[str],
    sandbox_packages: list[SandboxPackage],
) -> str:
    """Substitute placeholders in the docker-compose template.

    Template placeholders:
    - ``{{ python_version }}`` — replaced with ``"3.12"`` etc.
    - line ``      # {{ extension_mounts_sandbox }}`` — replaced with mount
      lines for each enabled PyPI extension (or removed when none).
    - line ``      # {{ extension_mounts_cron }}`` — same, for the cron service.
    - line ``        # {{ sandbox_package_args }}`` — replaced with one
      ``<KEY>: ${<KEY>:-<default>}`` per ``sandbox_packages`` entry (or removed
      when no agent module ships one).
    """
    def mount_block(target_path: str) -> str:
        if not extensions:
            return ""
        lines = [
            f"      - ../.venv/lib/python{python_version}/site-packages/{ext}:{target_path}/{ext}:ro"
            for ext in extensions
        ]
        return "\n".join(lines) + "\n"

    def sandbox_args_block() -> str:
        if not sandbox_packages:
            return ""
        lines = [
            f"        {pkg.version_env_key}: ${{{pkg.version_env_key}:-{pkg.default_range}}}"
            for pkg in sandbox_packages
        ]
        return "\n".join(lines) + "\n"

    rendered = template.replace(
        "      # {{ extension_mounts_sandbox }}\n",
        mount_block("/opt/agento-src"),
    )
    rendered = rendered.replace(
        "      # {{ extension_mounts_cron }}\n",
        mount_block("/opt/agento-src"),
    )
    rendered = rendered.replace(
        "        # {{ sandbox_package_args }}\n",
        sandbox_args_block(),
    )
    rendered = rendered.replace("{{ python_version }}", python_version)
    return rendered


def build_base_images(project_dir: Path, version: str) -> None:
    """Build the managed ``agento-<service>:<version>`` base images.

    Runs ``docker build`` directly against ``.agento/docker/<service>/Dockerfile``
    rather than ``docker compose build``, so a user override in
    ``docker-compose.override.yml`` cannot shadow the managed base build.
    Without this, an override that defines its own ``build:`` section (e.g. a
    custom toolbox Dockerfile that ``FROM agento-toolbox:<version>``) leaves
    the base tag missing and the override build fails with "pull access denied"
    on Docker Hub.

    Call AFTER ``materialize_docker_context()`` and BEFORE any
    ``docker compose build``. Build args mirror
    ``src/agento/framework/cli/templates/docker-compose.yml`` — kept in sync by
    ``TestBuildBaseImagesTemplateDriftGuard``.
    """
    env_path = project_dir / "docker" / ".env"
    env = parse_env_file(env_path) if env_path.is_file() else {}
    host_uid = env.get("HOST_UID", "1000")
    host_gid = env.get("HOST_GID", "1000")
    sandbox_tag = f"agento-sandbox:{version}"
    ctx_root = project_dir / ".agento" / "docker"

    host_ids = [
        "--build-arg", f"HOST_UID={host_uid}",
        "--build-arg", f"HOST_GID={host_gid}",
    ]
    sandbox_args = list(host_ids)
    for pkg in enumerate_sandbox_packages(project_dir):
        sandbox_args.extend([
            "--build-arg",
            f"{pkg.version_env_key}={env.get(pkg.version_env_key, pkg.default_range)}",
        ])
    # Order matters: sandbox before cron (cron's FROM resolves SANDBOX_IMAGE).
    specs = [
        ("sandbox", ctx_root / "sandbox", sandbox_args),
        ("toolbox", ctx_root / "toolbox", host_ids),
        ("cron", ctx_root / "cron",
            ["--build-arg", f"SANDBOX_IMAGE={sandbox_tag}"]),
    ]

    for service, ctx, args in specs:
        tag = f"agento-{service}:{version}"
        log_info(f"Building base image {tag}...")
        result = subprocess.run(
            ["docker", "build", "-t", tag, *args, str(ctx)]
        )
        if result.returncode != 0:
            log_error(f"Failed to build base image {tag}.")
            sys.exit(result.returncode)


def regenerate_compose(project_dir: Path) -> None:
    """Render and write ``<project>/docker/docker-compose.yml`` from template.

    Reads enabled extensions from ``app/etc/modules.json``, detects the
    project venv's Python version, substitutes mount lines, writes the result.
    Sandbox build args (CLI version pins) are sourced from each agent module's
    ``sandbox_packages`` di.json declaration — adding a new agent requires no
    framework edit here.
    """
    py_ver = detect_python_version(project_dir / ".venv")
    extensions = enumerate_enabled_extensions(project_dir)
    sandbox_packages = enumerate_sandbox_packages(project_dir)
    template = get_template("docker-compose.yml")
    rendered = render_compose(
        template,
        python_version=py_ver,
        extensions=extensions,
        sandbox_packages=sandbox_packages,
    )
    out = project_dir / "docker" / "docker-compose.yml"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(rendered)
