Query scout.db outcomes table and print win-rate stats by signal combination.

```bash
uv run python -c "
import asyncio
from scout.db import Database
from scout.config import Settings

async def main():
    settings = Settings()
    db = Database(settings.DB_PATH)
    await db.initialize()
    alerts = await db.get_recent_alerts(days=30)
    print(f'Total alerts: {len(alerts)}')
    # TODO: cross-reference with outcomes table for win-rate
    await db.close()

asyncio.run(main())
"
```

Report: total alerts, win-rate (price_change_pct > 0), breakdown by signal combination.
