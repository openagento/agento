"""Refuse a release wheel that has no built frontend (PRD E8 §11).

The panel and the miniapp kit are built by ``cd frontend && npm ci && npm run build`` and
are gitignored. A wheel without them would install a proxy that serves 404 for the panel.
An editable install (``uv sync`` in this repo) is not checked.
"""
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

# The built files proxy needs: the panel shell, and at least one kit version's stylesheet,
# elements, bridge and font. An empty directory does not count.
REQUIRED = (
    "src/agento/framework/web/panel/index.html",
    "src/agento/framework/web/miniapp-ui/*/agento-ui.css",
    "src/agento/framework/web/miniapp-ui/*/agento-ui.js",
    "src/agento/framework/web/miniapp-ui/*/agento-bridge.js",
    "src/agento/framework/web/miniapp-ui/*/fonts/*.woff2",
)


def missing(root: Path) -> list[str]:
    return [p for p in REQUIRED if not any(f.is_file() and f.stat().st_size for f in root.glob(p))]


class FrontendBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        if version == "editable":
            return
        gone = missing(Path(self.root))
        if gone:
            raise RuntimeError(
                f"frontend is not built ({', '.join(gone)} missing): run `cd frontend && npm ci && npm run build`"
            )
