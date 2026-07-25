"""Pure article-body renderers and body-parsing helpers for the columnist
system: one _assemble_* function per format_style (analytics, hot_take,
recap, potm, trade_report, passthrough, tank_watch, default -- string-body
renderers, dispatched via _RENDERERS/_assemble_article), the [SENTINEL]-based
body parsers they call (_parse_trade_body, _parse_marcus_cole_body,
_parse_potm_body), and _assemble_article, the dispatcher that routes to the
right string-body renderer.

No DB, no async, no LLM calls -- these take the LLM's already-parsed JSON
dict and a persona display string, and return the final Discord-ready
article body string (or, for the EmbedField-based renderers below, an
EmbedData).

Extracted from columnist_service.py (Phase 3 opportunistic split, see
HANDOFF.md). Only called internally by columnist_service.generate via
_assemble_article -- no external caller touches the string-body renderers
directly.

EmbedField-based renderers (Phase 1 fix B1)
--------------------------------------------
_assemble_moment, _assemble_verdict, _assemble_index, _assemble_recap, and
_assemble_tactical return an EmbedData with real fields: list[EmbedField]
instead of a flat string -- they genuinely use Discord's field-grid layout
rather than reformatting text within one description blob. They are
DELIBERATELY NOT in _RENDERERS / reachable via _assemble_article: every
other renderer in this module returns a plain string body that
columnist_service.generate() stores as article_repo.body and slices into an
EmbedData.description at the call site, and mixing an EmbedData-returning
renderer into that same dispatch path would silently break that contract for
whichever caller hits it. As of this fix no live persona sets format_style
to any of these five values (grep confirmed), so they are current dead code
by design -- revived and correctness-tested, but not yet wired to any
persona. Phase 2 picks which persona gets which renderer and updates the
relevant _maybe_post_* call site in sim_content_pipeline.py to build its
embed from the returned EmbedData directly (fields=..., description=...)
instead of the current description=body[:N] pattern.
"""
from __future__ import annotations

import logging
import re

from services.announcer_protocol import EmbedData, EmbedField

log = logging.getLogger(__name__)

# Discord embed field values cap at 1024 chars; staying under 950 leaves
# headroom for markdown rendering overhead. Mirrors bot/embeds/intel_embeds.py's
# _truncate_field pattern -- duplicated locally (not imported) because
# columnist_assembly.py is a services module and that helper is a private
# bot/embeds implementation detail.
_FIELD_CHAR_LIMIT = 950


def _truncate_field(lines: list[str], limit: int = _FIELD_CHAR_LIMIT) -> str:
    """Join lines; if the result exceeds `limit` chars, drop trailing lines
    and append a count of what got cut."""
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    kept: list[str] = []
    for line in lines:
        candidate = "\n".join(kept + [line])
        if len(candidate) > limit:
            break
        kept.append(line)
    remaining = len(lines) - len(kept)
    return "\n".join(kept) + f"\n…({remaining} more)"


def _truncate_text(text: str, limit: int = _FIELD_CHAR_LIMIT) -> str:
    """Truncate a single prose blob (not a line list) to a safe field-value length."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


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


def _assemble_tactical(parsed: dict, persona_display: str) -> EmbedData:
    """Tactical/coaching format — What Worked / What Didn't / The Adjustment.
    Revived for B1 (see module docstring — not wired into _RENDERERS).

    SUITED FOR: a coaching-film-room breakdown of ONE game or matchup — the
    format already splits cleanly into 3 named sections, which is the most
    natural fit of the five revived renderers for Discord's field grid. NOT
    for a season-long or multi-game arc — this is single-game analysis.

    Layout: lede (+ an inline stat note built from key_stats) stays in the
    description; What Worked / What Didn't / The Adjustment each get their
    own field, worked/didn't set inline=True so they sit side by side.
    """
    headline = str(parsed.get("headline", "")).strip()
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

    if lede and stat_note:
        description = f"{lede} {stat_note}"
    else:
        description = lede or stat_note

    fields: list[EmbedField] = []
    if worked:
        fields.append(EmbedField(
            name="✅ What Worked",
            value=_truncate_field([f"• {b}" for b in worked]),
            inline=True,
        ))
    if didnt:
        fields.append(EmbedField(
            name="❌ What Didn't",
            value=_truncate_field([f"• {b}" for b in didnt]),
            inline=True,
        ))
    if verdict:
        fields.append(EmbedField(name="🔧 The Adjustment", value=_truncate_text(verdict), inline=False))

    return EmbedData(title=headline, description=description, fields=fields, footer=persona_display)


def _assemble_recap(parsed: dict, persona_display: str) -> EmbedData:
    """Game recap format — tight and Twitter-paced. Revived for B1 (see
    module docstring — not wired into _RENDERERS). Like the other four
    revived renderers, format_style="recap" was ALSO not assigned to any
    live persona before this fix (grep confirmed) — the "Used by Maya Chen,
    Keisha Williams on recaps" note in the prior docstring described intent,
    not actual wiring.

    SUITED FOR: a fast, 2-beat game recap — lede, two quick highlights, a
    closing line. NOT for anything needing more than 2 supporting points;
    deliberately capped tight.

    Layout: lede stays in the description; the two beats each get their own
    inline field (so they sit side by side rather than reading as a run-on
    bullet list); verdict closes as its own field.
    """
    lede = str(parsed.get("lede", "")).strip()
    bullets: list[str] = parsed.get("bullets", []) or []
    verdict = str(parsed.get("verdict", "")).strip()

    fields: list[EmbedField] = []
    for i, bullet in enumerate(bullets[:2], start=1):
        text = str(bullet).strip()
        if text:
            fields.append(EmbedField(name=f"Beat {i}", value=_truncate_text(text), inline=True))
    if verdict:
        fields.append(EmbedField(name="Final Word", value=_truncate_text(verdict), inline=False))

    return EmbedData(title="", description=lede, fields=fields, footer=persona_display)


def _assemble_moment(parsed: dict, persona_display: str) -> EmbedData:
    """Maya Chen — 'The Moment' format. Revived for B1 (see module docstring
    — not wired into _RENDERERS).

    SUITED FOR: a cinematic, single-highlight vignette — one play, one
    sequence, isolated and slowed down. NOT for anything that needs to
    summarize multiple events or a whole game.

    Layout: the italic scene-setter stays in the description as the hook;
    the play-by-play and the "why it matters" get their own fields so the
    play itself and its significance are visually distinct instead of
    blurred into one paragraph.
    """
    headline = str(parsed.get("headline", "")).strip()
    scene = str(parsed.get("scene", "")).strip()
    moment = str(parsed.get("moment", "")).strip()
    meaning = str(parsed.get("meaning", "")).strip()

    fields: list[EmbedField] = []
    if moment:
        fields.append(EmbedField(name="The Play", value=_truncate_text(moment), inline=False))
    if meaning:
        fields.append(EmbedField(name="Why It Matters", value=_truncate_text(meaning), inline=False))
    # If only the headline came back, emit a minimal placeholder field so the
    # embed isn't a bare title with zero fields.
    if not fields and headline:
        fields.append(EmbedField(name="The Moment", value=f"Maya Chen on {headline}.", inline=False))

    return EmbedData(
        title=headline,
        description=f"*{scene}*" if scene else "",
        fields=fields,
        footer=persona_display,
    )


def _assemble_verdict(parsed: dict, persona_display: str) -> EmbedData:
    """Jordan Rivera — 'The Verdict' format. Revived for B1 (see module
    docstring — not wired into _RENDERERS).

    SUITED FOR: an opinionated, courtroom-framed ruling on ONE player, trade,
    or decision — case, supporting evidence, final verdict. NOT for anything
    covering multiple subjects at once.

    Layout: the argument prose stays in the description (it IS the column);
    the case statement, the receipts (bulleted so each piece of evidence
    reads as a distinct line, truncated via _truncate_field), and the final
    verdict each get their own field so a reader can scan case → evidence →
    ruling without reading the whole argument first.
    """
    headline = str(parsed.get("headline", "")).strip()
    case = str(parsed.get("case", "")).strip()
    argument = str(parsed.get("argument", "")).strip()
    receipts: list = parsed.get("receipts", []) or []
    verdict = str(parsed.get("verdict", "")).strip()

    fields: list[EmbedField] = []
    if case:
        fields.append(EmbedField(name="⚖️ The Case", value=_truncate_text(case), inline=False))
    receipt_lines = [f"• {str(r).strip()}" for r in receipts[:4] if str(r).strip()]
    if receipt_lines:
        fields.append(EmbedField(name="The Receipts", value=_truncate_field(receipt_lines), inline=False))
    if verdict:
        fields.append(EmbedField(name="🔨 VERDICT", value=_truncate_text(verdict), inline=False))
    # If only the headline came back, emit a minimal ruling so the embed isn't bare.
    if not fields and headline:
        fields.append(EmbedField(
            name="The Verdict", value=f"Jordan Rivera: the verdict is in on {headline}.", inline=False,
        ))

    return EmbedData(
        title=headline,
        description=_truncate_text(argument, limit=2000) if argument else "",
        fields=fields,
        footer=persona_display,
    )


def _assemble_index(parsed: dict, persona_display: str) -> EmbedData:
    """Keisha Williams — 'The Index' format. Revived for B1 (see module
    docstring — not wired into _RENDERERS).

    SUITED FOR: a single-metric analytics deep-dive — one number, one
    definition, a handful of standout players/lineups that number
    highlights. NOT for a general recap; deliberately stats-first.

    Layout: metric name/value and the one-sentence definition stay in the
    description (short, belongs right under the headline number). Each
    standout gets its OWN field — genuinely using the field grid, inline
    when there are 2+ so they sit side by side — instead of being crammed
    into one bulleted blob; implication closes as its own field.
    """
    headline = str(parsed.get("headline", "")).strip()
    metric_name = str(parsed.get("metric_name", "THE INDEX")).strip()
    headline_value = str(parsed.get("headline_value", "")).strip()
    definition = str(parsed.get("definition", "")).strip()
    standouts: list = parsed.get("standouts", []) or []
    implication = str(parsed.get("implication", "")).strip()

    desc_parts = [f"**{metric_name}**"]
    if headline_value:
        desc_parts.append(f"`{headline_value}`")
    if definition:
        desc_parts.append(definition)
    description = "\n".join(desc_parts)

    fields: list[EmbedField] = []
    standout_entries = [s for s in standouts[:3] if str(s.get("name", "")).strip()]
    for s in standout_entries:
        name = str(s.get("name", "")).strip()
        value = str(s.get("value", "")).strip()
        note = str(s.get("note", "")).strip()
        field_value = value or "—"
        if note:
            field_value += f"\n{note}"
        fields.append(EmbedField(
            name=name,
            value=_truncate_text(field_value),
            inline=len(standout_entries) > 1,
        ))
    if implication:
        fields.append(EmbedField(name="Why It Matters", value=_truncate_text(implication), inline=False))
    # If only the headline came back, emit a minimal stat note so the embed isn't bare.
    if not fields and headline:
        fields.append(EmbedField(
            name="The Index", value=f"Keisha Williams: the numbers tell the story on {headline}.", inline=False,
        ))

    return EmbedData(title=headline, description=description, fields=fields, footer=persona_display)


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


def _render_trade_asset_line(item: dict) -> str:
    """Format a single asset line: '• Player Name (PG, 28, OVR 84)' or a pick
    label. Module-level (not nested in _assemble_trade_report) so B4's
    _marcus_cole_asset_fields can reuse it for the detail-embed asset grid."""
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
                block_lines.append(f"> {_render_trade_asset_line(item)}")
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
                        block_lines.append(f"> {_render_trade_asset_line(item)}")
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
            out.append(f"> 🔄 **{counterparty}** receives\n> " + "\n> ".join(f"• {line}" for line in counterparty_sends))
            out.append(f"> 🔄 **{proposer}** receives\n> " + "\n> ".join(f"• {line}" for line in proposer_sends))
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


# ---------------------------------------------------------------------------
# Marcus Cole 2-embed trade report (Phase 2 fix B4)
# ---------------------------------------------------------------------------
# _assemble_trade_report (above) is the ONLY consumer of format_style=
# "trade_report" (grep confirmed -- marcus_cole.py is the sole persona using
# it), so it's safe to keep producing its single interleaved string body
# (columnist_service.generate()'s contract is fixed at {"headline","body"},
# both strings -- see that module's docstring) while these two helpers split
# that same information into a 2-embed shape for the Discord-facing call
# site (cpu_trade_round_trigger.py) to build: a summary embed (Marcus's
# reporter take + grades) and a detail embed (the structured asset
# breakdown, reusing the _truncate_field/_truncate_text discipline already
# established for B1/B2 above).

_MC_GRADE_LINE_RE = re.compile(r"^\*\*Grade:\*\*\s*(.+)$", re.MULTILINE)


def _marcus_cole_summary_text(body: str) -> tuple[str, str]:
    """Split the fully-assembled trade_report body (from _assemble_trade_report)
    into (blurb_prose, grade_line) for the B4 summary embed -- the asset
    blockquote blocks (destined for the detail embed instead) and the
    trailing byline (the embed footer already shows the persona) are dropped.

    Works for all three marker schemes _assemble_trade_report supports (new
    [TEAM_A]/[TEAM_B], legacy [FRAMING]/[ANALYSIS], and no-marker raw body)
    since none of those produce blockquote-prefixed paragraphs -- only the
    asset blocks do.
    """
    chunks = [c for c in (body or "").split("\n\n") if c.strip()]
    blurbs: list[str] = []
    grade_line = ""
    for chunk in chunks:
        stripped = chunk.strip()
        if stripped.startswith(">"):
            continue  # asset blockquote block -- belongs in the detail embed
        if stripped.startswith("— *"):
            continue  # byline -- the embed footer already names the persona
        grade_match = _MC_GRADE_LINE_RE.match(stripped)
        if grade_match:
            grade_line = grade_match.group(1).strip()
            continue
        blurbs.append(stripped)
    return "\n\n".join(blurbs), grade_line


def _marcus_cole_asset_fields(teams: list[dict]) -> list[EmbedField]:
    """Build one EmbedField per team's structured asset list for the B4 detail
    embed -- reuses _render_trade_asset_line (the same per-item formatting
    _assemble_trade_report's blockquote blocks use) and _truncate_field (the
    same 950-char field safety margin B1/B2 use), but as a real field-grid
    entry instead of a blockquote inside one description string.

    `teams` is the same ctx["teams"] structure _assemble_trade_report reads:
    [{"name": str, "gets": [{"type", "name", "position"?, "age"?, "ovr"?,
    "via"?}, ...]}, ...]. Returns [] when teams is empty/malformed so the
    caller can fall back to a single-field placeholder rather than posting an
    embed with zero fields.
    """
    fields: list[EmbedField] = []
    for team in teams or []:
        team_name = str(team.get("name", "")).strip() or "Team"
        gets: list[dict] = team.get("gets") or []
        if not gets:
            fields.append(EmbedField(name=f"🔄 {team_name} receives", value="*(nothing)*", inline=True))
            continue
        lines = [_render_trade_asset_line(item) for item in gets]
        fields.append(EmbedField(
            name=f"🔄 {team_name} receives",
            value=_truncate_field(lines),
            inline=True,
        ))
    return fields


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
        if ppg is not None:
            parts.append(f"{ppg:.1f}p")
        if rpg is not None:
            parts.append(f"{rpg:.1f}r")
        if apg is not None:
            parts.append(f"{apg:.1f}a")
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


# ---------------------------------------------------------------------------
# List-style persona field parsers (Phase 1 fix B2)
# ---------------------------------------------------------------------------
# Power List, The Race, The Ledger, Triage Report, and Rookie Watch are all
# format_style="passthrough" -- their LLM output is a single {headline, body}
# JSON pair where `body` is markdown text following a fixed template each
# persona's voice_notes spells out exactly (ranked list, graded table, medal
# lines, etc.). _assemble_passthrough (still their live string renderer) is
# shared with several OTHER passthrough personas (Jordan Rivera, Big Picture,
# Coach Beat, The Prelude, Pat Chen, Keisha Williams, Carla Knox) that are
# tight single-focus prose, not lists -- so it can't be repurposed here
# without breaking them.
#
# Instead of a new format_style + _RENDERERS entry (which would hit the same
# str-vs-EmbedData contract mismatch B1 hit), these five functions take the
# ALREADY-ASSEMBLED passthrough body string and regex-parse it back into
# structured EmbedFields. sim_content_pipeline.py's five _maybe_post_* call
# sites for these categories call the matching function directly instead of
# doing description=article["body"][:N] -- see the updated call sites.
#
# Each returns (description, fields) so the caller can still set its own
# title (with the category emoji prefix) and footer exactly as before.
# Falls back to a single field holding the raw body when the LLM deviated
# from the required template and the row pattern doesn't match anything --
# nothing is silently dropped, matching this module's established
# never-blank-output convention.

_POWER_LIST_ROW_RE = re.compile(r"^>\s*\*\*(\d+)\.\*\*\s+(\S+)\s+(\S+)\s+—\s+(.+)$", re.MULTILINE)
_POWER_LIST_MOVER_RE = re.compile(r"\*\*Biggest mover:\*\*\s*(.+)", re.IGNORECASE)


def _power_list_fields(body: str) -> tuple[str, list[EmbedField]]:
    """Power List (B2) — splits the fixed '> **N.** TEAM ARROW — note' rows
    into two side-by-side rank-cluster fields (1-5, 6-10) plus a Biggest
    Mover field, instead of one 10-line description blob."""
    rows = _POWER_LIST_ROW_RE.findall(body or "")
    fields: list[EmbedField] = []
    if rows:
        lines = [f"**{rank}.** {team} {arrow} — {note}" for rank, team, arrow, note in rows]
        first_half, second_half = lines[:5], lines[5:]
        if first_half:
            fields.append(EmbedField(name="Ranks 1-5", value=_truncate_field(first_half), inline=True))
        if second_half:
            fields.append(EmbedField(name="Ranks 6-10", value=_truncate_field(second_half), inline=True))
    elif body:
        fields.append(EmbedField(name="Rankings", value=_truncate_text(body), inline=False))

    mover_match = _POWER_LIST_MOVER_RE.search(body or "")
    if mover_match:
        fields.append(EmbedField(name="Biggest Mover", value=_truncate_text(mover_match.group(1).strip()), inline=False))

    return "", fields


_LEDGER_ROW_RE = re.compile(r"^([A-Z]{2,4})\s*\|\s*(.+?)\s*\|\s*([A-Z][+-]?)\s*$", re.MULTILINE)
_LEDGER_VERDICT_RE = re.compile(r"\*\*The Verdict:\*\*\s*(.+)", re.IGNORECASE)
_LEDGER_WINDOW_RE = re.compile(r"^\*(Window:.+?)\*\s*$", re.MULTILINE)


def _the_ledger_fields(body: str) -> tuple[str, list[EmbedField]]:
    """The Ledger (B2) — one field per graded move (TEAM — GRADE : move text)
    instead of the whole fenced table crammed into one description."""
    body = body or ""
    rows = [
        (team, move.strip(), grade)
        for team, move, grade in _LEDGER_ROW_RE.findall(body)
        if team != "TEAM"  # skip the header row if it slips through the pattern
    ]
    fields = [
        EmbedField(name=f"{team} — {grade}", value=_truncate_text(move), inline=True)
        for team, move, grade in rows[:5]
    ]
    if not fields and body:
        fields.append(EmbedField(name="Grades", value=_truncate_text(body), inline=False))

    verdict_match = _LEDGER_VERDICT_RE.search(body)
    if verdict_match:
        fields.append(EmbedField(name="The Verdict", value=_truncate_text(verdict_match.group(1).strip()), inline=False))

    window_match = _LEDGER_WINDOW_RE.search(body)
    description = window_match.group(1).strip() if window_match else ""
    return description, fields


_RACE_MEDAL_RE = re.compile(r"^>\s*(🥇|🥈|🥉)\s*\*\*(.+?)\*\*\s*—\s*(.+)$", re.MULTILINE)
_RACE_ELIMINATED_RE = re.compile(r"\*\*Eliminated this week:\*\*\s*(.+)", re.IGNORECASE)
_RACE_SLEEPER_RE = re.compile(r"\*\*Sleeper:\*\*\s*(.+)", re.IGNORECASE)
_RACE_HEADER_RE = re.compile(r"^\*(.+?Race.+?)\*\s*$", re.MULTILINE)


def _the_race_fields(body: str) -> tuple[str, list[EmbedField]]:
    """The Race (B2) — one field per medal candidate, plus separate
    Eliminated This Week / Sleeper fields, instead of one flat blob."""
    body = body or ""
    medals = _RACE_MEDAL_RE.findall(body)
    fields = [
        EmbedField(name=f"{medal} {name.strip()}", value=_truncate_text(case.strip()), inline=False)
        for medal, name, case in medals[:3]
    ]
    if not fields and body:
        fields.append(EmbedField(name="Candidates", value=_truncate_text(body), inline=False))

    eliminated_match = _RACE_ELIMINATED_RE.search(body)
    if eliminated_match:
        fields.append(EmbedField(name="Eliminated This Week", value=_truncate_text(eliminated_match.group(1).strip()), inline=True))
    sleeper_match = _RACE_SLEEPER_RE.search(body)
    if sleeper_match:
        fields.append(EmbedField(name="Sleeper", value=_truncate_text(sleeper_match.group(1).strip()), inline=True))

    header_match = _RACE_HEADER_RE.search(body)
    description = header_match.group(1).strip() if header_match else ""
    return description, fields


_TRIAGE_HEADER_RE = re.compile(r"🩹\s*\*\*(.+?)\*\*\s*\((\w+)\)\s*—\s*(.+)")
_TRIAGE_FILLING_RE = re.compile(r"\*\*Filling in:\*\*\s*(.+)", re.IGNORECASE)
_TRIAGE_IMPACT_RE = re.compile(r"\*\*Impact:\*\*\s*(.+)", re.IGNORECASE)


def _triage_report_fields(body: str) -> tuple[str, list[EmbedField]]:
    """Triage Report (B2) — Status / Filling In / Impact as three separate
    fields instead of one flat 3-line blob.

    Each Triage Report post currently covers exactly ONE injury
    (_maybe_post_triage_report fires once per injury event), so this yields
    exactly one Status+Filling-In+Impact trio, not N per-injury fields. If a
    future batched-injury post is added, extend this to emit one trio per
    injury instead of restructuring the field shape.
    """
    body = body or ""
    fields: list[EmbedField] = []

    header_match = _TRIAGE_HEADER_RE.search(body)
    if header_match:
        player, team, status = header_match.groups()
        fields.append(EmbedField(name=f"🩹 {player.strip()} ({team})", value=_truncate_text(status.strip()), inline=False))

    filling_match = _TRIAGE_FILLING_RE.search(body)
    if filling_match:
        fields.append(EmbedField(name="Filling In", value=_truncate_text(filling_match.group(1).strip()), inline=True))

    impact_match = _TRIAGE_IMPACT_RE.search(body)
    if impact_match:
        fields.append(EmbedField(name="Impact", value=_truncate_text(impact_match.group(1).strip()), inline=True))

    if not fields and body:
        fields.append(EmbedField(name="Injury Report", value=_truncate_text(body), inline=False))

    return "", fields



# Team code parens are optional in the regex even though rookie_watch.py's own
# FORMAT instructions require them — that persona's worked _SHAPE example
# (the "Wemby vs Edey" one) omits them, so real LLM output may follow either
# the example or the instruction. Not this fix's job to reconcile that
# pre-existing prompt inconsistency; the parser just needs to survive both.
_ROOKIE_MEDAL_RE = re.compile(r"^(🥇|🥈)\s*\*\*(.+?)\*\*\s*(?:\((\w+)\)\s*)?—\s*(.+)$", re.MULTILINE)
_ROOKIE_POSTERIZE_RE = re.compile(r"\*\*(Posterize of the week|Stat of the week):\*\*\s*(.+)", re.IGNORECASE)


def _rookie_watch_fields(body: str) -> tuple[str, list[EmbedField]]:
    """Rookie Watch (B2) — one field per rookie (medal + team + stat line),
    plus a Posterize/Stat of the Week field, instead of one flat blob. The
    banter line between the medal lines and the closer becomes the
    description (whatever prose is left after stripping the matched rows)."""
    body = body or ""
    rookies = _ROOKIE_MEDAL_RE.findall(body)
    fields = []
    for medal, name, team, stat in rookies[:2]:
        field_name = f"{medal} {name.strip()}" + (f" ({team})" if team else "")
        fields.append(EmbedField(name=field_name, value=_truncate_text(stat.strip()), inline=True))
    if not fields and body:
        fields.append(EmbedField(name="Rookies", value=_truncate_text(body), inline=False))

    posterize_match = _ROOKIE_POSTERIZE_RE.search(body)
    if posterize_match:
        label, text = posterize_match.groups()
        fields.append(EmbedField(name=label.title(), value=_truncate_text(text.strip()), inline=False))

    # Whatever prose is left after removing the matched medal rows and the
    # posterize/stat line is the banter line -- becomes the description.
    remaining = _ROOKIE_MEDAL_RE.sub("", body)
    remaining = _ROOKIE_POSTERIZE_RE.sub("", remaining)
    description = "\n".join(line.strip() for line in remaining.splitlines() if line.strip())
    return description, fields


# Maps format_style strings to STRING-BODY renderer functions only.
# Most renderers take (parsed: dict, persona_display: str) → str.
# Renderers in _CTX_RENDERERS additionally accept an optional `ctx` dict for
# structural data (stats, names) that comes from the calling context
# rather than the LLM.
#
# tactical/recap/moment/verdict/index are DELIBERATELY ABSENT (B1) — they now
# return EmbedData, not str, so including them here would break
# _assemble_article's uniform string-return contract for whichever caller
# eventually hit them. Call them directly (see module docstring) once Phase 2
# assigns one to a persona.
_RENDERERS = {
    "analytics": _assemble_analytics,
    "hot_take": _assemble_hot_take,
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
    """Dispatch to the correct STRING-BODY renderer based on persona format_style.

    Falls back to _assemble_default when the style key is unrecognised —
    this includes the five EmbedData-returning renderers (tactical, recap,
    moment, verdict, index; see module docstring), since none of them are in
    _RENDERERS. `ctx` is only consumed by renderers that need extra
    structural data (currently just `potm`); other renderers ignore it.
    Returns None when the renderer signals the post should be skipped
    (currently only passthrough when body is empty).
    """
    renderer = _RENDERERS.get(format_style, _assemble_default)
    if format_style in ("potm", "trade_report", "tank_watch"):
        return renderer(parsed, persona_display, ctx)
    return renderer(parsed, persona_display)
