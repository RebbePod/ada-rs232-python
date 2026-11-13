"""PAC multi-address commands and verified, explicit setup operations."""

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import patch

import pytest

from ada_rs232 import AdaBus, AdaClient, AdaConnectionError, AdaSetupError
from ada_rs232.setup import parse_setup_screen

OFF = (False,) * 8
DEFAULTS = tuple(tuple(t == z for z in range(8)) for t in range(8))


class ChainStream:
    """Serial emulator with normal and physically selected setup modes."""

    def __init__(self) -> None:
        self.reader = asyncio.StreamReader()
        self.outputs = {1: "10000000", 9: "00000001"}
        self.commands: list[bytes] = []
        self.setup = False
        self.quick = False
        self.address = 1
        self.mappings = list(DEFAULTS)
        self.ignore_settings = False
        self.closed = False
        self.silent = False

    def screen(self) -> bytes:
        rows = ["OUTPUTS", "__|_1_|_2_|_3_|_4_|_5_|_6_|_7_|_8_|"]
        for index, row in enumerate(self.mappings):
            cells = "|".join(" X " if value else "   " for value in row)
            rows.append(f"T{index + 1}|{cells}|")
        rows.extend(
            [
                "VERSION 1.0",
                f"CURRENT ADDRESS = {self.address} QUICK TURN ON = "
                + ("ON" if self.quick else "OFF"),
                "ENTER ADDRESS IN THE FORM OF:",
                "A1 <enter> (A1-A9 allowed)",
            ]
        )
        return "\r\n".join(rows).encode() + b"\r\n"

    def write(self, data: bytes) -> None:
        self.commands.append(data)
        if self.silent:
            return
        command = data.decode().strip()
        if self.setup:
            if not self.ignore_settings:
                if command == "Q":
                    self.quick = not self.quick
                elif command == "D":
                    self.mappings = list(DEFAULTS)
                elif command.startswith("A"):
                    self.address = int(command[1:])
                elif command.startswith("T"):
                    self.mappings[int(command[1]) - 1] = tuple(
                        bit == "1" for bit in command[3:]
                    )
            self.reader.feed_data(self.screen())
        elif command == "GA":
            for address, bits in self.outputs.items():
                self.reader.feed_data(f"{address}={bits}\r\n".encode())
        elif command == "SA=00000000":
            self.outputs = dict.fromkeys(self.outputs, "00000000")
        elif command.startswith("G") and command[1:].isdigit():
            address = int(command[1:])
            if address in self.outputs:
                self.reader.feed_data(f"{address}={self.outputs[address]}\r\n".encode())
        elif command.startswith("S"):
            self.outputs[int(command[1])] = command[3:]

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


@pytest.fixture
async def chain() -> AsyncIterator[tuple[AdaBus, ChainStream]]:
    stream = ChainStream()
    with (
        patch(
            "ada_rs232.client.open_serial_connection",
            return_value=(stream.reader, stream),
        ),
        patch("ada_rs232.client._SETUP_IDLE_TIME", 0.01),
        patch("ada_rs232.client._BROADCAST_WINDOW", 0.02),
    ):
        bus = AdaBus("/dev/test", timeout=0.1)
        yield bus, stream
        await bus.async_close()


async def test_shared_addressed_clients(chain) -> None:
    bus, stream = chain
    first = AdaClient("/dev/test", address=1, bus=bus)
    ninth = AdaClient("/dev/test", address=9, bus=bus)
    first_states, ninth_states = [], []
    first.subscribe(first_states.append)
    ninth.subscribe(ninth_states.append)
    with patch(
        "ada_rs232.client.open_serial_connection", return_value=(stream.reader, stream)
    ) as opened:
        results = await asyncio.gather(first.async_get_state(), ninth.async_get_state())
    assert results == [(True,) + (False,) * 7, (False,) * 7 + (True,)]
    opened.assert_awaited_once()
    assert first_states == [results[0]]
    assert ninth_states == [results[1]]
    await first.async_close()
    assert not stream.closed
    await bus.async_close()
    assert first_states[-1] is None
    assert ninth_states[-1] is None


async def test_query_all_drops_stale_addresses(chain) -> None:
    bus, stream = chain
    assert set(await bus.async_get_states()) == {1, 9}
    del stream.outputs[9]
    assert await bus.async_get_states() == {1: (True,) + (False,) * 7}
    assert stream.commands == [b"GA\r\n", b"GA\r\n"]


async def test_atomic_mask_and_broadcast_off(chain) -> None:
    bus, stream = chain
    assert await bus.async_set_zones(9, (True,) * 8) == (True,) * 8
    assert stream.commands == [b"S9=11111111\r\n", b"G9\r\n"]
    assert await bus.async_all_off() == {1: OFF, 9: OFF}
    assert stream.commands[-2:] == [b"SA=00000000\r\n", b"GA\r\n"]


async def test_broadcast_no_replies_closes_connection(chain) -> None:
    bus, stream = chain
    stream.silent = True
    with pytest.raises(AdaConnectionError):
        await bus.async_get_states()
    assert stream.closed


async def test_setup_read_parse_pause_and_resume(chain) -> None:
    bus, stream = chain
    stream.setup = True
    state = await bus.async_read_setup()
    assert state.address == 1
    assert state.firmware_version == "1.0"
    assert not state.quick_turn_on
    assert state.trigger_mappings == DEFAULTS
    assert stream.commands == [b"\r"]
    with pytest.raises(AdaSetupError, match="Exit"):
        await bus.async_get_state(1)
    assert stream.commands == [b"\r"]
    stream.setup = False
    assert await bus.async_resume(1) == (True,) + (False,) * 7
    assert not bus.in_setup


async def test_setup_commands_and_verified_address(chain) -> None:
    bus, stream = chain
    stream.setup = True
    await bus.async_read_setup()
    changed = await bus.async_set_trigger_mapping(8, (True,) * 8)
    assert changed.trigger_mappings[7] == (True,) * 8
    assert (await bus.async_set_quick_turn_on(True)).quick_turn_on
    commands = list(stream.commands)
    assert (await bus.async_set_quick_turn_on(True)).quick_turn_on
    assert stream.commands == [*commands, b"\r"]
    assert not (await bus.async_set_quick_turn_on(False)).quick_turn_on
    assert (await bus.async_set_address(9)).address == 9
    assert (await bus.async_restore_trigger_defaults()).trigger_mappings == DEFAULTS
    assert b"T8=11111111\r" in stream.commands
    assert b"A9\r" in stream.commands
    assert stream.commands[-1] == b"D\r"


@pytest.mark.parametrize("operation", ["mapping", "quick", "address", "defaults"])
async def test_setup_writes_require_read_screen(chain, operation: str) -> None:
    bus, stream = chain
    operations = {
        "mapping": lambda: bus.async_set_trigger_mapping(1, OFF),
        "quick": lambda: bus.async_set_quick_turn_on(True),
        "address": lambda: bus.async_set_address(2),
        "defaults": bus.async_restore_trigger_defaults,
    }
    with pytest.raises(AdaSetupError, match="Read a complete"):
        await operations[operation]()
    assert stream.commands == []


@pytest.mark.parametrize("operation", ["mapping", "quick", "address", "defaults"])
async def test_unconfirmed_settings_report_error(chain, operation: str) -> None:
    bus, stream = chain
    stream.setup = True
    stream.mappings[0] = (True,) * 8
    await bus.async_read_setup()
    stream.ignore_settings = True
    operations = {
        "mapping": lambda: bus.async_set_trigger_mapping(2, OFF),
        "quick": lambda: bus.async_set_quick_turn_on(True),
        "address": lambda: bus.async_set_address(2),
        "defaults": bus.async_restore_trigger_defaults,
    }
    with pytest.raises(AdaSetupError, match="not confirmed"):
        await operations[operation]()


@pytest.mark.parametrize("mask", [(), (True,) * 7, (1,) * 8, (False,) * 9])
async def test_invalid_masks_never_write(chain, mask) -> None:
    bus, stream = chain
    with pytest.raises(ValueError):
        await bus.async_set_zones(1, mask)
    assert not stream.commands


async def test_missing_setup_screen_does_not_enable_writes(chain) -> None:
    bus, stream = chain
    with pytest.raises(AdaConnectionError):
        await bus.async_read_setup()
    assert not bus.in_setup
    assert stream.commands == [b"\r"]


@pytest.mark.parametrize(
    "screen",
    [
        "",
        "CURRENT ADDRESS = 1 QUICK TURN ON = OFF\r\nVERSION 1.0",
        "T1| ? | | | | | | | |",
    ],
)
def test_setup_parser_rejects_partial_or_invalid_screen(screen: str) -> None:
    with pytest.raises(AdaSetupError):
        parse_setup_screen(screen)


async def test_broadcast_waits_for_late_address_nine(chain) -> None:
    bus, stream = chain
    stream.silent = True

    async def replies() -> None:
        await asyncio.sleep(0.1)
        stream.reader.feed_data(b"1=10000000\r\n")
        await asyncio.sleep(0.8)
        stream.reader.feed_data(b"9=00000001\r\n")

    with patch("ada_rs232.client._BROADCAST_WINDOW", 1.1):
        task = asyncio.create_task(replies())
        try:
            assert set(await bus.async_get_states()) == {1, 9}
        finally:
            await task


async def test_fragmented_setup_with_idle_gap(chain) -> None:
    bus, stream = chain
    stream.silent = True
    screen = stream.screen()

    async def reply() -> None:
        await asyncio.sleep(0.01)
        stream.reader.feed_data(screen[:100])
        await asyncio.sleep(0.03)
        stream.reader.feed_data(screen[100:])

    task = asyncio.create_task(reply())
    try:
        assert (await bus.async_read_setup()).trigger_mappings == DEFAULTS
    finally:
        await task


async def test_toggle_lost_confirmation_is_not_replayed(chain) -> None:
    bus, stream = chain
    stream.setup = True
    await bus.async_read_setup()
    original = stream.write

    def lose_reply(data: bytes) -> None:
        if data == b"Q\r":
            stream.quick = True
            stream.commands.append(data)
        else:
            original(data)

    with patch.object(stream, "write", side_effect=lose_reply):
        with pytest.raises(AdaConnectionError):
            await bus.async_set_quick_turn_on(True)
    assert stream.quick
    assert stream.commands.count(b"Q\r") == 1
    fresh = ChainStream()
    fresh.setup = True
    fresh.quick = True
    with patch(
        "ada_rs232.client.open_serial_connection", return_value=(fresh.reader, fresh)
    ):
        assert (await bus.async_read_setup()).quick_turn_on
        assert (await bus.async_set_quick_turn_on(True)).quick_turn_on
    assert b"Q\r" not in fresh.commands


async def test_query_all_invalidates_missing_amp_feedback(chain) -> None:
    bus, stream = chain
    states = []
    bus.subscribe(lambda address, state: states.append((address, state)))
    await bus.async_get_states()
    del stream.outputs[9]
    await bus.async_get_states()
    assert states[-1] == (9, None)
    assert 9 not in bus.states


def test_client_rejects_mismatched_shared_port() -> None:
    bus = AdaBus("/dev/first")
    with pytest.raises(ValueError, match="match the shared bus"):
        AdaClient("/dev/other", bus=bus)
