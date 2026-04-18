#!/usr/bin/env python3
import json
import logging
from logging.handlers import RotatingFileHandler
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Dict, Optional


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "conf.json"
DEFAULT_LOG_FILE = BASE_DIR / "ssd_health.log"
MAX_LOG_SIZE = 5 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 2


def setup_logging(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("ssd_health_udp")
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=MAX_LOG_SIZE,
        backupCount=BACKUP_COUNT,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    stream_handler.setLevel(logging.INFO)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    return logger


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"Configuration file not found: {CONFIG_FILE}")

    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    required = ["udp_ip", "udp_port"]
    for key in required:
        if key not in cfg:
            raise ValueError(f"Missing required key in conf.json: {key}")

    return cfg


def run_command(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False
    )


def discover_devices(logger: logging.Logger) -> List[Dict[str, str]]:
    result = run_command(["smartctl", "--scan-open"])

    if result.returncode != 0 and not result.stdout.strip():
        raise RuntimeError(
            f"smartctl --scan-open failed: {result.stderr.strip()}"
        )

    devices: List[Dict[str, str]] = []

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        match = re.match(r"^(\S+)\s+-d\s+(\S+)", line)
        if match:
            devices.append({
                "device": match.group(1),
                "type": match.group(2)
            })
        else:
            first = line.split()[0]
            devices.append({
                "device": first,
                "type": ""
            })

    logger.info(
        "Discovered drives: %s",
        ", ".join(d["device"] for d in devices) if devices else "none"
    )
    return devices


def read_smart(device: str, dev_type: str = "") -> str:
    cmd = ["smartctl", "-a"]
    if dev_type:
        cmd.extend(["-d", dev_type])
    cmd.append(device)

    result = run_command(cmd)

    if result.returncode != 0 and not result.stdout.strip():
        raise RuntimeError(
            f"smartctl failed for {device}: {result.stderr.strip()}"
        )

    return result.stdout


def sanitize_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"[^A-Za-z0-9_]+", "_", name)
    name = re.sub(r"_+", "_", name)
    name = name.strip("_")
    return name or "unknown"


def parse_model_name(smart_output: str, fallback_device: str) -> str:
    patterns = [
        r"Model Number:\s+(.+)",
        r"Device Model:\s+(.+)",
        r"Product:\s+(.+)",
        r"Model Family:\s+(.+)"
    ]

    for pattern in patterns:
        match = re.search(pattern, smart_output, re.IGNORECASE)
        if match:
            return sanitize_name(match.group(1).strip())

    return sanitize_name(Path(fallback_device).name)


def parse_health_percent(smart_output: str) -> Optional[float]:
    match = re.search(r"Percentage Used:\s+(\d+)%", smart_output, re.IGNORECASE)
    if match:
        used = float(match.group(1))
        health = 100.0 - used
        return max(0.0, min(100.0, health))

    sata_patterns = [
        r"Percent_Lifetime_Remain\s+\S+\s+\S+\s+\S+\s+\S+\s+(\d+)",
        r"SSD_Life_Left\s+\S+\s+\S+\s+\S+\s+\S+\s+(\d+)",
        r"Media_Wearout_Indicator\s+\S+\s+\S+\s+\S+\s+\S+\s+(\d+)",
        r"Wear_Leveling_Count\s+\S+\s+\S+\s+\S+\s+\S+\s+(\d+)"
    ]

    for pattern in sata_patterns:
        match = re.search(pattern, smart_output, re.IGNORECASE)
        if match:
            value = float(match.group(1))
            return max(0.0, min(100.0, value))

    if re.search(r"SMART overall-health self-assessment test result:\s+PASSED", smart_output, re.IGNORECASE):
        return 100.0

    if re.search(r"SMART Health Status:\s+OK", smart_output, re.IGNORECASE):
        return 100.0

    return None


def unique_name(base_name: str, used_names: set) -> str:
    if base_name not in used_names:
        used_names.add(base_name)
        return base_name

    index = 2
    while f"{base_name}_{index}" in used_names:
        index += 1

    final_name = f"{base_name}_{index}"
    used_names.add(final_name)
    return final_name


def collect_health_values(logger: logging.Logger) -> List[Dict[str, object]]:
    devices = discover_devices(logger)
    results: List[Dict[str, object]] = []
    used_names = set()

    for entry in devices:
        device = entry["device"]
        dev_type = entry["type"]

        try:
            smart_output = read_smart(device, dev_type)
            base_name = parse_model_name(smart_output, device)
            name = unique_name(base_name, used_names)
            health = parse_health_percent(smart_output)

            if health is None:
                logger.warning("No health value could be derived for %s", device)
                continue

            logger.info(
                "Drive evaluated: device=%s type=%s name=%s health=%.2f",
                device,
                dev_type if dev_type else "-",
                name,
                health
            )

            results.append({
                "name": name,
                "device": device,
                "type": dev_type,
                "health": health
            })

        except Exception as exc:
            logger.error("Drive evaluation failed: device=%s error=%s", device, exc)

    return results


def build_payload(results: List[Dict[str, object]]) -> str:
    parts = []

    for item in results:
        name = str(item["name"])
        health = float(item["health"])
        parts.append(f"{name}_health:{health:.2f}")

    parts.append(f"timestamp:{int(time.time())}")
    return ";".join(parts)


def send_udp(message: str, ip: str, port: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(message.encode("utf-8"), (ip, port))
    finally:
        sock.close()


def main() -> int:
    logger = None

    try:
        cfg = load_config()
        log_file = Path(cfg.get("log_file", str(DEFAULT_LOG_FILE))).expanduser().resolve()
        logger = setup_logging(log_file)

        logger.info("Script started")
        logger.info("UDP target configured: ip=%s port=%s", cfg["udp_ip"], cfg["udp_port"])
        logger.info("Log file: %s", log_file)

        results = collect_health_values(logger)

        if not results:
            raise RuntimeError("No evaluable drives were found.")

        payload = build_payload(results)
        send_udp(payload, cfg["udp_ip"], int(cfg["udp_port"]))

        logger.info(
            "UDP sent to ip=%s port=%s payload=%s",
            cfg["udp_ip"],
            cfg["udp_port"],
            payload
        )

        print(payload)
        return 0

    except Exception as exc:
        if logger is None:
            logger = setup_logging(DEFAULT_LOG_FILE)
        logger.error("Execution failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())