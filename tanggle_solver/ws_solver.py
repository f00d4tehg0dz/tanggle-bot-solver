"""WebSocket protocol-based puzzle solver for tanggle.io.

Reads piece positions directly from the game server via MessagePack
WebSocket messages, then sends pick-up and move commands to place
pieces in their correct grid positions. No CV, no screenshots needed.
"""

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Optional

import msgpack

from .browser import TanggleBrowser
from .config import TanggleCredentials
from .vpn import VpnProvider, create_vpn

logger = logging.getLogger(__name__)


@dataclass
class Piece:
    piece_id: int
    x: float
    y: float
    is_placed: bool
    group_id: int
    target_x: float = 0.0
    target_y: float = 0.0


@dataclass
class BoardInfo:
    cols: int
    rows: int
    border: list  # [x, y, w, h] or [x1, y1, x2, y2]
    cell_w: float = 0.0
    cell_h: float = 0.0
    origin_x: float = 0.0
    origin_y: float = 0.0


class WsSolver:
    """Solves tanggle.io puzzles using the WebSocket protocol directly."""

    def __init__(self, puzzle_url: str, credentials: Optional[TanggleCredentials] = None,
                 move_delay: float = 0.5, cell_size_override: float = 0,
                 vpn_provider: Optional[str] = None, vpn_dir: Optional[str] = None):
        self.puzzle_url = puzzle_url
        self.credentials = credentials
        self.move_delay = move_delay
        self.cell_size_override = cell_size_override
        self.browser = TanggleBrowser()
        self.pieces: list[Piece] = []
        self.board: Optional[BoardInfo] = None
        self._state_id: int = 0
        self.vpn: Optional[VpnProvider] = None
        if vpn_provider:
            self.vpn = create_vpn(vpn_provider, vpn_dir)

    async def solve(self):
        """Full solving pipeline using WebSocket protocol.

        If a VPN directory was provided and the site returns 403 Forbidden,
        the solver rotates to the next .ovpn config and retries automatically.
        """
        try:
            # Connect to VPN first if configured
            if self.vpn and self.vpn.has_configs:
                logger.info("VPN rotation enabled — connecting to first VPN...")
                if not await self.vpn.connect_next():
                    logger.error("Failed to connect to any VPN")
                    return

            # Launch and navigate (with VPN rotation on 403)
            await self.browser.launch()
            if self.credentials:
                await self.browser.login(self.credentials)

            status = await self.browser.navigate_to_puzzle(self.puzzle_url)

            # Rotate VPN on 403 Forbidden
            while status == 403 and self.vpn and self.vpn.has_configs:
                logger.warning(
                    f"IP blocked (403). Rotating VPN... "
                    f"({self.vpn.configs_remaining} configs remaining)"
                )
                if not await self.vpn.connect_next():
                    logger.error("All VPN configs exhausted — cannot bypass 403")
                    return

                # Close and relaunch browser to use the new IP
                await self.browser.close()
                await asyncio.sleep(2)
                self.browser = TanggleBrowser()
                await self.browser.launch()
                if self.credentials:
                    await self.browser.login(self.credentials)
                status = await self.browser.navigate_to_puzzle(self.puzzle_url)

            if status == 403:
                logger.error("Still blocked after all VPN rotations")
                return

            await self.browser.wait_for_game_ready(timeout=30)

            # Wait a moment for WebSocket messages to accumulate
            logger.info("Waiting for game state from WebSocket...")
            await asyncio.sleep(3)

            # Read the game state from captured WebSocket messages
            if not await self._read_game_state():
                logger.error("Failed to read game state from WebSocket")
                return

            # Compute target positions for all pieces
            self._compute_targets()

            # Place all unplaced pieces
            unplaced = [p for p in self.pieces if not p.is_placed]
            logger.info(f"Placing {len(unplaced)} unplaced pieces...")

            # Build pieces outward: start with piece 0, then its neighbors, etc.
            # This ensures each piece can reference an already-placed neighbor
            placed_set = set()
            queue = self._build_placement_order()

            placed = 0
            failed_streak = 0
            for i, (piece, neighbor_id) in enumerate(queue):
                success = await self._place_piece(piece, neighbor_id)
                if success:
                    placed += 1
                    placed_set.add(piece.piece_id)
                    failed_streak = 0
                else:
                    failed_streak += 1
                    if failed_streak >= 10:
                        logger.error("10 consecutive failures — stopping")
                        break

                if (i + 1) % 50 == 0:
                    logger.info(f"  Progress: {i + 1}/{len(queue)} attempted, {placed} placed")

                # Randomize delay between pieces to appear more human
                jitter = self.move_delay * random.uniform(0.5, 1.8)
                await asyncio.sleep(jitter)

                # Occasionally pause longer (like a human thinking)
                if random.random() < 0.08:
                    think_pause = random.uniform(1.0, 3.5)
                    logger.debug(f"  Simulating think pause: {think_pause:.1f}s")
                    await asyncio.sleep(think_pause)

            logger.info(f"Done! Placed {placed}/{len(queue)} pieces")

        except Exception as e:
            logger.error(f"Solver error: {e}", exc_info=True)
            raise
        finally:
            await self.browser.close()
            if self.vpn:
                await self.vpn.cleanup()

    async def _read_game_state(self) -> bool:
        """Read the initial game state from captured WebSocket messages."""
        messages = await self.browser.get_ws_messages()
        logger.info(f"Got {len(messages)} WebSocket messages")

        for msg in messages:
            if msg.get("direction") != "in":
                continue
            data = msg.get("data")
            if not isinstance(data, list) or len(data) < 100:
                continue

            try:
                raw = bytes(data)
                decoded = msgpack.unpackb(raw, raw=False)

                if not isinstance(decoded, (list, tuple)) or len(decoded) < 2:
                    continue

                state = decoded[1]
                if not isinstance(state, dict):
                    continue

                if "pieces" not in state or "meta" not in state:
                    continue

                # Found the game state!
                pieces_data = state["pieces"]
                meta = state["meta"]
                border = state.get("border", [0, 0, 0, 0])
                self._state_id = state.get("stateId", 0)

                cols, rows = meta[0], meta[1]
                self.board = BoardInfo(
                    cols=cols, rows=rows, border=border,
                )

                self.pieces = []
                for p in pieces_data:
                    self.pieces.append(Piece(
                        piece_id=p[0],
                        x=p[1],
                        y=p[2],
                        is_placed=bool(p[3]),
                        group_id=p[4] if p[4] is not None else 0,
                    ))

                unplaced = sum(1 for p in self.pieces if not p.is_placed)
                logger.info(
                    f"Game state loaded: {len(self.pieces)} pieces, "
                    f"{cols}x{rows} grid, {unplaced} unplaced, "
                    f"border={border}"
                )
                return True

            except Exception as e:
                logger.debug(f"Failed to decode message: {e}")
                continue

        return False

    def _compute_targets(self):
        """Compute target (solved) positions for each piece on the board.

        Piece IDs map to grid positions: col = id % cols, row = id // cols.
        The board area is defined by the border field.
        """
        if not self.board:
            return

        cols = self.board.cols
        rows = self.board.rows
        bx1, by1, bx2, by2 = self.board.border

        # border is [x_min, y_min, x_max, y_max]
        # The puzzle board is centered in this area
        center_x = (bx1 + bx2) / 2
        center_y = (by1 + by2) / 2

        if self.cell_size_override > 0:
            cell_size = self.cell_size_override
        else:
            # Empirically determined: cell size ≈ 52 game units for tanggle.io
            # This can be overridden via --cell-size if needed
            cell_size = 52.0

        board_w = cell_size * cols
        board_h = cell_size * rows

        # Center the board in the border area
        origin_x = center_x - board_w / 2
        origin_y = center_y - board_h / 2

        self.board.cell_w = cell_size
        self.board.cell_h = cell_size
        self.board.origin_x = origin_x
        self.board.origin_y = origin_y

        for piece in self.pieces:
            col = piece.piece_id % cols
            row = piece.piece_id // cols
            piece.target_x = origin_x + (col + 0.5) * cell_size
            piece.target_y = origin_y + (row + 0.5) * cell_size

        logger.info(
            f"Board: origin=({origin_x:.1f}, {origin_y:.1f}), "
            f"cell={cell_size:.1f}, "
            f"size=({board_w:.1f}x{board_h:.1f})"
        )

        # Log first few target positions for debugging
        for p in self.pieces[:3]:
            col = p.piece_id % cols
            row = p.piece_id // cols
            logger.info(
                f"  Piece {p.piece_id}: grid ({col},{row}) -> "
                f"target ({p.target_x:.1f}, {p.target_y:.1f})"
            )

    async def _send_ws(self, data: list) -> bool:
        """Send a msgpack-encoded message through the game WebSocket."""
        packed = msgpack.packb(data)
        byte_list = list(packed)
        result = await self.browser.execute_js(
            f"""() => {{
                const ws = window.__tanggleSolverWs;
                if (!ws || ws.readyState !== 1) return 'closed';
                ws.send(new Uint8Array({byte_list}).buffer);
                return 'ok';
            }}"""
        )
        return result == "ok"

    def _build_placement_order(self) -> list[tuple]:
        """Build placement order: BFS from piece 0, each piece references its neighbor.

        Returns list of (piece, neighbor_piece_id) tuples.
        The first piece has neighbor_id=None (placed on canvas).
        """
        cols = self.board.cols
        piece_map = {p.piece_id: p for p in self.pieces}
        unplaced_ids = {p.piece_id for p in self.pieces if not p.is_placed}

        order = []
        visited = set()
        # Start from piece 0 (top-left corner)
        start_id = 0
        if start_id not in unplaced_ids:
            # Find any unplaced piece
            start_id = next(iter(unplaced_ids))

        queue = [(start_id, None)]  # (piece_id, neighbor_id)
        visited.add(start_id)

        while queue:
            pid, neighbor = queue.pop(0)
            if pid in unplaced_ids and pid in piece_map:
                order.append((piece_map[pid], neighbor))

            # Add grid neighbors (up, down, left, right)
            col = pid % cols
            row = pid // cols
            for dc, dr in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                nc, nr = col + dc, row + dr
                if 0 <= nc < cols and 0 <= nr < self.board.rows:
                    nid = nr * cols + nc
                    if nid not in visited:
                        visited.add(nid)
                        queue.append((nid, pid))

        logger.info(f"Placement order: {len(order)} pieces (BFS from piece {start_id})")
        return order

    async def _place_piece(self, piece: Piece, neighbor_id: Optional[int] = None) -> bool:
        """Place a single piece using the full protocol cycle.

        Protocol:
        1. [1, 1]                                    — mouse down
        2. [2, piece_id, 0, 20]                      — pick up piece
        3. [0, target_x, target_y]                   — move cursor to target
        4. [4, target_x, target_y, neighbor, group]  — drop near neighbor
        5. [1, 0]                                    — mouse up

        The neighbor_id tells the server which piece we're connecting to.
        """
        # Mouse down
        ok = await self._send_ws([1, 1])
        if not ok:
            logger.warning(f"Piece {piece.piece_id}: WS not ready")
            return False

        await asyncio.sleep(random.uniform(0.06, 0.18))

        # Pick up the piece
        await self._send_ws([2, piece.piece_id, 0, 20])
        await asyncio.sleep(random.uniform(0.08, 0.25))

        # Move cursor to target
        await self._send_ws([0, piece.target_x, piece.target_y])
        await asyncio.sleep(random.uniform(0.08, 0.25))

        # Drop the piece — specify the neighbor to trigger snap
        drop_target = neighbor_id if neighbor_id is not None else "canvas"
        await self._send_ws([4, piece.target_x, piece.target_y, drop_target, None])
        await asyncio.sleep(random.uniform(0.06, 0.18))

        # Mouse up
        await self._send_ws([1, 0])
        await asyncio.sleep(random.uniform(0.06, 0.18))

        return True