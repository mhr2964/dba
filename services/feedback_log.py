"""Feedback capture — anchors bot posts in Postgres and records Discord replies.

Two callers:
  - send-side: `register_bot_post(...)` (or the persona/trade convenience wrappers)
    runs immediately after a successful non-ephemeral `channel.send` so the
    bot's outgoing message is bound to its league/sim/entity context.
  - reply-side: `record_reply(...)` runs inside the on_message listener when
    a user replies to a tracked post. Writes a JSONL line first so the data
    survives a DB outage, then inserts the queryable row.

The JSONL file at `headless_logs/feedback_<bot_start_ts>.jsonl` is the artifact
the user attaches when feeding a session back to Claude — each line is fully
self-contained (bot post + reply joined) so a future session can read one line
and immediately query Postgres to investigate the live league state.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
from datetime import timezone
from pathlib import Path
from typing import Any, Optional, TypedDict

import asyncpg
import discord

from core.logging import get_logger
from data.repositories import bot_message_repo, feedback_repo

log = get_logger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOG_DIR = _PROJECT_ROOT / "headless_logs"
_SESSION_TS = datetime.datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
_SESSION_PATH = _LOG_DIR / f"feedback_{_SESSION_TS}.jsonl"

# Serialises JSONL appends so concurrent reply events don't interleave bytes.
_jsonl_lock = asyncio.Lock()


class FeedbackContext(TypedDict, total=False):
    """All anchor fields a caller can pass when registering a bot post.

    Only `kind` is strictly required; everything else is best-effort context.
    Missing fields stay NULL in the row so a partial registration is still
    queryable by the fields that were supplied.
    """
    kind: str
    league_id: Optional[int]
    game_index: Optional[int]
    sim_date: Optional[datetime.date]
    season: Optional[int]
    subject_team_ids: list[int]
    subject_player_ids: list[int]
    subject_trade_id: Optional[int]
    context_blob: dict
    content_preview: str


def current_session_path() -> Path:
    """Path the on_message listener appends each reply line to."""
    return _SESSION_PATH


# ---------------------------------------------------------------------------
# Send-side registration
# ---------------------------------------------------------------------------

async def register_bot_post(
    pool: asyncpg.Pool,
    sent_message: discord.Message | discord.WebhookMessage | None,
    ctx: FeedbackContext,
) -> Optional[int]:
    """Anchor a sent Discord message to its league/sim/entity context.

    Safe to call with `sent_message=None` (the send failed earlier) — silently
    returns. Safe to call without a `guild_id` on the message (DMs); the
    listener won't find a match anyway and registration is harmless.
    """
    if sent_message is None or sent_message.id is None:
        return None
    if not ctx.get("kind"):
        log.warning("register_bot_post: ctx missing 'kind' — skipping registration")
        return None

    channel_id = getattr(sent_message, "channel", None)
    channel_id = channel_id.id if channel_id is not None else 0
    guild = getattr(sent_message, "guild", None)
    guild_id = guild.id if guild is not None else 0

    preview = ctx.get("content_preview", "") or ""
    if len(preview) > 2000:
        preview = preview[:2000]

    try:
        return await bot_message_repo.insert(
            pool,
            message_id=sent_message.id,
            channel_id=channel_id,
            guild_id=guild_id,
            kind=ctx["kind"],
            league_id=ctx.get("league_id"),
            game_index=ctx.get("game_index"),
            sim_date=ctx.get("sim_date"),
            season=ctx.get("season"),
            subject_team_ids=list(ctx.get("subject_team_ids") or []),
            subject_player_ids=list(ctx.get("subject_player_ids") or []),
            subject_trade_id=ctx.get("subject_trade_id"),
            context_blob=ctx.get("context_blob") or {},
            content_preview=preview,
        )
    except Exception as exc:
        # Registration failure must never break the user-facing send — the
        # message already landed in Discord; losing tracking is acceptable.
        log.warning(
            f"register_bot_post failed for message {sent_message.id} "
            f"(kind={ctx['kind']}): {exc}"
        )
        return None


async def register_columnist_post(
    pool: asyncpg.Pool,
    sent_message: discord.Message | None,
    *,
    league_id: int,
    season: int,
    persona_id: str,
    category: str,
    headline: str,
    body: str,
    game_index: Optional[int] = None,
    sim_date: Optional[datetime.date] = None,
    subject_team_ids: Optional[list[int]] = None,
    subject_player_ids: Optional[list[int]] = None,
    subject_trade_id: Optional[int] = None,
) -> Optional[int]:
    """Convenience wrapper — every columnist article uses the same context shape.

    Keeps per-call-site code to one line in batch_sim_runner.py.
    """
    ctx: FeedbackContext = {
        "kind": "columnist_article",
        "league_id": league_id,
        "season": season,
        "game_index": game_index,
        "sim_date": sim_date,
        "subject_team_ids": list(subject_team_ids or []),
        "subject_player_ids": list(subject_player_ids or []),
        "subject_trade_id": subject_trade_id,
        "context_blob": {
            "persona_id": persona_id,
            "category": category,
            "headline": headline,
        },
        "content_preview": (body or "")[:500],
    }
    return await register_bot_post(pool, sent_message, ctx)


async def register_trade_announcement(
    pool: asyncpg.Pool,
    sent_message: discord.Message | None,
    *,
    league_id: int,
    season: int,
    trade_id: int,
    proposer_team_id: int,
    counterparty_team_id: int,
    status: str,
    headline: str = "",
) -> Optional[int]:
    """Convenience wrapper for trade announcements posted to #transactions."""
    ctx: FeedbackContext = {
        "kind": "trade_announcement",
        "league_id": league_id,
        "season": season,
        "subject_team_ids": [proposer_team_id, counterparty_team_id],
        "subject_trade_id": trade_id,
        "context_blob": {"status": status, "headline": headline},
        "content_preview": headline[:500],
    }
    return await register_bot_post(pool, sent_message, ctx)


# ---------------------------------------------------------------------------
# Reply-side recording
# ---------------------------------------------------------------------------

async def record_reply(
    pool: asyncpg.Pool,
    bot_row: dict,
    reply_msg: discord.Message,
) -> Optional[int]:
    """Persist a reply both to Postgres and to the session JSONL.

    JSONL append happens AFTER the DB insert because the DB has the dedup
    constraint (`UNIQUE(reply_message_id)`); skipping the JSONL line on dup
    keeps the file from growing duplicate entries on Discord redelivery.
    Returns the new reply id, or None if the reply was already recorded.
    """
    session_path = str(current_session_path())
    attachments_payload = [
        {"id": a.id, "filename": a.filename, "url": a.url, "size": a.size}
        for a in reply_msg.attachments
    ]

    reply_id = await feedback_repo.insert_reply(
        pool,
        bot_message_id=bot_row["message_id"],
        reply_message_id=reply_msg.id,
        author_id=reply_msg.author.id,
        author_name=str(reply_msg.author),
        reply_text=reply_msg.content or "",
        attachments=attachments_payload,
        session_log_path=session_path,
    )
    if reply_id is None:
        return None

    try:
        await _append_jsonl_line(session_path, bot_row, reply_msg, attachments_payload)
    except OSError as exc:
        log.warning(
            f"record_reply: JSONL append failed for reply {reply_msg.id} "
            f"(DB row {reply_id} written): {exc}"
        )
    return reply_id


async def _append_jsonl_line(
    session_path: str,
    bot_row: dict,
    reply_msg: discord.Message,
    attachments_payload: list[dict],
) -> None:
    """Append one self-contained JSON line to the session log."""
    posted_at = bot_row.get("posted_at")
    sim_date = bot_row.get("sim_date")
    line = {
        "ts": datetime.datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "reply": {
            "message_id": reply_msg.id,
            "author": str(reply_msg.author),
            "author_id": reply_msg.author.id,
            "text": reply_msg.content or "",
            "attachments": attachments_payload,
        },
        "bot_post": {
            "message_id": bot_row["message_id"],
            "channel_id": bot_row["channel_id"],
            "guild_id": bot_row.get("guild_id"),
            "kind": bot_row["kind"],
            "posted_at": posted_at.isoformat() if isinstance(posted_at, datetime.datetime) else posted_at,
            "league_id": bot_row.get("league_id"),
            "season": bot_row.get("season"),
            "game_index": bot_row.get("game_index"),
            "sim_date": sim_date.isoformat() if isinstance(sim_date, datetime.date) else sim_date,
            "subject_team_ids": list(bot_row.get("subject_team_ids") or []),
            "subject_player_ids": list(bot_row.get("subject_player_ids") or []),
            "subject_trade_id": bot_row.get("subject_trade_id"),
            "context": bot_row.get("context_blob") or {},
            "content_preview": bot_row.get("content_preview"),
        },
    }
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(line, separators=(",", ":"), default=_json_default) + "\n"
    async with _jsonl_lock:
        await asyncio.to_thread(_append_text, session_path, payload)


def _append_text(path: str, payload: str) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(payload)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    return str(obj)


# ---------------------------------------------------------------------------
# Session export — used by the /feedback export slash command
# ---------------------------------------------------------------------------

async def export_session(pool: asyncpg.Pool) -> tuple[Path, int]:
    """Return (path, line_count) for the current session's JSONL.

    If the JSONL file is missing or empty but the DB has rows for this session
    path, rebuild the file from DB so the export is never empty when data exists.
    """
    path = current_session_path()
    line_count = 0
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            line_count = sum(1 for _ in fh)
    if line_count == 0:
        rows = await feedback_repo.list_for_session(pool, str(path))
        if rows:
            line_count = await _rebuild_jsonl_from_db(pool, path, rows)
    return path, line_count


async def _rebuild_jsonl_from_db(
    pool: asyncpg.Pool,
    path: Path,
    reply_rows: list[dict],
) -> int:
    """Reconstruct the JSONL when the file got truncated or deleted mid-session."""
    written = 0
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    async with _jsonl_lock:
        with open(path, "w", encoding="utf-8") as fh:
            for r in reply_rows:
                bot_row = await bot_message_repo.get_by_message_id(pool, r["bot_message_id"])
                if bot_row is None:
                    continue
                attachments = r.get("attachments") or []
                if isinstance(attachments, str):
                    try:
                        attachments = json.loads(attachments)
                    except (TypeError, ValueError):
                        attachments = []
                posted_at = bot_row.get("posted_at")
                sim_date = bot_row.get("sim_date")
                created_at = r.get("created_at")
                line = {
                    "ts": created_at.isoformat() if isinstance(created_at, datetime.datetime) else str(created_at),
                    "reply": {
                        "message_id": r["reply_message_id"],
                        "author": r["author_name"],
                        "author_id": r["author_id"],
                        "text": r["reply_text"],
                        "attachments": attachments,
                    },
                    "bot_post": {
                        "message_id": bot_row["message_id"],
                        "channel_id": bot_row["channel_id"],
                        "guild_id": bot_row.get("guild_id"),
                        "kind": bot_row["kind"],
                        "posted_at": posted_at.isoformat() if isinstance(posted_at, datetime.datetime) else posted_at,
                        "league_id": bot_row.get("league_id"),
                        "season": bot_row.get("season"),
                        "game_index": bot_row.get("game_index"),
                        "sim_date": sim_date.isoformat() if isinstance(sim_date, datetime.date) else sim_date,
                        "subject_team_ids": list(bot_row.get("subject_team_ids") or []),
                        "subject_player_ids": list(bot_row.get("subject_player_ids") or []),
                        "subject_trade_id": bot_row.get("subject_trade_id"),
                        "context": bot_row.get("context_blob") or {},
                        "content_preview": bot_row.get("content_preview"),
                    },
                }
                fh.write(json.dumps(line, separators=(",", ":"), default=_json_default) + "\n")
                written += 1
    return written
