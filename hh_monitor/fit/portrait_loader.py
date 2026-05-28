"""YAML-first, DB-fallback portrait loader for Search rows.

Historical layout: portraits live as YAML files in ``config/portraits/`` keyed
by ``position_code``.  Session 12 introduces an in-bot FSM wizard that creates
new searches with the portrait stored only in ``searches.portrait`` jsonb — no
YAML file exists for those rows.

Every per-search portrait lookup must use :func:`load_portrait_for_search` so
both legacy YAML-backed searches and new FSM-backed searches resolve uniformly.
Global registries (startup validation, ``portraits list`` CLI) keep using
:func:`load_all_portraits` directly.
"""

from __future__ import annotations

from hh_monitor.db.models import Search
from hh_monitor.fit.portrait import Portrait, load_all_portraits


def load_portrait_for_search(
    search: Search, *, portraits: dict[str, Portrait] | None = None
) -> Portrait:
    """Resolve the Portrait for a given Search row.

    Priority:
        1. YAML — if ``search.position_code`` matches a file in
           ``config/portraits/``, return that Portrait.  YAML wins even when
           ``search.portrait`` jsonb is populated; this preserves current
           behaviour for all migrated searches and lets ops hot-edit YAMLs.
        2. DB jsonb — if no YAML match and ``search.portrait`` is a non-empty
           dict, return ``Portrait.model_validate(search.portrait)``.
        3. Raise :class:`ValueError` with both ``search_code`` and
           ``position_code`` in the message.

    Args:
        search:    Search row with ``position_code`` and ``portrait`` populated.
        portraits: Optional pre-loaded YAML registry to avoid disk re-reads in
                   batch callers.  If None, the registry is loaded on demand.
    """
    if portraits is None:
        portraits = load_all_portraits()
    yaml_portrait = portraits.get(search.position_code)
    if yaml_portrait is not None:
        return yaml_portrait

    if search.portrait:
        return Portrait.model_validate(search.portrait)

    raise ValueError(
        f"No portrait for search {search.search_code!r} "
        f"(position_code={search.position_code!r}): "
        f"neither YAML nor DB jsonb yielded a Portrait"
    )
