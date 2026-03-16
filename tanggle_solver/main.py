"""Main entry point and CLI for the tanggle.io puzzle solver."""

import argparse
import asyncio
import logging
import sys


TANGGLE_BASE_URL = "https://tanggle.io/play/"


def resolve_puzzle_url(url_or_uuid: str) -> str:
    """Accept a full tanggle.io URL or just a UUID and return the full URL."""
    url_or_uuid = url_or_uuid.strip()
    if url_or_uuid.startswith("http://") or url_or_uuid.startswith("https://"):
        return url_or_uuid
    return TANGGLE_BASE_URL + url_or_uuid


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main():
    parser = argparse.ArgumentParser(
        description="Tanggle.io Automated Puzzle Solver",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Solve a puzzle by UUID
  tanggle-solver solve 25b55ea4-c8d6-4f44-8975-b84a5f9080a5

  # Solve by full URL
  tanggle-solver solve https://tanggle.io/play/25b55ea4-c8d6-4f44-8975-b84a5f9080a5

  # Capture WebSocket traffic for protocol analysis
  tanggle-solver capture 25b55ea4-c8d6-4f44-8975-b84a5f9080a5 --duration 30
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Solve command (WebSocket protocol-based)
    solve_parser = subparsers.add_parser("solve", help="Solve a puzzle via WebSocket protocol")
    solve_parser.add_argument("puzzle", help="Puzzle UUID or full tanggle.io URL")
    solve_parser.add_argument("--delay", type=float, default=0.5, help="Delay between moves (seconds)")
    solve_parser.add_argument("--cell-size", type=float, default=0, help="Override cell size in game units (0=auto)")
    solve_parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    # Logout command — clear saved session
    subparsers.add_parser("logout", help="Clear saved login session (to switch accounts)")

    # Capture command — analyze WebSocket protocol
    capture_parser = subparsers.add_parser("capture", help="Capture and analyze WebSocket protocol")
    capture_parser.add_argument("puzzle", help="Puzzle UUID or full tanggle.io URL")
    capture_parser.add_argument("--duration", type=int, default=15, help="Seconds to capture (default 15)")
    capture_parser.add_argument("--screenshots", default="screenshots", help="Directory for output")
    capture_parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    setup_logging(getattr(args, "verbose", False))

    # Logout doesn't need puzzle URL or credentials
    if args.command == "logout":
        run_logout()
        return

    # Resolve UUID or URL to a full URL
    args.url = resolve_puzzle_url(args.puzzle)
    print(f"Puzzle URL: {args.url}")

    # Load credentials from .env / environment
    from .config import load_credentials
    args.credentials = load_credentials()
    if args.credentials:
        print(f"Logged in as: {args.credentials.email}")
    else:
        print("No credentials configured (set TANGGLE_EMAIL/TANGGLE_PASSWORD in .env)")

    if args.command == "solve":
        asyncio.run(run_solve(args))
    elif args.command == "capture":
        asyncio.run(run_capture(args))


def run_logout():
    """Clear the persistent Chrome profile to reset login session."""
    import shutil
    from pathlib import Path

    profile_dir = Path.home() / ".tanggle-solver" / "chrome-profile"
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
        print(f"Cleared login session at {profile_dir}")
        print("Next run will prompt for login.")
    else:
        print("No saved session found.")


async def run_solve(args):
    """Run the WebSocket protocol-based solver."""
    from .ws_solver import WsSolver

    solver = WsSolver(
        puzzle_url=args.url,
        credentials=args.credentials,
        move_delay=args.delay,
        cell_size_override=args.cell_size,
    )
    await solver.solve()


async def run_capture(args):
    """Capture WebSocket traffic and analyze the game protocol."""
    import json
    from pathlib import Path
    from .browser import TanggleBrowser
    from .protocol import ProtocolAnalyzer, format_analysis

    browser = TanggleBrowser()
    await browser.launch()

    creds = args.credentials
    if creds:
        await browser.login(creds)

    print("\nNavigating to puzzle...")
    await browser.navigate_to_puzzle(args.url)
    await browser.wait_for_game_ready(timeout=30)

    print(f"Capturing WebSocket traffic for {args.duration} seconds...")
    print("(Try manually moving a piece in the browser to generate move messages)\n")

    import asyncio as _asyncio
    await _asyncio.sleep(args.duration)

    # Collect all captured messages
    messages = await browser.get_ws_messages()
    print(f"Captured {len(messages)} WebSocket messages\n")

    # Analyze the protocol
    analyzer = ProtocolAnalyzer()
    analyzer.add_messages(messages)

    summary = analyzer.analyze()
    print(format_analysis(summary))

    # Look for piece data
    print("\n--- Piece data analysis ---")
    piece_data = analyzer.find_piece_data()
    for i, pd in enumerate(piece_data):
        print(f"\nCandidate {i + 1} (length={pd['length']}):")
        print(f"  Header: {pd['header']}")
        if "float_sample" in pd:
            print(f"  Float32 LE sample: {pd['float_sample'][:10]}")
            print(f"  Reasonable floats: {pd.get('reasonable_floats', 0)}/{pd.get('float32_count', 0)}")
        if "float_be_sample" in pd:
            print(f"  Float32 BE reasonable: {pd.get('float32_be_reasonable', 0)}")
        for key, val in pd.items():
            if key.startswith("possible_records"):
                print(f"  {key}: {val} records")

    # Save raw data for further analysis
    out_dir = Path(args.screenshots)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_path = out_dir / "ws_capture.json"
    with open(raw_path, "w") as f:
        json.dump(messages, f, indent=2)
    print(f"\nRaw messages saved to {raw_path}")

    await browser.close()


if __name__ == "__main__":
    main()