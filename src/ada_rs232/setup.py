"""Parse the PAC setup screen without guessing unreported settings."""

import re
from dataclasses import dataclass

from .errors import AdaSetupError

type ZoneState = tuple[bool, ...]


def validate_address(address: int) -> None:
    """Validate a PAC address."""
    if type(address) is not int or not 1 <= address <= 9:
        raise ValueError("Amplifier address must be between 1 and 9")


def validate_mask(states: ZoneState) -> str:
    """Encode an exact eight-trigger mask."""
    if len(states) != 8 or any(type(value) is not bool for value in states):
        raise ValueError("A mask must contain exactly eight booleans")
    return "".join("1" if value else "0" for value in states)


@dataclass(frozen=True)
class SetupState:
    """Settings reported by a complete PAC setup screen."""

    address: int
    quick_turn_on: bool
    firmware_version: str
    trigger_mappings: tuple[ZoneState, ...]


def parse_setup_screen(screen: str) -> SetupState:
    """Read the documented table and metadata; reject incomplete screens."""
    address = re.search(r"CURRENT ADDRESS\s*=\s*([1-9])\b", screen)
    quick = re.search(r"QUICK TURN ON\s*=\s*(ON|OFF)\b", screen)
    version = re.search(r"\bVERSION\s+([^\r\n]+)", screen)
    rows = re.findall(r"\bT([1-8])\s*\|([^\r\n]+)", screen)
    mappings: dict[int, ZoneState] = {}
    for number, row in rows:
        cells = row.split("|")
        if len(cells) < 9 or any(cell.strip() not in ("", "X") for cell in cells[:8]):
            raise AdaSetupError("Invalid trigger mapping table")
        mappings[int(number)] = tuple(cell.strip() == "X" for cell in cells[:8])
    if (
        address is None
        or quick is None
        or version is None
        or len(mappings) != 8
        or len(rows) != 8
    ):
        raise AdaSetupError("No complete PAC setup screen received")
    return SetupState(
        address=int(address[1]),
        quick_turn_on=quick[1] == "ON",
        firmware_version=version[1].strip(),
        trigger_mappings=tuple(mappings[number] for number in range(1, 9)),
    )
