"""Keep the simulator's own statement as opaque bytes, without verifying its claims."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import tempfile

from .common import eprint

STATEMENT_PATH = ".rig/simulation/statement"


def pending(source: str, recipe: Path) -> dict:
    """Freeze the input location, relative to the recipe, without reading the simulator's file.

    A simulator may create or replace the file only when it finishes. Resolve the directory
    spelling now, but keep any source symlink for the read at seal time.
    """
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = Path(recipe).absolute().parent / path
    return dict(source=os.path.abspath(path), reported_by="simulator", verified=False)


def capture(run_dir: Path, simulation: dict) -> None:
    """Copy and hash exactly the bytes read at sealing; a failed copy records a warning.

    No parsing or simulator-specific interpretation takes place. The digest identifies the
    copied bytes; it does not verify the simulator's account of the run.
    """
    statement = simulation.get("statement")
    if not isinstance(statement, dict) or not isinstance(statement.get("source"), str):
        return
    record = dict(source=statement["source"], reported_by="simulator", verified=False)
    temporary = None
    try:
        # A FIFO named by mistake must not wait forever for a writer during teardown.
        fd = os.open(record["source"], os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise OSError("statement source is not a regular file")
            destination = Path(run_dir) / STATEMENT_PATH
            destination.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".statement-",
                                             delete=False) as output:
                temporary = Path(output.name)
                while block := source.read(1024 * 1024):
                    output.write(block)
                    digest.update(block)
            temporary.replace(destination)
            temporary = None
        record.update(path=STATEMENT_PATH, sha256=digest.hexdigest())
    except OSError as exc:
        # The source has its own provenance field. Keep an equal failure at another source
        # path comparable as the same outcome, rather than embedding that path twice.
        record["warning"] = f"could not capture simulator statement: {exc.strerror or str(exc)}"
        eprint(f"rig: warning: {record['warning']} ({record['source']})")
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
    simulation["statement"] = record
