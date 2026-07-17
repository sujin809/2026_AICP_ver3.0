from __future__ import annotations

import csv
import json
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import config


class SimulationLogger:
    def __init__(
        self,
        *,
        root_dir: Path | str = config.LOG_DIR,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        overwrite_root: bool = False,
        append_existing: bool = False,
    ) -> None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.run_id = run_id or f"simulation_{timestamp}_{os.getpid()}"
        root = Path(root_dir)
        if overwrite_root and root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)
        self.run_dir = root / self.run_id
        if self.run_dir.exists() and not append_existing:
            shutil.rmtree(self.run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._update_current_pointer(root)
        self._lock = threading.Lock()
        self._agent_csv_fields = [
            "run_id",
            "date",
            "turn",
            "subturn",
            "agent_id",
            "decision_date",
            "market_features_date",
            "news_start_date",
            "news_start_time",
            "news_max_date",
            "news_end_time",
            "execution_date",
            "information_mode",
            "news_depth",
            "selected_news_count",
            "read_news_count",
            "search_read_count",
            "fake_exposed",
            "fake_base_count",
            "fake_read_count",
            "fake_search_count",
            "fake_selected_count",
            "fake_public_ids",
            "fake_synthetic_ids",
            "fake_related_events",
            "depth2_search_keywords",
            "depth2_search_result_count",
            "depth2_view_change",
            "news_sentiment",
            "action",
            "quantity",
            "submitted_order",
            "belief_summary",
            "view_change",
            "decision_reason",
            "risk_control",
            "order_corrections",
        ]
        self._orders_csv_fields = [
            "run_id",
            "date",
            "turn",
            "subturn",
            "agent_id",
            "decision_date",
            "market_features_date",
            "news_start_date",
            "news_start_time",
            "news_max_date",
            "news_end_time",
            "execution_date",
            "information_mode",
            "stock_code",
            "action",
            "quantity",
            "reason",
        ]
        self._fills_csv_fields = [
            "run_id",
            "date",
            "turn",
            "subturn",
            "stock_code",
            "agent_id",
            "action",
            "quantity",
            "executed_price",
            "status",
        ]
        self._daily_csv_fields = [
            "run_id",
            "date",
            "turn",
            "subturn",
            "stock_code",
            "submitted_orders",
            "announced_price",
            "close_price",
            "volume",
            "fill_count",
        ]
        self._community_posts_csv_fields = [
            "run_id",
            "date",
            "turn",
            "agent_id",
            "post_id",
            "post_type",
            "title",
            "content",
        ]
        self._community_interactions_csv_fields = [
            "run_id",
            "date",
            "turn",
            "agent_id",
            "selected_post_ids",
            "post_id",
            "title",
            "post_type",
            "reaction",
            "author_badges",
            "author_profile",
        ]
        self._community_selection_csv_fields = [
            "run_id",
            "date",
            "turn",
            "agent_id",
            "depth",
            "read_limit",
            "visible_post_count",
            "visible_post_ids",
            "visible_posts_json",
        ]
        self._community_best_csv_fields = [
            "run_id",
            "date",
            "turn",
            "rank",
            "post_id",
            "title",
            "post_type",
            "score",
        ]
        self._community_logs_csv_fields = [
            "run_id",
            "date",
            "turn",
            "agent_id",
            "best_posts_count",
            "posts_read_count",
            "community_thinking",
            "best_posts_json",
            "posts_read_json",
        ]
        self._init_csv(self.run_dir / "agent_turns.csv", self._agent_csv_fields, append_existing=append_existing)
        self._init_csv(self.run_dir / "submitted_orders.csv", self._orders_csv_fields, append_existing=append_existing)
        self._init_csv(self.run_dir / "exchange_fills.csv", self._fills_csv_fields, append_existing=append_existing)
        self._init_csv(self.run_dir / "daily_exchange_summary.csv", self._daily_csv_fields, append_existing=append_existing)
        self._init_csv(self.run_dir / "community_posts.csv", self._community_posts_csv_fields, append_existing=append_existing)
        self._init_csv(self.run_dir / "community_selection_inputs.csv", self._community_selection_csv_fields, append_existing=append_existing)
        self._init_csv(self.run_dir / "community_interactions.csv", self._community_interactions_csv_fields, append_existing=append_existing)
        self._init_csv(self.run_dir / "community_best_posts.csv", self._community_best_csv_fields, append_existing=append_existing)
        self._init_csv(self.run_dir / "community_logs.csv", self._community_logs_csv_fields, append_existing=append_existing)
        if not append_existing or not (self.run_dir / "run_metadata.json").exists():
            self.write_json("run_metadata.json", {"run_id": self.run_id, "created_at": timestamp, **(metadata or {})})

    def log_agent_turn(
        self,
        *,
        agent: dict[str, Any],
        turn: int,
        date: str,
        context: dict[str, Any],
        news_interpretation: dict[str, Any],
        belief: dict[str, Any],
        market_analysis: dict[str, Any],
        decision: dict[str, Any],
        order: dict[str, Any] | None,
        depth2_flow: dict[str, Any] | None = None,
        fake_news_audit: dict[str, Any] | None = None,
    ) -> None:
        news_context = context.get("news_context") or {}
        event = {
            "run_id": self.run_id,
            "event": "agent_turn",
            "date": date,
            "turn": turn,
            "agent": self._compact_agent(agent),
            "context": context,
            "news_interpretation": news_interpretation,
            "belief": belief,
            "market_analysis": market_analysis,
            "decision": decision,
            "submitted_order": order,
        }
        if depth2_flow is not None:
            event["depth2_flow"] = depth2_flow
        if fake_news_audit is not None:
            event["fake_news_audit"] = fake_news_audit
        self.write_jsonl("agent_turns.jsonl", event)
        selected_news = news_interpretation.get("selected_news") or []
        step3 = (depth2_flow or {}).get("step3_search") or {}
        step4 = (depth2_flow or {}).get("step4_post_search_thinking") or {}
        self.append_csv(
            "agent_turns.csv",
            self._agent_csv_fields,
            {
                "run_id": self.run_id,
                "date": date,
                "turn": turn,
                "subturn": context.get("subturn", "full"),
                "agent_id": agent.get("agent_id"),
                "decision_date": context.get("decision_date", date),
                "market_features_date": context.get("market_features_date", date),
                "news_start_date": context.get("news_start_date") or "",
                "news_start_time": context.get("news_start_time") or "",
                "news_max_date": context.get("news_max_date", date),
                "news_end_time": context.get("news_end_time") or "",
                "execution_date": context.get("execution_date", date),
                "information_mode": context.get("information_mode", "pre_close_cutoff"),
                "news_depth": news_context.get("news_depth"),
                "selected_news_count": len(selected_news) if isinstance(selected_news, list) else 0,
                "read_news_count": len(news_context.get("read_contents") or []),
                "search_read_count": len(news_context.get("search_read_contents") or []),
                "fake_exposed": bool((fake_news_audit or {}).get("fake_exposed")),
                "fake_base_count": (fake_news_audit or {}).get("fake_base_count", 0),
                "fake_read_count": (fake_news_audit or {}).get("fake_read_count", 0),
                "fake_search_count": (fake_news_audit or {}).get("fake_search_count", 0),
                "fake_selected_count": (fake_news_audit or {}).get("fake_selected_count", 0),
                "fake_public_ids": ", ".join((fake_news_audit or {}).get("fake_public_ids") or []),
                "fake_synthetic_ids": ", ".join((fake_news_audit or {}).get("fake_synthetic_ids") or []),
                "fake_related_events": json.dumps(
                    (fake_news_audit or {}).get("fake_related_events") or [],
                    ensure_ascii=False,
                ),
                "depth2_search_keywords": ", ".join(step3.get("keywords") or []),
                "depth2_search_result_count": step3.get("result_count", ""),
                "depth2_view_change": step4.get("view_change", ""),
                "news_sentiment": news_interpretation.get("news_sentiment"),
                "action": decision.get("action"),
                "quantity": decision.get("quantity"),
                "submitted_order": bool(order),
                "belief_summary": belief.get("belief_summary"),
                "view_change": belief.get("view_change"),
                "decision_reason": decision.get("reason"),
                "risk_control": decision.get("risk_control"),
                "order_corrections": ", ".join(decision.get("order_corrections") or []),
            },
        )
        if order:
            self.log_submitted_order(order, turn=turn, date=date)

    def log_agent_error(self, *, agent: dict[str, Any], turn: int, date: str, error: BaseException) -> None:
        self.write_jsonl(
            "errors.jsonl",
            {
                "run_id": self.run_id,
                "event": "agent_error",
                "date": date,
                "turn": turn,
                "agent_id": agent.get("agent_id"),
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )

    def log_submitted_order(self, order: dict[str, Any], *, turn: int, date: str) -> None:
        self.append_csv(
            "submitted_orders.csv",
            self._orders_csv_fields,
            {
                "run_id": self.run_id,
                "date": date,
                "turn": turn,
                "subturn": order.get("subturn", ""),
                "agent_id": order.get("user_id"),
                "decision_date": order.get("decision_date", date),
                "market_features_date": order.get("market_features_date", date),
                "news_start_date": order.get("news_start_date") or "",
                "news_start_time": order.get("news_start_time") or "",
                "news_max_date": order.get("news_max_date", date),
                "news_end_time": order.get("news_end_time") or "",
                "execution_date": order.get("execution_date", date),
                "information_mode": order.get("information_mode", ""),
                "stock_code": order.get("stock_code"),
                "action": order.get("direction") or order.get("action"),
                "quantity": order.get("quantity"),
                "reason": order.get("reason"),
            },
        )

    def log_daily_exchange(
        self,
        *,
        date: str,
        turn: int,
        orders: list[dict[str, Any]],
        results: dict[str, dict[str, Any]],
    ) -> None:
        self.write_jsonl(
            "daily_exchange.jsonl",
            {
                "run_id": self.run_id,
                "event": "daily_exchange",
                "date": date,
                "turn": turn,
                "submitted_orders": orders,
                "results": results,
            },
        )
        for stock_code, result in sorted(results.items()):
            transactions = result.get("transactions") or []
            self.append_csv(
                "daily_exchange_summary.csv",
                self._daily_csv_fields,
                {
                    "run_id": self.run_id,
                    "date": date,
                    "turn": turn,
                    "subturn": _subturn_from_turn(turn),
                    "stock_code": stock_code,
                    "submitted_orders": len([order for order in orders if order.get("stock_code") == stock_code]),
                    "announced_price": result.get("announced_price"),
                    "close_price": result.get("close_price"),
                    "volume": result.get("volume"),
                    "fill_count": len(transactions),
                },
            )
            for tx in transactions:
                self.append_csv(
                    "exchange_fills.csv",
                    self._fills_csv_fields,
                    {
                        "run_id": self.run_id,
                        "date": date,
                        "turn": turn,
                        "subturn": _subturn_from_turn(turn),
                        "stock_code": tx.get("stock_code", stock_code),
                        "agent_id": tx.get("agent_id") or tx.get("user_id"),
                        "action": tx.get("action") or tx.get("direction"),
                        "quantity": tx.get("quantity") or tx.get("executed_quantity"),
                        "executed_price": tx.get("executed_price"),
                        "status": tx.get("status", "filled"),
                    },
                )

    def log_community_post(
        self,
        *,
        agent_id: str,
        turn: int,
        date: str,
        post: dict[str, Any],
    ) -> None:
        event = {
            "run_id": self.run_id,
            "event": "community_post",
            "date": date,
            "turn": turn,
            "agent_id": agent_id,
            "post": post,
        }
        self.write_jsonl("community_events.jsonl", event)
        self.append_csv(
            "community_posts.csv",
            self._community_posts_csv_fields,
            {
                "run_id": self.run_id,
                "date": date,
                "turn": turn,
                "agent_id": agent_id,
                "post_id": post.get("post_id"),
                "post_type": post.get("post_type"),
                "title": post.get("title"),
                "content": post.get("content"),
            },
        )

    def log_community_reading(
        self,
        *,
        agent_id: str,
        turn: int,
        date: str,
        selected_post_ids: list[int],
        posts_read: list[dict[str, Any]],
    ) -> None:
        event = {
            "run_id": self.run_id,
            "event": "community_reading",
            "date": date,
            "turn": turn,
            "agent_id": agent_id,
            "selected_post_ids": selected_post_ids,
            "posts_read": posts_read,
        }
        self.write_jsonl("community_events.jsonl", event)
        if not posts_read:
            self.append_csv(
                "community_interactions.csv",
                self._community_interactions_csv_fields,
                {
                    "run_id": self.run_id,
                    "date": date,
                    "turn": turn,
                    "agent_id": agent_id,
                    "selected_post_ids": json.dumps(selected_post_ids, ensure_ascii=False),
                },
            )
            return
        for post in posts_read:
            self.append_csv(
                "community_interactions.csv",
                self._community_interactions_csv_fields,
                {
                    "run_id": self.run_id,
                    "date": date,
                    "turn": turn,
                    "agent_id": agent_id,
                    "selected_post_ids": json.dumps(selected_post_ids, ensure_ascii=False),
                    "post_id": post.get("post_id"),
                    "title": post.get("title"),
                    "post_type": post.get("post_type"),
                    "reaction": post.get("reaction"),
                    "author_badges": json.dumps(post.get("author_badges") or [], ensure_ascii=False),
                    "author_profile": json.dumps(post.get("author_profile"), ensure_ascii=False, default=str),
                },
            )

    def log_community_selection_input(
        self,
        *,
        agent_id: str,
        turn: int,
        date: str,
        depth: int,
        read_limit: int,
        visible_posts: list[dict[str, Any]],
    ) -> None:
        compact_posts = [
            {
                "post_id": post.get("post_id"),
                "anonymous_code": post.get("anonymous_code"),
                "post_type": post.get("post_type"),
                "title": post.get("title"),
                "like_count": post.get("like_count"),
                "unlike_count": post.get("unlike_count"),
                "score": post.get("score"),
                "author_badges": post.get("author_badges") or [],
            }
            for post in visible_posts
        ]
        event = {
            "run_id": self.run_id,
            "event": "community_selection_input",
            "date": date,
            "turn": turn,
            "agent_id": agent_id,
            "depth": depth,
            "read_limit": read_limit,
            "visible_posts": compact_posts,
        }
        self.write_jsonl("community_selection_inputs.jsonl", event)
        self.append_csv(
            "community_selection_inputs.csv",
            self._community_selection_csv_fields,
            {
                "run_id": self.run_id,
                "date": date,
                "turn": turn,
                "agent_id": agent_id,
                "depth": depth,
                "read_limit": read_limit,
                "visible_post_count": len(compact_posts),
                "visible_post_ids": json.dumps([post.get("post_id") for post in compact_posts], ensure_ascii=False),
                "visible_posts_json": json.dumps(compact_posts, ensure_ascii=False),
            },
        )

    def log_community_best_posts(self, *, turn: int, date: str, best_posts: list[dict[str, Any]]) -> None:
        self.write_jsonl(
            "community_events.jsonl",
            {
                "run_id": self.run_id,
                "event": "community_best_posts",
                "date": date,
                "turn": turn,
                "best_posts": best_posts,
            },
        )
        for rank, post in enumerate(best_posts, start=1):
            self.append_csv(
                "community_best_posts.csv",
                self._community_best_csv_fields,
                {
                    "run_id": self.run_id,
                    "date": date,
                    "turn": turn,
                    "rank": rank,
                    "post_id": post.get("post_id"),
                    "title": post.get("title"),
                    "post_type": post.get("post_type"),
                    "score": post.get("score"),
                },
            )

    def log_community_log(
        self,
        *,
        agent_id: str,
        turn: int,
        date: str,
        best_posts: list[dict[str, Any]],
        posts_read: list[dict[str, Any]],
        community_thinking: str = "",
    ) -> None:
        event = {
            "run_id": self.run_id,
            "event": "community_log_saved",
            "date": date,
            "turn": turn,
            "agent_id": agent_id,
            "best_posts": best_posts,
            "posts_read": posts_read,
            "community_thinking": community_thinking,
        }
        self.write_jsonl("community_events.jsonl", event)
        self.append_csv(
            "community_logs.csv",
            self._community_logs_csv_fields,
            {
                "run_id": self.run_id,
                "date": date,
                "turn": turn,
                "agent_id": agent_id,
                "best_posts_count": len(best_posts),
                "posts_read_count": len(posts_read),
                "community_thinking": community_thinking,
                "best_posts_json": json.dumps(best_posts, ensure_ascii=False),
                "posts_read_json": json.dumps(posts_read, ensure_ascii=False, default=str),
            },
        )

    def write_json(self, filename: str, data: dict[str, Any]) -> None:
        with self._lock:
            with (self.run_dir / filename).open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    def write_jsonl(self, filename: str, data: dict[str, Any]) -> None:
        with self._lock:
            with (self.run_dir / filename).open("a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")

    def append_csv(self, filename: str, fieldnames: list[str], row: dict[str, Any]) -> None:
        with self._lock:
            with (self.run_dir / filename).open("a", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writerow({field: row.get(field, "") for field in fieldnames})

    def _init_csv(self, path: Path, fieldnames: list[str], *, append_existing: bool = False) -> None:
        if append_existing and path.exists() and path.stat().st_size > 0:
            return
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()

    def _update_current_pointer(self, root: Path) -> None:
        current = root / "current"
        tmp_current = root / f".current.{self.run_id}.tmp"
        if tmp_current.exists() or tmp_current.is_symlink():
            if tmp_current.is_dir() and not tmp_current.is_symlink():
                shutil.rmtree(tmp_current)
            else:
                tmp_current.unlink()
        try:
            tmp_current.symlink_to(self.run_dir.name, target_is_directory=True)
            tmp_current.replace(current)
        except OSError:
            if tmp_current.exists() or tmp_current.is_symlink():
                tmp_current.unlink()
            if current.exists() or current.is_symlink():
                if current.is_dir() and not current.is_symlink():
                    shutil.rmtree(current)
                else:
                    current.unlink()
            try:
                shutil.copytree(self.run_dir, current)
            except FileExistsError:
                pass

    @staticmethod
    def _compact_agent(agent: dict[str, Any]) -> dict[str, Any]:
        keys = [
            "agent_id",
            "user_type",
            "age",
            "gender",
            "strategy",
            "news_depth",
            "segment_key",
        ]
        return {key: agent.get(key) for key in keys}


def _subturn_from_turn(turn: int) -> str:
    if turn <= 0:
        return "full"
    return "am" if turn % 2 == 1 else "pm"
