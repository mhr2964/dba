"""Quick verification of ast_tendency values for top PGs after calibration."""
import asyncio
import asyncpg
import os


async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    lid = await conn.fetchval("SELECT id FROM leagues ORDER BY id DESC LIMIT 1")
    print(f"League: {lid}")

    rows = await conn.fetch(
        """
        SELECT first_name, last_name, position, ast_tendency, overall
        FROM players
        WHERE league_id = $1 AND position = 'PG'
        ORDER BY overall DESC LIMIT 25
        """,
        lid,
    )
    print(f"{'Name':<28} {'OVR':>4} {'AST_TEND':>8}")
    print("-" * 45)
    for r in rows:
        name = f"{r['first_name']} {r['last_name']}"
        print(f"{name:<28} {r['overall']:>4} {r['ast_tendency']:>8}")

    # Count how many PGs still have the default 50
    still_50 = sum(1 for r in rows if r["ast_tendency"] == 50)
    print(f"\nPGs still at 50: {still_50}/{len(rows)}")
    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
