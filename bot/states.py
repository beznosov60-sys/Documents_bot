from aiogram.fsm.state import State, StatesGroup


class ContractStates(StatesGroup):
    waiting_for_passport = State()
    waiting_for_manual_data = State()
    waiting_for_amount = State()
    waiting_for_first_payment = State()
    confirmation = State()
