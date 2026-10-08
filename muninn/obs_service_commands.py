"""Managed service commands without shell interpolation of paths."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

BOOTSTRAP = (
    "import os,stat,sys; from pathlib import Path; "
    "root=Path(sys.argv.pop(1)).resolve(strict=True); "
    "data=Path(sys.argv.pop(1)); "
    "assert all(stat.S_ISDIR(p.lstat().st_mode) and "
    "p.lstat().st_uid==os.getuid() and p.lstat().st_mode&0o777==0o700 "
    "for p in (root,data)); "
    "os.environ['MUNINN_INSTALL_SHA']=root.name; "
    "sys.path.insert(0,str(root)); "
    "os.environ['MUNINN_HOME']=str(data); "
    "from muninn.cli import main; raise SystemExit(main(sys.argv[1:]))"
)


def arguments(release: Path, data: Path, *args: str) -> list[str]:
    """Build an isolated pinned entrypoint with no batch wrapper process."""
    return [
        "-I",
        "-B",
        "-X",
        "utf8",
        "-c",
        BOOTSTRAP,
        str(release),
        str(data),
        *args,
    ]


def unit_quote(value: str) -> str:
    """Quote unit values without specifier or environment expansion."""
    return json.dumps(value.replace("%", "%%"), ensure_ascii=False)


def render_unit(
    python: Path, release: Path, data: Path, env: Mapping[str, str]
) -> str:
    """Render the finite owned unit with transaction-safe stop settings."""
    command = " ".join(
        unit_quote(value)
        for value in [
            str(python),
            *arguments(release, data, "serve", "--interval", "60"),
        ]
    )
    environment = "\n".join(
        f"Environment={unit_quote(f'{key}={value}')}"
        for key, value in env.items()
    )
    return (
        "[Unit]\nDescription=Muninn private poller\n[Service]\nType=simple\n"
        f"ExecStart=:{command}\n{environment}\nUMask=0077\n"
        "UnsetEnvironment=MUNINN_ROOTS MUNINN_CURSOR_DB\n"
        "Restart=on-failure\nRestartSec=5\nTimeoutStopSec=infinity\n"
        "SendSIGKILL=no\nStandardOutput=null\nStandardError=null\n"
        "[Install]\nWantedBy=default.target\n"
    )
