"""One place to take a ``[ns/]name[@version]`` package ref apart.

The codebase grew ~10 hand-rolled ``rpartition("/")[-1].split("@")[0]`` splits (and one verb —
pkg info — that forgot the ``@`` entirely). Zero-dependency on purpose: importable from every
module without cycles.
"""
from __future__ import annotations


def parse_ref(ref: str) -> tuple[str | None, str, str | None]:
    """``[ns/]name[@version]`` -> (ns | None, name, version | None)."""
    body, _, version = str(ref).partition("@")
    ns, _, name = body.rpartition("/")
    return (ns or None), name, (version or None)


def unqualified(ref: str) -> str:
    """``ns/name@ver`` -> the bare package name."""
    return parse_ref(ref)[1]
