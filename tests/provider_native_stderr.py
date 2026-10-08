"""Describe bounded synthetic PowerShell stderr using stream counts only."""

from __future__ import annotations

from xml.etree import ElementTree

_NAMESPACE = "{http://schemas.microsoft.com/powershell/2004/04}"


def classify(raw: bytes) -> dict[str, int]:
    """Count only recognized CLIXML stream IDs without exporting bodies.

    Args:
        raw: Captured stderr from the isolated synthetic provider command.

    Returns:
        Numeric size and stream codes; unknown is not progress.
    """
    data = raw.removeprefix(b"\xef\xbb\xbf")
    marked = data.startswith(b"#< CLIXML")
    result = {
        "stderr_bytes": len(raw),
        "stderr_clixml": int(marked),
        "stderr_progress_only": 0,
        "stderr_error_count": 0,
        "stderr_other_count": int(bool(raw)),
    }
    if not raw or not marked or len(raw) > 65536:
        return result
    _, separator, payload = data.partition(b"\n")
    if not separator or b"<!" in payload:
        return result
    try:
        root = ElementTree.fromstring(payload)
    except (ElementTree.ParseError, LookupError, ValueError):
        return result
    if root.tag != _NAMESPACE + "Objs" or not len(root):
        return result
    progress = errors = other = 0
    for record in root:
        stream = record.get("S", "").casefold()
        known = record.tag in {
            _NAMESPACE + "Obj",
            _NAMESPACE + "S",
            _NAMESPACE + "Ref",
        }
        if known and stream == "progress":
            progress += 1
        elif known and stream == "error":
            errors += 1
        else:
            other += 1
    result.update(
        stderr_progress_only=int(progress > 0 and not errors and not other),
        stderr_error_count=errors,
        stderr_other_count=other,
    )
    return result
