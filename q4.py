import re, asyncio, asyncpg, os
from dotenv import load_dotenv
load_dotenv()
u = re.sub(r'\+asyncpg', '', os.environ['DATABASE_URL'])
async def m():
    c = await asyncpg.connect(u)
    r = await c.fetch("SELECT b.name, u.phone, u.created_at FROM users u JOIN businesses b ON b.id=u.business_id WHERE b.name ILIKE 'Test Shop%' ORDER BY u.created_at")
    for x in r: print(dict(x))
asyncio.run(m())