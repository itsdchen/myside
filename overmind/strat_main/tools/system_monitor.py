#!/usr/bin/env python3

"""
System health monitor - alerts when memory usage, CPU load, or core temperatures get high

To run manually to check just once and exit: system_monitor.py --once
To run manually and keep running (e.g., in tmux): system_monitory.py
RECOMMENDED: To have system automatically run and restart on reboot, etc.:
> Create file ~/.config/systemd/user/system-monitor.service and put the following in it:

[Unit]
Description=System Health Monitor
After=graphical-session.target

[Service]
Type=simple
ExecStart=%h/.venvs/v1/bin/python3 -u %h/scripts/system_monitor.py
Restart=on-failure
RestartSec=10
StandardOutput=append:%h/syslogs/system_monitor.log
StandardError=append:%h/syslogs/system_monitor.log

[Install]
WantedBy=default.target

> Copy this script to ~/scripts/ (or change the ExecStart line above to point to it).
> You can change the log file to wherever you want logs to be saved.
> systemctl --user daemon-reload
> systemctl --user enable system-monitor.service
> systemctl --user start system-monitor.service
> To check status and make sure it's running:
> systemctl --user status memory-monitor.service
"""

import argparse
import os
import psutil
import subprocess
import socket
import sys
import time
from datetime import datetime as dt
from pathlib import Path

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from util import email_utils

HOSTNAME = socket.gethostname()
CPU_COUNT = os.cpu_count()
CRITICAL_RATE_LIMIT = 120 # Only re-notify on critical every 2min
WARNING_RATE_LIMIT = 300 # Only re-notify on warning every 5min
CHECK_INTERVAL = 60  # Check every 60 seconds
NTFY_PRI_MAP = {"low": "low", "normal": "default", "critical": "high"}

# Thresholds
MEMORY_WARNING_PERCENT = 80  # Warn at 80% memory usage
MEMORY_CRITICAL_PERCENT = 85  # Critical at 85%
PRESSURE_WARNING = 30  # Memory pressure avg10 (% of last 10s spent reclaiming memory)
LOAD_WARNING = CPU_COUNT * 1.0 # Warn when number of waiting processes is 100% of cores
LOAD_CRITICAL = CPU_COUNT * 3.0 # Critical when number of waiting processes is 300% of cores
TEMP_WARNING = 85 # Warn at 85°C
TEMP_CRITICAL = 95 # Critical at 95°C

def log(message):
    """Print message with timestamp"""
    timestamp = dt.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] {message}")

def send_notification(title, message, urgency="normal", send_ntfy=False):
    """Send desktop notification or ntfy"""
    if send_ntfy:
        message = f"System Monitor Alert for {HOSTNAME}\n{message}"
        email_utils.send_ntfy_alert(msg=message, title=title, priority=NTFY_PRI_MAP[urgency])
    else:
        try:
            subprocess.run(
                ["notify-send", "-u", urgency, title, message],
                check=False
            )
        except FileNotFoundError:
            # notify-send not available, probably headless
            pass

def get_memory_usage():
    """Get current memory usage percentage"""
    with open("/proc/meminfo") as f:
        lines = f.readlines()

    mem_total = mem_available = 0
    for line in lines:
        if line.startswith("MemTotal:"):
            mem_total = int(line.split()[1])
        elif line.startswith("MemAvailable:"):
            mem_available = int(line.split()[1])

    used_percent = ((mem_total - mem_available) / mem_total) * 100
    return used_percent, mem_total // 1024, mem_available // 1024

def get_memory_pressure():
    """Get memory pressure for user.slice (where Firefox/apps run)"""
    pressure_file = Path("/sys/fs/cgroup/user.slice/memory.pressure")

    if not pressure_file.exists():
        return None

    try:
        with open(pressure_file) as f:
            for line in f:
                if line.startswith("some"):
                    # Format: some avg10=X.XX avg60=Y.YY avg300=Z.ZZ total=...
                    parts = line.split()
                    avg10 = float(parts[1].split('=')[1])
                    return avg10
    except (PermissionError, FileNotFoundError):
        return None

    return None

def get_top_memory_hogs(n=3):
    """Get top N memory-consuming processes"""
    result = subprocess.run(
        ["ps", "aux", "--sort=-%mem"],
        capture_output=True,
        text=True
    )

    lines = result.stdout.strip().split('\n')[1:]  # Skip header
    hogs = []
    for line in lines[:n]:
        parts = line.split()
        if len(parts) >= 11:
            user, pid, cpu, mem = parts[0], parts[1], parts[2], parts[3]
            cmd = ' '.join(parts[10:])[:50]  # Truncate command
            hogs.append(f"  {cmd}: {mem}% (PID {pid})")

    return '\n'.join(hogs)

def get_load_average():
    """Get 1, 5, and 15 minute load averages"""
    with open("/proc/loadavg") as f:
        loads = f.read().split()[:3]
        return [float(x) for x in loads]

def get_coretemps():
    """Get temperatures of cores and other system sensors"""
    temp_data = psutil.sensors_temperatures()
    sensor_to_temp = {}
    chip_names = ["coretemp", "k10temp", "nvme", "iwlwifi_1", "amdgpu"]
    for chip_name in chip_names:
        if chip_name in temp_data:
            for shwtemp in temp_data[chip_name]:
                sensor_to_temp[f"{chip_name}:{shwtemp.label}"] = shwtemp.current
    return sensor_to_temp

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ntfy-on-warn", action="store_true")
    parser.add_argument("--ntfy-on-critical", action="store_true")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    log("System monitor started")
    log(f"Memory warning threshold: {MEMORY_WARNING_PERCENT}%")
    log(f"Memory critical threshold: {MEMORY_CRITICAL_PERCENT}%")
    log(f"Memory pressure warning: {PRESSURE_WARNING}")
    log(f"CPU load warning: {LOAD_WARNING} ({CPU_COUNT} cores)")
    log(f"CPU load critical: {LOAD_CRITICAL} ({CPU_COUNT} cores)")
    log(f"Temp warning threshold: {TEMP_WARNING}")
    log(f"Temp critical threshold: {TEMP_CRITICAL}")
    log(f"Checking every {CHECK_INTERVAL}s...")

    last_warning_time = 0
    last_critical_time = 0
    check_count = 0

    while True:
        try:
            mem_percent, mem_total_mb, mem_avail_mb = get_memory_usage()
            pressure = get_memory_pressure()
            load_1min = get_load_average()[0]
            sensor_to_temp = get_coretemps()
            current_time = time.time()

            # Check memory usage
            if (mem_percent >= MEMORY_CRITICAL_PERCENT and
                current_time - last_critical_time > CRITICAL_RATE_LIMIT):
                hogs = get_top_memory_hogs()
                message = (f"Memory: {mem_percent:.1f}% ({mem_avail_mb}MB free)\n" +
                           f"Top processes:\n{hogs}")
                send_notification(
                    "⚠️ CRITICAL: Memory Almost Full!",
                    message,
                    "critical",
                    args.ntfy_on_critical
                )
                log(f"[CRITICAL] Memory at {mem_percent:.1f}%")
                last_critical_time = current_time

            elif (mem_percent >= MEMORY_WARNING_PERCENT and
                  current_time - last_warning_time > WARNING_RATE_LIMIT):
                hogs = get_top_memory_hogs()
                message = (f"Memory: {mem_percent:.1f}% ({mem_avail_mb}MB free)\n" +
                           f"Top processes:\n{hogs}")
                send_notification(
                    "⚠️ High Memory Usage",
                    message,
                    "normal",
                    args.ntfy_on_warn
                )
                log(f"[WARNING] Memory at {mem_percent:.1f}%")
                last_warning_time = current_time

            # Check memory pressure
            if (pressure and pressure >= PRESSURE_WARNING and
                current_time - last_warning_time > WARNING_RATE_LIMIT):
                message = f"Memory pressure: {pressure:.1f}\nSystem is thrashing!"
                send_notification(
                    "⚠️ High Memory Pressure",
                    message,
                    "normal",
                    args.ntfy_on_warn
                )
                log(f"[WARNING] Memory pressure at {pressure:.1f}")
                last_warning_time = current_time

            # Check CPU load
            if (load_1min >= LOAD_CRITICAL and
                current_time - last_critical_time > CRITICAL_RATE_LIMIT):
                message = f"Load: {load_1min:.1f} (cores: {CPU_COUNT})\nSystem overloaded!"
                send_notification("⚠️ CRITICAL: CPU Overloaded", message, "normal",
                                  args.ntfy_on_warn)
                log(f"[CRITICAL] CPU load at {load_1min:.1f} (cores: {CPU_COUNT})")
                last_critical_time = current_time

            elif (load_1min >= LOAD_WARNING and
                  current_time - last_warning_time > WARNING_RATE_LIMIT):
                message = f"Load: {load_1min:.1f} (cores: {CPU_COUNT})"
                send_notification("⚠️ High CPU Load", message, "normal", args.ntfy_on_warn)
                log(f"[WARNING] CPU load at {load_1min:.1f} (cores: {CPU_COUNT})")
                last_warning_time = current_time

            # Check core temps
            crit_sensors = []
            warn_sensors = []
            hottest_temp = float("-inf")
            hottest_core = None
            for sensor, temp in sorted(sensor_to_temp.items()):
                if temp >= TEMP_CRITICAL:
                    crit_sensors.append(sensor)
                elif temp >= TEMP_WARNING:
                    warn_sensors.append(sensor)
                if temp > hottest_temp:
                    hottest_temp = temp
                    hottest_core = sensor
            if crit_sensors and current_time - last_critical_time > CRITICAL_RATE_LIMIT:
                message = (f"Cores at critical temp:\n" +
                           '\n'.join([f"  {sensor}: {sensor_to_temp[sensor]}"
                                      for sensor in crit_sensors]))
                send_notification(
                    "⚠️ CRITICAL: Cores Overheating!",
                    message,
                    "critical",
                    args.ntfy_on_critical
                )
                log(f"[CRITICAL] Hottest core ({hottest_core}) at {hottest_temp}°C")
                last_critical_time = current_time
            elif warn_sensors and current_time - last_critical_time > WARNING_RATE_LIMIT:
                message = (f"Core temps elevated:\n" +
                           '\n'.join([f"  {sensor}: {sensor_to_temp[sensor]}"
                                      for sensor in warn_sensors]))
                send_notification(
                    "⚠️ Core Temps Elevated",
                    message,
                    "normal",
                    args.ntfy_on_warn
                )
                log(f"[WARNING] Hottest core ({hottest_core}) at {hottest_temp}°C")
                last_warning_time = current_time

            # Log status periodically (every 10 checks)
            if check_count % 10 == 0:
                pressure_str = f", pressure={pressure:.1f}" if pressure else ""
                memory_str = f"{mem_percent:.1f}% used ({mem_avail_mb}MB free{pressure_str})"
                load_str = f"CPU load at {load_1min:.1f} (cores: {CPU_COUNT})"
                temp_str = f"hottest core ({hottest_core}) at {hottest_temp}°C"
                log(f"Status: {memory_str} -- {load_str} -- {temp_str}")

            if args.once:
                return

            check_count += 1
            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            log("Monitor stopped")
            sys.exit(0)
        except Exception as e:
            log(f"Error: {e}")
            time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
