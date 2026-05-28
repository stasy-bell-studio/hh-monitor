"""FSM "Add Vacancy" wizard package (Session 12).

Exposes :data:`add_vacancy_router` for inclusion under the admin router.
"""

from hh_monitor.tg.add_vacancy.handlers import add_vacancy_router

__all__ = ["add_vacancy_router"]
