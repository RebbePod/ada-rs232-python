# Changelog

## 0.1.0

- Extract the hardware-tested PAC serial protocol from the ADA Home Assistant
  custom integration into an independent, typed async library.
- Support eight-zone readback, serialized zone updates, unsolicited feedback,
  disconnect handling, and reconnection after failures.
- Include protocol tests and a real serialx pseudo-terminal test.
