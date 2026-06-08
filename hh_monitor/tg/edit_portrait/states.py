"""FSM states for the "Edit Portrait" wizard.

Two states drive a menu→edit→menu loop:
  menu           — the section / field menu is shown.
  awaiting_value — a field was picked and the bot waits for a typed value.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class EditPortrait(StatesGroup):
    menu = State()
    awaiting_value = State()
