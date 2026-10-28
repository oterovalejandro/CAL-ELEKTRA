"""Frequency inputs: typing the letter of a unit after a number writes the unit and moves on.

In a frequency field, k, M, G or T typed right after a number write « kHz»,
« MHz», « GHz» or « THz» (H: « Hz»; the lower case letters too, m for MHz)
and the focus goes to the next field, or to the next cell in a table. In a list
of frequencies («10 MHz; 1 GHz»), the next item starts instead. If the rest
of the unit («Hz») is typed right after, out of habit, it is ignored.

``install_frequency_completion`` turns it on in a line edit;
``FrequencyDelegate`` in the frequency columns of a table.
"""

from __future__ import annotations

import re
import time

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication, QLineEdit, QStyledItemDelegate, QWidget

UNITS = {"k": "kHz", "K": "kHz", "m": "MHz", "M": "MHz", "g": "GHz", "G": "GHz", "t": "THz", "T": "THz", "h": "Hz", "H": "Hz"}
NUMBER = re.compile(r"\s*([-+]?(?:\d+(?:[.,]\d+)?|[.,]\d+))\s?")
ECHO_SECONDS = 1.5  # time to ignore the rest of the unit typed after the jump


def completed(text: str, letter: str, separator: str | None = None) -> str | None:
    """``text`` with the unit of ``letter`` after its last number; None if the (last item of the) text is not a number."""
    unit = UNITS.get(letter)
    if unit is None:
        return None
    last = text.rpartition(separator)[2] if separator else text
    match = NUMBER.fullmatch(last)
    if match is None:
        return None
    start = len(text) - len(last)
    leading = last[: len(last) - len(last.lstrip())]
    return f"{text[:start]}{leading}{match.group(1)} {unit}"


class _UnitEcho(QObject):
    """Application filter: the rest of a unit typed right after its completion («Hz» after «k») is not written."""

    def __init__(self) -> None:
        super().__init__()
        self.pending = ""
        self.until = 0.0

    def arm(self, rest: str) -> None:
        self.pending = rest
        self.until = time.monotonic() + ECHO_SECONDS

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not self.pending or event.type() != QEvent.Type.KeyPress:
            return False
        if time.monotonic() > self.until:
            self.pending = ""
            return False
        text = event.text()
        if not text or not text.isprintable():  # the Tab of the jump, Shift…: not typed characters
            return False
        if text.lower() == self.pending[0].lower():
            self.pending = self.pending[1:]
            return True
        self.pending = ""
        return False


_echo: _UnitEcho | None = None


def _unit_echo() -> _UnitEcho | None:
    global _echo
    app = QApplication.instance()
    if app is None:
        return None
    if _echo is None:
        _echo = _UnitEcho()
        app.installEventFilter(_echo)
    return _echo


class FrequencyCompletion(QObject):
    """Event filter of a line edit: a unit letter after a number completes the unit and moves on."""

    def __init__(self, edit: QLineEdit, advance: bool = True, separator: str | None = None):
        super().__init__(edit)
        self.edit = edit
        self.advance = advance
        self.separator = separator
        edit.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() != QEvent.Type.KeyPress or not isinstance(event, QKeyEvent):
            return False
        modifiers = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier | Qt.KeyboardModifier.MetaModifier
        letter = event.text()
        edit = self.edit
        if event.modifiers() & modifiers or len(letter) != 1 or letter not in UNITS or edit.isReadOnly():
            return False
        if edit.hasSelectedText() or edit.cursorPosition() != len(edit.text()):
            return False
        text = completed(edit.text(), letter, self.separator)
        if text is None:
            return False
        if self.separator:
            text += f"{self.separator} "
        edit.setText(text)
        edit.textEdited.emit(text)
        echo = _unit_echo()
        if echo is not None:
            echo.arm(UNITS[letter][1:] if letter.lower() != "h" else "z")
        if self.advance:
            # as the Tab key: the next field, or in a table the next cell (the delegate commits the value)
            QCoreApplication.postEvent(edit, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Tab, Qt.KeyboardModifier.NoModifier))
        return True


def install_frequency_completion(edit: QLineEdit, advance: bool = True, separator: str | None = None) -> FrequencyCompletion:
    """Turn the unit completion on in ``edit``; ``separator`` for a list of frequencies (no jump: the next item)."""
    return FrequencyCompletion(edit, advance=advance and separator is None, separator=separator)


class FrequencyDelegate(QStyledItemDelegate):
    """Editor of the frequency cells of a table, with the unit completion (the next cell after the unit)."""

    def createEditor(self, parent: QWidget, option, index) -> QWidget:
        editor = super().createEditor(parent, option, index)
        if isinstance(editor, QLineEdit):
            install_frequency_completion(editor)
        return editor
