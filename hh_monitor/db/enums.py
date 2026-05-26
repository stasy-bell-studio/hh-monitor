from enum import Enum


class ScreeningStatus(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"
    DOUBT = "doubt"
    STOP_LIST = "stop_list"
