"""SQLite persistence: observed profiles, bounded memory, crash-safe budget.

All money amounts are integer nano-USD (1 USD = 1,000,000,000 nano), so documented
cost examples reproduce exactly and no float rounding can leak into the ledger.
Budget rules are conservative by construction: a reservation charges the maximum
possible cost the moment it is created, actual usage replaces it only at settlement,
and reservations that never settle (crash, provider failure) stay charged at maximum
until independently reconciled.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from playerbot_claude import protocol

CONVERSATION_TURN_LIMIT = 20
NANO_PER_USD = 1_000_000_000


def usd_to_nano(usd: float) -> int:
    return round(usd * NANO_PER_USD)


def nano_to_usd_string(nano: int) -> str:
    """Exact decimal rendering without float artifacts (2900000 -> "0.0029")."""

    sign = "-" if nano < 0 else ""
    whole, fraction = divmod(abs(nano), NANO_PER_USD)
    digits = f"{fraction:09d}".rstrip("0")
    return f"{sign}{whole}.{digits}" if digits else f"{sign}{whole}"


@dataclass(frozen=True)
class PriceSnapshot:
    """Per-token prices in nano-USD, snapshotted into every ledger row."""

    input_nano_per_token: int
    output_nano_per_token: int

    @classmethod
    def from_usd_per_mtok(cls, input_usd: float, output_usd: float) -> PriceSnapshot:
        # 1 USD per million tokens == 1000 nano-USD per token.
        return cls(
            input_nano_per_token=round(input_usd * 1000),
            output_nano_per_token=round(output_usd * 1000),
        )

    def cost_nano(self, input_tokens: int, output_tokens: int) -> int:
        return input_tokens * self.input_nano_per_token + output_tokens * self.output_nano_per_token


_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    bot_guid INTEGER PRIMARY KEY,
    profile_version INTEGER NOT NULL,
    crafting_affinity INTEGER NOT NULL,
    exploration_affinity INTEGER NOT NULL,
    sociability INTEGER NOT NULL,
    voice TEXT NOT NULL,
    bot_name TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_guid INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_bot ON conversation_turns (bot_guid, id);

CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    max_cost_nano INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('reserved', 'submitted', 'settled')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL REFERENCES reservations (id),
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    input_nano_per_token INTEGER NOT NULL,
    output_nano_per_token INTEGER NOT NULL,
    cost_nano INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Storage:
    """Single-connection SQLite store. Callers serialize access (the sidecar's
    generation lock); WAL journaling makes each transaction crash-durable."""

    def __init__(self, path: str) -> None:
        self._connection = sqlite3.connect(path)
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.executescript(_SCHEMA)
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    # --- Observed trusted profiles ---

    def record_profile(self, request: protocol.ChatRequest) -> None:
        with self._connection:
            self._connection.execute(
                """
                INSERT INTO profiles (bot_guid, profile_version, crafting_affinity,
                                      exploration_affinity, sociability, voice, bot_name,
                                      updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (bot_guid) DO UPDATE SET
                    profile_version = excluded.profile_version,
                    crafting_affinity = excluded.crafting_affinity,
                    exploration_affinity = excluded.exploration_affinity,
                    sociability = excluded.sociability,
                    voice = excluded.voice,
                    bot_name = excluded.bot_name,
                    updated_at = excluded.updated_at
                """,
                (
                    request.bot_guid,
                    request.profile_version,
                    request.crafting_affinity,
                    request.exploration_affinity,
                    request.sociability,
                    request.voice,
                    request.bot_name,
                    _now(),
                ),
            )

    def get_profile(self, bot_guid: int) -> dict[str, object] | None:
        row = self._connection.execute(
            """
            SELECT profile_version, crafting_affinity, exploration_affinity, sociability,
                   voice, bot_name, updated_at
            FROM profiles WHERE bot_guid = ?
            """,
            (bot_guid,),
        ).fetchone()
        if row is None:
            return None

        return {
            "profile_version": row[0],
            "crafting_affinity": row[1],
            "exploration_affinity": row[2],
            "sociability": row[3],
            "voice": row[4],
            "bot_name": row[5],
            "updated_at": row[6],
        }

    # --- Bounded conversation memory ---

    def append_turn(self, bot_guid: int, role: str, content: str) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO conversation_turns (bot_guid, role, content, created_at) VALUES (?, ?, ?, ?)",
                (bot_guid, role, content, _now()),
            )
            self._connection.execute(
                """
                DELETE FROM conversation_turns
                WHERE bot_guid = ? AND id NOT IN (
                    SELECT id FROM conversation_turns WHERE bot_guid = ?
                    ORDER BY id DESC LIMIT ?
                )
                """,
                (bot_guid, bot_guid, CONVERSATION_TURN_LIMIT),
            )

    def recent_turns(self, bot_guid: int) -> list[tuple[str, str]]:
        rows = self._connection.execute(
            "SELECT role, content FROM conversation_turns WHERE bot_guid = ? ORDER BY id ASC",
            (bot_guid,),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    # --- Crash-safe budget ledger ---

    def reserve(
        self,
        request_id: int,
        input_tokens: int,
        max_output_tokens: int,
        prices: PriceSnapshot,
        budget_nano: int,
    ) -> int | None:
        """Charges the maximum possible cost up front, inside one transaction.

        Returns the reservation id, or None when the maximum cost cannot fit in
        what remains of the budget (spent plus outstanding reservations).
        """

        max_cost = prices.cost_nano(input_tokens, max_output_tokens)
        with self._connection:
            committed = self.spent_nano() + self.outstanding_nano()
            if committed + max_cost > budget_nano:
                return None

            cursor = self._connection.execute(
                """
                INSERT INTO reservations (request_id, input_tokens, max_output_tokens,
                                          max_cost_nano, state, created_at)
                VALUES (?, ?, ?, ?, 'reserved', ?)
                """,
                (request_id, input_tokens, max_output_tokens, max_cost, _now()),
            )
            return cursor.lastrowid

    def mark_submitted(self, reservation_id: int) -> None:
        with self._connection:
            self._connection.execute(
                "UPDATE reservations SET state = 'submitted' WHERE id = ? AND state = 'reserved'",
                (reservation_id,),
            )

    def settle(
        self,
        reservation_id: int,
        input_tokens: int,
        output_tokens: int,
        prices: PriceSnapshot,
    ) -> None:
        """Atomically replaces the maximum charge with actual usage."""

        cost = prices.cost_nano(input_tokens, output_tokens)
        with self._connection:
            self._connection.execute(
                "UPDATE reservations SET state = 'settled' WHERE id = ?",
                (reservation_id,),
            )
            self._connection.execute(
                """
                INSERT INTO usage_log (reservation_id, input_tokens, output_tokens,
                                       input_nano_per_token, output_nano_per_token,
                                       cost_nano, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reservation_id,
                    input_tokens,
                    output_tokens,
                    prices.input_nano_per_token,
                    prices.output_nano_per_token,
                    cost,
                    _now(),
                ),
            )

    def spent_nano(self) -> int:
        row = self._connection.execute("SELECT COALESCE(SUM(cost_nano), 0) FROM usage_log").fetchone()
        return int(row[0])

    def outstanding_nano(self) -> int:
        """Unsettled reservations, charged at their maximum cost."""

        row = self._connection.execute(
            "SELECT COALESCE(SUM(max_cost_nano), 0) FROM reservations"
            " WHERE state IN ('reserved', 'submitted')"
        ).fetchone()
        return int(row[0])
