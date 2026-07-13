# AbstractCamera documentation

## Start here

1. [Project overview + quickstart](../README.md)
2. [Getting started](getting-started.md)
3. [Architecture](architecture.md)

## Reference

- [API](api.md)
- [FAQ](faq.md)
- [Troubleshooting](troubleshooting.md)
- [Architecture decisions](adr/README.md)
- [Backlog](backlog/README.md)
- AI-readable: [`../llms.txt`](../llms.txt), [`../llms-full.txt`](../llms-full.txt)

## Hardware validation status

| Family | Body | Status |
| --- | --- | --- |
| Nikon Z | Z6 II | Hardware-validated 2026-07-07/08 (in the origin host); protected by the simulator regression suite + the golden write-sequence pin |
| Sony Alpha | A7R IV | Hardware-validated 2026-07-12 through this package (22 + 11 checks) |
| Webcam | MacBook Pro camera | Hardware-validated 2026-07-12 through this package (21 checks) |
| Generic PTP | — | Simulator-covered fallback; honest ledger applies |
