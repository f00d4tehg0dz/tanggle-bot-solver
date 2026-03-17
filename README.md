# Tanggle Bot Solver

Automated jigsaw puzzle solver for [tanggle.io](https://tanggle.io). Intercepts the game's WebSocket protocol to read piece data and send move commands directly.

<img width="1200" height="1194" alt="image" src="https://github.com/user-attachments/assets/7b851a97-9a73-431d-80af-7c5a47d147a6" />

[Blog Post about how it works in more depth](https://www.adrianchrysanthou.com/blog/how-i-built-an-automated-jigsaw-puzzle-solver-for-tanggle-io-with-claude-s-help) 

## How It Works

The solver reverse-engineers tanggle.io's WebSocket protocol (MessagePack) to control the game programmatically:

1. **Browser launch** — Opens your installed Chrome via Playwright with a persistent profile (so login sessions survive between runs)
2. **Login** — Checks for an existing session; if not logged in, opens the login page with credentials pre-filled for you to complete the Cloudflare challenge
3. **WebSocket interception** — Patches `WebSocket.prototype.send` to capture all game traffic between the client and server
4. **Game state extraction** — Decodes the initial MessagePack state message to get all piece positions, grid dimensions (`meta`), and board boundaries (`border`)
5. **Target computation** — Maps each piece ID to its grid cell (`col = id % cols`, `row = id // cols`) and computes target coordinates centered in the board area
6. **BFS placement** — Places pieces in breadth-first order starting from piece 0, so each piece references an already-placed neighbor for snapping
7. **Protocol commands** — For each piece, sends the full pickup/move/drop cycle via WebSocket:
   - `[1, 1]` — mouse down
   - `[2, piece_id, 0, 20]` — pick up piece
   - `[0, target_x, target_y]` — move cursor
   - `[4, target_x, target_y, neighbor_id, None]` — drop near neighbor (triggers server-side snap)
   - `[1, 0]` — mouse up

### Why WebSocket instead of Computer Vision?

An earlier version used screenshots + OpenCV to detect pieces and match them by color. This approach failed because:
- Pieces are ~15px on screen — not enough data for reliable matching
- Frame detection breaks across different puzzle states (empty vs partial vs complete)
- Mouse drag on empty space pans the viewport, destroying all coordinates
- Color matching is unreliable for puzzles with similar-colored regions

The WebSocket approach bypasses all of this by talking directly to the game server.

## Setup

Requires Python 3.10+ and Chrome installed.

```bash
pip install -r requirements.txt
playwright install chromium
```

### Login

Copy `.env.example` to `.env` and fill in your tanggle.io credentials:

```bash
cp .env.example .env
```

```
TANGGLE_EMAIL=your-email@example.com
TANGGLE_PASSWORD=your-password
```

Tanggle.io uses Cloudflare protection, so login is semi-manual on first run: the solver opens the login page with your credentials pre-filled, then you complete the Cloudflare challenge and click Login. The solver detects the successful login and continues automatically. Subsequent runs skip login (session persists in the Chrome profile at `~/.tanggle-solver/chrome-profile/`).

## Usage

### Solve a puzzle

```bash
# By UUID
python -m tanggle_solver.main solve 2xxxxxx4-cxx6-4xxx-xxxxx

# By full URL
python -m tanggle_solver.main solve https://tanggle.io/play/2xxxxxx4-cxx6-4xxx-xxxxx

# With options
python -m tanggle_solver.main solve <uuid> --delay 0.2 --cell-size 52 -v
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--delay SEC` | 0.5 | Pause between piece moves (lower = faster but may get rate-limited) |
| `--cell-size N` | 52 | Grid cell size in game units (0 = auto) |
| `--vpn PROVIDER` | off | VPN provider for IP rotation on 403 blocks (`openvpn`, `pia`, `nordvpn`) |
| `--vpn-dir DIR` | — | Directory containing `.ovpn` files (required when `--vpn openvpn`) |
| `-v, --verbose` | off | Enable debug logging |

### VPN Rotation (IP Block Bypass)

If tanggle.io blocks your IP with a 403 Forbidden, the solver can automatically rotate through VPN servers and retry. Three providers are supported:

**Private Internet Access (PIA)**

Requires the [PIA desktop app](https://www.privateinternetaccess.com/) installed with the CLI enabled.

```bash
python -m tanggle_solver.main solve <uuid> --vpn pia
```

Rotates through ~26 PIA regions (US, Canada, UK, EU, Asia-Pacific). The solver calls `piactl` to switch regions automatically.

**NordVPN**

Requires the [NordVPN desktop app](https://nordvpn.com/) installed.

```bash
python -m tanggle_solver.main solve <uuid> --vpn nordvpn
```

Rotates through ~20 NordVPN countries. The solver calls the `nordvpn` CLI to switch servers automatically.

**Raw OpenVPN Configs**

Drop `.ovpn` files into a directory and point the solver at it. Requires [OpenVPN](https://openvpn.net/) installed and an elevated terminal (admin privileges needed for TUN/TAP).

```bash
python -m tanggle_solver.main solve <uuid> --vpn openvpn --vpn-dir ./vpn
```

**How it works:** When the solver navigates to the puzzle and gets a 403, it disconnects the current VPN, connects to the next server/config, relaunches the browser with the new IP, and retries. This repeats until a connection succeeds or all servers are exhausted.

### Switch accounts

Clear the saved Chrome session to log in with a different account:

```bash
python -m tanggle_solver.main logout
```

This deletes the persistent Chrome profile at `~/.tanggle-solver/chrome-profile/`. The next run will prompt for a fresh login.

### Capture WebSocket traffic

Useful for debugging or reverse-engineering the protocol further:

```bash
python -m tanggle_solver.main capture <uuid> --duration 30 -v
```

During capture, manually move pieces in the browser. The captured MessagePack messages are saved to `screenshots/ws_capture.json`.

## Project Structure

```
tanggle_solver/
├── main.py            # CLI entry point with subcommands
├── config.py          # Credential loading from .env
├── browser.py         # Playwright browser control + WebSocket hooking
├── ws_solver.py       # WebSocket protocol-based solver
├── vpn.py             # VPN manager (OpenVPN, PIA, NordVPN) with rotation
└── protocol.py        # Protocol analyzer for captured WS traffic
vpn/                   # Drop .ovpn files here for OpenVPN rotation
```

## Protocol Reference

Tanggle.io uses MessagePack. ey message types:

| Direction | Format | Meaning |
|-----------|--------|---------|
| Server→Client | `[1, {uuid, pieces, meta, border, ...}]` | Initial game state |
| Client→Server | `[0, x, y]` | Cursor position update |
| Client→Server | `[1, state]` | Mouse button (1=down, 0=up) |
| Client→Server | `[2, piece_id, 0, 20]` | Pick up piece |
| Client→Server | `[4, x, y, target_entity, group]` | Drop piece |
| Server→Client | `[4, player, seq, x, y, entity, group]` | Drop confirmed (group = negative int if snapped) |

The `meta` field is `[cols, rows]` and piece IDs map to grid positions: `col = id % cols`, `row = id // cols`.

## Limitations

- Cell size (52 game units) is empirically determined and may vary for different puzzle configurations — use `--cell-size` to adjust
- The board origin calculation assumes the puzzle is centered in the border area — this works for all tested puzzles but edge cases may exist
- Cloudflare login requires manual completion on first run
- The solver places all pieces to their computed positions; if the cell size is slightly off, pieces will be close but may not snap
- VPN rotation with OpenVPN requires admin/elevated terminal privileges for TUN/TAP interface creation
- PIA and NordVPN rotation require their respective desktop apps to be installed and logged in