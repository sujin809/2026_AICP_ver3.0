from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from twinmarket_kr.agents.memory_agent import MemoryAgent
from twinmarket_kr.community.validation import validate_post_body
from twinmarket_kr.db.connection import connect, init_sim_db


ANIMAL_CODES = ["황소", "곰", "독수리", "여우", "늑대", "사자", "호랑이", "코끼리", "펭귄", "돌고래"]


class CommunityAgent:
    def __init__(self, db_path: Path | str) -> None:
        self._db = db_path
        init_sim_db(self._db)

    def generate_anonymous_code(self, agent_id: str) -> str:
        h = int(hashlib.md5(str(agent_id).encode()).hexdigest(), 16)
        animal = ANIMAL_CODES[h % len(ANIMAL_CODES)]
        number = h % 9000 + 1000
        return f"{animal}-{number}"

    def save_post(
        self,
        agent_id: str,
        turn: int,
        date: str,
        post_type: str,
        title: str,
        content: str,
        source_ltb_id: str | None = None,
        source_fill_id: str | None = None,
        source_decision_id: str | None = None,
    ) -> int:
        content = validate_post_body(content)
        anonymous_code = self.generate_anonymous_code(agent_id)
        with connect(self._db) as conn:
            cur = conn.execute(
                """
                INSERT INTO community_posts (
                    agent_id, anonymous_code, turn, date, post_type, title, content,
                    source_ltb_id, source_fill_id, source_decision_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    anonymous_code,
                    int(turn),
                    date,
                    post_type,
                    title,
                    content,
                    source_ltb_id,
                    source_fill_id,
                    source_decision_id,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def get_today_posts(self, date: str) -> list[dict[str, Any]]:
        with connect(self._db) as conn:
            rows = conn.execute(
                """
                SELECT post_id, agent_id, anonymous_code, post_type, title,
                       like_count, unlike_count, score
                FROM community_posts
                WHERE date = ?
                ORDER BY post_id
                """,
                (date,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_post_content(self, post_id: int) -> dict[str, Any]:
        with connect(self._db) as conn:
            row = conn.execute("SELECT * FROM community_posts WHERE post_id = ?", (int(post_id),)).fetchone()
        return dict(row) if row else {}

    def get_author_profile(self, author_agent_id: str, memory_agent: MemoryAgent, turn: int) -> dict[str, Any]:
        portfolio = memory_agent._latest_portfolio(author_agent_id, before_or_at_turn=turn)
        portfolio_summary: dict[str, Any] = {}
        if portfolio:
            raw_positions = portfolio["positions"]
            portfolio_summary = {
                "turn": int(portfolio["turn"]),
                "date": str(portfolio["date"]),
                "cash": float(portfolio["cash"]),
                "positions": json.loads(raw_positions) if isinstance(raw_positions, str) else raw_positions,
                "total_value": float(portfolio["total_value"]),
                "realized_pnl": float(portfolio["realized_pnl"]),
                "total_return_rate": float(portfolio["total_return_rate"]),
            }
        recent_trades = self._get_recent_trades(
            author_agent_id,
            n=3,
            before_or_at_turn=turn,
        )
        return {
            "snapshot_turn": int(turn),
            "portfolio_summary": portfolio_summary,
            "recent_trades": recent_trades,
        }

    def _get_recent_trades(
        self,
        agent_id: str,
        n: int = 3,
        *,
        before_or_at_turn: int,
    ) -> list[dict[str, Any]]:
        with connect(self._db) as conn:
            rows = conn.execute(
                """
                SELECT turn, date, action, stock_code, quantity,
                       executed_price, trade_value, status, filled_quantity
                FROM trade_log
                WHERE agent_id = ?
                  AND turn <= ?
                  AND status = 'filled'
                  AND filled_quantity > 0
                ORDER BY turn DESC, rowid DESC
                LIMIT ?
                """,
                (agent_id, int(before_or_at_turn), int(n)),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_reaction(self, agent_id: str, post_id: int, turn: int, date: str, reaction: str) -> bool:
        normalized = "none" if reaction == "read" else reaction
        if normalized not in {"like", "unlike", "none"}:
            normalized = "none"
        with connect(self._db) as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO community_interactions (
                    agent_id, post_id, turn, date, reaction
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (agent_id, int(post_id), int(turn), date, normalized),
            )
            conn.commit()
            return cur.rowcount > 0

    def update_post_score_live(self, post_id: int, reaction: str) -> None:
        if reaction == "like":
            sql = "UPDATE community_posts SET like_count = like_count + 1, score = score + 1 WHERE post_id = ?"
        elif reaction == "unlike":
            sql = "UPDATE community_posts SET unlike_count = unlike_count + 1, score = score - 1 WHERE post_id = ?"
        else:
            return
        with connect(self._db) as conn:
            conn.execute(sql, (int(post_id),))
            conn.commit()

    def mark_best_posts(self, date: str, n: int) -> list[dict[str, Any]]:
        with connect(self._db) as conn:
            rows = conn.execute(
                """
                SELECT post_id, title, post_type, score
                FROM community_posts
                WHERE date = ?
                ORDER BY score DESC, like_count DESC, post_id ASC
                LIMIT ?
                """,
                (date, int(n)),
            ).fetchall()
            best_ids = [int(row["post_id"]) for row in rows]
            conn.execute("UPDATE community_posts SET is_best = 0 WHERE date = ?", (date,))
            if best_ids:
                placeholders = ",".join("?" * len(best_ids))
                conn.execute(f"UPDATE community_posts SET is_best = 1 WHERE post_id IN ({placeholders})", best_ids)
            conn.commit()
        return [dict(row) for row in rows]

    def freeze_best_posts(
        self,
        *,
        date: str,
        turn: int,
        n: int,
        memory_agent: MemoryAgent,
        badges: dict[str, list[str]],
    ) -> list[dict[str, Any]]:
        """Freeze the globally ranked Best board at the PM information boundary."""
        ranked_posts = self.mark_best_posts(date, n)
        frozen: list[dict[str, Any]] = []
        for rank, ranked in enumerate(ranked_posts, start=1):
            post = self.get_post_content(int(ranked["post_id"]))
            if not post:
                raise RuntimeError(f"Best post disappeared before freeze: {ranked['post_id']}")
            author_agent_id = str(post["agent_id"])
            body = str(post.get("content") or "")
            frozen.append(
                {
                    "rank": rank,
                    "post_id": int(post["post_id"]),
                    "post_type": str(post.get("post_type") or ""),
                    "title": str(post.get("title") or ""),
                    "content": body,
                    "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    "score": int(post.get("score") or 0),
                    "like_count": int(post.get("like_count") or 0),
                    "unlike_count": int(post.get("unlike_count") or 0),
                    "author_agent_id": author_agent_id,
                    "anonymous_code": str(post.get("anonymous_code") or ""),
                    "author_badges": list(badges.get(author_agent_id, [])),
                    "author_profile_snapshot": self.get_author_profile(
                        author_agent_id,
                        memory_agent,
                        turn,
                    ),
                    "snapshot_turn": int(turn),
                    "snapshot_date": str(date),
                    "exposure_level": "full_body",
                    "is_best": True,
                }
            )
        return frozen

    def project_best_posts_for_reader(
        self,
        frozen_best_posts: list[dict[str, Any]],
        *,
        recipient_agent_id: str,
        depth: int,
    ) -> list[dict[str, Any]]:
        """Apply self-exclusion and depth-specific public profile visibility."""
        if depth not in {0, 1, 2}:
            raise ValueError(f"unsupported community depth: {depth}")
        projected: list[dict[str, Any]] = []
        for post in frozen_best_posts:
            if str(post.get("author_agent_id")) == str(recipient_agent_id):
                continue
            public_post = {
                key: value
                for key, value in post.items()
                if key not in {"author_agent_id", "author_profile_snapshot"}
            }
            public_post["author_profile"] = (
                post.get("author_profile_snapshot") if depth == 2 else None
            )
            public_post["profile_scope"] = "detailed" if depth == 2 else "minimal"
            public_post["self_exclusion_policy"] = "exclude_author_no_backfill"
            projected.append(public_post)
        return projected

    def save_community_log(
        self,
        agent_id: str,
        turn: int,
        date: str,
        best_posts: list[dict[str, Any]],
        posts_read: list[dict[str, Any]],
        thinking: str | dict[str, Any],
        candidate_posts_seen: list[dict[str, Any]] | None = None,
    ) -> None:
        thinking_text = (
            json.dumps(thinking, ensure_ascii=False, sort_keys=True)
            if isinstance(thinking, dict)
            else str(thinking)
        )
        with connect(self._db) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO community_logs (
                    agent_id, turn, date, best_posts_seen, posts_read,
                    candidate_posts_seen, community_thinking
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    int(turn),
                    date,
                    json.dumps(best_posts, ensure_ascii=False),
                    json.dumps(posts_read, ensure_ascii=False),
                    json.dumps(candidate_posts_seen or [], ensure_ascii=False),
                    thinking_text,
                ),
            )
            conn.commit()

    def update_community_thinking(
        self,
        agent_id: str,
        turn: int,
        thinking: str | dict[str, Any],
    ) -> None:
        thinking_text = (
            json.dumps(thinking, ensure_ascii=False, sort_keys=True)
            if isinstance(thinking, dict)
            else str(thinking)
        )
        with connect(self._db) as conn:
            conn.execute(
                """
                UPDATE community_logs
                SET community_thinking = ?
                WHERE agent_id = ? AND turn = ?
                """,
                (thinking_text, agent_id, int(turn)),
            )
            conn.commit()

    def get_community_log(self, agent_id: str, turn: int) -> dict[str, Any] | None:
        with connect(self._db) as conn:
            row = conn.execute(
                "SELECT * FROM community_logs WHERE agent_id = ? AND turn = ?",
                (agent_id, int(turn)),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["best_posts_seen"] = json.loads(data.get("best_posts_seen") or "[]")
        data["posts_read"] = json.loads(data.get("posts_read") or "[]")
        data["candidate_posts_seen"] = json.loads(
            data.get("candidate_posts_seen") or "[]"
        )
        raw_thinking = data.get("community_thinking")
        if isinstance(raw_thinking, str) and raw_thinking.strip():
            try:
                data["community_thinking"] = json.loads(raw_thinking)
            except json.JSONDecodeError:
                data["community_thinking"] = raw_thinking
        return data
