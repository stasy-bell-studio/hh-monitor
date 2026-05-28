"""FSM states for the "Add Vacancy" wizard."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AddVacancy(StatesGroup):
    """Six-step wizard: name → input mode → raw → review → critic → launch."""

    S1_name = State()
    S2_input_mode = State()
    S3_portrait_raw = State()
    S4_review = State()
    S5_critic_prompt = State()
    S6_launch = State()
