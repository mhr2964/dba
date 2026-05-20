from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from data.repositories import article_repo
from services.personas import PERSONAS

log = logging.getLogger(__name__)


@dataclass
class ColumnistRequest:
    """Standardised call shape for columnist article generation.

    Replaces the bespoke ``context`` dict that each call site previously had to
    construct.  Call sites build a ColumnistRequest; ``generate`` unpacks it.

    Fields
    ------
    persona_id          : which persona writes the article
    category            : article type (trade_report, power_rankings, tank_watch, …)
    subject_team_ids    : teams the article focuses on — drives intel injection
    subject_player_ids  : players referenced (stored in article_repo, not sent to AI)
    extra_context       : type-specific payload (trade details, game stats, etc.)
                          merged verbatim into the JSON context block seen by the AI
    """
    persona_id: str
    category: str
    subject_team_ids: list[int] = field(default_factory=list)
    subject_player_ids: list[int] = field(default_factory=list)
    extra_context: dict = field(default_factory=dict)


def _format_signals_block(context_signals_per_player: dict) -> str:
    """Render context_signals_per_player into a readable bullet list for the prompt.

    Only renders players with at least one signal.  Skips signals with near-zero
    delta (abs < 0.01) so the list stays concise.
    """
    lines: list[str] = []
    for player_id, signals in context_signals_per_player.items():
        notable = [s for s in signals if abs(s.get("delta", 0)) >= 0.01]
        if not notable:
            continue
        lines.append(f"  Player #{player_id}:")
        for sig in notable:
            direction = "+" if sig.get("delta", 0) > 0 else ""
            lines.append(
                f"    - [{sig.get('code', '?')}] {direction}{sig.get('delta', 0):.2f}: {sig.get('reason', '')}"
            )
    if not lines:
        return ""
    return "CPU evaluation signals (what drove this trade decision):\n" + "\n".join(lines)


async def generate(  # noqa: PLR0912, PLR0915
    pool,
    league_id: int,
    season: int,
    persona_id: str | ColumnistRequest,
    category: str | None = None,
    context: dict | None = None,
    subject_team_ids: list[int] | None = None,
    subject_player_ids: list[int] | None = None,
) -> dict | None:
    """Generate one article from a persona.

    Accepts two call shapes:

    1. New shape (preferred):
       ``generate(pool, league_id, season, ColumnistRequest(...))``

    2. Legacy positional shape (DEPRECATED: use ColumnistRequest):
       ``generate(pool, league_id, season, persona_id, category, context,
                  subject_team_ids=..., subject_player_ids=...)``

    Saves the article to article_repo before returning.
    Returns {"headline": str, "body": str} or None on any failure.
    Silent fallback — never raises.

    When persona.context_keys is non-empty AND subject_team_ids is provided,
    automatically augments the user message with team intel slices declared by
    the persona (capped to 8 teams to keep prompts lean).

    When context["context_signals_per_player"] is present AND the persona
    declares "context_signals" in context_keys, formats the signals into a
    readable block injected before the context JSON.
    """
    # ── Shim: accept ColumnistRequest as the 4th positional argument ─────────
    # DEPRECATED: use ColumnistRequest
    if isinstance(persona_id, ColumnistRequest):
        req = persona_id
        _persona_id = req.persona_id
        _category = req.category
        _context = req.extra_context
        _subject_team_ids = req.subject_team_ids or None
        _subject_player_ids = req.subject_player_ids or None
    else:
        _persona_id = persona_id
        _category = category or ""
        _context = context or {}
        _subject_team_ids = subject_team_ids
        _subject_player_ids = subject_player_ids

    persona = PERSONAS.get(_persona_id)
    if persona is None:
        log.warning(f"columnist_service: unknown persona_id {_persona_id!r}")
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        log.debug("columnist_service: ANTHROPIC_API_KEY not set, skipping article generation")
        return None

    # ── Team intel injection ──────────────────────────────────────────────────
    # When the persona declares context_keys and subject_team_ids is known,
    # fetch and inject the relevant intel slices.  This is a best-effort
    # augmentation — failures are logged and silently skipped so they never
    # block article generation.
    intel_block: str = ""
    if persona.context_keys and _subject_team_ids:
        try:
            from services import columnist_intel
            from data.repositories import league_repo as _lr

            _league_row = await pool.fetchrow("SELECT * FROM leagues WHERE id = $1", league_id)
            if _league_row:
                _league = _lr._league_from_record(_league_row)
                _intel = await columnist_intel.build_columnist_intel(
                    pool, _league, season, persona, _subject_team_ids
                )
                if _intel:
                    intel_block = (
                        "Team intel (posture, plan, philosophy, and recent changes — "
                        "use this to ground your analysis in what the front office actually knows):\n"
                        + json.dumps(_intel, indent=2, default=str)
                    )
        except Exception as _intel_exc:
            log.warning(
                "columnist_service: intel fetch failed for %s: %s", _persona_id, _intel_exc
            )

    # ── Context signals block ─────────────────────────────────────────────────
    # When the caller stashed signals on the context dict AND this persona
    # consumes "context_signals", format them into a readable block.
    signals_block: str = ""
    if "context_signals" in persona.context_keys:
        raw_signals = _context.get("context_signals_per_player")
        if raw_signals:
            signals_block = _format_signals_block(raw_signals)
    # ── End intel injection ───────────────────────────────────────────────────

    # Pull the last 3 articles this persona wrote about the subject teams for
    # continuity — so the AI doesn't repeat the same angle twice in a row.
    recent_headlines: list[str] = []
    if _subject_team_ids:
        for team_id in _subject_team_ids[:2]:  # at most 2 teams to keep it fast
            rows = await article_repo.recent_about_team(pool, league_id, team_id, limit=3)
            for row in rows:
                if row.get("persona_id") == _persona_id and row.get("headline"):
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

    # When a game_recap is requested, give the columnist freedom to choose the
    # most interesting angle — they don't have to write a pure box-score recap.
    # The context may include recent_trades, standings_leaders, award_race_leaders,
    # and narrative_hooks; the persona should use whatever is most compelling.
    if _category == "game_recap":
        task_line = (
            "Write one article for the DBA (Discord Basketball Association) simulation league. "
            "IMPORTANT: This league is the DBA — always say DBA, DBA Finals, DBA Champions, DBA season. "
            "Never write NBA, NBA Finals, or NBA Champions. "
            "Choose the most interesting angle from the context below — it can be a game recap, "
            "trade analysis, standings narrative, award race drama, or a personality-driven take. "
            "Pick whatever best fits your voice and the most compelling story in the data. "
            "Cover multiple players and team storylines — do not focus only on one stat line. "
            "The 'game_results' list in context shows actual results formatted as 'TEAM beat TEAM score-score'; "
            "use these when referencing game outcomes."
        )
    else:
        task_line = (
            f"Write one {_category} article for the DBA (Discord Basketball Association) simulation league. "
            "IMPORTANT: This league is the DBA — always say DBA, DBA Finals, DBA Champions, DBA season. "
            "Never write NBA, NBA Finals, or NBA Champions."
        )

    user_content_parts = [
        task_line,
        "",
        "Context:",
        json.dumps(_context, indent=2),
    ]
    if signals_block:
        user_content_parts += ["", signals_block]
    if intel_block:
        user_content_parts += ["", intel_block]
    if memory_block:
        user_content_parts += ["", memory_block]
    user_content_parts += [
        "",
        "Return ONLY valid JSON matching this EXACT shape — no other keys, no prose:",
        json.dumps({
            "headline": "punchy ≤80 chars",
            "lede": "ONE sentence, the take, ≤25 words",
            "key_stats": [
                {"label": "what the number means", "value": "the number with units"},
            ],
            "bullets": [
                "ONE line each, ≤18 words, lead with a number or proper noun when possible",
            ],
            "verdict": "ONE sentence closing take, ≤20 words",
        }),
        "",
        "Constraints: key_stats must have 2-4 entries. bullets must have 2-3 entries.",
        "No markdown, no code fences.",
    ]
    user_content = "\n".join(user_content_parts)

    score_accuracy_rule = (
        "SCORE ACCURACY RULE (mandatory, overrides everything else): "
        "If the context contains an 'actual_final_score' field, you MUST reproduce "
        "that exact score when mentioning the game result. Never state a different score. "
        "Do not invent, round, or approximate any score value.\n\n"
    )

    narrative_rule = (
        "NARRATIVE RULE: Write about the narrative of the week/round, not just a single player. "
        "Cover team stories, matchup dynamics, and multiple players. "
        "The best articles zoom out to team and league context, not just stat lines. "
        "Use standings, streaks, and recent results from the context to build a wider story.\n\n"
    )

    # Prevent hallucination of 3-team trades: only describe a trade as 3-team
    # when the context explicitly shows 3 distinct team names as parties.
    three_team_rule = (
        "THREE-TEAM TRADE RULE (mandatory): "
        "Do not describe any trade as involving 3 teams unless the context "
        "explicitly shows 3 distinct team names as parties to that trade. "
        "If only 2 teams are named, the trade is a 2-team deal — never imply a third team.\n\n"
    )

    player_style_rule = (
        "PLAYER ARCHETYPES: When a top_performer or player entry in the context includes "
        "'style', 'shot_profile', 'playmaking', or 'defense' fields, weave that information "
        "into your writing naturally. Examples:\n"
        "- A 3PT Specialist going 2-for-10 from deep is a story — their identity was challenged.\n"
        "- An Elite Playmaker logging 12 assists fits their archetype — call it out.\n"
        "- A Score-First player suddenly leading in assists is surprising — lean into that.\n"
        "- A Rim Attacker dominating inside is expected — frame it as them imposing their will.\n"
        "- A Closer stepping up in a tight game is the narrative — connect style to the moment.\n"
        "Do not just repeat the label verbatim. Translate it into natural basketball language.\n\n"
    )

    output_shape_rule = (
        "OUTPUT SHAPE (mandatory — the only acceptable response format):\n"
        "You must return a JSON object with exactly these keys: "
        "headline, lede, key_stats, bullets, verdict.\n"
        "key_stats is an array of {label, value} objects (2-4 entries).\n"
        "bullets is an array of strings (2-3 entries).\n"
        "All other fields are strings.\n"
        "Example:\n"
        '{"headline": "OKC Stands Alone at the Top", '
        '"lede": "SGA dropped 34 on 62% shooting and OKC won their 8th straight.", '
        '"key_stats": [{"label": "SGA points", "value": "34 pts"}, {"label": "OKC streak", "value": "8 straight W"}], '
        '"bullets": ["OKC outscored GSW by 22 in the paint — a size mismatch the Warriors had no answer for.", '
        '"SGA has gone 30+ in 5 of the last 6 games, all wins."], '
        '"verdict": "Until someone slows SGA down, this OKC run is far from over."}\n\n'
    )

    system_prompt = (
        score_accuracy_rule + three_team_rule +
        narrative_rule + player_style_rule + output_shape_rule +
        persona.voice_notes
    )

    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=api_key)
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = message.content[0].text.strip()

        # Strip markdown code fences if the model wraps its JSON output.
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            log.warning(
                "columnist_service: JSON parse failed for %s/%s — falling back to truncated plain text | raw: %r",
                _persona_id, _category, raw[:120],
            )
            headline = f"{_category.replace('_', ' ').title()} Report"
            body = raw[:200].strip()
            await article_repo.insert(
                pool, league_id=league_id, season=season,
                persona_id=_persona_id, category=_category,
                headline=headline, body=body,
                subject_team_ids=_subject_team_ids,
                subject_player_ids=_subject_player_ids,
            )
            return {"headline": headline, "body": body}

        required_keys = {"headline", "lede", "key_stats", "bullets", "verdict"}
        if not isinstance(parsed, dict) or not required_keys.issubset(parsed.keys()):
            log.warning(
                "columnist_service: unexpected JSON shape from %s — missing keys; got: %r",
                _persona_id, list(parsed.keys()) if isinstance(parsed, dict) else raw[:120],
            )
            # Graceful fallback: if old shape {"headline","body"} slips through, accept it
            if isinstance(parsed, dict) and "headline" in parsed and "body" in parsed:
                headline = str(parsed["headline"]).strip()
                body = str(parsed["body"]).strip()[:1400]
                await article_repo.insert(
                    pool, league_id=league_id, season=season,
                    persona_id=_persona_id, category=_category,
                    headline=headline, body=body,
                    subject_team_ids=_subject_team_ids,
                    subject_player_ids=_subject_player_ids,
                )
                return {"headline": headline, "body": body}
            return None

        headline = str(parsed["headline"]).strip()
        if not headline:
            log.warning("columnist_service: empty headline from %s", _persona_id)
            return None

        persona_display = persona.display_name
        body = _assemble_article(parsed, persona_display)

        await article_repo.insert(
            pool,
            league_id=league_id,
            season=season,
            persona_id=_persona_id,
            category=_category,
            headline=headline,
            body=body,
            subject_team_ids=_subject_team_ids,
            subject_player_ids=_subject_player_ids,
        )

        return {"headline": headline, "body": body}

    except Exception as exc:
        raw_preview = locals().get("raw", "<no response>")
        if isinstance(raw_preview, str):
            raw_preview = raw_preview[:120]
        log.warning(
            f"columnist_service: article generation failed for {_persona_id}/{_category}: {exc} "
            f"| response preview: {raw_preview!r}"
        )
        return None


def _assemble_article(parsed: dict, persona: str) -> str:
    """Assemble Discord markdown from structured LLM output.

    Format is guaranteed by code — the LLM provides semantic content only.
    Returns a string suitable for use as an embed description.
    """
    lede = str(parsed.get("lede", "")).strip()
    key_stats: list[dict] = parsed.get("key_stats", []) or []
    bullets: list[str] = parsed.get("bullets", []) or []
    verdict = str(parsed.get("verdict", "")).strip()

    parts: list[str] = []

    if lede:
        parts.append(lede)

    if key_stats:
        parts.append("")
        parts.append("__Key Numbers__")
        for stat in key_stats[:4]:
            label = str(stat.get("label", "")).strip()
            value = str(stat.get("value", "")).strip()
            if label and value:
                parts.append(f"> • **{label}**: {value}")

    if bullets:
        parts.append("")
        parts.append("__The Read__")
        for bullet in bullets[:3]:
            text = str(bullet).strip()
            if text:
                parts.append(f"• {text}")

    if verdict:
        parts.append("")
        parts.append(f"**Verdict:** {verdict}")

    if persona:
        parts.append("")
        parts.append(f"— *{persona}*")

    return "\n".join(parts)
