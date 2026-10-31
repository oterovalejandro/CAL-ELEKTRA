"""Reading and writing S-parameter data by the extension of the file.

``.sdatb`` and ``.sdatcv`` (METAS VNA Tools) keep the uncertainties; Touchstone (``.sNp``, ``.ts``) only
the values.
"""

from __future__ import annotations

import re
from pathlib import Path

from calelektra.core.touchstone import TouchstoneError, read_touchstone, write_touchstone
from calelektra.sdata.model import SParamData
from calelektra.sdata.sdatb import read_sdatb, write_sdatb
from calelektra.sdata.sdatcv import read_sdatcv, write_sdatcv
from calelektra.sdata.unclib import SDataError

FORMATS = ".sdatb, .sdatcv o Touchstone (.sNp, .ts)"


def _touchstone(path: Path) -> bool:
    return bool(re.fullmatch(r"\.s\d+p|\.ts", path.suffix.lower()))


def load_sparam_data(path: str | Path) -> SParamData:
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        if suffix == ".sdatb":
            return read_sdatb(path)
        if suffix == ".sdatcv":
            return read_sdatcv(path)
        if _touchstone(path):
            return SParamData.from_touchstone(read_touchstone(path))
    except OSError as exc:
        raise SDataError(f"No se puede leer {path.name}: {exc}") from exc
    except TouchstoneError as exc:
        raise SDataError(str(exc)) from exc
    raise SDataError(f"{path.name}: formato no admitido (se leen {FORMATS})")


def save_sparam_data(path: str | Path, data: SParamData, comments: list[str] | None = None) -> Path:
    """Write ``data`` in the format of the extension (Touchstone: values in RI, Hz)."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".sdatb":
        return write_sdatb(path, data)
    if suffix == ".sdatcv":
        return write_sdatcv(path, data)
    if re.fullmatch(r"\.s\d+p", suffix):
        if int(suffix[2:-1]) != data.n_ports:
            raise SDataError(f"{path.name}: el fichero es de {suffix[2:-1]} puertos y los datos de {data.n_ports}")
        write_touchstone(path, data.to_touchstone(comments))
        return path
    raise SDataError(f"{path.name}: formato no admitido (se escriben .sdatb, .sdatcv o Touchstone .sNp)")
