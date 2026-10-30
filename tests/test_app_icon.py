"""Icon of the application: drawn per size, navy outside the application and light inside it."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import struct

import pytest
from PySide6.QtWidgets import QApplication, QLabel

from calelektra.gui.app import configure_application
from calelektra.gui.app_icon import (
    ICO_SIZES,
    RESOURCES,
    SIZES,
    app_icon,
    app_image,
    level_of,
    line_width,
    write_ico,
)
from calelektra.gui.main_window import RAIL_ICON_SIZE, MainWindow


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def test_a_drawing_for_each_size(qapp):
    icon = app_icon("marino")
    assert {size.width() for size in icon.availableSizes()} == set(SIZES)
    assert (level_of(16), level_of(32), level_of(256)) == (0, 1, 1)  # below 21 px, only the circle, the axis and the marker
    assert line_width(256) == 2.0 and line_width(16) > line_width(32) > 2.0  # as the setups; thicker when small
    navy, light = app_image(64, "marino"), app_image(64, "claro")
    assert navy.pixelColor(32, 6).lightness() < 80 and light.pixelColor(32, 6).lightness() > 200  # the tile
    assert navy.pixelColor(0, 0).alpha() == 0  # transparent around it


def test_windows_icon_file(qapp, tmp_path):
    data = write_ico(tmp_path / "icono.ico").read_bytes()
    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, len(ICO_SIZES))
    for index, size in enumerate(ICO_SIZES):
        width, height, _c, _r, _planes, bpp, length, offset = struct.unpack("<BBBBHHII", data[6 + 16 * index : 22 + 16 * index])
        assert (width or 256, height or 256, bpp) == (size, size, 32)
        assert data[offset : offset + 8] == b"\x89PNG\r\n\x1a\n" and offset + length <= len(data)
    assert (RESOURCES / "cal-elektra.ico").is_file() and (RESOURCES / "cal-elektra-256.png").is_file()  # tools/icono_app.py


def test_icon_of_the_application_and_inside_it(qapp, library):
    configure_application(qapp)
    assert qapp.applicationName() == "CAL-ELEKTRA" and not qapp.windowIcon().isNull()
    window = MainWindow(library)
    mark = window.findChild(QLabel, "railIcon")
    pixmap = mark.pixmap()
    assert not pixmap.isNull() and round(pixmap.width() / pixmap.devicePixelRatio()) == RAIL_ICON_SIZE
    assert pixmap.toImage().pixelColor(pixmap.width() // 2, 8).lightness() > 200  # light, as the setups
    about = window._about()
    assert "CAL-ELEKTRA" in about.text() and not about.iconPixmap().isNull()
    about.close()
    window.close()
