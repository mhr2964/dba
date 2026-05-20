"""
Recalibrate ast_tendency for elite-passing bigs and fix PG over-inflation.

Strategy:
- Centers/PFs whose real-world APG > 6: they ARE primary playmakers.
  Their ast_tendency should reflect this — bump to 92-97 range.
- PG ast_tendency: many are at 84-86, giving 10-13 APG. Real elite PGs
  get 10-11 APG so the ceiling is ok, but average PGs at 13+ are too high.
  Cap PG ast_tendency at 80 for non-elite passers.

Known elite-passing bigs (real APG > 6):
  Nikola Jokic (DEN) ~9 APG -> ast_tendency 97
  Domantas Sabonis (SAC) ~8 APG -> ast_tendency 94
  LaMarcus Aldridge-era is over, but current bigs:
  Bam Adebayo ~3.5 APG -> keep as is
  Also playmaking attribute correction for Jokic.
"""
import asyncio
import asyncpg

ELITE_PASSING_BIGS = {
    # last_name: (min_ovr, new_ast_tendency)
    "Jokic": (88, 97),   # Nikola Jokic - best passing center ever, real ~9 APG
    "Sabonis": (80, 93),  # Domantas Sabonis - real ~8 APG
    "Jovic": (72, 75),   # Nikola Jovic - young, not elite yet, slight bump
}

async def fix():
    c = await asyncpg.connect("postgresql://dba:dba@localhost:5434/dba")

    # Fix Jokic-level passers
    for last_name, (min_ovr, new_tendency) in ELITE_PASSING_BIGS.items():
        result = await c.execute(f"""
            UPDATE players
            SET ast_tendency = {new_tendency}
            WHERE league_id = 1
              AND last_name ILIKE '%{last_name}%'
              AND overall >= {min_ovr}
              AND position IN ('C', 'PF')
        """)
        print(f"Updated {last_name}: {result}")

    # Check who changed
    rows = await c.fetch("""
        SELECT first_name || ' ' || last_name as name, position, overall, ast_tendency
        FROM players WHERE league_id=1 AND position IN ('C','PF')
        ORDER BY ast_tendency DESC LIMIT 8
    """)
    print("\nTop C/PF ast_tendency after fix:")
    for r in rows:
        name = r['name'].encode('ascii','replace').decode()
        print(f"  {name} ({r['position']}, OVR {r['overall']}): ast_tendency={r['ast_tendency']}")

    # Fix PG over-inflation: cap non-elite PG ast_tendency
    # Players with playmaking < 88 shouldn't have ast_tendency > 75
    result = await c.execute("""
        UPDATE players
        SET ast_tendency = LEAST(ast_tendency, 75)
        WHERE league_id = 1
          AND position = 'PG'
          AND playmaking < 88
          AND ast_tendency > 75
    """)
    print(f"\nCapped non-elite PG ast_tendency: {result}")

    # Check PG tendencies now
    rows = await c.fetch("""
        SELECT first_name || ' ' || last_name as name, playmaking, ast_tendency
        FROM players WHERE league_id=1 AND position='PG'
        ORDER BY ast_tendency DESC LIMIT 10
    """)
    print("\nTop PG ast_tendency after fix:")
    for r in rows:
        name = r['name'].encode('ascii','replace').decode()
        print(f"  {name}: pm={r['playmaking']} ast_t={r['ast_tendency']}")

    await c.close()

asyncio.run(fix())
