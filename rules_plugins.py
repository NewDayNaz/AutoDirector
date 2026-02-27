from __future__ import annotations

"""
Rule plugin hooks for DirectorCore.

This module defines a simple plugin interface: callables that can inspect and
optionally adjust the candidate selection before a cut is made. Plugins are
discovered from config.director.rules_plugins (list of dotted paths to callables).
"""

from importlib import import_module
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

CandidateContext = Dict[str, Any]
CandidatePlugin = Callable[
    [
        str,  # phase
        Sequence[int],  # eligible input ids
        CandidateContext,
    ],
    Tuple[Optional[int], Sequence[int]],
]


def _load_callable(path: str) -> Optional[CandidatePlugin]:
    """Import a callable from 'module:attr' or 'module.attr'."""
    module_name: str
    attr_name: str
    if ":" in path:
        module_name, attr_name = path.split(":", 1)
    else:
        parts = path.rsplit(".", 1)
        if len(parts) != 2:
            return None
        module_name, attr_name = parts
    try:
        mod = import_module(module_name)
        fn = getattr(mod, attr_name, None)
    except Exception:
        return None
    if not callable(fn):
        return None
    return fn  # type: ignore[return-value]


def load_candidate_plugins(paths: Iterable[str]) -> List[CandidatePlugin]:
    """Load candidate plugins from a list of dotted-path strings."""
    plugins: List[CandidatePlugin] = []
    for p in paths:
        fn = _load_callable(str(p))
        if fn:
            plugins.append(fn)
    return plugins

