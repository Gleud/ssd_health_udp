# SSD Health UDP Reporter

## Overview

This Python script automatically scans all storage devices detected by `smartctl`, reads their SMART / health information, calculates an estimated health percentage, and sends the result as a single UDP message.

It is intended for lightweight monitoring setups where a storage health value should be forwarded periodically to another system, for example via cron.

The script also writes a rotating log file with a maximum size of 5 MB per file.

## Features

- Automatically discovers all drives supported by `smartctl`
- Reads SMART / NVMe health information
- Calculates remaining health as a value from `0.00` to `100.00`
- Sends all drive health values in one UDP packet
- Appends a Unix timestamp to the payload
- Writes log output in English
- Uses rotating log files with a maximum size of 5 MB
- Works on Linux and macOS, provided `smartctl` is available

## Output Format

Example UDP payload:

```text
APPLE_SSD_AP1024Z_health:98.00;Samsung_SSD_990_EVO_Plus_2TB_health:100.00;timestamp:1776506977