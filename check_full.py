import asyncio
import asyncpg

async def check():
    c = await asyncpg.connect("postgresql://dba:dba@localhost:5434/dba")

    simmed = await c.fetchval("SELECT COUNT(*) FROM games WHERE league_id=1 AND status='simmed'")
    print(f"Games simmed: {simmed}")

    # Top scorers
    print("\n--- Top PPG after first batch ---")
    rows = await c.fetch("""
        SELECT p.first_name, p.last_name, t.nba_team_code,
               ROUND(AVG(bs.points)::numeric,1) as ppg,
               ROUND(AVG(bs.assists)::numeric,1) as apg,
               ROUND(AVG(bs.rebounds_off + bs.rebounds_def)::numeric,1) as rpg,
               COUNT(*) as gp
        FROM game_box_scores bs
        JOIN games g ON g.id = bs.game_id
        JOIN players p ON p.id = bs.player_id
        JOIN teams t ON t.id = bs.team_id
        WHERE g.league_id = 1
        GROUP BY p.id, p.first_name, p.last_name, t.nba_team_code
        HAVING COUNT(*) >= 2
        ORDER BY ppg DESC
        LIMIT 10
    """)
    for r in rows:
        name = f"{r['first_name']} {r['last_name']}".encode('ascii','replace').decode()
        print(f"  {name} ({r['nba_team_code']}): {r['ppg']} PPG / {r['rpg']} RPG / {r['apg']} APG in {r['gp']} GP")

    # Jokic specifically
    print("\n--- Nikola Jokic ---")
    jokic = await c.fetchrow("""
        SELECT ROUND(AVG(bs.points)::numeric,1) as ppg,
               ROUND(AVG(bs.assists)::numeric,1) as apg,
               ROUND(AVG(bs.rebounds_off + bs.rebounds_def)::numeric,1) as rpg
        FROM game_box_scores bs
        JOIN games g ON g.id = bs.game_id
        JOIN players p ON p.id = bs.player_id
        WHERE g.league_id = 1 AND p.first_name = 'Nikola' AND p.last_name = 'Jokic'
    """)
    if jokic and jokic['ppg']:
        print(f"  Jokic: {jokic['ppg']} PPG / {jokic['rpg']} RPG / {jokic['apg']} APG")
    else:
        print("  No Jokic stats yet")

    # Standings top 5
    print("\n--- Standings Top 5 ---")
    standings = await c.fetch("""
        SELECT t.nba_team_code, sc.wins, sc.losses
        FROM standings_cache sc
        JOIN teams t ON t.id = sc.team_id
        WHERE sc.league_id = 1 AND sc.season = 2024
        ORDER BY sc.wins DESC, sc.losses ASC
        LIMIT 5
    """)
    for r in standings:
        print(f"  {r['nba_team_code']}: {r['wins']}-{r['losses']}")

    # OKC and LAL records
    print("\n--- User teams ---")
    for code in ['OKC', 'LAL']:
        row = await c.fetchrow("""
            SELECT sc.wins, sc.losses
            FROM standings_cache sc
            JOIN teams t ON t.id = sc.team_id
            WHERE sc.league_id = 1 AND sc.season = 2024 AND t.nba_team_code = $1
        """, code)
        if row:
            print(f"  {code}: {row['wins']}-{row['losses']}")

    # Trades
    trades = await c.fetchval("SELECT COUNT(*) FROM trades WHERE league_id=1")
    pending = await c.fetchval("SELECT COUNT(*) FROM trades WHERE league_id=1 AND status='pending_commissioner'")
    print(f"\nTrades: {trades} total, {pending} pending commissioner")

    # Trade block
    block = await c.fetchval("SELECT COUNT(*) FROM trade_block WHERE league_id=1")
    print(f"Trade block listings: {block}")

    await c.close()

asyncio.run(check())
