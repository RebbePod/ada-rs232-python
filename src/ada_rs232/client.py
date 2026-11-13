"""Async PAC bus transport, addressed clients, and explicit setup operations."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress

from serialx import Parity, SerialException, StopBits, open_serial_connection

from .errors import AdaConnectionError, AdaSetupError
from .setup import (
    SetupState,
    ZoneState,
    parse_setup_screen,
    validate_address,
    validate_mask,
)

type StateCallback = Callable[[ZoneState | None], None]
type BusCallback = Callable[[int, ZoneState | None], None]

COMMUNICATION_ERRORS = (OSError, SerialException, TimeoutError)
_STATE_FRAME = re.compile(rb"([1-9])=([01]{8})")
_MAX_LINE_LENGTH = 128
_SETTLE_TIME = 0.25
_FRAME_IDLE_TIME = 0.05
_SETUP_IDLE_TIME = 0.2
_BROADCAST_WINDOW = 1.1  # Nine replies are staggered by 0.1 seconds per address.
_LOGGER = logging.getLogger(__name__)


class AdaBus:
    """One transport and transaction queue for up to nine addressed amplifiers."""

    def __init__(self, port: str, timeout: float = 3) -> None:
        """Initialize without opening the connection."""
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Response timeout must be positive and finite")
        self.port = port
        self.timeout = timeout
        self._states: dict[int, ZoneState] = {}
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: asyncio.Future[ZoneState] | None = None
        self._pending_address: int | None = None
        self._broadcast: dict[int, ZoneState] | None = None
        self._lock = asyncio.Lock()
        self._listeners: set[BusCallback] = set()
        self._setup_buffer: bytearray | None = None
        self._setup_changed = asyncio.Event()
        self._setup_until = 0.0

    @property
    def states(self) -> dict[int, ZoneState]:
        """Return a copy of the last confirmed output states."""
        return dict(self._states)

    @property
    def in_setup(self) -> bool:
        """Whether a confirmed setup session may still be active."""
        return asyncio.get_running_loop().time() < self._setup_until

    def subscribe(self, callback: BusCallback) -> Callable[[], None]:
        """Subscribe to addressed feedback and per-address disconnects."""
        self._listeners.add(callback)
        return lambda: self._listeners.discard(callback)

    async def async_get_state(self, address: int) -> ZoneState:
        """Query one amplifier."""
        validate_address(address)
        async with self._transaction():
            return await self._query(address)

    async def async_get_states(self) -> dict[int, ZoneState]:
        """Send GA and collect this transaction's replies from the whole chain."""
        async with self._transaction():
            return await self._query_all()

    async def async_set_zone(self, address: int, zone: int, enabled: bool) -> ZoneState:
        """Read, modify one trigger, write the complete mask, and confirm."""
        validate_address(address)
        if type(zone) is not int or not 1 <= zone <= 8:
            raise ValueError("Zone must be between 1 and 8")
        if type(enabled) is not bool:
            raise ValueError("Enabled must be a boolean")
        async with self._transaction():
            desired = list(await self._query(address))
            desired[zone - 1] = enabled
            return await self._set_mask(address, tuple(desired))

    async def async_set_zones(self, address: int, states: ZoneState) -> ZoneState:
        """Apply an explicit eight-trigger mask in one write, then read back."""
        validate_address(address)
        validate_mask(states)
        async with self._transaction():
            return await self._set_mask(address, states)

    async def async_all_off(self) -> dict[int, ZoneState]:
        """Send the documented SA all-off broadcast and query actual outputs."""
        async with self._transaction():
            await self._write("SA=00000000\r\n")
            await asyncio.sleep(_SETTLE_TIME)
            return await self._query_all()

    async def _set_mask(self, address: int, states: ZoneState) -> ZoneState:
        await self._write(f"S{address}={validate_mask(states)}\r\n")
        await asyncio.sleep(_SETTLE_TIME)
        return await self._query(address)

    async def async_read_setup(self) -> SetupState:
        """Refresh the setup screen after the user physically enters setup mode.

        Only one amplifier may be connected. This sends Enter, never a command
        that changes settings. Runtime operations pause after setup is detected.
        """
        async with self._transaction(setup=True):
            return await self._setup_command("")

    async def async_set_trigger_mapping(
        self, trigger: int, states: ZoneState
    ) -> SetupState:
        """Set a trigger mapping in an already confirmed setup session."""
        if type(trigger) is not int or not 1 <= trigger <= 8:
            raise ValueError("Trigger must be between 1 and 8")
        bits = validate_mask(states)
        async with self._transaction(setup=True):
            self._require_setup()
            result = await self._setup_command(f"T{trigger}={bits}")
            if result.trigger_mappings[trigger - 1] != states:
                raise AdaSetupError("Trigger mapping was not confirmed")
            return result

    async def async_set_quick_turn_on(self, enabled: bool) -> SetupState:
        """Read Q's current state before toggling, and verify the requested state."""
        if type(enabled) is not bool:
            raise ValueError("Enabled must be a boolean")
        async with self._transaction(setup=True):
            self._require_setup()
            current = await self._setup_command("")
            if current.quick_turn_on == enabled:
                return current
            result = await self._setup_command("Q")
            if result.quick_turn_on != enabled:
                raise AdaSetupError("Quick Turn On setting was not confirmed")
            return result

    async def async_set_address(self, address: int) -> SetupState:
        """Change the stored PAC address and verify the returned setup screen."""
        validate_address(address)
        async with self._transaction(setup=True):
            self._require_setup()
            result = await self._setup_command(f"A{address}")
            if result.address != address:
                raise AdaSetupError("Amplifier address change was not confirmed")
            self._states.clear()
            return result

    async def async_restore_trigger_defaults(self) -> SetupState:
        """Restore the documented one-to-one trigger mappings, then verify."""
        async with self._transaction(setup=True):
            self._require_setup()
            result = await self._setup_command("D")
            defaults = tuple(
                tuple(zone == trigger for zone in range(8)) for trigger in range(8)
            )
            if result.trigger_mappings != defaults:
                raise AdaSetupError("Default trigger mappings were not confirmed")
            return result

    async def async_resume(self, address: int) -> ZoneState:
        """Verify normal operation after the user physically exits setup mode."""
        validate_address(address)
        async with self._transaction(setup=True):
            state = await self._query(address)
            self._setup_until = 0
            return state

    def _require_setup(self) -> None:
        if not self.in_setup:
            raise AdaSetupError("Read a complete setup screen before changing settings")

    async def _setup_command(self, command: str) -> SetupState:
        self._setup_buffer = bytearray()
        self._setup_changed.clear()
        try:
            await self._write(f"{command}\r")
            async with asyncio.timeout(self.timeout):
                while True:
                    await self._setup_changed.wait()
                    self._setup_changed.clear()
                    try:
                        async with asyncio.timeout(_SETUP_IDLE_TIME):
                            await self._setup_changed.wait()
                    except TimeoutError:
                        try:
                            result = parse_setup_screen(
                                self._setup_buffer.decode("ascii", errors="replace")
                            )
                        except AdaSetupError:
                            continue
                        self._setup_until = asyncio.get_running_loop().time() + 60
                        return result
        finally:
            self._setup_buffer = None

    @asynccontextmanager
    async def _transaction(self, *, setup: bool = False) -> AsyncIterator[None]:
        async with self._lock:
            if not setup and self.in_setup:
                raise AdaSetupError(
                    "Exit amplifier setup mode before using power controls"
                )
            try:
                if self._writer is None:
                    async with asyncio.timeout(self.timeout):
                        reader, self._writer = await open_serial_connection(
                            self.port,
                            baudrate=19200,
                            bytesize=8,
                            parity=Parity.NONE,
                            stopbits=StopBits.ONE,
                            xonxoff=False,
                            rtscts=False,
                        )
                    self._reader_task = asyncio.create_task(
                        self._read_loop(reader, self._writer), name="ada_rs232_reader"
                    )
                yield
            except asyncio.CancelledError:
                await self._close()
                raise
            except COMMUNICATION_ERRORS as err:
                await self._close()
                raise AdaConnectionError(
                    "Unable to communicate with amplifier"
                ) from err

    async def _query(self, address: int) -> ZoneState:
        pending = self._pending = asyncio.get_running_loop().create_future()
        self._pending_address = address
        try:
            async with asyncio.timeout(self.timeout):
                await self._write(f"G{address}\r\n")
                await pending
                if (state := self._states.get(address)) is None:
                    raise ConnectionError("Amplifier connection closed")
                return state
        finally:
            self._pending = None
            self._pending_address = None
            if not pending.done():
                pending.cancel()
            elif not pending.cancelled():
                pending.exception()

    async def _query_all(self) -> dict[int, ZoneState]:
        self._broadcast = {}
        try:
            await self._write("GA\r\n")
            await asyncio.sleep(max(_BROADCAST_WINDOW, self.timeout))
            if self._writer is None or not self._broadcast:
                raise ConnectionError("No amplifier replied to broadcast query")
            for address in self._states.keys() - self._broadcast.keys():
                del self._states[address]
                self._notify(address, None)
            return dict(self._broadcast)
        finally:
            self._broadcast = None

    async def _write(self, command: str) -> None:
        if (writer := self._writer) is None:
            raise ConnectionError("Amplifier is disconnected")
        writer.write(command.encode("ascii"))
        async with asyncio.timeout(self.timeout):
            await writer.drain()

    async def _read_loop(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        buffer = bytearray()
        discard = False
        try:
            while True:
                idle = _FRAME_IDLE_TIME if _STATE_FRAME.fullmatch(buffer) else None
                try:
                    async with asyncio.timeout(idle):
                        chunk = await reader.read(256)
                except TimeoutError:
                    self._handle_frame(bytes(buffer))
                    buffer.clear()
                    continue
                if not chunk:
                    break
                if self._setup_buffer is not None:
                    if len(self._setup_buffer) + len(chunk) > 32768:
                        raise ConnectionError("Oversized PAC setup response")
                    self._setup_buffer.extend(chunk)
                    self._setup_changed.set()
                for value in chunk:
                    if value in (10, 13):
                        if not discard:
                            self._handle_frame(bytes(buffer))
                        buffer.clear()
                        discard = False
                    elif not discard:
                        buffer.append(value)
                        if len(buffer) > _MAX_LINE_LENGTH:
                            buffer.clear()
                            discard = True
        except COMMUNICATION_ERRORS:
            pass
        finally:
            if self._writer is writer:
                self._writer = None
                self._reader_task = None
                self._disconnected()
            writer.close()
            with suppress(*COMMUNICATION_ERRORS):
                async with asyncio.timeout(2):
                    await writer.wait_closed()

    def _handle_frame(self, frame: bytes) -> None:
        match = _STATE_FRAME.fullmatch(frame.strip())
        if match is None:
            return
        address = int(match[1])
        state = self._states[address] = tuple(value == ord("1") for value in match[2])
        if self._broadcast is not None:
            self._broadcast[address] = state
        if (
            (pending := self._pending) is not None
            and not pending.done()
            and address == self._pending_address
        ):
            pending.set_result(state)
        self._notify(address, state)

    def _disconnected(self) -> None:
        addresses = set(self._states)
        if self._pending_address is not None:
            addresses.add(self._pending_address)
        self._states.clear()
        if (pending := self._pending) is not None and not pending.done():
            pending.set_exception(ConnectionError("Amplifier connection closed"))
        for address in addresses:
            self._notify(address, None)

    def _notify(self, address: int, state: ZoneState | None) -> None:
        for callback in tuple(self._listeners):
            try:
                callback(address, state)
            except Exception:
                _LOGGER.exception("Amplifier state listener failed")

    async def async_close(self) -> None:
        """Release the shared connection after any active transaction."""
        async with self._lock:
            await self._close()

    async def _close(self) -> None:
        writer, self._writer = self._writer, None
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if writer is not None:
            writer.close()
            with suppress(*COMMUNICATION_ERRORS):
                async with asyncio.timeout(2):
                    await writer.wait_closed()
        self._disconnected()


class AdaClient:
    """Addressed view of a standalone or shared PAC bus."""

    def __init__(
        self,
        port: str,
        address: int = 1,
        timeout: float = 3,
        *,
        bus: AdaBus | None = None,
    ) -> None:
        """Initialize an addressed client; a supplied bus owns its connection."""
        validate_address(address)
        if bus is not None and bus.port != port:
            raise ValueError("Client port must match the shared bus")
        self.address = address
        self.port = port
        self.timeout = timeout
        self.bus = bus or AdaBus(port, timeout)
        self._owns_bus = bus is None

    @property
    def state(self) -> ZoneState | None:
        """Last confirmed output state at this address."""
        return self.bus.states.get(self.address)

    def subscribe(self, callback: StateCallback) -> Callable[[], None]:
        """Subscribe to this amplifier's feedback."""

        def addressed(address: int, state: ZoneState | None) -> None:
            if address == self.address:
                callback(state)

        return self.bus.subscribe(addressed)

    async def async_get_state(self) -> ZoneState:
        """Query this amplifier."""
        return await self.bus.async_get_state(self.address)

    async def async_set_zone(self, zone: int, enabled: bool) -> ZoneState:
        """Change one trigger and confirm actual output state."""
        return await self.bus.async_set_zone(self.address, zone, enabled)

    async def async_set_zones(self, states: ZoneState) -> ZoneState:
        """Set a complete trigger mask and confirm actual output state."""
        return await self.bus.async_set_zones(self.address, states)

    async def async_close(self) -> None:
        """Close an owned connection; a shared bus is closed by its owner."""
        if self._owns_bus:
            await self.bus.async_close()
