# Nikon lens monitor

Checks Nikon product pages and sends updates to a Discord webhook when items
become available or the price changes while in stock.

## Setup

1. Install dependencies:

   `python3 -m pip install -r requirements.txt`

2. Set a Discord webhook URL:

   `export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."`.

3. Edit `config.json` to include the products you want to monitor.

## Run once

`python3 monitor.py`

## Run continuously

`python3 monitor.py --loop-minutes 10`

## Cron example

```
*/10 * * * * /usr/bin/python3 /Users/steve/Desktop/Project/NikonSale/monitor.py --config /Users/steve/Desktop/Project/NikonSale/config.json >> /Users/steve/Desktop/Project/NikonSale/monitor.log 2>&1
```
