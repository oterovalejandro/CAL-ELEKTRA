"""Debugging of the remote control of the R&S ZNB/ZNx analyzers, without GUI.

``ZnxDebugger`` sets and reads back each setting with the SCPI commands of the
RsZnx driver and checks the error queue; ``SimulatedZnb`` answers those
commands without an instrument; ``console`` is the text console
(``python -m calelektra.instruments.znx_debug``).
"""

from calelektra.instruments.znx_debug.debugger import ScpiError, ScpiExchange, ZnxDebugger
from calelektra.instruments.znx_debug.settings import SETTINGS
from calelektra.instruments.znx_debug.simulator import SimulatedZnb

__all__ = ["SETTINGS", "ScpiError", "ScpiExchange", "SimulatedZnb", "ZnxDebugger"]
