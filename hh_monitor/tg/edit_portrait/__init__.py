"""FSM "Edit Portrait" wizard package.

Exposes :data:`edit_portrait_router` for inclusion under the admin router.
"""

from hh_monitor.tg.edit_portrait.handlers import edit_portrait_router

__all__ = ["edit_portrait_router"]
