import asyncio
import asyncpg

async def check():
    c = await asyncpg.connect("postgresql://dba:dba@localhost:5434/dba")
    simmed = await c.fetchval("SELECT COUNT(*) FROM games WHERE league_id=1 AND status='simmed'")
    phase = await c.fetchval("SELECT current_phase FROM leagues WHERE id=1")
    # Check trade block
    block_count = await c.fetchval("SELECT COUNT(*) FROM trade_block WHERE league_id=1")
    # Check trades
    trades = await c.fetchval("SELECT COUNT(*) FROM trades WHERE league_id=1")
    print(f"Simmed: {simmed} | Phase: {phase} | Trade block: {block_count} | Trades: {trades}")
    await c.close()

asyncio.run(check())
