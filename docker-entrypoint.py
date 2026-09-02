"""Prepare the bind-mounted data directory, then run the application unprivileged."""

import os
import sys
from pathlib import Path


DATA_DIR = Path("/data")
APP_UID = 10001
APP_GID = 10001


def chown_data_directory(path: Path, uid: int, gid: int) -> None:
    """Change ownership without following symlinks outside the data volume."""
    path.mkdir(parents=True, exist_ok=True)
    os.lchown(path, uid, gid)
    for root, directories, filenames in os.walk(path, followlinks=False):
        for name in directories + filenames:
            try:
                os.lchown(Path(root, name), uid, gid)
            except FileNotFoundError:
                # A transient SQLite sidecar may disappear while walking /data.
                continue


def drop_privileges() -> None:
    os.initgroups("app", APP_GID)
    os.setgid(APP_GID)
    os.setuid(APP_UID)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("A command is required")

    if os.geteuid() == 0:
        chown_data_directory(DATA_DIR, APP_UID, APP_GID)
        drop_privileges()

    os.execvp(sys.argv[1], sys.argv[1:])


if __name__ == "__main__":
    main()
