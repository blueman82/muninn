"""Native command construction without shell interpolation of paths."""

from __future__ import annotations

from pathlib import Path

BOOTSTRAP = (
    "import os,stat,sys; from pathlib import Path; "
    "root=Path(sys.argv.pop(1)).resolve(strict=True); "
    "data=Path(sys.argv.pop(1)); "
    "assert all(stat.S_ISDIR(p.lstat().st_mode) and "
    "p.lstat().st_uid==os.getuid() and p.lstat().st_mode&0o777==0o700 "
    "for p in (root,data)); "
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
