# RND 320-KA3005P — MCP Server (LLM-controllable bench PSU)

Turn an **RND 320-KA3005P** lab power supply (0–32 V, 0–5 A) into a set of MCP
tools that any LLM (Kilo, Claude Desktop, Cursor, ...) can call:

```
┌─────────┐  MCP/stdio  ┌──────────┐    SSH     ┌───────────┐   USB serial   ┌─────────────┐
│ LLM /   │ ──────────> │ server.py│ ─────────> │ Raspberry │ ─────────────> │ RND 320-    │
│ client  │             │ (Win/Mac/│            │ Pi (or any│  /dev/ttyACM1  │ KA3005P PSU │
└─────────┘             │ Linux)   │            │ Linux box)│                └─────────────┘
                        └──────────┘            └───────────┘
```

The PSU enumerates as a USB virtual COM port (`0416:5011` Winbond/Nuvoton).
Its serial port is attached to a Raspberry Pi; the server drives it over SSH
using plain shell I/O (`stty` + `printf`/`cat`), so **nothing needs to be
installed on the Pi** — the SSH user only needs to be in the `dialout` group.

Once running, you can literally ask your assistant things like
*"set the PSU to 5 V, 500 mA and turn the output on"*.

---

## Step 1 — Install the server

Requires Python 3.10+ on the machine your MCP client runs on.

```bash
git clone <this repo>
cd RND-320-KA3005P-MCP
pip install -r requirements.txt
python server.py --help        # optional sanity check (starts stdio server)
```

## Step 2 — Find the PSU on the Pi

SSH into the Pi and identify the serial device:

```bash
lsusb | grep -i 0416:5011          # Winbond Virtual Com Port = the PSU
ls -l /dev/serial/by-id/           # shows which ttyACM* it maps to
groups | grep dialout              # your SSH user needs dialout access
```

Typical result: `/dev/ttyACM0` or `/dev/ttyACM1`.

## Step 3 — Configure credentials

The server needs three values: Pi address, SSH user, SSH password.
**No credentials are stored in this repo.** They come from either
environment variables or a local `config.json` (gitignored):

```bash
cp config.example.json config.json
# then edit config.json:
#   host        e.g. "192.168.x.x" or "benchpi.local"
#   user        SSH username on the Pi
#   password    SSH password
#   serial_dev  from Step 2, default /dev/ttyACM1
```

Environment variables override `config.json`:

| Variable           | Default       | Meaning                    |
|--------------------|---------------|----------------------------|
| `PSU_SSH_HOST`     | — (required)  | Pi the PSU hangs on        |
| `PSU_SSH_USER`     | — (required)  | SSH user (needs `dialout`) |
| `PSU_SSH_PASSWORD` | — (required)  | SSH password               |
| `PSU_SSH_PORT`     | `22`          | SSH port                   |
| `PSU_SERIAL_DEV`   | `/dev/ttyACM1`| PSU serial device on Pi    |

## Step 4 — Register the server with your MCP client

**Kilo** — project `.kilo/kilo.jsonc` (or global `~/.config/kilo/kilo.json`):

```jsonc
{
  "mcp": {
    "rnd-320-ka3005p": {
      "type": "local",
      "command": ["python", "/absolute/path/to/RND 320-KA3005P-MCP/server.py"],
      "enabled": true
    }
  }
}
```

**Claude Desktop** — `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "rnd-320-ka3005p": {
      "command": "python",
      "args": ["/absolute/path/to/RND 320-KA3005P-MCP/server.py"]
    }
  }
}
```

Restart the client and check the server appears (in Kilo: `/mcps`).

## Step 5 — First test drive

Ask your assistant to run, in order:

1. `identify` — should answer `RND 320-KA3005P Vx.x`
2. `get_settings` / `get_measurements` — read setpoints and actual output
3. `set_voltage(5.0)` + `set_current(0.5)` + `set_output(true)` — power a test load
4. `log_current(duration_seconds=30)` — watch the load current over time
5. `set_output(false)` — done

You can also exercise the server without any LLM:

```bash
python -c "import server; print(server.identify()); print(server.get_settings())"
```

---

## Tools

| Tool               | Purpose                                                      |
|--------------------|--------------------------------------------------------------|
| `identify`         | `*IDN?` identification string                                |
| `get_status`       | CV/CC mode, output state, OCP/OVP state, protection trip     |
| `get_settings`     | Configured V/I setpoints                                     |
| `get_measurements` | Actual output voltage/current                                |
| `log_current`      | Sample current over 1–300 s: min/max/avg/last + trend sparkline |
| `set_voltage`      | 0–32.00 V (verified by readback)                             |
| `set_current`      | 0–5.000 A current limit (verified by readback)               |
| `set_output`       | Output ON/OFF (verified via status byte)                     |
| `set_ocp`          | OCP enable (threshold required) / disable                    |
| `disable_ovp`      | Disable OVP / clear a latched protection trip                |
| `set_beeper`       | Key beeper ON/OFF                                            |
| `save_settings`    | Store panel settings in memory slot 1–5                      |
| `recall_settings`  | Recall slot 1–5 (⚠ may switch the output on)                 |

## Safety design

- Setpoints are range-checked (0–32 V, 0–5 A) and **rejected**, not clamped.
- `set_voltage`/`set_current` never switch the output on — output power is a
  separate, explicit `set_output` call.
- Every write is verified (readback / status byte).
- Commands are allow-listed (regex) before they reach the device.

## Troubleshooting

| Symptom                       | Fix                                                          |
|-------------------------------|--------------------------------------------------------------|
| `Missing SSH configuration`   | Create `config.json` or export the `PSU_SSH_*` variables     |
| All queries return `''`       | Another process holds the port: `pkill -f 'cat /dev/ttyACM*'` on the Pi; also verify `PSU_SERIAL_DEV` |
| `PSU did not answer ...`      | Wrong device or PSU unplugged — recheck Step 2               |
| Status byte `0xFF`            | Protection trip latched — call `disable_ovp`                 |
| Want OVP enabled?             | Do it from the front panel: remote `OVP1` always trips instantly on firmware V2.0 |

## Protocol reference (Korad-style ASCII, validated on firmware V2.0)

Commands from `RND_320-KA_Control_Commands_eng_tds.pdf`, behavior verified live:

- `VSET1:12.00`, `ISET1:0.500` — setpoints; `VSET1?`, `ISET1?` — read back
- `VOUT1?`, `IOUT1?` — measurements; `*IDN?` — identity
- `OUT0`/`OUT1`, `BEEP0`/`BEEP1`, `OCP0`/`OCP1` (+ `OCP1:4.000` threshold), `OVP0`
- `SAV1..5` / `RCL1..5` — memory slots; `STATUS?` — one raw status byte

Quirks found by probing the actual unit:

- Commands are ASCII **without any line terminator** — a trailing `\n`/`\r`
  makes the unit ignore the command.
- The baud rate is irrelevant (USB CDC); 9600 8N1 raw is applied anyway.
- The unit needs ~0.2 s idle between commands; writes return no response.
- `STATUS?` byte: bit0 = CV(1)/CC(0), bit4 = beeper, bit5 = OCP,
  bit6 = output, bit7 = OVP. `0xFF` = protection trip latched.
- Remote `OVP1` always trips protection immediately regardless of threshold.

## Repo layout

```
server.py             MCP server (single file, stdio transport)
config.json           your credentials (gitignored, created in Step 3)
config.example.json   template for config.json
requirements.txt      Python dependencies (mcp, paramiko)
RND supplied software/ original vendor software + command-reference PDFs
```
