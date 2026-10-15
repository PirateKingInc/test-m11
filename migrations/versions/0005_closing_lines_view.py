"""v_closing_lines: one closing line per source/game/book/market/outcome

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# A source's own closing line (nflverse, CFBD) wins; otherwise the last polled state at or
# before kickoff. Snapshots are change-only, so the latest row before kickoff *is* the state
# at kickoff. `fetched_at` says how fresh that was; check it before trusting a close.
# game_outcome is in the game's home/away orientation (see v_odds_snapshots).
V_CLOSING_LINES = """
CREATE VIEW v_closing_lines AS
WITH ranked AS (
    SELECT
        s.snapshot_id, s.source, s.source_game_id, s.book_id, s.market_id, s.outcome,
        s.line, s.price_american, s.price_decimal, s.price_status, s.fetched_at,
        s.snapshot_kind, m.game_id, m.home_away_swapped, g.start_time_utc,
        ROW_NUMBER() OVER (
            PARTITION BY s.source, s.source_game_id, s.book_id, s.market_id, s.outcome,
                         s.snapshot_kind
            ORDER BY s.fetched_at DESC, s.snapshot_id DESC
        ) AS rn
    FROM odds_snapshots s
    JOIN game_source_ids m ON m.source = s.source AND m.source_game_id = s.source_game_id
    JOIN games g ON g.game_id = m.game_id
    WHERE s.snapshot_kind = 'source_closing'
       OR (s.snapshot_kind = 'poll' AND s.fetched_at <= g.start_time_utc)
)
SELECT
    r.game_id, r.source, r.source_game_id, r.book_id, r.market_id, r.outcome,
    CASE
        WHEN r.home_away_swapped AND r.outcome = 'home' THEN 'away'
        WHEN r.home_away_swapped AND r.outcome = 'away' THEN 'home'
        ELSE r.outcome
    END AS game_outcome,
    r.line, r.price_american, r.price_decimal, r.price_status, r.fetched_at,
    r.start_time_utc,
    CASE WHEN r.snapshot_kind = 'source_closing' THEN 'source' ELSE 'last_poll_before_start' END
        AS closing_basis
FROM ranked r
WHERE r.rn = 1
  AND (
    r.snapshot_kind = 'source_closing'
    OR NOT EXISTS (
        SELECT 1 FROM odds_snapshots c
        WHERE c.snapshot_kind = 'source_closing'
          AND c.source = r.source AND c.source_game_id = r.source_game_id
          AND c.book_id = r.book_id AND c.market_id = r.market_id AND c.outcome = r.outcome
    )
  )
"""


def upgrade() -> None:
    op.execute(V_CLOSING_LINES)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_closing_lines")
