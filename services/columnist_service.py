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

    # 1. Strip markdown code fences ONLY when they wrap the whole response.
    # Anchored at start/end so backticks INSIDE the JSON (e.g. multi-line
    # code blocks within a body string) are preserved as content.
    fence_match = re.match(r"^```(?:json)?\s*([\s\S]*?)```\s*$", text)
    if fence_match:
        text = fence_match.group(1).strip()
    else:
        # Possibly truncated — strip a leading fence if present, trailing if
        # at the very end. Don't touch fences in the middle.
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)

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

    def _escape_newlines_in_strings(s: str) -> str:
        """Walk the string and escape literal newlines that appear inside JSON
        string values. Required because Claude sometimes returns multi-line code
        blocks (```...```) inside a JSON string with raw newlines instead of \\n
        — which is invalid JSON. Tracks string boundaries via unescaped quotes.
        """
        out: list[str] = []
        in_string = False
        i = 0
        while i < len(s):
            c = s[i]
            if c == "\\" and i + 1 < len(s):
                # Pass through escape sequences as-is.
                out.append(c)
                out.append(s[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = not in_string
                out.append(c)
                i += 1
                continue
            if in_string and c == "\n":
                out.append("\\n")
                i += 1
                continue
            if in_string and c == "\r":
                out.append("\\r")
                i += 1
                continue
            if in_string and c == "\t":
                out.append("\\t")
                i += 1
                continue
            out.append(c)
            i += 1
        return "".join(out)

    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
        log.warning(
            "columnist_service: parsed JSON is not a dict for %s/%s — type: %s",
            persona_id, category, type(result).__name__,
        )
        return None
    except json.JSONDecodeError:
        # Retry with literal newlines/tabs/CRs escaped inside string values.
        # Catches the common Claude failure mode where multi-line code blocks
        # inside a body string are emitted with raw newlines.
        try:
            normalized = _escape_newlines_in_strings(text)
            result = json.loads(normalized)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError as exc:
            log.warning(
                "columnist_service: _tolerant_json_parse failed for %s/%s: %s | cleaned text: %r",
                persona_id, category, exc, text[:120],
            )
        return None


_REFUSAL_PREFIXES: tuple[str, ...] = (
    "i appreciate",
    "i cannot",
    "i can't",
    "i need to flag",
    "i'm unable",
    "i am unable",
    "without sufficient",
    "the context doesn't",
    "the context does not",
    "based on the limited",
    "based on the provided context",
    "unfortunately, i",
    "i don't have",
    "i do not have",
    # The Race TBD / editor's note pattern
    "tbd —",
    "no candidate data",
    "editor's note",
    "i don't have enough",
    "unfortunately there",
)


def _is_refusal(body: str) -> bool:
    """Return True when the LLM responded with meta-commentary instead of an article.

    Checks whether the assembled body starts with any known refusal/explanation
    prefix (case-insensitive).  When True the caller should return None so the
    post is silently skipped rather than spamming Discord with refusal text.
    """
    if not body:
        return False
    low = body.strip().lower()
    return any(low.startswith(p) for p in _REFUSAL_PREFIXES)


async def generate(  # noqa: PLR0912, PLR0915
    pool,
    league_id: int,
    season: int,
    persona_id: str | ColumnistRequest,
    category: str | None = None,
    context: dict | None = None,
    subject_team_ids: list[int] | None = None,
    subject_player_ids: list[int] | None = None,
    _capture_prompt: dict | None = None,
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

    # Columnist ride-along: when a sidecar is attached to a specific persona,
    # suppress every other persona at the entrypoint so no LLM call is made
    # and no article lands in Discord. Returning None here is identical to any
    # other "skip this fire" outcome the callers already handle.
    from services import columnist_ride_along as _cra
    if not _cra.should_fire_for(_persona_id):
        log.debug(
            "columnist_service: persona %r suppressed by ride-along (target=%r)",
            _persona_id,
            _cra.target_persona_id(),
        )
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

    # Resolve the effective output shape for this specific category.
    # category_shape_overrides takes precedence over output_shape_override so
    # personas with multiple renderers (e.g. Pat Chen: Observation vs POTM) each
    # send exactly ONE format instruction — never two conflicting ones.
    _effective_shape = (
        persona.category_shape_overrides.get(_category)
        or persona.output_shape_override
    )

    user_content_parts = [
        task_line,
        "",
        "Context:",
        json.dumps(_context, indent=2, default=str),
    ]
    if signals_block:
        user_content_parts += ["", signals_block]
    if intel_block:
        user_content_parts += ["", intel_block]
    if memory_block:
        user_content_parts += ["", memory_block]
    # Only inject the default format example when the persona has no custom shape.
    # Custom-shape personas (output_shape_override or category_shape_overrides) get
    # their format instruction from output_shape_rule in the system prompt only —
    # injecting it here too would give the LLM two conflicting format specs.
    if not _effective_shape:
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
        "ANALYTICAL VARIETY (mandatory): Do NOT default to 'Player X had a big game but his team "
        "needed more' or 'Y's brilliance wasn't enough.' Find a fresh angle EVERY article. "
        "Acceptable angles: team defense at the rim/perimeter/forcing turnovers; a specific "
        "matchup that turned the game (player vs player, scheme vs scheme); a teammate's quiet "
        "contribution that made the star's night possible; a rivalry storyline that gives the game "
        "extra weight; a coaching decision (rotation, late-game) that swung the result; a "
        "pace/efficiency angle (TS%, eFG%, pace differential); a trend across the recent batch "
        "(e.g. 'this is the third time TEAM has X'). Reference at least one OTHER player in the "
        "game by name with a specific stat. The 'team needed more' framing is BANNED unless "
        "explicitly named as a cliche the columnist is rejecting.\n\n"
        "HEADLINE RULE (mandatory): The headline must convey the WHAT at a glance. A reader who "
        "scrolls past should know what happened without opening it. Include at least ONE of: a "
        "player's last name, a team code, a specific number, or a specific event. Generic "
        "vibes-headlines are BANNED. Better: 'Brunson's 38 Lifts NYK Over BOS in Double-OT "
        "Thriller' or 'LAL's Defense Holds MIA Under 90 for First Time This Season'.\n\n"
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
        _effective_shape
        if _effective_shape
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

    # _capture_prompt is a ride-along-only internal hook (see services/columnist_ride_along.py).
    # Populate it before the API call so the full prompt is preserved even if the call fails.
    # Default None is a no-op — production callers pay zero cost.
    if _capture_prompt is not None:
        _capture_prompt["system"] = system_prompt
        _capture_prompt["user"] = user_content
        _capture_prompt["model"] = "claude-haiku-4-5-20251001"
        _capture_prompt["max_tokens"] = 1400

    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=api_key)
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1400,  # raised from 900 — passthrough personas produce longer bodies; truncation broke JSON parse
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = message.content[0].text.strip()

        # Ride-along: capture the raw LLM response alongside the prompt.
        if _capture_prompt is not None:
            _capture_prompt["raw_llm_response"] = raw

        # DIAGNOSTIC — log first 800 chars of every LLM response so we can
        # verify each persona's actual output shape.  Leave in place.
        log.info(
            "columnist_service [RAW] persona=%s category=%s | %.800s",
            _persona_id, _category, raw,
        )

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
                        _body = _assemble_article(_json_candidate, persona_display, _fmt, ctx=_context)
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
        # Exception: trade_report must go through _assemble_trade_report so that
        # [FRAMING]/[ANALYSIS] markers are stripped and asset blocks are rendered.
        # Passthrough personas also must go through _assemble_article (not raw body)
        # so _dedupe_headline and the empty-body guard fire correctly.
        _bypass_old_shape = _category == "trade_report" or persona.format_style in (
            "passthrough", "tank_watch", "potm",
        )
        if "headline" in parsed and "body" in parsed and "lede" not in parsed and not _bypass_old_shape:
            headline = str(parsed["headline"]).strip()
            body = str(parsed["body"]).strip()[:1400]
            if _is_refusal(body):
                log.warning(
                    "columnist_service: %s/%s returned meta-commentary (old shape), skipping: %r",
                    _persona_id, _category, body[:80],
                )
                return None
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
        # Personas with a named format_style that has its own renderer also pass
        # with headline alone — the renderer handles empty optional fields.
        headline = str(parsed.get("headline", "")).strip()
        lede = str(parsed.get("lede", "")).strip()
        _uses_custom_shape = bool(_effective_shape)
        _has_named_renderer = persona.format_style in (
            "moment", "verdict", "index", "hot_take",
            "analytics", "tactical", "recap", "potm", "trade_report",
            "passthrough", "tank_watch",
        )
        _required_ok = headline and (lede or _uses_custom_shape or _has_named_renderer)
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
        body = _assemble_article(parsed, persona_display, _fmt, ctx=_context)

        # Passthrough renderer returns None when body is empty — skip the post.
        if body is None:
            return None

        # Refusal detector: if the LLM explained lack of data instead of writing
        # an article, skip silently rather than posting the refusal to Discord.
        if _is_refusal(body):
            log.warning(
                "columnist_service: %s/%s returned meta-commentary, skipping: %r",
                _persona_id, _category, body[:80],
            )
            return None

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


def _dedupe_headline(headline: str, body: str) -> str:
    """Strip a duplicate headline from the start of body text.

    When the LLM opens the body with the same text as the headline (with or
    without ** markdown), remove that first line so the assembled post doesn't
    open "**Headline**\\n\\n**Headline**".  Comparison is case-insensitive and
    ignores leading/trailing ** and whitespace.

    Two-layer check:
    1. Exact match (case-insensitive, markdown-stripped) — strip that first line.
    2. Prefix match — the first line STARTS WITH the normalised headline followed
       by a separator (—, :, ., or whitespace-newline boundary).  Strip up to and
       including the separator so trailing elaboration ("… and what it means for
       ROY") is also removed.  Only applied when the exact match fails.
    """
    if not headline or not body:
        return body
    # Strip the first line of body and compare to headline (both normalised).
    first_line_end = body.find("\n")
    first_line = body[:first_line_end].strip() if first_line_end != -1 else body.strip()
    # Normalise: strip markdown bold markers and whitespace.
    normalised_first = re.sub(r"^\*+|\*+$", "", first_line).strip().lower()
    normalised_headline = re.sub(r"^\*+|\*+$", "", headline).strip().lower()
    # Layer 1: exact match.
    if normalised_first == normalised_headline:
        remainder = body[first_line_end:].lstrip("\n") if first_line_end != -1 else ""
        return remainder
    # Layer 2: prefix match — first line starts with headline followed by a
    # recognised separator.  Only strip when the headline is meaningfully long
    # (>= 10 chars) to avoid false positives on very short headlines.
    if len(normalised_headline) >= 10 and normalised_first.startswith(normalised_headline):
        after = normalised_first[len(normalised_headline):]
        if after and after[0] in ("—", ":", ".", " ", "\t"):
            # The first line is just the headline with a chaser — drop the whole line.
            remainder = body[first_line_end:].lstrip("\n") if first_line_end != -1 else ""
            return remainder
    return body


def _assemble_passthrough(parsed: dict, persona_display: str) -> str | None:
    """Passthrough renderer — emits body verbatim, never adds structured fields.

    Used for Maya Chen, Jordan Rivera, Keisha Williams, and Dr. Pat Chen.
    These personas format their own Discord markdown inside the body; the
    renderer's only job is to bolt on the headline and byline.
    Returns None when body is missing so callers can skip the post entirely
    rather than emitting an empty stub.
    """
    headline = str(parsed.get("headline", "")).strip()
    body = str(parsed.get("body", "")).strip()

    # Remove headline duplication at the top of body.
    body = _dedupe_headline(headline, body)
    # Re-strip after dedupe in case only whitespace remained.
    body = body.strip()

    if not body:
        log.warning(
            "columnist_service: passthrough persona %r returned empty body — skipping post",
            persona_display,
        )
        return None

    parts: list[str] = []
    # Do NOT prepend headline here — the Discord embed title already shows it.
    # Including it in body would cause a doubled title in every passthrough post.
    parts.append(body)
    parts.append(f"— *{persona_display}*")
    return "\n\n".join(parts)


def _assemble_tank_watch(parsed: dict, persona_display: str, ctx: dict | None = None) -> str:
    """Darius Cole tank/draft watch renderer.

    Expects body to contain:
    - ODDS LADDER section (teams with lottery percentages)
    - Stock Watch section (rising/falling prospects)
    - A short Darius take

    Body is formatted by the LLM following Darius's template instruction.
    This renderer bolts on headline, strips duplicate headline from body,
    and adds the byline.  Falls back to _assemble_analytics when body is
    absent (e.g. old-shape LLM response).
    """
    headline = str(parsed.get("headline", "")).strip()
    body = str(parsed.get("body", "")).strip()

    if not body:
        # Old analytics-shape response — fall through to analytics renderer.
        return _assemble_analytics(parsed, persona_display)

    body = _dedupe_headline(headline, body)

    parts: list[str] = []
    # Do NOT prepend headline here — the Discord embed title already shows it.
    parts.append(body)
    parts.append(f"— *{persona_display}*")
    return "\n\n".join(parts)


def _assemble_default(parsed: dict, persona_display: str) -> str:
    """Original template-stamp format — fallback for unrecognised styles.

    If the LLM returned `body` (personas using custom body templates like
    Maya/Jordan/Keisha), emit body verbatim — those templates already contain
    fully formatted Discord markdown. Otherwise fall back to assembling from
    structured fields: __Key Numbers__ + __The Read__ + Verdict.
    """
    headline = str(parsed.get("headline", "")).strip()
    body = str(parsed.get("body", "")).strip()
    if body:
        body = _dedupe_headline(headline, body)
        # Persona used a body-template shape — body IS the formatted content.
        parts: list[str] = [body]
        if persona_display:
            parts.append(f"— *{persona_display}*")
        return "\n\n".join(parts)

    lede = str(parsed.get("lede", "")).strip()
    key_stats: list[dict] = parsed.get("key_stats", []) or []
    bullets: list[str] = parsed.get("bullets", []) or []
    verdict = str(parsed.get("verdict", "")).strip()

    parts: list[str] = []

    if lede:
        parts.append(lede)

    if key_stats:
        stat_lines = ["## Key Numbers"]
        for stat in key_stats[:3]:
            label = str(stat.get("label", "")).strip()
            value = str(stat.get("value", "")).strip()
            if label and value:
                stat_lines.append(f"> • **{label}**: {value}")
        parts.append("\n".join(stat_lines))

    if bullets:
        bullet_lines = ["## The Read"]
        for bullet in bullets[:3]:
            text = str(bullet).strip()
            if text:
                bullet_lines.append(f"• {text}")
        parts.append("\n".join(bullet_lines))

    if verdict:
        parts.append(f"**Verdict:** {verdict}")

    if persona_display:
        parts.append(f"— *{persona_display}*")

    return "\n\n".join(parts)


def _assemble_analytics(parsed: dict, persona_display: str) -> str:
    """Analytics format — bold-label bullet stats, minimal prose, no section headers.

    Used by data-driven writers (Marcus Brooks, Darius Cole).
    Stats render as "**Label:** value" bullets — reliable on Discord mobile and
    desktop without the alignment issues that plague code-block tables.
    Ends with 'Bottom line:' instead of 'Verdict:' — the label itself signals the voice.
    """
    lede = str(parsed.get("lede", "")).strip()
    key_stats: list[dict] = parsed.get("key_stats", []) or []
    bullets: list[str] = parsed.get("bullets", []) or []
    verdict = str(parsed.get("verdict", "")).strip()

    parts: list[str] = []

    # Stats as bold-label bullet list — works on all Discord clients.
    if key_stats:
        cleaned = [
            (str(s.get("label", "")).strip(), str(s.get("value", "")).strip())
            for s in key_stats[:4]
        ]
        stat_lines = [f"**{label}:** {value}" for label, value in cleaned if label and value]
        if stat_lines:
            parts.append("\n".join(stat_lines))

    if lede:
        parts.append(lede)

    bullet_parts = [f"• {str(b).strip()}" for b in bullets[:3] if str(b).strip()]
    if bullet_parts:
        parts.append("\n".join(bullet_parts))

    if verdict:
        parts.append(f"**Bottom line:** {verdict}")

    if persona_display:
        parts.append(f"— *{persona_display}*")

    return "\n\n".join(parts)


def _assemble_hot_take(parsed: dict, persona_display: str) -> str:
    """Hot Take Hour debate renderer.

    Prefers the new `turns` array shape: assembles SPEAKER: line pairs separated
    by a blank line so each exchange reads as a back-and-forth.  Falls back to
    the old `body` string for any cached articles that used the previous shape.

    Headline is NOT prepended — the Discord embed title already shows it, so
    including it in the body would produce a doubled title.
    """
    turns: list = parsed.get("turns") or []

    parts: list[str] = []

    if turns:
        # New shape: [{speaker, line}, ...]
        turn_lines: list[str] = []
        for t in turns[:4]:
            speaker = str(t.get("speaker", "")).strip().upper()
            line = str(t.get("line", "")).strip()
            if speaker and line:
                turn_lines.append(f"**{speaker}:** {line}")
        if turn_lines:
            parts.append("\n\n".join(turn_lines))
    else:
        # Old shape fallback: body is a pre-formatted DAVE: / TONY: string.
        body = str(parsed.get("body", "")).strip()
        if body:
            # Bold the speaker labels for Discord readability.
            import re as _re
            body = _re.sub(r"\b(DAVE|TONY):", r"**\1:**", body)
            parts.append(body)

    # If neither turns nor body produced content, return None so the post is skipped.
    if not parts:
        return None

    if persona_display:
        parts.append(f"— *{persona_display}*")

    return "\n\n".join(parts)


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
    # Cap at 3 total so we never get 4-bullet walls of text.
    clean_bullets = [str(b).strip() for b in bullets[:3] if str(b).strip()]
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
        parts.append("## What Worked")
        for b in worked:
            parts.append(f"• {b}")

    if didnt:
        parts.append("## What Didn't")
        for b in didnt:
            parts.append(f"• {b}")

    if verdict:
        parts.append(f"## The Adjustment\n{verdict}")

    if persona_display:
        parts.append(f"— *{persona_display}*")

    return "\n\n".join(parts)


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
    Falls back to a single-line summary when only headline is present so
    Discord never receives a blank post.
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

    # If only the headline came back, emit a one-liner so the post isn't empty.
    if not scene and not moment and not meaning and headline:
        parts.append(f"*Maya Chen on {headline}*")

    if persona_display:
        parts.append(f"— *{persona_display}*")

    return "\n\n".join(parts)


def _assemble_verdict(parsed: dict, persona_display: str) -> str:
    """Jordan Rivera — 'The Verdict' format.

    Courtroom structure: case callout → argument → receipts blockquote → verdict ruling.
    Falls back to a single-line ruling when only headline is present.
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
            "━" * 20 + "\n"
            f"## VERDICT: {verdict}\n"
            + "━" * 20
        )

    # If only the headline came back, emit a minimal ruling so the post isn't empty.
    if not case and not argument and not verdict and headline:
        parts.append(f"*Jordan Rivera: The verdict is in on {headline}.*")

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

    # If only the headline came back, emit a minimal stat note so the post isn't empty.
    if not headline_value and not definition and not standouts and not implication and headline:
        parts.append(f"*Keisha Williams: The numbers tell the story on {headline}.*")

    if persona_display:
        parts.append(f"— *{persona_display}*")

    return "\n\n".join(parts)


def _parse_trade_body(body: str) -> tuple[str, str]:
    """Split a trade report body string on [FRAMING] / [ANALYSIS] sentinels (legacy scheme).

    Returns (framing, analysis).  If sentinels are missing, framing gets the
    raw body verbatim and analysis is empty string — caller renders verbatim
    so output is never blank.
    """
    import re
    pattern = re.compile(r"\*{0,2}\[(?:FRAMING|ANALYSIS)\]\*{0,2}", re.IGNORECASE)
    _kw2 = re.compile(r"\b(FRAMING|ANALYSIS)\b", re.IGNORECASE)
    raw_markers = []
    for m in pattern.finditer(body):
        kw_m = _kw2.search(m.group(0))
        if kw_m:
            raw_markers.append((kw_m.group(1).upper(), m.start(), m.end()))
    if not raw_markers:
        # No sentinels — return raw body as framing so it still renders.
        return body, ""

    chunks: dict[str, str] = {}
    for i, (key, _start, end) in enumerate(raw_markers):
        text_start = end
        text_end = raw_markers[i + 1][1] if i + 1 < len(raw_markers) else len(body)
        chunks[key] = body[text_start:text_end].strip()

    return chunks.get("FRAMING", ""), chunks.get("ANALYSIS", "")


def _parse_marcus_cole_body(body: str) -> tuple[str, str]:
    """Split a Marcus Cole trade body on [TEAM_A] / [TEAM_B] sentinels (new scheme).

    Returns (team_a_blurb, team_b_blurb).  If sentinels are absent, returns
    ("", "") so the caller falls through to legacy or raw-body rendering.

    Lenient match handles LLM variants like **[TEAM_A]**, *[TEAM_A]*, or bare [TEAM_A].
    """
    import re
    pattern = re.compile(r"\*{0,2}\[(?:TEAM_A|TEAM_B)\]\*{0,2}", re.IGNORECASE)
    _kw = re.compile(r"\b(TEAM_A|TEAM_B)\b", re.IGNORECASE)
    markers = []
    for m in pattern.finditer(body):
        kw_m = _kw.search(m.group(0))
        if kw_m:
            markers.append((kw_m.group(1).upper(), m.start(), m.end()))
    if not markers:
        return "", ""

    chunks: dict[str, str] = {}
    for i, (key, _start, end) in enumerate(markers):
        text_start = end
        text_end = markers[i + 1][1] if i + 1 < len(markers) else len(body)
        chunks[key] = body[text_start:text_end].strip()

    return chunks.get("TEAM_A", ""), chunks.get("TEAM_B", "")


def _assemble_trade_report(parsed: dict, persona_display: str, ctx: dict | None = None) -> str:
    """Trade report — structured swap blocks that make get/give visible at a glance.

    Structural data (teams, assets) comes from ctx; the LLM supplies headline,
    per-team blurbs, and optional grades.

    Marker scheme detection (three paths, in priority order):
    1. New scheme: [TEAM_A] / [TEAM_B] — per-team blurbs interleaved with asset blocks.
    2. Legacy scheme: [FRAMING] / [ANALYSIS] — backward-compat for articles already in DB.
    3. No markers: render raw body as *Analysis:* prose above asset blocks.

    Falls back gracefully if ctx is absent or malformed.
    """
    headline = str(parsed.get("headline", "")).strip()

    raw_body = str(parsed.get("body", "")).strip()
    # Strip headline duplication before parsing sentinels (defense-in-depth).
    raw_body = _dedupe_headline(headline, raw_body)

    # Detect marker scheme.
    team_a_blurb, team_b_blurb = _parse_marcus_cole_body(raw_body)
    use_new_scheme = bool(team_a_blurb or team_b_blurb)

    framing = analysis = ""
    if not use_new_scheme:
        framing, analysis = _parse_trade_body(raw_body)
        # Backward-compat: older top-level keys (pre-body era).
        if not framing and not analysis:
            framing = str(parsed.get("framing", "")).strip()
            analysis = str(parsed.get("analysis", "")).strip()

    ctx = ctx or {}
    teams: list[dict] = ctx.get("teams") or []

    # Grade labels — optional; only shown when the LLM supplied them.
    grade_keys = ["grade_a", "grade_b", "grade_c"]
    grades = [str(parsed.get(k, "")).strip() for k in grade_keys]
    grades = [g for g in grades if g]

    out: list[str] = []
    # Do NOT prepend headline here — the Discord embed title already shows it.

    def _render_item(item: dict) -> str:
        """Format a single asset line: Player Name (PG, 28, OVR 84) or pick label."""
        itype = item.get("type", "")
        name = str(item.get("name", "")).strip()
        if itype == "player":
            meta_parts: list[str] = []
            if item.get("position"):
                meta_parts.append(str(item["position"]))
            if item.get("age") is not None:
                meta_parts.append(str(item["age"]))
            if item.get("ovr") is not None:
                meta_parts.append(f"OVR {item['ovr']}")
            meta = f" ({', '.join(meta_parts)})" if meta_parts else ""
            return f"• {name}{meta}"
        elif itype == "pick":
            via = item.get("via")
            suffix = f" (via {via})" if via else ""
            return f"• {name}{suffix}"
        else:
            # cash or unknown
            return f"• {name}" if name else "• Cash considerations"

    def _render_team_block(team: dict, blurb: str) -> list[str]:
        """Render one team's receives block with optional per-team blurb."""
        team_name = str(team.get("name", "")).strip()
        gets: list[dict] = team.get("gets") or []
        MAX_ITEMS = 8
        shown = gets[:MAX_ITEMS]
        overflow = len(gets) - MAX_ITEMS
        block_lines = [f"> 🔄 **{team_name}** receives"]
        if shown:
            for item in shown:
                block_lines.append(f"> {_render_item(item)}")
            if overflow > 0:
                block_lines.append(f"> *(…and {overflow} more)*")
        else:
            block_lines.append("> *(nothing)*")
        lines: list[str] = ["\n".join(block_lines)]
        if blurb:
            lines.append(blurb)
        return lines

    if teams:
        if use_new_scheme:
            # New scheme: per-team blurb follows each asset block.
            blurbs = [team_a_blurb, team_b_blurb]
            for i, team in enumerate(teams[:3]):
                blurb = blurbs[i] if i < len(blurbs) else ""
                out.extend(_render_team_block(team, blurb))
        else:
            # Legacy scheme: framing above all blocks, analysis below.
            if framing:
                if not analysis and raw_body and framing == raw_body:
                    out.append(f"*Analysis:* {framing}")
                else:
                    out.append(f"*{framing}*")
            for i, team in enumerate(teams[:3]):
                team_name = str(team.get("name", f"Team {i + 1}")).strip()
                gets: list[dict] = team.get("gets") or []
                MAX_ITEMS = 8
                shown = gets[:MAX_ITEMS]
                overflow = len(gets) - MAX_ITEMS
                block_lines = [f"> 🔄 **{team_name}** receives"]
                if shown:
                    for item in shown:
                        block_lines.append(f"> {_render_item(item)}")
                    if overflow > 0:
                        block_lines.append(f"> *(…and {overflow} more)*")
                else:
                    block_lines.append("> *(nothing)*")
                out.append("\n".join(block_lines))
            if analysis:
                out.append(analysis)
    else:
        # ctx missing or malformed — fall back to prose from existing fields.
        proposer_sends = ctx.get("proposer_sends") or []
        counterparty_sends = ctx.get("counterparty_sends") or []
        proposer = ctx.get("proposer_team", "Team A")
        counterparty = ctx.get("counterparty_team", "Team B")
        if proposer_sends or counterparty_sends:
            out.append(f"> 🔄 **{counterparty}** receives\n> " + "\n> ".join(f"• {l}" for l in counterparty_sends))
            out.append(f"> 🔄 **{proposer}** receives\n> " + "\n> ".join(f"• {l}" for l in proposer_sends))
        # Fallback prose when no ctx at all.
        if not use_new_scheme and raw_body and framing:
            out.append(f"*Analysis:* {framing}")
        elif use_new_scheme:
            if team_a_blurb:
                out.append(team_a_blurb)
            if team_b_blurb:
                out.append(team_b_blurb)

    if grades:
        # Pair each grade with its team name when possible.
        grade_parts = []
        for idx, grade in enumerate(grades):
            team_name = teams[idx]["name"] if idx < len(teams) else f"Team {idx + 1}"
            grade_parts.append(f"**{team_name}:** {grade}")
        out.append("**Grade:** " + " · ".join(grade_parts))

    if persona_display:
        out.append(f"— *{persona_display}*")

    return "\n\n".join(out)


def _parse_potm_body(body: str) -> tuple[str, str, str]:
    """Split a POTM body string on [EAST] / [WEST] / [CLOSER] sentinels.

    Returns (east_blurb, west_blurb, closer).  If a sentinel is missing the
    affected field is empty string and the caller will fall back to rendering
    the raw body verbatim so output is never blank.

    Lenient match handles LLM variants like **[EAST]**, *[EAST]*, or bare [EAST].
    """
    import re
    # Match optional surrounding bold/italic markers so **[EAST]** is caught.
    pattern = re.compile(r"\*{0,2}\[(?:EAST|WEST|CLOSER)\]\*{0,2}", re.IGNORECASE)
    _kw = re.compile(r"\b(EAST|WEST|CLOSER)\b", re.IGNORECASE)
    markers = []
    for m in pattern.finditer(body):
        kw_match = _kw.search(m.group(0))
        if kw_match:
            markers.append((kw_match.group(1).upper(), m.start(), m.end()))
    if not markers:
        return body, "", ""

    chunks: dict[str, str] = {}
    for i, (key, _start, end) in enumerate(markers):
        text_start = end
        text_end = markers[i + 1][1] if i + 1 < len(markers) else len(body)
        chunks[key] = body[text_start:text_end].strip()

    east_blurb = chunks.get("EAST", "")
    west_blurb = chunks.get("WEST", "")
    closer = chunks.get("CLOSER", "")

    # If any required section missing, surface the raw body so nothing is blank.
    if not east_blurb and not west_blurb:
        return body, "", ""

    return east_blurb, west_blurb, closer


def _assemble_potm(parsed: dict, persona_display: str, ctx: dict | None = None) -> str:
    """Player of the Month — features both conference winners side-by-side.

    Uses context (which carries the actual stats and player/team data) for the
    structural pieces, and the LLM-supplied parsed dict for narrative
    (east_blurb, west_blurb, closer).
    """
    headline = str(parsed.get("headline", "")).strip()

    # Body uses [EAST] / [WEST] / [CLOSER] sentinels.  Fall back to the raw body
    # string rendered verbatim if any marker is missing (never produce blank output).
    raw_body = str(parsed.get("body", "")).strip()
    # Strip headline duplication before parsing sentinels (defense-in-depth).
    raw_body = _dedupe_headline(headline, raw_body)
    east_blurb, west_blurb, closer = _parse_potm_body(raw_body)

    # Backward-compat: if LLM returned the older shape with east_blurb/west_blurb/
    # closer as their own top-level keys (and body is empty/missing), use those.
    if not east_blurb and not west_blurb and not closer:
        east_blurb = str(parsed.get("east_blurb", "")).strip()
        west_blurb = str(parsed.get("west_blurb", "")).strip()
        closer = str(parsed.get("closer", "")).strip()

    ctx = ctx or {}
    month_label = str(ctx.get("month_label", "")).strip()
    east = ctx.get("east_winner") or {}
    west = ctx.get("west_winner") or {}

    def _stat_line(w: dict) -> str:
        ppg = w.get("ppg")
        rpg = w.get("rpg")
        apg = w.get("apg")
        games = w.get("games")
        parts = []
        if ppg is not None: parts.append(f"{ppg:.1f}p")
        if rpg is not None: parts.append(f"{rpg:.1f}r")
        if apg is not None: parts.append(f"{apg:.1f}a")
        return " · ".join(parts) + (f"  ({games} games)" if games is not None else "")

    # Detect fallback: _parse_potm_body returns raw body in east_blurb when sentinels
    # are missing (west_blurb and closer will both be "").  In that case render the
    # ctx asset blocks with the raw body as a prose paragraph rather than slotting
    # it into just the East section.
    body_fallback = bool(east_blurb and not west_blurb and not closer)

    out: list[str] = []
    # Do NOT prepend headline here — the Discord embed title already shows it.
    if month_label:
        out.append(f"*{month_label}*")

    if body_fallback and east_blurb:
        out.append(east_blurb)

    divider = "━" * 24
    out.append(divider)

    if east:
        out.append(f"🌅 **EAST — {east.get('player', '?')}**  ·  {east.get('team', '?')}")
        if not body_fallback and east_blurb:
            out.append(east_blurb)
        out.append(f"> `{_stat_line(east)}`")

    if west:
        out.append("")  # spacer between conferences
        out.append(f"🌇 **WEST — {west.get('player', '?')}**  ·  {west.get('team', '?')}")
        if west_blurb:
            out.append(west_blurb)
        out.append(f"> `{_stat_line(west)}`")

    out.append(divider)

    if closer:
        out.append(closer)

    if persona_display:
        out.append(f"— *{persona_display}*")

    return "\n".join(out)


# Maps format_style strings to renderer functions.
# Most renderers take (parsed: dict, persona_display: str) → str.
# Renderers in _CTX_RENDERERS additionally accept an optional `ctx` dict for
# structural data (stats, names) that comes from the calling context
# rather than the LLM.
_RENDERERS = {
    "analytics": _assemble_analytics,
    "hot_take": _assemble_hot_take,
    "tactical": _assemble_tactical,
    "recap": _assemble_recap,
    "moment": _assemble_moment,
    "verdict": _assemble_verdict,
    "index": _assemble_index,
    "potm": _assemble_potm,
    "trade_report": _assemble_trade_report,
    "passthrough": _assemble_passthrough,
    "tank_watch": _assemble_tank_watch,
    "default": _assemble_default,
}


def _assemble_article(
    parsed: dict,
    persona_display: str,
    format_style: str = "default",
    ctx: dict | None = None,
) -> str | None:
    """Dispatch to the correct renderer based on persona format_style.

    Falls back to _assemble_default when the style key is unrecognised.
    `ctx` is only consumed by renderers that need extra structural data
    (currently just `potm`); other renderers ignore it.
    Returns None when the renderer signals the post should be skipped
    (currently only passthrough when body is empty).
    """
    renderer = _RENDERERS.get(format_style, _assemble_default)
    if format_style in ("potm", "trade_report", "tank_watch"):
        return renderer(parsed, persona_display, ctx)
    return renderer(parsed, persona_display)
