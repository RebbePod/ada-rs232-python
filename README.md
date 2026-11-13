# ADA RS-232

An asyncio library for the Audio Design Associates PTM-1645 PAC amplifier
controller. The protocol implementation is independent of Home Assistant and
uses serialx for local serial ports, ESPHome serial proxies, and network serial
connections.

The PTM-1645 protocol and hardware connection were tested by the maintainer using
an ESPHome RS-232 proxy. Version 0.1.0 is the dependency for the prepared
Home Assistant Core change.
The 0.2.0 beta adds the remaining documented commands; those additions require
hardware verification.

## Usage

```python
import asyncio

from ada_rs232 import AdaClient


async def main() -> None:
    client = AdaClient("/dev/serial/by-id/your-adapter", address=1)
    try:
        print(await client.async_get_state())
        print(await client.async_set_zone(1, True))
    finally:
        await client.async_close()


asyncio.run(main())
```

Addresses are 1–9; zone numbers are 1–8. The returned tuple contains eight
booleans in zone order. Serial settings are fixed at 19200 baud, 8N1, with no
flow control. Use the amplifier's default one-to-one trigger mapping.

`subscribe(callback)` registers a synchronous listener and returns an unsubscribe
function. The listener receives the actual zone tuple, or `None` when the
connection fails. A failed transaction raises `AdaConnectionError`; the next
request reconnects. Single-zone updates read current state before writing.
Explicit mask and broadcast actions write the mask the caller requested.
Invalid arguments raise
`ValueError`. Connection URLs are never included in library error messages.

All read/modify/write/readback operations share one lock. A set command can be
silent, so it is followed by an explicit query. A voltage trigger or bypass can
override data control; returned hardware state is authoritative. The protocol
does not report the origin of a trigger and has no transaction IDs. External
changes between read and write cannot be made atomic. Use one `AdaBus` per serial
connection and an addressed `AdaClient` for each
amplifier. Never create independent buses or standalone clients for the same port.

## Shared bus and additional commands

```python
from ada_rs232 import AdaBus, AdaClient

bus = AdaBus("/dev/serial/by-id/your-adapter")
first = AdaClient(bus.port, address=1, bus=bus)
ninth = AdaClient(bus.port, address=9, bus=bus)
try:
    print(await bus.async_get_states())  # GA; wait for staggered replies.
    print(await ninth.async_set_zones((True,) * 8))
    print(await bus.async_all_off())  # SA=00000000, then GA readback.
finally:
    await bus.async_close()
```

Bus transactions serialize across every addressed client. Closing a client view
never closes a supplied bus. Its owner must close the bus after releasing all
views and unsubscribing their listeners. Bus subscribers receive `(address,
state)` callbacks; missing addresses in a successful GA cycle are invalidated.
The GA collection window is at least 1.1 seconds because replies are staggered
by address in 0.1 second increments. `states` returns a copy of the cache.
A timeout closes the shared transport, invalidating its cached states.

### Physical setup mode

Connect only one amplifier with no loop-throughs, hold its rear Setup button for
more than five seconds, then release. `await bus.async_read_setup()` sends Enter
and parses all eight trigger rows, the firmware version, current address, and
Quick Turn On. Settings are returned as an immutable `SetupState`.

After a complete setup screen has been read, these methods change and verify
settings: `async_set_trigger_mapping(trigger, eight_booleans)`,
`async_set_quick_turn_on(enabled)`, `async_set_address(address)`, and
`async_restore_trigger_defaults()`. Q is a toggle: the library first reads the
current setting, sends Q only if it differs, and verifies the result. No setup
write is retried automatically after losing confirmation.

Power commands raise `AdaSetupError` while a confirmed setup session remains
active. Press the physical Setup button once to exit, then call
`async_resume(address)` to verify normal operation. The device also exits setup
after 60 seconds without commands. A failed or partial setup response never
permits a settings write. The host cannot verify disconnected loop-throughs;
callers must enforce the single-amplifier prerequisite.

The screen parser follows the manufacturer's printed ASCII table. It has tests
for fragmented screens and incomplete data, but needs comparison with a real
terminal capture. Custom trigger mappings prevent independent zone control:
feedback reports outputs, while mask writes operate configured triggers.

## Development

```sh
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov=ada_rs232 --cov-report=term-missing
uv build
```

Source and wheel distributions are built from the release tag in GitHub Actions.
Publishing uses PyPI Trusted Publishing, without a stored API token.
