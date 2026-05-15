from phase.states import Phase

ALLOWED: dict[str, set[Phase]] = {
    "sim_rivalry":         {Phase.REGULAR_SEASON_ACTIVE, Phase.REGULAR_SEASON_POSTDEADLINE},
    "sim_games":           {Phase.REGULAR_SEASON_ACTIVE, Phase.REGULAR_SEASON_POSTDEADLINE},
    "sim_season":          {Phase.REGULAR_SEASON_ACTIVE, Phase.REGULAR_SEASON_POSTDEADLINE},
    "sim_deadline":        {Phase.REGULAR_SEASON_ACTIVE},
    "sim_round":           {Phase.PLAYIN_ACTIVE, Phase.PLAYOFFS_R1, Phase.PLAYOFFS_R2,
                            Phase.CONFERENCE_FINALS, Phase.NBA_FINALS},
    "sim_playin":          {Phase.PLAYIN_ACTIVE},
    "ready":               {Phase.REGULAR_SEASON_ACTIVE, Phase.REGULAR_SEASON_POSTDEADLINE,
                            Phase.PLAYIN_ACTIVE, Phase.PLAYOFFS_R1, Phase.PLAYOFFS_R2,
                            Phase.CONFERENCE_FINALS, Phase.NBA_FINALS},
    "trade_propose":       {Phase.REGULAR_SEASON_ACTIVE, Phase.TRADE_DEADLINE_OPEN,
                            Phase.REGULAR_SEASON_POSTDEADLINE, Phase.POST_DRAFT_TRADES_OPEN},
    "trade_approve":       {Phase.REGULAR_SEASON_ACTIVE, Phase.TRADE_DEADLINE_OPEN,
                            Phase.REGULAR_SEASON_POSTDEADLINE, Phase.POST_DRAFT_TRADES_OPEN},
    "trade_veto":          {Phase.REGULAR_SEASON_ACTIVE, Phase.TRADE_DEADLINE_OPEN,
                            Phase.REGULAR_SEASON_POSTDEADLINE, Phase.POST_DRAFT_TRADES_OPEN},
    "draft_advance":       {Phase.DRAFT_IN_PROGRESS},
    "draft_pick":          {Phase.DRAFT_IN_PROGRESS},
    "fa_offer":            {Phase.FA_OPEN},
    "fa_advance":          {Phase.FA_OPEN},
    "awards_open":         {Phase.OFFSEASON_AWARDS_CLOSED, Phase.NBA_FINALS},
    "awards_vote":         {Phase.OFFSEASON_AWARDS_OPEN},
    "awards_close":        {Phase.OFFSEASON_AWARDS_OPEN},
    "waiver_claim":        {Phase.WAIVERS_OPEN, Phase.FA_CLOSED},
    "season_start":        {Phase.SETUP, Phase.PRESEASON_READY},
    "progression_run":     {Phase.PROGRESSION_PENDING},
}


def is_allowed(command_name: str, current_phase: str) -> bool:
    allowed_phases = ALLOWED.get(command_name, set())
    return Phase(current_phase) in allowed_phases


def allowed_phases_for(command_name: str) -> list[str]:
    return [p.value for p in ALLOWED.get(command_name, set())]
