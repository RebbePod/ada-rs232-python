# Changelog

## 0.2.0b1

- Add a shared `AdaBus`, addressed client views, GA queries, atomic masks, and
  the documented SA all-off broadcast with actual output readback.
- Add complete setup-screen parsing, verified trigger mappings, Quick Turn On,
  stored address changes, trigger defaults, and explicit normal-mode resumption.
- Serialize all bus operations; invalidate missing GA addresses and disconnects.
- Require confirmed physical setup mode before settings writes and never replay
  an uncertain toggle. Preserve the existing standalone `AdaClient` interface.
- These new command paths are beta and require hardware testing.

## 0.1.0

- Extract the hardware-tested PAC serial protocol from the ADA Home Assistant
  custom integration into an independent, typed async library.
- Support eight-zone readback, serialized zone updates, unsolicited feedback,
  disconnect handling, and reconnection after failures.
- Include protocol tests and a real serialx pseudo-terminal test.
