"""WebSocket protocol analyzer and solver for tanggle.io.

Instead of using CV to match tiny piece screenshots, this module intercepts
the game's WebSocket traffic to read piece data and send move commands
directly. This works for any puzzle regardless of size, color, or state.
"""

import logging
import struct
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class GamePiece:
    """A puzzle piece with its current and target positions."""
    piece_id: int
    current_x: float = 0.0
    current_y: float = 0.0
    target_x: float = 0.0  # Where it should go (grid position)
    target_y: float = 0.0
    is_placed: bool = False
    group_id: int = -1


@dataclass
class GameState:
    """Current state of the puzzle game extracted from WebSocket data."""
    pieces: list[GamePiece] = field(default_factory=list)
    grid_cols: int = 0
    grid_rows: int = 0
    board_x: float = 0.0
    board_y: float = 0.0
    board_width: float = 0.0
    board_height: float = 0.0
    raw_messages: list[dict] = field(default_factory=list)


class ProtocolAnalyzer:
    """Analyzes captured WebSocket messages to understand the game protocol."""

    def __init__(self):
        self.messages: list[dict] = []
        self.state = GameState()

    def add_messages(self, messages: list[dict]):
        """Add captured WebSocket messages for analysis."""
        self.messages.extend(messages)
        self.state.raw_messages.extend(messages)
        logger.info(f"Added {len(messages)} messages (total: {len(self.messages)})")

    def analyze(self) -> dict:
        """Analyze all captured messages and return a summary."""
        in_msgs = [m for m in self.messages if m.get("direction") == "in"]
        out_msgs = [m for m in self.messages if m.get("direction") == "out"]

        summary = {
            "total": len(self.messages),
            "incoming": len(in_msgs),
            "outgoing": len(out_msgs),
            "message_types": {},
        }

        # Analyze message formats
        for msg in self.messages:
            data = msg.get("data")
            direction = msg.get("direction", "?")

            if isinstance(data, list):
                # Binary message (array of bytes)
                length = len(data)
                first_byte = data[0] if data else None
                key = f"{direction}_binary_{first_byte}_{length}"

                if key not in summary["message_types"]:
                    summary["message_types"][key] = {
                        "count": 0,
                        "direction": direction,
                        "first_byte": first_byte,
                        "lengths": [],
                        "sample": data[:50],
                    }
                summary["message_types"][key]["count"] += 1
                summary["message_types"][key]["lengths"].append(length)

            elif isinstance(data, str):
                # Text message
                key = f"{direction}_text"
                if key not in summary["message_types"]:
                    summary["message_types"][key] = {
                        "count": 0,
                        "direction": direction,
                        "samples": [],
                    }
                summary["message_types"][key]["count"] += 1
                if len(summary["message_types"][key]["samples"]) < 3:
                    summary["message_types"][key]["samples"].append(data[:200])

        return summary

    def find_piece_data(self) -> list[dict]:
        """Try to find piece position data in the captured messages.

        Look for the largest incoming binary message — it likely contains
        the initial game state with all piece positions.
        """
        candidates = []

        for msg in self.messages:
            data = msg.get("data")
            if msg.get("direction") != "in" or not isinstance(data, list):
                continue

            candidates.append({
                "length": len(data),
                "first_byte": data[0] if data else None,
                "data": data,
            })

        # Sort by length descending — largest messages likely have piece data
        candidates.sort(key=lambda c: c["length"], reverse=True)

        results = []
        for c in candidates[:5]:
            analysis = self._analyze_binary(c["data"])
            analysis["length"] = c["length"]
            analysis["first_byte"] = c["first_byte"]
            results.append(analysis)

        return results

    def _analyze_binary(self, data: list[int]) -> dict:
        """Analyze a binary message for structure patterns."""
        raw = bytes(data)
        result = {
            "raw_length": len(raw),
            "header": list(raw[:20]),
        }

        # Try to decode as different formats
        # Look for float32 sequences (piece positions are likely floats)
        if len(raw) >= 8:
            try:
                float_count = len(raw) // 4
                floats = struct.unpack(f"<{float_count}f", raw[:float_count * 4])
                # Check if they look like coordinates (reasonable range)
                reasonable = [f for f in floats if -10000 < f < 10000 and f != 0]
                result["float32_count"] = float_count
                result["reasonable_floats"] = len(reasonable)
                result["float_sample"] = list(floats[:20])
            except struct.error:
                pass

        # Try big-endian floats
        if len(raw) >= 8:
            try:
                float_count = len(raw) // 4
                floats = struct.unpack(f">{float_count}f", raw[:float_count * 4])
                reasonable = [f for f in floats if -10000 < f < 10000 and f != 0]
                result["float32_be_reasonable"] = len(reasonable)
                result["float_be_sample"] = list(floats[:20])
            except struct.error:
                pass

        # Try to find repeated structures (piece records)
        # If pieces have fixed-size records, we'll see a pattern
        if len(raw) > 100:
            # Look for common record sizes by checking auto-correlation
            for record_size in [8, 12, 16, 20, 24, 28, 32]:
                if len(raw) % record_size == 0:
                    n_records = len(raw) // record_size
                    if n_records > 10:
                        result[f"possible_records_{record_size}b"] = n_records

        return result

    def decode_move_message(self, data: list[int]) -> Optional[dict]:
        """Try to decode a piece move message from outgoing WS data."""
        raw = bytes(data)

        # Small outgoing messages are likely move commands
        if len(raw) < 4 or len(raw) > 100:
            return None

        result = {
            "raw": list(raw),
            "length": len(raw),
        }

        # Try various interpretations
        if len(raw) >= 12:
            # Could be: [msg_type(1-4 bytes)] [piece_id(2-4 bytes)] [x(4 bytes)] [y(4 bytes)]
            for offset in [0, 1, 2, 4]:
                remaining = raw[offset:]
                if len(remaining) >= 8:
                    try:
                        x, y = struct.unpack("<ff", remaining[:8])
                        if -10000 < x < 10000 and -10000 < y < 10000:
                            result[f"float_pair_at_{offset}"] = (x, y)
                    except struct.error:
                        pass

        return result


def format_analysis(summary: dict) -> str:
    """Format a protocol analysis summary for display."""
    lines = [
        f"Total messages: {summary['total']}",
        f"  Incoming: {summary['incoming']}",
        f"  Outgoing: {summary['outgoing']}",
        "",
        "Message types:",
    ]

    for key, info in sorted(summary["message_types"].items()):
        lines.append(f"  {key}:")
        lines.append(f"    Count: {info['count']}")
        if "lengths" in info:
            lengths = info["lengths"]
            lines.append(f"    Lengths: min={min(lengths)}, max={max(lengths)}, avg={sum(lengths)/len(lengths):.0f}")
            lines.append(f"    Sample bytes: {info['sample']}")
        if "samples" in info:
            for s in info["samples"]:
                lines.append(f"    Sample: {s}")

    return "\n".join(lines)