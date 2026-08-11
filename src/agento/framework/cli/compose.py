"""agento up / down / logs — Docker Compose wrappers."""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

from ._output import log_error, log_info, log_warn
from ._project import compose_file_flags, find_project_root


def _get_compose_cmd() -> list[str]:
    """Get the base docker compose command with the project's compose file(s)."""
    project_root = find_project_root()
    if project_root is None:
        log_error("Not inside an agento project. Run 'agento install' first.")
        sys.exit(1)

    flags = compose_file_flags(project_root)
    if not flags:
        log_error("docker-compose.yml not found. Run 'agento install' first.")
        sys.exit(1)

    return ["docker", "compose", *flags]


class UpCommand:
    @property
    def name(self) -> str:
        return "up"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "Start the agento runtime (Docker Compose)"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--force-recreate",
            action="store_true",
            dest="force_recreate",
            help="Recreate containers even if their configuration/image is unchanged",
        )

    def execute(self, args: argparse.Namespace) -> None:
        compose = _get_compose_cmd()

        cmd = [*compose, "up", "-d"]
        if getattr(args, "force_recreate", False):
            cmd.append("--force-recreate")

        log_info("Starting containers...")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            log_error("Failed to start containers.")
            sys.exit(result.returncode)

        # Wait for MySQL health
        log_info("Waiting for MySQL...")
        for _ in range(30):
            check = subprocess.run(
                [*compose, "exec", "-T", "mysql", "mysqladmin", "ping", "-h", "localhost", "--silent"],
                capture_output=True,
            )
            if check.returncode == 0:
                break
            time.sleep(2)
        else:
            log_warn("MySQL may not be ready yet. Continuing anyway...")

        log_info("Containers started.")
        print()
        print("Next steps:")
        print("  agento setup:upgrade          Apply migrations and install crontab")
        print("  agento credential:register <scope> <label>   Register an agent credential")
        print("  agento logs                    View container logs")
        print()


class DownCommand:
    @property
    def name(self) -> str:
        return "down"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "Stop the agento runtime"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        pass

    def execute(self, args: argparse.Namespace) -> None:
        compose = _get_compose_cmd()

        log_info("Stopping containers...")
        result = subprocess.run([*compose, "down"])
        if result.returncode != 0:
            log_error("Failed to stop containers.")
            sys.exit(result.returncode)

        log_info("Containers stopped.")


class LogsCommand:
    @property
    def name(self) -> str:
        return "logs"

    @property
    def shortcut(self) -> str:
        return ""

    @property
    def help(self) -> str:
        return "Show container logs"

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("service", nargs="?", default=None, help="Service name (cron, toolbox, mysql)")

    def execute(self, args: argparse.Namespace) -> None:
        compose = _get_compose_cmd()

        cmd = [*compose, "logs", "-f"]
        if hasattr(args, "service") and args.service:
            cmd.append(args.service)

        import contextlib

        with contextlib.suppress(KeyboardInterrupt):
            subprocess.run(cmd)
