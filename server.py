"""MCP server for the RND 320-KA3005P bench power supply.

Transport chain:

    MCP client (stdio) -> this server -> SSH (paramiko) -> Raspberry Pi
        -> /dev/ttyACM1 (USB virtual COM port of the PSU)

Protocol facts established by live probing of an RND 320-KA3005P V2.0:

* Commands are ASCII, sent WITHOUT any line terminator.  A trailing \\n or
  \\r makes the unit ignore the command entirely.
* The baud rate is irrelevant (USB CDC-ACM) but 9600 8N1 raw is applied
  anyway so the tty is in a known state.
* Write commands produce no response and the unit needs ~0.2 s of idle
  time before it accepts the next command.
* Queries reply within ~0.3 s; ``STATUS?`` returns a single raw byte.
* STATUS byte: bit0 = mode (1=CV, 0=CC), bit4 = beeper, bit5 = OCP,
  bit6 = output on, bit7 = OVP.  0xFF means a protection trip is latched.
* ``OVP1`` (enable OVP remotely) ALWAYS trips protection on this firmware
  regardless of any threshold sent beforehand, so only ``OVP0`` is exposed.
* OCP works remotely: send the threshold (``OCP1:4.000``) then ``OCP1``.

Configuration (env vars override config.json, which overrides defaults):

    PSU_SSH_HOST      no default (required)
    PSU_SSH_PORT      default 22
    PSU_SSH_USER      no default (required)
    PSU_SSH_PASSWORD  no default (required)
    PSU_SERIAL_DEV    default /dev/ttyACM1
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import paramiko

try:  # MCP SDK 2.x
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:  # MCP SDK 1.x
    from mcp.server.fastmcp import FastMCP as Server

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_MAX_VOLTS = 32.0
_MAX_AMPS = 5.0
_CONFIG_FILE = Path(__file__).with_name("config.json")

_DEFAULTS = {
    "host": None,
    "port": 22,
    "user": None,
    "password": None,
    "serial_dev": "/dev/ttyACM1",
}


def _load_config() -> dict:
    cfg = dict(_DEFAULTS)
    if _CONFIG_FILE.is_file():
        with open(_CONFIG_FILE, encoding="utf-8") as f:
            cfg.update({k: v for k, v in json.load(f).items() if v})
    env_map = {
        "host": "PSU_SSH_HOST",
        "port": "PSU_SSH_PORT",
        "user": "PSU_SSH_USER",
        "password": "PSU_SSH_PASSWORD",
        "serial_dev": "PSU_SERIAL_DEV",
    }
    for key, var in env_map.items():
        if os.environ.get(var):
            cfg[key] = os.environ[var]
    cfg["port"] = int(cfg["port"])
    missing = [k for k in ("host", "user", "password") if not cfg.get(k)]
    if missing:
        raise RuntimeError(
            f"Missing SSH configuration: {', '.join(missing)}. "
            f"Set PSU_SSH_HOST / PSU_SSH_USER / PSU_SSH_PASSWORD env vars or create "
            f"{_CONFIG_FILE} (see config.example.json)."
        )
    return cfg


# ---------------------------------------------------------------------------
# PSU client: one persistent SSH connection, one exec channel per command
# ---------------------------------------------------------------------------

_VALID_CMD = re.compile(r"^[A-Za-z*0-9:.?]+$")


class PsuClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._ssh: paramiko.SSHClient | None = None

    # -- SSH plumbing -------------------------------------------------------

    def _connect(self):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(
            self.cfg["host"],
            port=self.cfg["port"],
            username=self.cfg["user"],
            password=self.cfg["password"],
            timeout=10,
            allow_agent=False,
            look_for_keys=False,
        )
        self._ssh = ssh

    def _exec(self, cmd: str, timeout: float = 15.0) -> str:
        with self._lock:
            for attempt in (0, 1):
                if self._ssh is None or self._ssh.get_transport() is None:
                    self._connect()
                try:
                    _, stdout, _ = self._ssh.exec_command(cmd, timeout=timeout)
                    return stdout.read().decode(errors="replace")
                except Exception:
                    # Stale/dead connection: drop it and retry once.
                    try:
                        if self._ssh is not None:
                            self._ssh.close()
                    except Exception:
                        pass
                    self._ssh = None
                    if attempt == 1:
                        raise
            return ""  # unreachable

    # -- Serial helpers -----------------------------------------------------

    def _stty(self) -> str:
        dev = self.cfg["serial_dev"]
        return f"stty -F {dev} 9600 cs8 -cstopb -parenb -ixon raw -echo; "

    def _send(self, cmd: str) -> None:
        if not _VALID_CMD.match(cmd):
            raise ValueError(f"Unsafe PSU command rejected: {cmd!r}")
        self._exec(self._stty() + f"printf '%s' '{cmd}' > {self.cfg['serial_dev']}")
        # The PSU needs a short idle gap after every command.
        time.sleep(0.2)

    def write(self, cmd: str) -> None:
        """Send a command that produces no response."""
        self._send(cmd)

    def query(self, cmd: str, wait: float = 0.4) -> str:
        """Send a query and collect the response.

        A short pre-drain discards stale bytes so a timed-out earlier read
        can never corrupt the next answer.  ``wait`` must never be 0:
        ``timeout 0 cat`` on GNU coreutils means *no* timeout and the cat
        process would hold the serial port forever.
        """
        wait = max(0.2, wait)
        dev = self.cfg["serial_dev"]
        shell = (
            self._stty()
            + f"timeout 0.05 cat {dev}; "
            + f"printf '%s' '{cmd}' > {dev}; "
            + f"timeout {wait} cat {dev} | head -c 64"
        )
        out = self._exec(shell, timeout=wait + 10)
        if not _VALID_CMD.match(cmd):
            raise ValueError(f"Unsafe PSU command rejected: {cmd!r}")
        return out

    # -- Typed helpers ------------------------------------------------------

    def query_float(self, cmd: str) -> float:
        raw = self.query(cmd).strip()
        m = re.match(r"[-+]?\d*\.?\d+", raw)
        if not m:
            raise RuntimeError(f"PSU did not answer {cmd!r} with a number (got {raw!r})")
        return float(m.group())

    def status_byte(self) -> int:
        raw = self.query("STATUS?")
        if not raw:
            raise RuntimeError("PSU did not answer STATUS?")
        return ord(raw[0])


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

mcp = Server("rnd-320-ka3005p")

_psu: PsuClient | None = None


def psu() -> PsuClient:
    global _psu
    if _psu is None:
        _psu = PsuClient(_load_config())
    return _psu


def _decode_status(b: int) -> str:
    lines = [f"status byte: 0x{b:02X}"]
    if b == 0xFF:
        lines.append("PROTECTION TRIPPED (latched) - disable OVP/OCP to clear")
        return "\n".join(lines)
    lines.append(f"mode: {'CV (constant voltage)' if b & 1 else 'CC (constant current)'}")
    lines.append(f"output: {'ON' if b & 0x40 else 'OFF'}")
    lines.append(f"ocp: {'enabled' if b & 0x20 else 'disabled'}")
    lines.append(f"ovp: {'enabled' if b & 0x80 else 'disabled'}")
    return "\n".join(lines)


@mcp.tool()
def identify() -> str:
    """Return the power supply identification string (*IDN?)."""
    return psu().query("*IDN?").strip()


@mcp.tool()
def get_status() -> str:
    """Read the power supply status byte: CV/CC mode, output on/off, OCP/OVP state, protection trip."""
    return _decode_status(psu().status_byte())


@mcp.tool()
def get_settings() -> str:
    """Read the configured setpoints: voltage limit (V) and current limit (A)."""
    v = psu().query_float("VSET1?")
    i = psu().query_float("ISET1?")
    return f"setpoints: voltage={v:.2f} V, current={i:.3f} A"


@mcp.tool()
def get_measurements() -> str:
    """Measure the actual output: voltage (V) and current (A) right now."""
    v = psu().query_float("VOUT1?")
    i = psu().query_float("IOUT1?")
    return f"measured: voltage={v:.2f} V, current={i:.3f} A"


@mcp.tool()
def set_voltage(volts: float) -> str:
    """Set the voltage setpoint (V). Range 0..32.00 V, resolution 10 mV.

    Does NOT switch the output on; use set_output separately.
    """
    if not 0 <= volts <= _MAX_VOLTS:
        return f"ERROR: voltage out of range 0..{_MAX_VOLTS} V (got {volts})"
    p = psu()
    p.write(f"VSET1:{volts:.2f}")
    return f"OK: voltage setpoint={p.query_float('VSET1?'):.2f} V"


@mcp.tool()
def set_current(amps: float) -> str:
    """Set the current limit (A). Range 0..5.000 A, resolution 1 mA.

    Does NOT switch the output on; use set_output separately.
    """
    if not 0 <= amps <= _MAX_AMPS:
        return f"ERROR: current out of range 0..{_MAX_AMPS} A (got {amps})"
    p = psu()
    p.write(f"ISET1:{amps:.3f}")
    return f"OK: current limit={p.query_float('ISET1?'):.3f} A"


@mcp.tool()
def set_output(on: bool) -> str:
    """Turn the DC output ON or OFF."""
    p = psu()
    p.write("OUT1" if on else "OUT0")
    b = p.status_byte()
    actual = bool(b & 0x40) if b != 0xFF else None
    if actual is not None and actual != on:
        return f"WARNING: output state mismatch (wanted {'ON' if on else 'OFF'}). {_decode_status(b)}"
    return f"OK: output {'ON' if on else 'OFF'}\n{_decode_status(b)}"


@mcp.tool()
def set_ocp(enabled: bool, threshold_amps: float | None = None) -> str:
    """Enable or disable over-current protection (OCP).

    When enabling, threshold_amps (0..5.000 A) is required and is sent to the
    unit immediately before the enable command.  If the load ever draws more
    than the threshold the output shuts off and the trip latches until OCP is
    disabled again.
    """
    p = psu()
    if enabled:
        if threshold_amps is None:
            return "ERROR: threshold_amps is required when enabling OCP"
        if not 0 < threshold_amps <= _MAX_AMPS:
            return f"ERROR: OCP threshold out of range 0..{_MAX_AMPS} A (got {threshold_amps})"
        p.write(f"OCP1:{threshold_amps:.3f}")
        p.write("OCP1")
        b = p.status_byte()
        state = "enabled" if b & 0x20 else "NOT enabled"
        return f"OK: OCP {state} at {threshold_amps:.3f} A\n{_decode_status(b)}"
    p.write("OCP0")
    return f"OK: OCP disabled\n{_decode_status(p.status_byte())}"


@mcp.tool()
def disable_ovp() -> str:
    """Disable OVP / clear a latched OVP protection trip.

    On this unit (firmware V2.0) enabling OVP remotely ALWAYS trips
    protection immediately, so remote enabling is deliberately not exposed.
    Configure OVP from the front panel if you need it.
    """
    p = psu()
    p.write("OVP0")
    return f"OK: OVP disabled / trip cleared\n{_decode_status(p.status_byte())}"


@mcp.tool()
def set_beeper(on: bool) -> str:
    """Turn the key-press beeper ON or OFF."""
    psu().write("BEEP1" if on else "BEEP0")
    return f"OK: beeper {'ON' if on else 'OFF'}"


@mcp.tool()
def save_settings(slot: int) -> str:
    """Store the current panel settings (V, I, output state) in memory slot 1..5."""
    if not 1 <= slot <= 5:
        return "ERROR: slot must be 1..5"
    psu().write(f"SAV{slot}")
    return f"OK: settings saved to slot {slot}"


@mcp.tool()
def recall_settings(slot: int) -> str:
    """Recall panel settings from memory slot 1..5.

    WARNING: this applies the stored voltage, current AND output state, so
    the output may switch on immediately.
    """
    if not 1 <= slot <= 5:
        return "ERROR: slot must be 1..5"
    p = psu()
    p.write(f"RCL{slot}")
    v = p.query_float("VSET1?")
    i = p.query_float("ISET1?")
    b = p.status_byte()
    return (f"OK: recalled slot {slot}: voltage={v:.2f} V, current={i:.3f} A, "
            f"output={'ON' if b & 0x40 else 'OFF'}")


@mcp.tool()
def log_current(duration_seconds: float = 10.0, interval_seconds: float = 1.0,
                include_samples: bool = False) -> str:
    """Log the output current (A) over time and report statistics.

    Samples IOUT1? every interval_seconds for duration_seconds, then returns
    min/max/avg/last current plus a sparkline of the trend.  Practical sample
    rate over the SSH-serial bridge is about 1 sample/second, so smaller
    intervals are clamped up automatically.  Duration range 1..300 s.
    """
    duration = min(max(duration_seconds, 1.0), 300.0)
    interval = min(max(interval_seconds, 1.0), 60.0)
    p = psu()
    start = time.monotonic()
    samples: list[float] = []
    while True:
        try:
            samples.append(p.query_float("IOUT1?"))
        except RuntimeError:
            pass  # keep logging through transient hiccups
        if time.monotonic() - start >= duration:
            break
        time.sleep(max(0.0, interval - (time.monotonic() - start) % interval))
    elapsed = time.monotonic() - start
    if not samples:
        return "ERROR: no current samples collected (is the PSU reachable?)"
    lo, hi = min(samples), max(samples)
    avg = sum(samples) / len(samples)
    blocks = " .:-=+*#%@"
    spark = "".join(
        blocks[min(int((v - lo) / (hi - lo) * (len(blocks) - 1)), len(blocks) - 1)]
        if hi > lo else blocks[len(blocks) // 2]
        for v in samples)
    lines = [
        f"current log: {len(samples)} samples over {elapsed:.1f} s",
        f"min={lo:.3f} A  max={hi:.3f} A  avg={avg:.3f} A  last={samples[-1]:.3f} A",
        f"trend: |{spark}|",
    ]
    if include_samples:
        lines += [f"t={t * interval:.1f}s I={v:.3f} A" for t, v in enumerate(samples)]
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
