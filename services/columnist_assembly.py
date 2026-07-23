"""Pure article-body renderers and body-parsing helpers for the columnist
system: one _assemble_* function per format_style (analytics, hot_take,
tactical, recap, moment, verdict, index, potm, trade_report, passthrough,
tank_watch, default), the [SENTINEL]-based body parsers they call
(_parse_trade_body, _parse_marcus_cole_body, _parse_potm_body), and
_assemble_article, the dispatcher that routes to the right renderer.

No DB, no async, no LLM calls -- these take the LLM's already-parsed JSON
dict and a persona display string, and return the final Discord-ready
article body string.

Extracted from columnist_service.py (Phase 3 opportunistic split, see
HANDOFF.md). Only called internally by columnist_service.generate via
_assemble_article -- no external caller touches these renderers directly.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


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
