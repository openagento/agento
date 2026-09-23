"""CLI command: workspace:ssh-purge — clear SSH private keys already on disk.

**Deprecated: scheduled for removal in v0.17 or later.** It exists to clean up
deployments upgrading ACROSS the release that stopped writing the key to disk. Once
every deployment has upgraded and purged, nothing writes a key file any more and the
sweep has nothing to find — see ROADMAP.md "Deprecation removals due in v0.17".

One-time admin cleanup for the defect this release closes: `workspace:build` used to
write ``agent_view/identity/ssh_private_key`` into ``<build_dir>/.ssh/id_rsa``, build
retention kept every generation, and artifact retention kept every copy a run made. Those
keys stay readable until something deletes them, and a build only cleans its OWN view's
tree — so an admin needs a repository-wide sweep.
"""
from __future__ import annotations

import argparse


class SshPurgeCommand:
    @property
    def name(self) -> str:
        return "workspace:ssh-purge"

    @property
    def shortcut(self) -> str:
        return "wo:sp"

    @property
    def help(self) -> str:
        return ("Delete SSH private keys left on disk under workspace/ (one-time cleanup; "
                "deprecated, removal in v0.17+)")

    def configure(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--dry-run", action="store_true",
            help="List what would be deleted and exit without deleting anything",
        )

    def execute(self, args: argparse.Namespace) -> None:
        from pathlib import Path

        from agento.framework.cli import terminal
        from agento.framework.ssh_identity import find_private_keys
        from agento.framework.workspace_paths import BASE_WORKSPACE_DIR

        # Deprecation stated where the operator actually is: this command only serves
        # deployments upgrading across the release that stopped writing the key file.
        print(
            "NOTE: workspace:ssh-purge is DEPRECATED and scheduled for removal in v0.17 or "
            "later. It is only for deployments upgrading from a version that wrote "
            "ssh_private_key to disk; on this release no run writes a key file."
        )

        root = Path(BASE_WORKSPACE_DIR)
        if not root.is_dir():
            print(f"No workspace directory at {root} — nothing to purge")
            return

        print(
            "Scanning for SSH private keys under "
            f"{root} (build generations, artifacts run dirs, state dirs)."
        )
        # Scope, stated rather than implied: this finds keys stored the way this system
        # stored them — a complete PEM envelope at the start of a file, or a file named
        # `id_rsa`. A key embedded mid-file, re-encoded, compressed or split is NOT found.
        # It is a cleanup tool for keys this system itself wrote, not an exfiltration
        # detector.
        print(
            "Scope: a complete PEM private-key envelope at the start of a file, or a file "
            "named 'id_rsa'. A key that is embedded mid-file, re-encoded, compressed or "
            "split is NOT detected — this clears what this system wrote, it is not an "
            "exfiltration detector."
        )
        # Ordering, stated so the operator does not have to derive it: run this AFTER this
        # release is deployed. On the OLD code a `workspace:build` re-materialized the key,
        # so a purge before the deploy is undone by the next build; on this code no run
        # writes a key file, so the running consumer cannot re-create one behind the sweep.
        # Purging an old deployment therefore means stopping cron first, and then this
        # command needs the one-off-container form (see docs/cli/workspace-build.md) because
        # the host CLI reaches it by exec'ing into the RUNNING cron container.
        print(
            "Order: deploy this release first, then purge. On this code no run writes a key "
            "file, so the consumer may keep running. To purge a deployment still on the old "
            "code, stop cron first (see docs/cli/workspace-build.md). Safe to re-run."
        )

        scan = find_private_keys(root)
        hits = list(scan.keys)
        if scan.unreadable:
            # Fail closed: "no keys here" is not something a scan that could not read
            # everything is entitled to say.
            print("Could NOT scan (a key may still be present):")
            for problem in scan.unreadable:
                print(f"  {problem}")
        if not hits:
            if scan.unreadable:
                # NOT "no keys found" — the scan is not entitled to that sentence.
                raise SystemExit(
                    "Scan incomplete — no key found in what could be read, but the paths "
                    "above were not scanned. Fix the permissions and re-run."
                )
            print("No private keys found.")
            return

        for path in hits:
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            print(f"  {path} ({size} bytes)")
        print(f"{len(hits)} private key(s) found.")

        if args.dry_run:
            print("--dry-run: nothing deleted.")
            if scan.unreadable:
                raise SystemExit(
                    f"{len(scan.unreadable)} path(s) could not be scanned — the list above "
                    "is incomplete. Fix the permissions and re-run."
                )
            return

        # No `--yes`: the scan reaches workspace/artifacts, where agents clone
        # repositories, so a human sees the full list before anything is unlinked.
        choice = terminal.select(
            f"Delete these {len(hits)} file(s) permanently?",
            ["No, cancel", "Yes, delete them"],
        )
        if choice != 1:
            print("Cancelled — nothing deleted.")
            if scan.unreadable:
                # The list the human decided from was incomplete.
                raise SystemExit(
                    f"Cancelled with {len(scan.unreadable)} unscannable path(s) — keys may "
                    "still be on disk. Fix the permissions and re-run."
                )
            return

        deleted = 0
        failed: list[str] = []
        for path in hits:
            try:
                path.unlink()
                deleted += 1
            except FileNotFoundError:
                continue
            except OSError as exc:
                failed.append(f"{path}: {exc.strerror or exc}")
                print(f"  Could not delete {path}: {exc}")
        print(f"Deleted {deleted} of {len(hits)} private key(s).")
        print(
            "Now ROTATE every affected agent_view/identity/ssh_private_key: deleting a "
            "copy cannot un-leak a key that was readable by every agent_view."
        )
        if failed or scan.unreadable:
            # A non-zero exit is the only report an admin script can act on.
            raise SystemExit(
                f"{len(failed) + len(scan.unreadable)} path(s) unresolved — keys may "
                "still be on disk. Fix the permissions and re-run."
            )
