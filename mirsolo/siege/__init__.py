"""攻城战役（P2-S1）—— 沙巴克：守军分层布防 + 据点推进。"""

from mirsolo.siege.campaign import (
    EVT_SIEGE_LOST,
    EVT_SIEGE_OBJECTIVE_DOWN,
    EVT_SIEGE_STARTED,
    EVT_SIEGE_WAVE,
    EVT_SIEGE_WON,
    KEY_SIEGE,
    LOST,
    RUNNING,
    WON,
    SiegeError,
    SiegeState,
    SiegeSystem,
    spawn_objective,
    spawn_wave_guards,
    siege_status,
    start_siege,
)

__all__ = [
    "EVT_SIEGE_LOST",
    "EVT_SIEGE_OBJECTIVE_DOWN",
    "EVT_SIEGE_STARTED",
    "EVT_SIEGE_WAVE",
    "EVT_SIEGE_WON",
    "KEY_SIEGE",
    "LOST",
    "RUNNING",
    "WON",
    "SiegeError",
    "SiegeState",
    "SiegeSystem",
    "siege_status",
    "spawn_objective",
    "spawn_wave_guards",
    "start_siege",
]
