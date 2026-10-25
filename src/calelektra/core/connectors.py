"""RF connectors: family + gender, mating rules and mapping to VNA connector names.

The connector of each port of an equipment comes from one of its fields (see
the category ``puertos``). Setups use these rules to check every connection,
and the measurement procedures use them to decide the calibration (e.g. TOSM
with a flush through when both reference planes can be mated directly).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from calelektra.core.issues import Severity

# canonical key → display name
FAMILIES: dict[str, str] = {
    "N": "N 50 Ω",
    "N75": "N 75 Ω",
    "3.5 mm": "3,5 mm",
    "2.92 mm": "2,92 mm (K)",
    "NMD 2.92 mm": "NMD 2,92 mm",
    "2.4 mm": "2,4 mm",
    "1.85 mm": "1,85 mm (V)",
    "1.0 mm": "1,0 mm",
    "SMA": "SMA",
    "7/16": "7/16",
    "4.3-10": "4.3-10",
    "BNC": "BNC 50 Ω",
    "BNC75": "BNC 75 Ω",
    "TNC": "TNC",
    "PC-7": "PC-7 (APC-7, sin género)",
    "terminales": "Terminales / bornas (no coaxial)",
}
GENDERS: dict[str, str] = {"macho": "macho", "hembra": "hembra", "sin_genero": "sin género"}

# Families that mate mechanically although they are different (with care).
MECHANICAL_GROUPS = [frozenset({"3.5 mm", "2.92 mm", "SMA"}), frozenset({"2.4 mm", "1.85 mm"})]
# Ruggedized (NMD) test-port connectors and the standard interface inside them: the NMD male of a VNA
# port takes NMD females (test cables) and the standard females of its interface; an NMD female only
# fits an NMD male.
RUGGEDIZED = {"NMD 2.92 mm": "2.92 mm"}
SEXLESS = frozenset({"PC-7"})
NOT_COAXIAL = frozenset({"terminales"})

# Calibration connector names of R&S ZNx analyzers (RsZnx driver, SENS:CORR:COLL:CONN<port>)
ZNX_CONNECTOR = {
    ("N", "hembra"): "N50FEMALE",
    ("N", "macho"): "N50MALE",
    ("N75", "hembra"): "N75FEMALE",
    ("N75", "macho"): "N75MALE",
    ("3.5 mm", "hembra"): "PC35FEMALE",
    ("3.5 mm", "macho"): "PC35MALE",
    ("2.92 mm", "hembra"): "PC292FEMALE",
    ("2.92 mm", "macho"): "PC292MALE",
    ("NMD 2.92 mm", "hembra"): "PC292FEMALE",
    ("NMD 2.92 mm", "macho"): "PC292MALE",
    ("2.4 mm", "hembra"): "PC24FEMALE",
    ("2.4 mm", "macho"): "PC24MALE",
    ("1.85 mm", "hembra"): "PC185FEMALE",
    ("1.85 mm", "macho"): "PC185MALE",
    ("SMA", "hembra"): "SMAFEMALE",
    ("SMA", "macho"): "SMAMALE",
    ("7/16", "hembra"): "S7_16FEMALE",
    ("7/16", "macho"): "S7_16MALE",
    ("BNC", "hembra"): "BNC50FEMALE",
    ("BNC", "macho"): "BNC50MALE",
    ("BNC75", "hembra"): "BNC75FEMALE",
    ("BNC75", "macho"): "BNC75MALE",
    ("PC-7", "sin_genero"): "PC7",
}
# Cal-kit connector selector of R&S ZNx analyzers (SENS:CORR:CKIT:<conn>:SEL)
ZNX_KIT_CONNECTOR = {"N": "N50", "N75": "N75", "PC-7": "PC7", "3.5 mm": "PC35", "2.92 mm": "PC292", "NMD 2.92 mm": "PC292", "SMA": "SMA"}

_ALIASES = {
    "n": "N",
    "n50": "N",
    "n 50": "N",
    "n 50 ω": "N",
    "n 50 ohm": "N",
    "n75": "N75",
    "n 75 ω": "N75",
    "3.5": "3.5 mm",
    "3,5 mm": "3.5 mm",
    "pc35": "3.5 mm",
    "pc 3.5": "3.5 mm",
    "2.92": "2.92 mm",
    "2,92 mm": "2.92 mm",
    "k": "2.92 mm",
    "pc292": "2.92 mm",
    "nmd 2.92": "NMD 2.92 mm",
    "nmd 2,92": "NMD 2.92 mm",
    "nmd 2,92 mm": "NMD 2.92 mm",
    "nmd2.92mm": "NMD 2.92 mm",
    "2.4": "2.4 mm",
    "2,4 mm": "2.4 mm",
    "1.85": "1.85 mm",
    "1,85 mm": "1.85 mm",
    "v": "1.85 mm",
    "sma": "SMA",
    "pc7": "PC-7",
    "apc-7": "PC-7",
    "bnc": "BNC",
    "tnc": "TNC",
}
_GENDER_ALIASES = {
    "m": "macho",
    "macho": "macho",
    "male": "macho",
    "h": "hembra",
    "f": "hembra",
    "hembra": "hembra",
    "female": "hembra",
    "sin_genero": "sin_genero",
    "sin género": "sin_genero",
    "sin genero": "sin_genero",
}


class ConnectorError(ValueError):
    pass


def interface_family(family: str) -> str:
    """The standard interface of a family (that of an NMD connector; the family itself otherwise)."""
    return RUGGEDIZED.get(family, family)


def family_key(text: str) -> str:
    """Canonical family key from a key, display name or common alias."""
    if text in FAMILIES:
        return text
    lowered = text.strip().lower()
    for key, name in FAMILIES.items():
        if lowered in (key.lower(), name.lower()):
            return key
    if lowered in _ALIASES:
        return _ALIASES[lowered]
    raise ConnectorError(f"Familia de conector desconocida: {text!r}")


@dataclass(frozen=True)
class Connector:
    family: str
    gender: str

    def __post_init__(self) -> None:
        if self.family not in FAMILIES:
            raise ConnectorError(f"Familia de conector desconocida: {self.family!r}")
        if self.gender not in GENDERS:
            raise ConnectorError(f"Género de conector desconocido: {self.gender!r}")

    @classmethod
    def parse(cls, value: Any) -> "Connector":
        """From ``{familia, genero}`` or text such as ``"N hembra"``, ``"2.92 mm (m)"``, ``"N(f)"``."""
        if isinstance(value, Connector):
            return value
        if isinstance(value, dict):
            family = value.get("familia", value.get("family"))
            gender = value.get("genero", value.get("gender"))
            if family is None:
                raise ConnectorError(f"Conector sin familia: {value!r}")
            key = family_key(str(family))
            if gender is None:
                gender = "sin_genero" if key in SEXLESS or key in NOT_COAXIAL else None
            if gender is None:
                raise ConnectorError(f"Conector sin género: {value!r}")
            return cls(key, _GENDER_ALIASES.get(str(gender).strip().lower(), str(gender)))
        if isinstance(value, str):
            text = value.strip()
            match = re.match(r"^(?P<family>.+?)\s*(?:\((?P<g1>[^)]+)\)|\s+(?P<g2>macho|hembra|m|h|f|male|female))$", text, re.I)
            if match:
                gender = (match.group("g1") or match.group("g2")).strip().lower()
                if gender not in _GENDER_ALIASES:
                    raise ConnectorError(f"Género de conector desconocido en {value!r}")
                return cls(family_key(match.group("family")), _GENDER_ALIASES[gender])
            key = family_key(text)
            if key in SEXLESS or key in NOT_COAXIAL:
                return cls(key, "sin_genero")
            raise ConnectorError(f"Conector sin género: {value!r}")
        raise ConnectorError(f"Conector no válido: {value!r}")

    @property
    def coaxial(self) -> bool:
        return self.family not in NOT_COAXIAL

    def label(self) -> str:
        if self.gender == "sin_genero":
            return FAMILIES[self.family]
        return f"{FAMILIES[self.family]} {GENDERS[self.gender]}"

    def to_yaml(self) -> dict[str, str]:
        return {"familia": self.family, "genero": self.gender}

    def mating_gender(self) -> str:
        return {"macho": "hembra", "hembra": "macho"}.get(self.gender, "sin_genero")

    def mates_directly_with(self, other: "Connector") -> bool:
        return mating_issue(self, other) is None

    def znx_name(self) -> str | None:
        return ZNX_CONNECTOR.get((self.family, self.gender))

    def __str__(self) -> str:
        return self.label()


def mating_issue(a: Connector, b: Connector) -> tuple[Severity, str] | None:
    """``None`` if both connectors mate correctly; otherwise severity and reason."""
    if not a.coaxial or not b.coaxial:
        return None if a.family == b.family else (Severity.INFO, "conexión no coaxial: no se comprueba")
    if (a.family in RUGGEDIZED) != (b.family in RUGGEDIZED):  # an NMD connector with a standard one
        nmd, other = (a, b) if a.family in RUGGEDIZED else (b, a)
        if nmd.gender == "hembra":
            return Severity.ERROR, f"{nmd.label()} solo se conecta a un {FAMILIES[nmd.family]} macho (puerto de prueba del VNA)"
        return mating_issue(Connector(RUGGEDIZED[nmd.family], nmd.gender), other)
    if a.family != b.family:
        if any(a.family in group and b.family in group for group in MECHANICAL_GROUPS):
            same_gender = a.gender == b.gender
            if same_gender:
                return Severity.ERROR, f"mismo género ({GENDERS[a.gender]}): hace falta un adaptador"
            return Severity.WARNING, f"{FAMILIES[a.family]} y {FAMILIES[b.family]} son compatibles mecánicamente; conecte con precaución"
        return Severity.ERROR, f"familias incompatibles: {FAMILIES[a.family]} con {FAMILIES[b.family]}"
    if a.family in SEXLESS:
        return None
    if a.gender == b.gender:
        return Severity.ERROR, f"mismo género ({GENDERS[a.gender]}): hace falta un adaptador"
    return None
