"""Async ADA PAC protocol client, independent of Home Assistant.

Only G<address> and S<address>=<eight bits> are sent. Setup commands and
broadcast writes are deliberately outside this client's public API.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress

from serialx import Parity, SerialException, StopBits, open_serial_connection

from .const import ZONE_COUNT
from .errors import AdaConnectionError

type ZoneState = tuple[bool, ...]
type StateCallback = Callable[[ZoneState | None], None]

COMMUNICATION_ERRORS = (OSError, SerialException, TimeoutError)
_STATE_FRAME = re.compile(rb"([1-9])=([01]{8})")
_MAX_LINE_LENGTH = 128
_SETTLE_TIME = 0.25
_FRAME_IDLE_TIME = 0.05
_LOGGER = logging.getLogger(__name__)


class AdaClient:
    """One connection and one serialized transaction queue for an amplifier."""

    def __init__(self, port: str, address: int = 1, timeout: float = 3) -> None:
        """Initialize without touching the serial port."""
        if type(address) is not int or not 1 <= address <= 9:
            raise ValueError("Amplifier address must be between 1 and 9")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Response timeout must be positive and finite")
        self.port = port
        self.address = address
        self.timeout = timeout
        self._state: ZoneState | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: asyncio.Future[ZoneState] | None = None
        self._lock = asyncio.Lock()
        self._listeners: set[StateCallback] = set()

    @property
    def state(self) -> ZoneState | None:
        """Return the most recently confirmed zone state, or None if disconnected."""
        return self._state

    def subscribe(self, callback: StateCallback) -> Callable[[], None]:
        """Subscribe to valid feedback and disconnects; return an unsubscribe."""
        self._listeners.add(callback)
        return lambda: self._listeners.discard(callback)

    async def async_get_state(self) -> ZoneState:
        """Query current hardware state, reconnecting when necessary."""
        async with self._transaction():
            return await self._query()

    async def async_set_zone(self, zone: int, enabled: bool) -> ZoneState:
        """Read, modify, write, then confirm under a single lock.

        Read before every write to preserve the latest state of other zones.
        Feedback is authoritative even when a voltage trigger overrides a write.
        """
        if type(zone) is not int or not 1 <= zone <= ZONE_COUNT:
            raise ValueError("Zone must be between 1 and 8")
        async with self._transaction():
            current = await self._query()
            desired = list(current)
            desired[zone - 1] = enabled
            bits = "".join("1" if value else "0" for value in desired)
            await self._write(f"S{self.address}={bits}\r\n")
            # S can be silent. Let the amp settle, then explicitly request state.
            await asyncio.sleep(_SETTLE_TIME)
            return await self._query()

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[None]:
        async with self._lock:
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

    async def _query(self) -> ZoneState:
        pending = self._pending = asyncio.get_running_loop().create_future()
        try:
            async with asyncio.timeout(self.timeout):
                await self._write(f"G{self.address}\r\n")
                await pending
                # More than one feedback frame may arrive before the query resumes.
                if (state := self._state) is None:
                    raise ConnectionError("Amplifier connection closed")
                return state
        finally:
            self._pending = None
            if not pending.done():
                pending.cancel()
            elif not pending.cancelled():
                pending.exception()  # Retrieve exceptions if the write also failed.

    async def _write(self, command: str) -> None:
        if (writer := self._writer) is None:
            raise ConnectionError("Amplifier is disconnected")
        writer.write(command.encode("ascii"))
        async with asyncio.timeout(self.timeout):
            await writer.drain()

    async def _read_loop(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Frame CR, LF, or CRLF lines, including fragmented serial packets."""
        buffer = bytearray()
        discard = False
        try:
            while True:
                # Some firmware omits the terminator. Accept only a complete
                # frame after a short idle gap, never an incomplete bit string.
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
        if match is None or int(match[1]) != self.address:
            return
        self._state = tuple(value == ord("1") for value in match[2])
        if (pending := self._pending) is not None and not pending.done():
            pending.set_result(self._state)
        self._notify(self._state)

    def _disconnected(self) -> None:
        self._state = None
        if (pending := self._pending) is not None and not pending.done():
            pending.set_exception(ConnectionError("Amplifier connection closed"))
        self._notify(None)

    def _notify(self, state: ZoneState | None) -> None:
        for callback in tuple(self._listeners):
            try:
                callback(state)
            except Exception:
                _LOGGER.exception("Amplifier state listener failed")

    async def async_close(self) -> None:
        """Wait for active work, release the reader and serial port."""
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
