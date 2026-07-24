"""LLM-backed columnist article generation.

ColumnistRequest is the standardised call shape every _maybe_post_* caller
builds; generate() sends the prompt to Claude, tolerantly parses the JSON
response, and dispatches to the correct format-style renderer.

Article-body rendering (one function per format_style) and
[SENTINEL]-based body parsing live in columnist_assembly.py (Phase 3
opportunistic split, see HANDOFF.md) -- this module is now the
LLM-calling orchestration layer only.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field

from data.repositories import article_repo
from services.columnist_assembly import _assemble_article
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
    structured_data     : optional machine-readable payload persisted alongside the
                          article (e.g. Power List's team-code-to-rank mapping) —
                          never sent to the LLM, purely a storage passthrough so a
                          later caller can read real data instead of re-parsing prose
    """
    persona_id: str
    category: str
    subject_team_ids: list[int] = field(default_factory=list)
    subject_player_ids: list[int] = field(default_factory=list)
    extra_context: dict = field(default_factory=dict)
    structured_data: dict | None = None


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


# ---------------------------------------------------------------------------
# Post-generation grounding check (Finding #1 — no fact-checking of LLM output)
# ---------------------------------------------------------------------------
# Pragmatic, regex-based net for the most common hallucination shape: a specific
# percentage or streak-length that doesn't correspond to anything in the context
# dict that was actually sent to the model. This is deliberately NOT a full NLP
# fact-checker (out of scope per the audit) — it targets numeric claims the model
# is instructed to ground ("use ONLY real stats") but that nothing downstream
# ever re-verifies. Plain integer claims (e.g. "34 points" that should've been
# 30) are out of scope: same-order-of-magnitude integers are too ambiguous to
# flag without false-positive noise, whereas percentages and streak lengths are
# specific enough that a mismatch is a strong hallucination signal.

_PCT_CLAIM_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
_STREAK_CLAIM_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?[\s-]+(?:straight|consecutive|game(?:s)?\s+(?:win|los(?:s|ing))(?:\s+streak)?)",
    re.IGNORECASE,
)
# Real make/attempt pairs commonly present in columnist context payloads — used
# to derive the shooting percentages personas (esp. Pat Chen) are instructed to
# compute, so a correctly-computed FG%/3P%/eFG% doesn't get flagged as invented.
_MAKE_ATTEMPT_PAIRS: tuple[tuple[str, str], ...] = (("fgm", "fga"), ("tpm", "tpa"), ("ftm", "fta"))
# Absolute tolerance (percentage points, or raw count) for a claim to count as grounded.
# Generous on purpose — this is a hallucination net, not a precision auditor.
_GROUNDING_TOLERANCE = 1.5


def _collect_context_numbers(obj, acc: set[float]) -> None:
    """Recursively collect numeric leaf values from a context dict/list.

    Ratios in [0, 1] are also added scaled to 0-100 so a context value like
    0.487 grounds a rendered claim of "48.7%". Numbers embedded in strings
    (e.g. "118-110" score summaries, "34-11-9" stat lines) are picked up too,
    since box-score summaries are frequently pre-formatted as strings rather
    than raw numeric fields.
    """
    if isinstance(obj, dict):
        for v in obj.values():
            _collect_context_numbers(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_context_numbers(v, acc)
    elif isinstance(obj, bool):
        return
    elif isinstance(obj, (int, float)):
        acc.add(round(float(obj), 1))
        if 0.0 <= obj <= 1.0:
            acc.add(round(float(obj) * 100, 1))
    elif isinstance(obj, str):
        for m in re.finditer(r"-?\d+(?:\.\d+)?", obj):
            try:
                acc.add(round(float(m.group(0)), 1))
            except ValueError:
                pass


def _collect_derived_percentages(obj, acc: set[float]) -> None:
    """Recursively derive shooting percentages from make/attempt pairs in context.

    Personas (Pat Chen especially) are explicitly instructed to compute TS%/eFG%/
    FG%/3P% from raw makes and attempts — those computed values legitimately won't
    appear verbatim in context, so they'd otherwise be flagged as hallucinated.
    """
    if isinstance(obj, dict):
        for made_key, att_key in _MAKE_ATTEMPT_PAIRS:
            made, att = obj.get(made_key), obj.get(att_key)
            if isinstance(made, (int, float)) and isinstance(att, (int, float)) and att:
                acc.add(round(made / att * 100, 1))
        fgm, tpm, fga = obj.get("fgm"), obj.get("tpm"), obj.get("fga")
        if (
            isinstance(fgm, (int, float)) and isinstance(tpm, (int, float))
            and isinstance(fga, (int, float)) and fga
        ):
            acc.add(round((fgm + 0.5 * tpm) / fga * 100, 1))
        for v in obj.values():
            _collect_derived_percentages(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _collect_derived_percentages(v, acc)


def _find_ungrounded_claims(headline: str, body: str, context: dict) -> list[str]:
    """Scan generated text for percentage/streak-length claims not grounded in context.

    Returns the list of matched claim substrings (e.g. "61.4%", "8 straight")
    that don't correspond — within tolerance — to any real number (or computable
    shooting percentage) present in the context dict sent to the prompt. Empty
    list means everything checked out (or nothing checkable was claimed).
    """
    text = f"{headline}\n{body}"
    allowed: set[float] = set()
    _collect_context_numbers(context, allowed)
    _collect_derived_percentages(context, allowed)

    def _grounded(value: float) -> bool:
        return any(abs(value - a) <= _GROUNDING_TOLERANCE for a in allowed)

    flagged: list[str] = []
    for m in _PCT_CLAIM_RE.finditer(text):
        if not _grounded(float(m.group(1))):
            flagged.append(m.group(0))
    for m in _STREAK_CLAIM_RE.finditer(text):
        if not _grounded(float(m.group(1))):
            flagged.append(m.group(0))
    return flagged


def _build_fact_check_addendum(flagged: list[str]) -> str:
    """System-prompt addendum appended before a one-time regeneration retry."""
    joined = "; ".join(flagged[:5])
    return (
        "\n\nCORRECTION NEEDED: Your previous draft included numeric claims that do not "
        f"match anything computable from the context provided ({joined}). Every percentage "
        "or streak length you state must be present in, or directly computable from, the "
        "context JSON. Remove or fix any number you cannot ground in the context. Do not invent."
    )


# ---------------------------------------------------------------------------
# Per-category token budgets (Phase 1 fix A1 — replaces the single global
# max_tokens=1400 every persona/category used to share).
# ---------------------------------------------------------------------------
# Every persona's own voice_notes already states a length target for its
# format (a word count, a line count, or a row count — see each persona
# module). This table converts those stated targets into a max_tokens ceiling
# tight enough that overshooting is expensive, while keeping a floor generous
# enough that valid short output never gets truncated mid-JSON — that failure
# mode already happened once at a flat max_tokens=900 (see the raised-to-1400
# comment this table replaces below): passthrough personas wrap their whole
# body in a single JSON string value, so escaped newlines/quotes add token
# overhead well beyond the raw word count, even for a short body.
#
# Tiers, keyed by category (not format_style — several format_styles are
# shared by personas with very different stated lengths, e.g. "passthrough"
# covers everything from Triage Report's 4-line cap to Big Picture's
# long-form column):
#   500  — very short, hard word/line caps stated in voice_notes
#          (rookie_watch: "Max ~80 words total"; injury_report: "Cap at 4
#          short lines"; transaction: "Maximum 2 sentences total").
#   700  — short single-take or fixed-row list formats (~100-160 stated
#          words: power rankings 10-line list, ledger's 3-5 graded rows,
#          award race's 3 candidates + eliminated + sleeper, tank watch's
#          ladder + 2 stock lines + 1-sentence take, hot take hour's 4 turns
#          at a 35-word cap each).
#   1000 — medium prose formats (~150-300 words: trade report blurbs +
#          grades, coach's-corner two paragraphs, Pat Chen's
#          Observation/Evidence/Implication, the game-recap rotation, POTM,
#          series preview table + 4 sections).
#   1400 — long-form only. Big Picture states "~600-800 characters of prose,
#          plus 3 bullets" as ITS OWN target, but that is still by far the
#          longest stated target of any persona, so it alone keeps the prior
#          global ceiling.
_CATEGORY_MAX_TOKENS: dict[str, int] = {
    # ~500 — hard word/line caps.
    "rookie_watch":       500,
    "injury_report":      500,
    "transaction":        500,
    # ~700 — short single-take / fixed-row list formats.
    "power_rankings":     700,
    "front_office_grade": 700,
    "award_race":         700,
    "hot_take":           700,
    "debate":             700,
    "draft_report":       700,
    "tank_watch":         700,
    "pick_analysis":      700,
    # ~1000 — medium prose formats.
    "trade_report":         1000,
    "analysis":             1000,
    "headline":             1000,
    "game_recap":           1000,
    "playoff_recap":        1000,
    "coaching_beat":        1000,
    "strategy_analysis":    1000,
    "player_of_the_month":  1000,
    "series_preview":       1000,
    # ~1400 — long-form only.
    "sunday_column": 1400,
}
# Fallback for any category not explicitly tuned above (new/experimental
# categories) — matches the "medium prose" tier rather than the old global
# 1400 so an untuned category doesn't silently get the most generous budget.
_DEFAULT_MAX_TOKENS = 1000


def _resolve_max_tokens(category: str) -> int:
    """Look up the max_tokens ceiling for this article category (see table above)."""
    return _CATEGORY_MAX_TOKENS.get(category, _DEFAULT_MAX_TOKENS)


# ---------------------------------------------------------------------------
# Per-persona word-count targets + code-level length enforcement (Phase 1
# fix A3 — the most important of the 8 fixes: prompt-only length instructions
# have already been tried and abandoned once. A documented ≤120-word cap for
# Pat Chen was scoped, marked ready, and never actually shipped with any code
# backing it (see Brain/Note Pad/dba/pat-chen-feedback-eval.md, theme P1).
# This table + _count_words + the retry loop in generate() below is that
# missing code backing, generalised to every persona instead of just Pat Chen.
# ---------------------------------------------------------------------------
# Values are each persona's OWN stated length target from its voice_notes,
# converted to a word count (a stated char count like Big Picture's
# "~600-800 characters of prose, plus 3 bullets" is divided by ~5.5 chars/word
# and the bullets added back in). Keyed by persona_id — several personas share
# a category (e.g. "game_recap" covers Carla Knox's tight scoreboard Wrap AND
# Pat Chen's tactical breakdown) but each still has its own distinct stated
# target, so persona_id is the correct granularity here (unlike the
# category-keyed token/description tables above, which are about API/Discord
# mechanics rather than persona voice).
_PERSONA_WORD_TARGET: dict[str, int] = {
    "rookie_watch":    80,   # rookie_watch.py: "Max ~80 words total body"
    "triage_report":   70,   # triage_report.py: "Cap at 4 short lines total"
    "ren_takahashi":   40,   # ren_takahashi.py: "Maximum 2 sentences total"
    "power_list":      90,   # power_list.py: 10-line ranked list (≤8 words/line) + mover line
    "the_ledger":     120,   # the_ledger.py: "Exactly 3-5 rows" graded table + one verdict sentence
    "the_race":       120,   # the_race.py: 3 candidates + eliminated + sleeper, one line each
    "darius_cole":     90,   # darius_cole.py: odds ladder + 2 stock lines + "EXACTLY ONE sentence" closer
    "hot_take_hour":  140,   # hot_take_hour.py: 4 turns, hard 35-word cap each
    "jordan_rivera":   90,   # jordan_rivera.py: take + why-it-matters + bold prediction, 1-2 sentences each
    "keisha_williams": 110,  # keisha_williams.py: metric block + definition + 2 standouts + why-it-matters
    "pat_chen":       130,   # pat_chen.py Observation/Evidence/Implication (POTM shape is longer but still tight per side)
    "carla_knox":     150,   # carla_knox.py: scoreboard table + 2-3 bullets + league pulse sentence
    "coach_beat":     150,   # coach_beat.py: 2 paragraphs (2-3 sentences each) + closer
    "big_picture":    180,   # big_picture.py: "~600-800 characters of prose, plus 3 bullets" (its OWN stated target)
    "the_prelude":    150,   # the_prelude.py: record table + 4 short sections
    "marcus_cole":     90,   # marcus_cole.py: "1-2 sentences MAX" per team + grades
    "marcus_brooks":   90,   # marcus_brooks.py: "Keep it 3-4 sentences"
}
# Fallback for any persona not explicitly tuned above.
_DEFAULT_WORD_TARGET = 150

# A draft exceeding its target by more than this multiplier triggers one
# regenerate-with-correction retry (mirrors the grounding-check retry's
# "meaningful margin, not any overage" philosophy — short drafts naturally
# vary a little, only a real blowout is worth spending a second API call on).
_LENGTH_OVERAGE_MULTIPLIER = 1.5

_WORD_RE = re.compile(r"[A-Za-z0-9']+")


def _count_words(text: str) -> int:
    """Word count for length enforcement — more meaningful than char count
    since it's insensitive to markdown punctuation (**, >, |, ─) that
    inflates char counts without inflating what a reader actually reads."""
    return len(_WORD_RE.findall(text or ""))


def _resolve_word_target(persona_id: str) -> int:
    """Look up this persona's own stated word-count target (see table above)."""
    return _PERSONA_WORD_TARGET.get(persona_id, _DEFAULT_WORD_TARGET)


def _build_length_correction_addendum(word_target: int, actual_words: int) -> str:
    """System-prompt addendum appended before a one-time length-correction retry."""
    return (
        "\n\nCORRECTION NEEDED: Your previous draft ran approximately "
        f"{actual_words} words, well over this format's ~{word_target}-word target. "
        f"Rewrite it to come in under {word_target} words. Cut redundant clauses, "
        "extra sentences, and restated points — do not just shorten individual words."
    )


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
    structured_data: dict | None = None,
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

    structured_data (optional) is a machine-readable payload persisted
    alongside the article — never sent to the LLM. Callers that need to read
    real data back later (e.g. The Power List's rank-delta tracking) should
    use this instead of re-parsing the rendered body prose.
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
        _structured_data = req.structured_data
    else:
        _persona_id = persona_id
        _category = category or ""
        _context = context or {}
        _subject_team_ids = subject_team_ids
        _subject_player_ids = subject_player_ids
        _structured_data = structured_data

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

    # A2: narrative_rule's "zoom out to team and league context, reference at
    # least one OTHER player by name" instruction is only appropriate for
    # categories that are actually supposed to cover multiple storylines.
    # It used to be injected into EVERY system prompt, which directly fought
    # personas whose own voice_notes demand tight, single-focus brevity
    # (Jordan Rivera: "reacts to ONE specific moment"; Keisha Williams: "picks
    # ONE metric... do NOT summarize the whole game"; Rookie Watch, The Race,
    # Power List, Triage Report, etc. are all single-subject-per-article by
    # design). Scoped to the two categories that are explicitly wide-angle:
    # sunday_column (Big Picture's whole point is a league-wide arc) and
    # game_recap (the main columnist rotation, whose task_line already asks
    # for "multiple players and team storylines").  The HEADLINE RULE half of
    # this block is generically useful (a vague headline is bad regardless of
    # scope) so it stays bundled with the narrative half only for these two
    # categories — other categories already get headline guidance from their
    # own output_shape_rule/voice_notes.
    _NARRATIVE_RULE_CATEGORIES = frozenset({"sunday_column", "game_recap"})
    if _category in _NARRATIVE_RULE_CATEGORIES:
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
    else:
        narrative_rule = ""

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

    # Per-category token budget (A1) — replaces the old flat 1400 for every
    # persona/category. See _CATEGORY_MAX_TOKENS above for the tier rationale.
    _max_tokens = _resolve_max_tokens(_category)

    # _capture_prompt is a ride-along-only internal hook (see services/columnist_ride_along.py).
    # Populate it before the API call so the full prompt is preserved even if the call fails.
    # Default None is a no-op — production callers pay zero cost.
    if _capture_prompt is not None:
        _capture_prompt["system"] = system_prompt
        _capture_prompt["user"] = user_content
        _capture_prompt["model"] = "claude-haiku-4-5-20251001"
        _capture_prompt["max_tokens"] = _max_tokens

    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=api_key)

        # Grounding-check retry loop (Finding #1): at most one regeneration when
        # the first draft contains numeric claims (percentages, streak lengths)
        # that don't correspond to anything in the context dict. Only applies to
        # the two "good" content paths below (old {"headline","body"} shape and
        # the main structured shape) — parse-failure and missing-field fallbacks
        # already produce degraded placeholder text that isn't worth fact-checking
        # or re-rolling, so they keep returning immediately as before.
        #
        # Length-check retry (A3) shares this exact loop shape: at most one
        # regeneration when the first draft blows past this persona's own
        # stated word-count target by more than _LENGTH_OVERAGE_MULTIPLIER.
        # Both checks can fire on the same attempt — their correction notes
        # are appended together for the retry rather than spending two API
        # calls on two separate re-rolls.
        _fact_check_addendum = ""
        _length_correction_addendum = ""
        _final_headline: str | None = None
        _final_body: str | None = None
        _word_target = _resolve_word_target(_persona_id)
        _first_attempt_word_count: int | None = None

        for _attempt in range(2):
            message = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=_max_tokens,  # per-category budget (A1) — see _CATEGORY_MAX_TOKENS
                system=system_prompt + _fact_check_addendum + _length_correction_addendum,
                messages=[{"role": "user", "content": user_content}],
            )
            raw = message.content[0].text.strip()

            # Ride-along: capture the raw LLM response alongside the prompt.
            if _capture_prompt is not None:
                _capture_prompt["raw_llm_response"] = raw

            # DIAGNOSTIC — log first 800 chars of every LLM response so we can
            # verify each persona's actual output shape.  Leave in place.
            log.info(
                "columnist_service [RAW] persona=%s category=%s attempt=%d | %.800s",
                _persona_id, _category, _attempt + 1, raw,
            )

            parsed = _tolerant_json_parse(raw, _persona_id, _category)

            if parsed is None:
                # All structured parsing failed — wrap raw text and return something.
                # Guard: if the raw text looks like JSON (starts with '{'), try one more
                # time to pull readable fields out of it so we never post raw JSON to Discord.
                # No grounding check / retry here — this is already degraded fallback text.
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
                                structured_data=_structured_data,
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
                                structured_data=_structured_data,
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
                    structured_data=_structured_data,
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
                _final_headline, _final_body = headline, body
            else:
                # Require headline. For standard shapes also require lede; custom-shape
                # personas (output_shape_override set) only need headline to be valid.
                # Personas with a named format_style that has its own renderer also pass
                # with headline alone — the renderer handles empty optional fields.
                # NOTE (B1): "moment", "verdict", "index", "tactical", "recap" are listed
                # here for forward-compat, but as of B1 those five renderers return
                # EmbedData (not str) and are NOT in columnist_assembly._RENDERERS — a
                # persona whose format_style is one of these would still pass this
                # leniency check, but _assemble_article() below would silently fall back
                # to _assemble_default instead of calling the EmbedData renderer. Phase 2
                # must add explicit dispatch for these five before assigning one to a
                # live persona (see columnist_assembly.py's module docstring).
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
                        structured_data=_structured_data,
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

                _final_headline, _final_body = headline, body

            # ── Grounding check (Finding #1) ──────────────────────────────────
            # Only reached by the two "good" content paths above. Retry once
            # (regenerate from scratch with a correction note) when the first
            # draft has ungrounded numeric claims; post whatever we have after
            # the retry rather than dropping the article entirely.
            _ungrounded = _find_ungrounded_claims(_final_headline, _final_body, _context)

            # ── Length check (A3) ─────────────────────────────────────────────
            # Code-level enforcement of this persona's own stated word-count
            # target — prompt-only length instructions were tried and dropped
            # once already (see _PERSONA_WORD_TARGET's docstring above).
            _word_count = _count_words(_final_body)
            if _attempt == 0:
                _first_attempt_word_count = _word_count
            _over_target = _word_count > _word_target * _LENGTH_OVERAGE_MULTIPLIER

            if (_ungrounded or _over_target) and _attempt == 0:
                if _ungrounded:
                    log.warning(
                        "columnist_service: %s/%s draft had ungrounded numeric claim(s) %r — retrying once",
                        _persona_id, _category, _ungrounded[:5],
                    )
                    _fact_check_addendum = _build_fact_check_addendum(_ungrounded)
                if _over_target:
                    log.warning(
                        "columnist_service: %s/%s draft ran %d words (target ~%d) — retrying once",
                        _persona_id, _category, _word_count, _word_target,
                    )
                    _length_correction_addendum = _build_length_correction_addendum(_word_target, _word_count)
                _final_headline = _final_body = None
                continue
            if _ungrounded:
                log.warning(
                    "columnist_service: %s/%s still has ungrounded numeric claim(s) %r after retry — posting anyway",
                    _persona_id, _category, _ungrounded[:5],
                )
            if _over_target:
                log.warning(
                    "columnist_service: %s/%s still over length after retry (first=%d, retry=%d words, target ~%d) "
                    "— posting anyway",
                    _persona_id, _category, _first_attempt_word_count, _word_count, _word_target,
                )
            break

        if _final_headline is None or _final_body is None:
            # Defensive — every loop exit path above either returns or sets both.
            return None

        await article_repo.insert(
            pool,
            league_id=league_id,
            season=season,
            persona_id=_persona_id,
            category=_category,
            headline=_final_headline,
            body=_final_body,
            subject_team_ids=_subject_team_ids,
            subject_player_ids=_subject_player_ids,
            structured_data=_structured_data,
        )

        return {"headline": _final_headline, "body": _final_body}

    except Exception as exc:
        raw_preview = locals().get("raw", "<no response>")
        if isinstance(raw_preview, str):
            raw_preview = raw_preview[:120]
        log.warning(
            f"columnist_service: article generation failed for {_persona_id}/{_category}: {exc} "
            f"| response preview: {raw_preview!r}"
        )
        return None
