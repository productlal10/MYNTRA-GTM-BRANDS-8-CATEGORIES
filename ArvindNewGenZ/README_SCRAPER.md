# Women's Ethnic Wear Scraper

This repository is dedicated solely to scraping Myntra Women's Ethnic Wear for the approved brand allowlist:
- Libas
- Jaypore
- Aurelia
- Biba
- Soch
- Aramya
- Suta
- Lakshita
- Aachho

## Run once
```bash
python3 main.py
```

## Full coverage rerun
```bash
python3 main.py --no-resume --pdp-retry-rounds 5
```

## Test safely
```bash
python3 main.py --brands "Libas" --max-pages-per-brand 1 --limit-products 5 --dry-run --no-snapshot
```

## Daily run
```bash
crontab daily_scrape.cron
```
Runs the scraper every day at 2:00 AM and appends logs to `logs/daily_scrape.log`.
