"""Pi workspace materialization.

Everything Pi reads is written to ``$HOME/.pi/agent/`` — which sits inside the per-run
HOME — rather than to the project's ``.pi/``. That is not a style preference: Pi's
default ``defaultProjectTrust: "ask"`` makes non-interactive runs **ignore** a project's
``.pi/settings.json`` and ``.pi/skills``, and the trust gate never prompts headlessly. A
global-HOME location is outside that gate, so the configuration actually applies.

The one exception is the bridge and its connection file, which live in the build/run
directory: Pi is given the extension by path (``-e``), and extension loading happens
before the trust gate too.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from agento.framework.agent_manager.models import CredentialRecord
from agento.framework.harness import ToolboxConnectionSpec

from .auth import CREDENTIAL_TYPE

BRIDGE_DIR = ".pi"
BRIDGE_FILENAME = "agento-toolbox.js"
BRIDGE_CONFIG_FILENAME = "agento-toolbox.json"

# Ollama needs no credential, but Pi hides a model whose provider has no key. This is the
# dummy Pi's own docs prescribe for keyless local servers — never a real secret, which is
# why it is a literal here rather than anything read from config.
OLLAMA_PLACEHOLDER_API_KEY = "ollama"

_BRIDGE_SOURCE = Path(__file__).resolve().parent.parent / "bridge" / BRIDGE_FILENAME


class PiWorkspaceAdapter:
    """Materializes Pi's configuration, the Toolbox bridge, and its connection file."""

    # -- workspace ------------------------------------------------------------

    def prepare_workspace(
        self,
        working_dir: Path,
        agent_config: dict[str, str],
        *,
        agent_view_id: int | None = None,
        toolbox_url: str,
        harness_config: dict[str, str] | None = None,
    ) -> None:
        working_dir = Path(working_dir)
        pi_home = working_dir / ".pi" / "agent"
        pi_home.mkdir(parents=True, exist_ok=True)

        (pi_home / "settings.json").write_text(
            json.dumps({"defaultProjectTrust": "trusted"}, indent=2) + "\n"
        )

        # NOT AGENTS.md. `workspace_build/builder.py` already writes it into the build
        # dir, which is Pi's cwd, and Pi reads context files from cwd natively. Writing
        # it to the global home as well would CONCATENATE both copies, duplicating every
        # instruction in every job.

        provider = agent_config.get("provider") or ""
        if provider == "ollama":
            self._write_models_json(pi_home, agent_config)

        self._install_bridge(working_dir)
        # agent_view scoping is applied here, where the id is in scope;
        # `ToolboxConnectionSpec` carries only (name, transport, url, headers).
        url = f"{toolbox_url.rstrip('/')}/mcp"
        if agent_view_id is not None:
            url = f"{url}?agent_view_id={agent_view_id}"
        self.serialize_toolbox_connection(
            ToolboxConnectionSpec(name="toolbox", transport="http", url=url),
            working_dir,
            expectations=self._model_expectations(agent_config),
            allow_substitution=self._allows_substitution(harness_config),
        )

    @staticmethod
    def _model_expectations(agent_config: dict[str, str]) -> dict[str, str]:
        """What the bridge's in-process model guard compares against.

        Without these keys in the connection file the guard takes its inactive branch and
        the whole cross-path check is dead code — which is what an earlier version shipped.
        Only non-empty strings are written, so a partially configured agent_view leaves the
        guard off rather than comparing against "".
        """
        out: dict[str, str] = {}
        provider = agent_config.get("provider")
        model = agent_config.get("model")
        if isinstance(provider, str) and provider.strip():
            out["expected_provider"] = provider.strip()
        if isinstance(model, str) and model.strip():
            out["expected_model"] = model.strip()
        return out

    @staticmethod
    def _allows_substitution(harness_config: dict[str, str] | None) -> bool:
        """Whether a router/meta model is configured — see `pi/allow_model_substitution`."""
        return bool(harness_config) and harness_config.get("allow_model_substitution") == "1"

    def _write_models_json(self, pi_home: Path, agent_config: dict[str, str]) -> None:
        """Custom-provider catalogue for Ollama.

        Four constraints, each of which breaks the run if missed:
        * at least one ``models[]`` entry — a provider with none is "Unknown provider"
          and exits 1;
        * the entry's ``id`` must be EXACTLY ``agent_view/model``, so Pi takes the
          exact-match path instead of substring-matching and cloning a fallback model
          with another model's context window and pricing;
        * a **placeholder** ``apiKey`` is REQUIRED even though Ollama needs no auth. Pi
          treats a model as unavailable until its provider has a key, so omitting it makes
          every run exit with "No API key found for ollama." before reaching the model —
          verified live. Pi's own models.md says a keyless local server "should keep a
          dummy value";
        * never ``authHeader: true`` — it throws when no key is present.
        """
        model = agent_config.get("model") or ""
        base_url = agent_config.get("provider_options/base_url") or ""
        catalogue = {
            "providers": {
                "ollama": {
                    "name": "Ollama",
                    "baseUrl": base_url,
                    "api": "openai-completions",
                    # Not a secret and not read by the server: Ollama ignores the value.
                    # It exists solely to satisfy Pi's "provider has auth" precondition.
                    "apiKey": OLLAMA_PLACEHOLDER_API_KEY,
                    # `compat` is required for Ollama per Pi's own docs.
                    "compat": {
                        "supportsDeveloperRole": False,
                        "supportsReasoningEffort": False,
                    },
                    "models": [
                        {
                            "id": model,
                            "name": model,
                            "contextWindow": 32768,
                            "maxTokens": 8192,
                        }
                    ],
                }
            }
        }
        (pi_home / "models.json").write_text(json.dumps(catalogue, indent=2) + "\n")

    def _install_bridge(self, target_dir: Path) -> None:
        bridge_dir = Path(target_dir) / BRIDGE_DIR
        bridge_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(_BRIDGE_SOURCE, bridge_dir / BRIDGE_FILENAME)

    # -- toolbox connection ---------------------------------------------------

    def serialize_toolbox_connection(
        self,
        spec: ToolboxConnectionSpec,
        target_dir: Path,
        expectations: dict[str, str] | None = None,
        allow_substitution: bool = False,
    ) -> None:
        """Render the connection into the file the bridge reads.

        Pi has no MCP of its own, so there is no ``.mcp.json`` equivalent — the bridge
        extension reads this file from its cwd. It cannot come from a CLI flag: flags
        registered by extensions are applied *after* the extension factories run, so the
        value would not exist when the factory needs it.
        """
        payload: dict[str, object] = {"url": spec.url}
        if spec.headers:
            payload["headers"] = dict(spec.headers)
        payload.update(expectations or {})
        if allow_substitution:
            # A router model dispatches by design, so its identity must not be enforced —
            # but say so EXPLICITLY rather than by deleting `expected_model`. Absence has a
            # second cause (an agent_view with no model configured), and inferring the
            # opt-out from it disabled the guard for a case that wanted it on. The
            # expectation still records intent; the marker decides enforcement.
            payload["allow_model_substitution"] = True
        target = Path(target_dir) / BRIDGE_DIR
        target.mkdir(parents=True, exist_ok=True)
        (target / BRIDGE_CONFIG_FILENAME).write_text(json.dumps(payload, indent=2) + "\n")

    def inject_runtime_params(
        self,
        artifacts_dir: Path,
        *,
        job_id: int | None,
        effective_model: str | None = None,
        effective_provider: str | None = None,
    ) -> None:
        """Rewrite the connection file in the per-run dir with per-run facts.

        Two things are per-run rather than per-build:

        * ``job_id`` — parity with claude's ``.mcp.json`` injection. Without it a run has
          no job scope and its Toolbox calls are unattributable. ``None`` is legal and
          means "no job scope": ``agento run`` identifies its run by a STRING id, and
          skipping the whole call for those runs is what used to lose the override below.
        * the model/provider expectations. ``prepare_workspace`` writes them from the
          agent_view config at BUILD time, but both the consumer and ``agento run``
          support a per-run override (``--model``). A legitimate override would otherwise
          be failed by a stale build-time expectation, so the effective values win.

        The effective values are SET, not merely corrected: with the router opt-out now an
        explicit ``allow_model_substitution`` marker, writing an expectation can no longer
        undo it, so a build that had no model configured still gets a live guard when the
        run names one.
        """
        path = Path(artifacts_dir) / BRIDGE_DIR / BRIDGE_CONFIG_FILENAME
        if not path.is_file():
            return
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict):
            return
        url = payload.get("url")
        if job_id is not None and isinstance(url, str) and url:
            sep = "&" if "?" in url else "?"
            payload["url"] = f"{url}{sep}job_id={job_id}"
        if effective_model and effective_model.strip():
            payload["expected_model"] = effective_model.strip()
        if effective_provider and effective_provider.strip():
            payload["expected_provider"] = effective_provider.strip()
        path.write_text(json.dumps(payload, indent=2) + "\n")

    # -- ownership and persistence -------------------------------------------

    def owned_paths(self) -> tuple[set[str], set[str]]:
        return (
            {
                f"{BRIDGE_DIR}/{BRIDGE_FILENAME}",
                f"{BRIDGE_DIR}/{BRIDGE_CONFIG_FILENAME}",
            },
            {BRIDGE_DIR},
        )

    def persistent_home_paths(self) -> list[str]:
        """REQUIRED for resume.

        ``prepare_artifacts_dir`` rmtree's the run directory on every attempt, and Pi
        buckets sessions by a slug of its cwd. Since the run dir is stable per job id, a
        resumed job lands in the same bucket — but only if the session store itself
        survived the rmtree.
        """
        return [".pi/agent/sessions"]

    # -- credentials ----------------------------------------------------------

    def credential_env(self, credential: CredentialRecord) -> dict[str, str]:
        """OpenRouter reads its key from the environment; nothing touches disk.

        The discriminator is ``credential.type`` — the column name on
        ``CredentialRecord``. (An earlier version read ``credential_type``, which does
        not exist, so this returned ``{}`` and the key never reached Pi.)
        """
        if credential.type != CREDENTIAL_TYPE:
            return {}
        payload = credential.credentials or {}
        key = payload.get("api_key") or payload.get("subscription_key")
        return {"OPENROUTER_API_KEY": key} if key else {}

    def write_credentials(self, build_dir: Path, credential: CredentialRecord) -> None:
        """No-op: the key is delivered via env, so it never lands on disk."""

    def remove_credentials(self, target_dir: Path) -> None:
        """Delete only Pi's credential state, keeping settings/models config.

        Pi writes ``auth.json`` as ``{}`` (0600) on every start; a run dir copied from a
        build may already carry one.
        """
        pi_home = Path(target_dir) / ".pi" / "agent"
        for name in ("auth.json", "auth.json.lock"):
            candidate = pi_home / name
            if candidate.exists():
                candidate.unlink()

    def capture_refreshed_credentials(
        self, home: Path, credential: CredentialRecord, conn
    ) -> bool:
        """An OpenRouter API key does not rotate, so there is nothing to capture.

        The three-argument shape is the protocol's, and the consumer calls it that way
        after every credentialed run — a one-argument version raised ``TypeError`` on
        every successful Pi job.
        """
        return False
