import re, asyncio, asyncpg, os
from dotenv import load_dotenv
load_dotenv()
u = re.sub(r'\+asyncpg', '', os.environ['DATABASE_URL'])
async def m():
    c = await asyncpg.connect(u)
    r = await c.fetch("SELECT name, created_at FROM businesses WHERE name !~ '^(POSTest|VoidRace|CancelRace|FIFO|RBAC|OwnerOnly|CoA|Journal|BOM|DocNum)' ORDER BY created_at")
    print(len(r))
    for x in r[:60]:
        print(x['name'], x['created_at'])
asyncio.run(m())