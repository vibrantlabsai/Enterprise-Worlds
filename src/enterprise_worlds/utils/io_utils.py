"""Tiny IO helpers. JSON only for the thin slice."""

import json
from pathlib import Path
from typing import Any, Union

PathLike = Union[str, Path]


def load_file(path: PathLike) -> Any:
    """Load a JSON file."""
    with open(path, "r", encoding="utf-8") as fp:
        return json.load(fp)


def dump_file(path: PathLike, data: Any, indent: int = 2, **kwargs: Any) -> None:
    """Dump data to a JSON file, creating parent directories as needed.

    The mkdir guard avoids a class of late-run crash: e.g. `eworlds run --save-to <dir>/results.json`
    writes BEFORE `--log-dir`'s `save_run_dir` (which creates `<dir>`), so without this the whole
    run's output is lost to a FileNotFoundError after all trials already executed.
    """
    parent = Path(path).parent
    if str(parent):
        parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump(data, fp, indent=indent, default=str, **kwargs)
