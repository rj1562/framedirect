# Architecture

How the kiosk is put together, how data moves through it, and what happens
from power-on to the live screen. Diagrams render on GitHub; the source is
plain text in this file.

## 1. The big picture

One small computer behind the TV, one program drawing the screen, a handful
of small helper services around it. There is no browser, window manager or
desktop.

```mermaid
flowchart LR
    subgraph Cloud["myindoor.global (cloud)"]
        CFG["Widget settings API<br/>layout, thresholds, branding links"]
        TS["Readings API<br/>latest values, weekly averages"]
    end

    subgraph Pi["Raspberry Pi Zero 2 W (behind the TV)"]
        API["api_client<br/>polls the APIs"]
        SNAP[("latest.json<br/>last good snapshot")]
        ASSETS[("assets cache<br/>background, logo, QR")]
        R["kiosk_renderer<br/>draws every frame"]
        FB["/dev/fb0<br/>framebuffer"]
        SPLASH["kiosk-splash +<br/>console guard"]
        WD["watchdogs<br/>network, WiFi scan,<br/>tamper, HDMI-CEC"]
        LOGS[("log files")]
    end

    TV["TV / monitor<br/>(HDMI)"]

    CFG -- every 30 min --> API
    TS -- every minute --> API
    API --> SNAP
    API --> LOGS
    SNAP --> R
    CFG -. image links .-> ASSETS
    ASSETS --> R
    R --> FB
    SPLASH --> FB
    FB --> TV
    WD --> LOGS
```

- **The kiosk only reads.** It never sends data to myindoor.global; it
  fetches the same two JSON endpoints the web widget uses.
- **The snapshot file decouples network from screen.** A slow or dead network
  never freezes the display: the renderer always draws from the last good
  snapshot on disk, which also survives reboots.
- **Branding is cached locally.** Images are downloaded once and reused, so
  the kiosk keeps its look offline.

## 2. Software components

```mermaid
flowchart TB
    subgraph systemd["systemd services"]
        S1["kiosk-splash.service<br/>root · oneshot · very early"]
        S2["kiosk-console-guard.service<br/>root · always running"]
        S3["kiosk-renderer.service<br/>user pi · always running"]
        S4["net-watchdog.timer"]
        S5["wifi-scan-watchdog.timer"]
        S6["tamper-watchdog.service<br/>once per boot"]
        S7["cec-watchdog.timer<br/>(optional)"]
    end

    subgraph code["renderer/ (Python)"]
        KR["kiosk_renderer.py<br/>main loop · framebuffer · boot screen ·<br/>indoor + outdoor layout"]
        IL["indoor_layout.py<br/>indoor-only layout"]
        WR["widget_rules.py<br/>readings order · units · grades · colours"]
        AC["api_client.py<br/>polling · snapshot · logs"]
        CA["client_assets.py<br/>branding download + cache"]
        KS["kiosk_splash.py<br/>boot image · console guard"]
    end

    S1 --> KS
    S2 --> KS
    S3 --> KR
    KR --> IL
    KR --> AC
    KR --> CA
    IL --> WR
```

| Piece | Runs as | Job |
|---|---|---|
| `kiosk-renderer` | `pi` | Draws the screen about once a second; runs the API poller in a background thread; downloads branding in the background when it changes. |
| `kiosk-splash` | root, once | Hides the text console and paints the boot image as early as possible. Imports nothing heavy, so it's ready in under a second. |
| `kiosk-console-guard` | root, always | Keeps the console in graphics mode for the whole uptime (the login service resets it at ~45 s). |
| watchdogs | root, timers | Network health (reboots after a long outage), nearby WiFi log, boot/tamper history, optional TV wake over HDMI-CEC. |

Root is used only where Linux requires it (the console device is root-only).
The renderer itself runs as the unprivileged `pi` user, with scoped sudoers
rules for exactly two commands (restart the renderer, reboot).

## 3. From power-on to live screen

```mermaid
sequenceDiagram
    autonumber
    participant FW as Firmware
    participant K as Linux kernel
    participant SP as kiosk-splash
    participant G as console guard
    participant GT as login service (getty)
    participant R as kiosk-renderer
    participant S as Screen

    FW->>S: black (rainbow splash disabled)
    FW->>K: start kernel (~9 s in)
    K->>S: early framebuffer appears, stays black (fbcon=map:1 keeps kernel text off it)
    SP->>S: console → graphics mode, paint boot image (~11 s)
    K->>S: real display driver takes over, screen wiped (~15 s)
    SP->>S: repaint boot image (~0.2 s later)
    G->>G: start watching the console
    GT->>S: resets console to text mode (~45 s)
    G->>S: back to graphics mode within 50 ms
    G->>R: "redraw now"
    R->>S: boot image, then live widget after the first good API reply
```

The boot image is pre-rendered by the renderer into raw framebuffer files
(one for the early 32-bit framebuffer, one for the real 16-bit one), so the
splash only has to copy bytes.

## 4. Where the data comes from and how often

```mermaid
flowchart LR
    A["every minute, on the minute"] --> P{"fetch readings"}
    B["~20 s after each expected<br/>5-minute sensor update"] --> P
    P -- OK --> C{"values changed?"}
    C -- yes --> D["log 'data changed after Ns'<br/>(flag gaps > 2 intervals)"]
    C -- no --> E["keep"]
    D --> F[("latest.json")]
    E --> F
    P -- failed --> H["retry after 15 s, 30 s, 60 s"]
    H --> P
```

- The API gives no "measured at" time, so an update is recognised by the
  numbers changing; the kiosk learns the rhythm from that.
- If every live value is empty, or it's outside working hours, the screen
  shows last week's averages instead (same rule as the web widget).
- After 15 minutes without a successful reply, the screen adds
  "Last update: over 15 minutes ago" (and "over 1 hour ago" later).

## 5. Choosing what to draw

```mermaid
flowchart TB
    Q{"installer answer:<br/>show outdoor?"}
    Q -- "no" --> IN["Indoor-only layout"]
    Q -- "yes" --> CMP["Indoor + outdoor layout"]
    Q -- "not set (older installs)" --> W{"widget layout"}
    W -- "TYPE_INDOOR" --> IN
    W -- "TYPE_INDOOR_OUTDOOR(_CMP)<br/>or has an outdoor station" --> CMP
    IN --> BG{"background image set?"}
    BG -- "yes" --> PH["client photo under 50% black,<br/>content on a dark panel"]
    BG -- "no" --> COL["colour field by grade:<br/>green good · orange fair · purple bad"]
```

Grades and colours come from the widget's own settings:

- **Overall grade** (good / fair / bad) from the widget's main species
  (usually AQI) and its two thresholds.
- **Each reading's colour tick** from that species' own thresholds, e.g.
  temperature is good in the middle band and fair/poor on both sides.
- Widgets on the national 0–500 AQI scale (`is_aqi_500`) use the fixed
  national thresholds instead.

## 6. Branding

```mermaid
flowchart LR
    I["installer asks, per image:<br/>show? which one?"] --> M{"choice"}
    M -- "widget's own" --> WG["follow the dashboard<br/>(re-checked every 30 min)"]
    M -- "other link / file" --> FX["fixed image"]
    M -- "none" --> NO["not shown"]
    WG --> DL["download · check it's an image ·<br/>convert SVG · cache"]
    FX --> DL
    NO --> RM["remove from cache"]
    DL --> USE["widget + boot screen"]
```

## 7. Safety rules built into the design

- **Boot files are touched as little as possible.** The only boot-line
  change is one added word, `fbcon=map:1`. It was proven with Raspberry Pi's
  `tryboot` first (one test boot; a power cycle returns to normal), then made
  permanent; the original is kept as `cmdline.txt.orig`. `console=tty1` is
  never removed and the login service is never disabled; those changes
  caused hangs in earlier builds.
- **Everything restarts itself.** The renderer and the guard restart
  automatically if they ever exit; the network watchdog reboots the Pi after
  a long outage and tags that reboot so it isn't mistaken for tampering.
- **Nothing depends on the network to show something.** Offline, the screen
  keeps the last good readings and says how old they are.

## 8. Planned: CPCB outdoor data

For widgets without their own outdoor station, the outdoor panel can show the
nearest official CPCB monitoring station, from the Government of India
open-data API (data.gov.in). Research is done, not built yet: see
[cpcb-data-reference.md](cpcb-data-reference.md).

```mermaid
flowchart LR
    PIN["installer: PIN code"] --> GEO["PIN → location<br/>(OpenStreetMap)"]
    GEO --> NEAR["5 nearest CPCB stations<br/>with distance + current AQI"]
    NEAR --> PICK["pick one<br/>(+2 backups)"]
    PICK --> HOURLY["kiosk fetches hourly<br/>(~HH:50)"]
    HOURLY --> PANEL["outdoor panel:<br/>AQI · band · main pollutant ·<br/>'Source: CPCB'"]
```
