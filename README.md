# ADA RS-232

An asyncio library for the Audio Design Associates PTM-1645 PAC amplifier
controller. The protocol implementation is independent of Home Assistant and
uses serialx for local serial ports, ESPHome serial proxies, and network serial
connections.

The PTM-1645 protocol and hardware connection were tested by the maintainer using
an ESPHome RS-232 proxy. The standalone packaging and typed exceptions are being
prepared for Home Assistant Core.

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
request reconnects and reads state before any write. Invalid arguments raise
`ValueError`. Connection URLs are never included in library error messages.

All read/modify/write/readback operations share one lock. A set command can be
silent, so it is followed by an explicit query. A voltage trigger or bypass can
override data control; returned hardware state is authoritative. The protocol
does not report the origin of a trigger and has no transaction IDs. External
changes between read and write cannot be made atomic. One amplifier is supported
per client connection; do not create multiple clients for the same port.

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
