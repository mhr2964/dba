"""Verify that sim engine fixes produce correct Jokic stats and minutes."""
import asyncio
import asyncpg


async def main():
    c = await asyncpg.connect("postgresql://dba:dba@localhost:5434/dba")

    # Get DEN players
    den_team = await c.fetchrow("SELECT id FROM teams WHERE league_id=1 AND nba_team_code='DEN'")
    den_id = den_team['id']

    players = await c.fetch("""
        SELECT p.id, p.first_name, p.last_name, p.position, p.overall,
               p.playmaking, p.ast_tendency, p.tendency_pass,
               l.is_starter, l.slot
        FROM lineups l JOIN players p ON p.id = l.player_id
        WHERE l.league_id=1 AND l.team_id=$1
        ORDER BY l.slot
    """, den_id)

    print("=== DEN Player attributes after fix ===")
    for p in players[:8]:
        name = (p['first_name'] + ' ' + p['last_name']).encode('ascii','replace').decode()
        print(f"  {name} ({p['position']}, OVR {p['overall']}, starter={p['is_starter']}): "
              f"pm={p['playmaking']} ast_t={p['ast_tendency']}")

    # Simulate minutes distribution using new get_team_minutes_plan logic
    print("\n=== Simulated minutes distribution (new logic) ===")
    player_ids = [p['id'] for p in players]
    ovr_by_id = {p['id']: p['overall'] for p in players}

    # Use the new formula: ** 1.4 with noise
    game_seed = 12345
    weights = []
    for pid in player_ids:
        base = max(0.5, ((float(ovr_by_id.get(pid, 50)) - 50.0) / 10.0) ** 1.4)
        noise = 1.0 + 0.20 * ((hash(pid ^ game_seed) % 100 - 50) / 50.0)
        weights.append(base * noise)

    total_w = sum(weights)
    minutes = [w / total_w * 240 for w in weights]

    # Apply 42-min starter cap (identify starters by is_starter flag)
    starter_ids = {p['id'] for p in players if p['is_starter']}
    overflow = 0.0
    for i, pid in enumerate(player_ids):
        cap = 42.0 if pid in starter_ids else 30.0
        if minutes[i] > cap:
            overflow += minutes[i] - cap
            minutes[i] = cap

    for i, p in enumerate(players[:10]):
        name = (p['first_name'] + ' ' + p['last_name']).encode('ascii','replace').decode()
        print(f"  {name} ({p['position']}, {'START' if p['is_starter'] else 'bench'}): "
              f"{minutes[i]:.1f} min (weight={weights[i]:.2f})")

    # Show Jokic's ast_tendency
    jokic = next((p for p in players if p['overall'] == 92), None)
    if jokic:
        name = (jokic['first_name'] + ' ' + jokic['last_name']).encode('ascii','replace').decode()
        ast_t = jokic['ast_tendency']
        pos_weight = 0.85  # C
        ast_weight = jokic['playmaking'] * pos_weight * (ast_t / 50.0) ** 1.6
        print(f"\n=== Jokic ast_tendency after fix: {ast_t} ===")
        print(f"  Expected ast_weight: {ast_weight:.1f}")
        print("  (higher = more assists vs teammates)")

    # PG APG check: LaMelo's expected weight vs Jokic
    lamelo = await c.fetchrow("""
        SELECT p.id, p.first_name, p.last_name, p.playmaking, p.ast_tendency, p.overall
        FROM players p WHERE p.league_id=1 AND p.first_name='LaMelo'
    """)
    if lamelo:
        new_pg_weight = 1.15  # Updated from 1.40
        new_c_weight = 0.85
        pm = lamelo['playmaking']
        ast_t = lamelo['ast_tendency']
        lamelo_w = pm * new_pg_weight * (ast_t / 50.0) ** 1.6

        if jokic:
            jokic_pm = jokic['playmaking']
            jokic_ast_t = jokic['ast_tendency']
            jokic_w = jokic_pm * new_c_weight * (jokic_ast_t / 50.0) ** 1.6

            ratio = lamelo_w / jokic_w if jokic_w > 0 else 0
            print("\n=== APG weight comparison (new PG weight 1.15) ===")
            print(f"  LaMelo (PG, pm={pm}, ast_t={ast_t}): weight={lamelo_w:.1f}")
            print(f"  Jokic  (C,  pm={jokic_pm}, ast_t={jokic_ast_t}):  weight={jokic_w:.1f}")
            print(f"  Ratio: {ratio:.2f}x (LaMelo vs Jokic)")
            print(f"  If LaMelo gets X APG, Jokic should get ~X/{ratio:.1f} APG")

    await c.close()


asyncio.run(main())
