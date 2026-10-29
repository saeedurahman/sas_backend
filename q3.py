import re, asyncio, asyncpg, os
from dotenv import load_dotenv
load_dotenv()
u = re.sub(r'\+asyncpg', '', os.environ['DATABASE_URL'])
async def m():
    c = await asyncpg.connect(u)
    r = await c.fetch("SELECT name FROM businesses ORDER BY created_at")
    print(len(r))
    for x in r: print(x['name'])
asyncio.run(m())