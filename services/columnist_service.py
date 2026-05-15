from __future__ import annotations

import json
import logging
import os
from typing import Optional

from data.repositories import article_repo
from services.personas import PERSONAS

log = logging.getLogger(__name__)


async def generate(
    pool,
    league_id: int,
    season: int,
    persona_id: str,
    category: str,
    context: dict,
    subject_team_ids: list[int] | None = None,
    subject_player_ids: list[int] | None = None,
) -> dict | None:
    """
    Generate one article from a persona.

    Saves the article to article_repo before returning.
    Returns {"headline": str, "body": str} or None on any failure.
    Silent fallback — never raises.
    """
    persona = PERSONAS.get(persona_id)
    if persona is None:
        log.warning(f"columnist_service: unknown persona_id {persona_id!r}")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.debug("columnist_service: ANTHROPIC_API_KEY not set, skipping article generation")
        return None

    # Pull the last 3 articles this persona wrote about the subject teams for
    # continuity — so the AI doesn't repeat the same angle twice in a row.
    recent_headlines: list[str] = []
    if subject_team_ids:
        for team_id in subject_team_ids[:2]:  # at most 2 teams to keep it fast
            rows = await article_repo.recent_about_team(pool, league_id, team_id, limit=3)
            for row in rows:
                if row.get("persona_id") == persona_id and row.get("headline"):
                    recent_headlines.append(row["headline"])
        # Deduplicate while preserving order
        seen: set[str] = set()
        deduped: list[str] = []
        for h in recent_headlines:
            if h not in seen:
                seen.add(h)
                deduped.append(h)
        recent_headlines = deduped[:3]

    # Build the memory block only when there's something to show.
    if recent_headlines:
        memory_block = "Recent coverage (for continuity — do not repeat these; build on them):\n" + "\n".join(
            f"- {h}" for h in recent_headlines
        )
    else:
        memory_block = ""

    user_content_parts = [
        f"Write one {category} article for the DBA NBA simulation league.",
        "",
        "Context:",
        json.dumps(context, indent=2),
    ]
    if memory_block:
        user_content_parts += ["", memory_block]
    user_content_parts += [
        "",
        'Return ONLY valid JSON: {"headline": "...", "body": "..."}',
        "No markdown, no code fences.",
    ]
    user_content = "\n".join(user_content_parts)

    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=api_key)
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            system=persona.voice_notes,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = message.content[0].text.strip()

        # Strip markdown code fences if the model wraps its JSON output.
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or "headline" not in parsed or "body" not in parsed:
            log.warning(
                f"columnist_service: unexpected JSON shape from {persona_id}: {raw[:120]!r}"
            )
            return None

        headline = str(parsed["headline"]).strip()
        body = str(parsed["body"]).strip()
        if not headline or not body:
            log.warning(f"columnist_service: empty headline or body from {persona_id}")
            return None

        await article_repo.insert(
            pool,
            league_id=league_id,
            season=season,
            persona_id=persona_id,
            category=category,
            headline=headline,
            body=body,
            subject_team_ids=subject_team_ids,
            subject_player_ids=subject_player_ids,
        )

        return {"headline": headline, "body": body}

    except Exception as exc:
        raw_preview = locals().get("raw", "<no response>")
        if isinstance(raw_preview, str):
            raw_preview = raw_preview[:120]
        log.warning(
            f"columnist_service: article generation failed for {persona_id}/{category}: {exc} "
            f"| response preview: {raw_preview!r}"
        )
        return None
