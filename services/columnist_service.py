from __future__ import annotations

import json
import logging
import os
import re
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


def _tolerant_json_parse(raw: str, persona_id: str, category: str) -> dict | None:
    """Parse LLM output tolerantly, handling common failure modes.

    Attempts in order:
    1. Strip markdown code fences (```json ... ```)
    2. Extract outermost {…} block to strip leading/trailing prose
    3. Drop trailing commas before } or ] (common LLM quirk)
    4. Standard json.loads

    Returns a dict on success, None if all attempts fail.
    """
    text = raw.strip()

    # 1. Strip markdown code fences (handles truncated responses where the
    # closing ``` is missing).
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence_match:
        text = fence_match.group(1).strip()
    else:
        # Truncated response — strip opening fence and trailing fence if any.
        text = re.sub(r"^`+\s*(?:json)?\s*", "", text)
        text = re.sub(r"`+\s*$", "", text)

    # 2. Extract outermost {...} block; fall back to starting-{ if no
    # matching close exists (truncated response).
    brace_match = re.search(r"\{[\s\S]*\}", text)
    if brace_match:
        text = brace_match.group(0)
    else:
        start = text.find("{")
        if start >= 0:
            text = text[start:]
            # Best-effort: balance braces by appending missing close.
            opens = text.count("{")
            closes = text.count("}")
            if opens > closes:
                # Trim trailing comma if any, then close to last clean state.
                text = text.rstrip().rstrip(",")
                # Close any open arrays first, then objects.
                arr_open = text.count("[")
                arr_close = text.count("]")
                if arr_open > arr_close:
                    text += "]" * (arr_open - arr_close)
                text += "}" * (opens - closes)

    # 3. Drop trailing commas before } or ] (e.g. {"a": 1,} or [1, 2,]).
    text = re.sub(r",\s*([}\]])", r"\1", text)

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
        log.warning(
            "columnist_service: parsed JSON is not a dict for %s/%s — type: %s",
            persona_id, category, type(result).__name__,
        )
        return None
    except json.JSONDecodeError as exc:
        log.warning(
            "columnist_service: _tolerant_json_parse failed for %s/%s: %s | cleaned text: %r",
            persona_id, category, exc, text[:120],
        )
        return None


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
        persona.output_shape_override
        if persona.output_shape_override
        else (
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
            max_tokens=900,  # 400 was truncating mid-JSON, breaking parse
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = message.content[0].text.strip()

        parsed = _tolerant_json_parse(raw, _persona_id, _category)

        if parsed is None:
            # All structured parsing failed — wrap raw text and return something.
            # Guard: if the raw text looks like JSON (starts with '{'), try one more
            # time to pull readable fields out of it so we never post raw JSON to Discord.
            log.warning(
                "columnist_service: JSON parse failed for %s/%s — using prose fallback | raw: %r",
                _persona_id, _category, raw[:120],
            )
            persona_display = persona.display_name
            _prose_raw = raw.strip()
            # Treat fence-prefixed raw the same as JSON-prefixed — both are
            # the LLM trying to return structured output. Never spill them.
            _looks_like_json = _prose_raw.startswith("{") or _prose_raw.startswith("`")
            if _looks_like_json:
                # Attempt a best-effort field extraction from JSON-shaped text.
                _json_candidate: dict | None = None
                try:
                    import re as _re
                    _cleaned = _re.sub(r",\s*([}\]])", r"\1", _prose_raw)
                    _json_candidate = json.loads(_cleaned)
                except Exception:
                    pass
                if isinstance(_json_candidate, dict):
                    # Got a valid dict — route through the normal assembly path.
                    _h = str(_json_candidate.get("headline", "")).strip()
                    _b = str(_json_candidate.get("body", "")).strip()
                    if _h and _b:
                        # Old-shape fallback.
                        await article_repo.insert(
                            pool, league_id=league_id, season=season,
                            persona_id=_persona_id, category=_category,
                            headline=_h, body=_b,
                            subject_team_ids=_subject_team_ids,
                            subject_player_ids=_subject_player_ids,
                        )
                        return {"headline": _h, "body": _b}
                    # New-shape keys available — assemble.
                    _json_candidate.setdefault("lede", "")
                    _json_candidate.setdefault("key_stats", [])
                    _json_candidate.setdefault("bullets", [])
                    _json_candidate.setdefault("verdict", "")
                    _h = str(_json_candidate.get("headline", "")).strip()
                    if _h:
                        _fmt = persona.category_overrides.get(_category, persona.format_style)
                        _body = _assemble_article(_json_candidate, persona_display, _fmt)
                        await article_repo.insert(
                            pool, league_id=league_id, season=season,
                            persona_id=_persona_id, category=_category,
                            headline=_h, body=_body,
                            subject_team_ids=_subject_team_ids,
                            subject_player_ids=_subject_player_ids,
                        )
                        return {"headline": _h, "body": _body}
                # JSON-shaped but still unparseable — discard JSON text entirely;
                # emit a generic headline so no raw braces reach Discord.
                headline = f"{_category.replace('_', ' ').title()} Report"
                body = f"**{headline}**\n\n— *{persona_display}*"
            else:
                first_line = _prose_raw.split("\n")[0].strip()[:80]
                headline = first_line if first_line else f"{_category.replace('_', ' ').title()} Report"
                rest = _prose_raw[len(first_line):].strip()[:300]
                body = f"**{headline}**\n\n{rest}\n\n— *{persona_display}*" if rest else f"**{headline}**\n\n— *{persona_display}*"
            await article_repo.insert(
                pool, league_id=league_id, season=season,
                persona_id=_persona_id, category=_category,
                headline=headline, body=body,
                subject_team_ids=_subject_team_ids,
                subject_player_ids=_subject_player_ids,
            )
            return {"headline": headline, "body": body}

        # Accept old {"headline","body"} shape if it slips through.
        if "headline" in parsed and "body" in parsed and "lede" not in parsed:
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

        # Require headline. For standard shapes also require lede; custom-shape
        # personas (output_shape_override set) only need headline to be valid.
        headline = str(parsed.get("headline", "")).strip()
        lede = str(parsed.get("lede", "")).strip()
        _uses_custom_shape = bool(persona.output_shape_override)
        _required_ok = headline and (lede or _uses_custom_shape)
        if not _required_ok:
            log.warning(
                "columnist_service: missing required fields (headline%s) from %s — prose fallback | got keys: %r",
                "" if _uses_custom_shape else "/lede",
                _persona_id, list(parsed.keys()),
            )
            persona_display = persona.display_name
            # Don't emit raw JSON into the body — use a generic headline instead.
            headline = f"{_category.replace('_', ' ').title()} Report"
            body = f"**{headline}**\n\n— *{persona_display}*"
            await article_repo.insert(
                pool, league_id=league_id, season=season,
                persona_id=_persona_id, category=_category,
                headline=headline, body=body,
                subject_team_ids=_subject_team_ids,
                subject_player_ids=_subject_player_ids,
            )
            return {"headline": headline, "body": body}

        # Inject empty defaults for optional fields so _assemble_article never KeyErrors.
        parsed.setdefault("key_stats", [])
        parsed.setdefault("bullets", [])
        parsed.setdefault("verdict", "")

        persona_display = persona.display_name
        _fmt = persona.category_overrides.get(_category, persona.format_style)
        body = _assemble_article(parsed, persona_display, _fmt)

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


def _assemble_default(parsed: dict, persona_display: str) -> str:
    """Original template-stamp format — fallback for unrecognised styles.

    __Key Numbers__ callout block + __The Read__ bullets + Verdict label.
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

    if persona_display:
        parts.append("")
        parts.append(f"— *{persona_display}*")

    return "\n".join(parts)


def _assemble_analytics(parsed: dict, persona_display: str) -> str:
    """Analytics format — stat table in a code block, minimal prose, no section headers.

    Used by data-driven writers (Marcus Brooks, Keisha Williams, Darius Cole).
    Leads with a fixed-width stat table so numbers stay scannable.
    Ends with 'Bottom line:' instead of 'Verdict:' — the label itself signals the voice.
    """
    lede = str(parsed.get("lede", "")).strip()
    key_stats: list[dict] = parsed.get("key_stats", []) or []
    bullets: list[str] = parsed.get("bullets", []) or []
    verdict = str(parsed.get("verdict", "")).strip()

    parts: list[str] = []

    # Fixed-width stat table inside a code block so Discord renders it monospaced.
    if key_stats:
        table_lines = ["```", f"{'Stat':<22} {'Value':>10}"]
        table_lines.append(f"{'─' * 22} {'─' * 10}")
        for stat in key_stats[:4]:
            label = str(stat.get("label", "")).strip()[:22]
            value = str(stat.get("value", "")).strip()[:10]
            if label and value:
                table_lines.append(f"{label:<22} {value:>10}")
        table_lines.append("```")
        parts.append("\n".join(table_lines))

    if lede:
        parts.append(lede)

    for bullet in bullets[:3]:
        text = str(bullet).strip()
        if text:
            parts.append(f"• {text}")

    if verdict:
        parts.append(f"\n**Bottom line:** {verdict}")

    if persona_display:
        parts.append(f"— *{persona_display}*")

    return "\n".join(parts)


def _assemble_hot_take(parsed: dict, persona_display: str) -> str:
    """Hot-take format — no headers, no bullets, no stat block.

    Used by opinionated/confrontational writers (Jordan Rivera).
    Short, spicy, reads as a single charged paragraph.
    The verdict or first bullet becomes a bold punchline embedded in prose.
    """
    lede = str(parsed.get("lede", "")).strip()
    bullets: list[str] = parsed.get("bullets", []) or []
    verdict = str(parsed.get("verdict", "")).strip()

    parts: list[str] = []

    # Punchline: pull from verdict first, then first bullet if verdict is empty.
    punchline = verdict or (str(bullets[0]).strip() if bullets else "")

    # Lede + bold punchline on one block.
    if lede and punchline:
        parts.append(f"{lede} **{punchline}**")
    elif lede:
        parts.append(lede)
    elif punchline:
        parts.append(f"**{punchline}**")

    # Remaining bullets joined as prose (skip the one already used as punchline).
    remaining = bullets[1:] if verdict else bullets[1:]
    if remaining:
        # Two sentences max; join with a space so it reads as a paragraph.
        prose = " ".join(str(b).strip() for b in remaining[:2] if str(b).strip())
        if prose:
            parts.append(prose)

    if persona_display:
        parts.append(f"\n— *{persona_display}*")

    return "\n".join(parts)


def _assemble_tactical(parsed: dict, persona_display: str) -> str:
    """Tactical/coaching format — What Worked / What Didn't / The Adjustment sections.

    Used by coaching beat and film-room writers (Quinn Park, Dr. Pat Chen).
    Stats are embedded inline in the section bodies rather than in a callout block.
    """
    lede = str(parsed.get("lede", "")).strip()
    key_stats: list[dict] = parsed.get("key_stats", []) or []
    bullets: list[str] = parsed.get("bullets", []) or []
    verdict = str(parsed.get("verdict", "")).strip()

    # Build an inline stat note to append to the lede (e.g. "(12 AST, +18 net)").
    stat_note = ""
    if key_stats:
        stat_parts = []
        for stat in key_stats[:3]:
            label = str(stat.get("label", "")).strip()
            value = str(stat.get("value", "")).strip()
            if label and value:
                stat_parts.append(f"{value} {label}")
        if stat_parts:
            stat_note = f"*({', '.join(stat_parts)})*"

    # Split bullets into "what worked" (first half) and "what didn't" (second half).
    clean_bullets = [str(b).strip() for b in bullets[:4] if str(b).strip()]
    mid = max(1, len(clean_bullets) // 2)
    worked = clean_bullets[:mid]
    didnt = clean_bullets[mid:]

    parts: list[str] = []

    if lede:
        if stat_note:
            parts.append(f"{lede} {stat_note}")
        else:
            parts.append(lede)

    if worked:
        parts.append("\n**What Worked**")
        for b in worked:
            parts.append(f"{b}")

    if didnt:
        parts.append("\n**What Didn't**")
        for b in didnt:
            parts.append(f"{b}")

    if verdict:
        parts.append(f"\n**The Adjustment**\n{verdict}")

    if persona_display:
        parts.append(f"\n— *{persona_display}*")

    return "\n".join(parts)


def _assemble_recap(parsed: dict, persona_display: str) -> str:
    """Game recap format — tight and Twitter-paced.

    Used by game reporters (Maya Chen, Keisha Williams on recaps).
    Lede → 2 game beats as bullets → verdict as a closing line.
    No section headers. Player names in the lede/verdict stay bolded by the LLM;
    we don't add extra markup beyond what the LLM already chose.
    """
    lede = str(parsed.get("lede", "")).strip()
    bullets: list[str] = parsed.get("bullets", []) or []
    verdict = str(parsed.get("verdict", "")).strip()

    parts: list[str] = []

    if lede:
        parts.append(lede)

    for bullet in bullets[:2]:
        text = str(bullet).strip()
        if text:
            parts.append(f"• {text}")

    if verdict:
        parts.append(f"\n{verdict}")

    if persona_display:
        parts.append(f"— *{persona_display}*")

    return "\n".join(parts)


def _assemble_moment(parsed: dict, persona_display: str) -> str:
    """Maya Chen — 'The Moment' format.

    Vignette structure: headline → italic scene-setter → play-by-play → The why.
    Isolates one cinematic sequence; no game summary.
    """
    headline = str(parsed.get("headline", "")).strip()
    scene = str(parsed.get("scene", "")).strip()
    moment = str(parsed.get("moment", "")).strip()
    meaning = str(parsed.get("meaning", "")).strip()

    parts: list[str] = []

    if headline:
        parts.append(f"**{headline}**")

    if scene:
        parts.append(f"*{scene}*")

    if moment:
        parts.append(moment)

    if meaning:
        parts.append(f"**The why:** {meaning}")

    if persona_display:
        parts.append(f"— *{persona_display}*")

    return "\n\n".join(parts)


def _assemble_verdict(parsed: dict, persona_display: str) -> str:
    """Jordan Rivera — 'The Verdict' format.

    Courtroom structure: case callout → argument → receipts blockquote → verdict ruling.
    """
    headline = str(parsed.get("headline", "")).strip()
    case = str(parsed.get("case", "")).strip()
    argument = str(parsed.get("argument", "")).strip()
    receipts: list = parsed.get("receipts", []) or []
    verdict = str(parsed.get("verdict", "")).strip()

    parts: list[str] = []

    if headline:
        parts.append(f"**{headline}**")

    if case:
        parts.append(f"> ⚖️ **The Case:** {case}")

    if argument:
        parts.append(argument)

    if receipts:
        receipt_lines = ["**THE RECEIPTS**"]
        for r in receipts[:4]:
            text = str(r).strip()
            if text:
                receipt_lines.append(f"> • {text}")
        parts.append("\n".join(receipt_lines))

    if verdict:
        parts.append(
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"## VERDICT: {verdict}\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )

    if persona_display:
        parts.append(f"— *{persona_display}*")

    return "\n\n".join(parts)


def _assemble_index(parsed: dict, persona_display: str) -> str:
    """Keisha Williams — 'The Index' format.

    Analyst structure: metric code block → definition → standouts bullets → implication.
    """
    headline = str(parsed.get("headline", "")).strip()
    metric_name = str(parsed.get("metric_name", "THE INDEX")).strip()
    headline_value = str(parsed.get("headline_value", "")).strip()
    definition = str(parsed.get("definition", "")).strip()
    standouts: list = parsed.get("standouts", []) or []
    implication = str(parsed.get("implication", "")).strip()

    parts: list[str] = []

    if headline:
        parts.append(f"**{headline}**")

    # Monospaced metric block
    metric_lines = [
        "```",
        f"THE INDEX: {metric_name}",
        "─────────────────────────",
    ]
    if headline_value:
        metric_lines.append(headline_value)
    metric_lines.append("```")
    parts.append("\n".join(metric_lines))

    if definition:
        parts.append(definition)

    if standouts:
        standout_lines = ["__Standouts__"]
        for s in standouts[:3]:
            name = str(s.get("name", "")).strip()
            value = str(s.get("value", "")).strip()
            note = str(s.get("note", "")).strip()
            if name:
                entry = f"> • **{name}**"
                if value:
                    entry += f" — {value}"
                if note:
                    entry += f", {note}"
                standout_lines.append(entry)
        parts.append("\n".join(standout_lines))

    if implication:
        parts.append(f"*Why it matters:* {implication}")

    if persona_display:
        parts.append(f"— *{persona_display}*")

    return "\n\n".join(parts)


# Maps format_style strings to renderer functions.
# Each renderer takes (parsed: dict, persona_display: str) → str.
_RENDERERS = {
    "analytics": _assemble_analytics,
    "hot_take": _assemble_hot_take,
    "tactical": _assemble_tactical,
    "recap": _assemble_recap,
    "moment": _assemble_moment,
    "verdict": _assemble_verdict,
    "index": _assemble_index,
    "default": _assemble_default,
}


def _assemble_article(parsed: dict, persona_display: str, format_style: str = "default") -> str:
    """Dispatch to the correct renderer based on persona format_style.

    Falls back to _assemble_default when the style key is unrecognised.
    The Persona object owns format_style; call sites that already pass
    persona.display_name should also pass persona.format_style.
    """
    renderer = _RENDERERS.get(format_style, _assemble_default)
    return renderer(parsed, persona_display)
