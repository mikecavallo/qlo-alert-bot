# QLO Alert Bot

Telegram-only alert bot for Pump.fun/PumpSwap graduation and momentum signals.
It never buys or sells tokens.

Watches every bonding curve that fills on pump.fun and tells you about the
slow ones.

## Why slow

Roughly 35,000 tokens are created on pump.fun every day. About 2% of them
ever fill their bonding curve. The median one that does takes around
twenty minutes.

Three minutes means bots pushed the price with nobody real behind it.
Six hours means people were buying, with their own money, for hours.

Measured across six months of graduations (97,146 of them), the slow ones
were about **1.5× more likely to double** after graduation and roughly
**2.4× more likely to do 5×** than the fast ones. That held on four
different price thresholds and on a holdout period the analysis had never
seen. The effect got stronger as the threshold went up.

It is a filter, not a crystal ball. Most of these tokens still go to zero.

## What it does

1. A Helius webhook sends pool creations on the pump.fun AMM
   (PumpSwap) to your server.
2. Pools that are not pump.fun graduations are dropped locally, with no
   API calls. That is about three quarters of the stream.
3. For each real graduation the bot walks the token's transaction history
   back to its first transaction and measures how long it sat on the curve.
   It stops paging as soon as the threshold is reached, to save credits.
4. If the token is older than your threshold, an alert goes to Telegram:
   ticker, name, time on the curve, contract and quick links.
5. A separate optional SWAP webhook can identify short-term momentum from
   PumpSwap trades. It is deliberately expensive in Helius credits, so keep
   it disabled unless you need momentum alerts.
6. Optionally, only members of your Telegram channel get alerts in DM.

### Alert gates

An alert is only eligible after all of these checks pass:

- Helius event type is `CREATE_POOL` and source is Pump AMM/Pump.fun.
- The transaction succeeded and contains a confirmed Pump.fun `Migrate` or
  `MigrateV2` instruction in its on-chain log messages.
- The token spent at least `min_age_hours` on the bonding curve.
- DexScreener reports the configured minimum liquidity, rolling five-minute
  volume, and buy count.
- Five-minute buys exceed sells by the configured ratio.
- The token has at least one website or social link, when enabled.
- The largest visible user owner is below `max_top_holder_percent`.

Rejected candidates are recorded in `rejections.jsonl` with the reason and
transaction signature when available. This is an alert filter, not a promise
that a token is safe or profitable.

The graduation webhook must point to `/hook`. If a SWAP webhook is used for
future volume research, it must point to `/volume-hook`; never send SWAP
events to `/hook`.

## Requirements

- A Linux server with a public domain and HTTPS (Helius and Telegram
  both need to reach it)
- Python 3.10+
- nginx (or any reverse proxy)
- A [Helius](https://helius.dev) account. The free plan is enough
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

### 1. Install

```bash
git clone https://github.com/mikecavallo/qlo-alert-bot.git
cd qlo
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp config.example.json config.json
```

### 2. Get your keys

**Helius API key**
1. Sign up at [dashboard.helius.dev](https://dashboard.helius.dev)
2. Copy the API key from the dashboard

**Telegram bot**
1. Open [@BotFather](https://t.me/BotFather), send `/newbot`, follow the steps
2. Copy the token it gives you
3. If you post to a channel, add the bot to that channel as an **admin**.
   Without admin rights membership checks fail with
   `member list is inaccessible`

### 3. Fill in `config.json`

Open `config.json` and fill in every field. `config.example.json` lists
all of them. The main ones:

| field | what it is |
|---|---|
| Helius key | API key from the Helius dashboard |
| Telegram token | bot token from @BotFather |
| channel | your channel, e.g. `@your_channel` |
| `min_age_hours` | minimum time on the curve before an alert. Default `6` |
| `confirmation_minutes` | delay before the post-graduation market check. Default `5` |
| `min_5m_volume_usd` | minimum rolling five-minute volume. Default `5000` |
| `min_5m_buys` | minimum rolling five-minute buys. Default `20` |
| `min_liquidity_usd` | minimum reported liquidity. Default `10000` |
| `min_buy_sell_ratio` | minimum buy-to-sell ratio. Default `1.1` |
| `require_social_or_website` | require at least one project link. Default `true` |
| `max_top_holder_percent` | maximum visible user-owner concentration. Default `20` |

Never commit `config.json`. It is already in `.gitignore`.

### 3.1 Recommended starting configuration

The example configuration is intentionally conservative. A token must have
market activity, liquidity, buy pressure, and a website or social link before
the bot sends an alert. These checks reduce noise but cannot identify scams or
guarantee performance.

For a low-credit setup, configure only the graduation webhook and leave the
SWAP/momentum webhook disabled. For momentum alerts, use a separate Helius
webhook pointed at `/qlo/volume-hook` and set `webhook_auth` to a long random
secret that exactly matches the webhook authentication header.

### 4. Run behind nginx

The app listens on `127.0.0.1` only. Expose it through nginx over HTTPS:

```nginx
location /qlo/ {
    proxy_pass http://127.0.0.1:8091/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

```bash
nginx -t && systemctl reload nginx
```

### 5. Run as a service

Create `/etc/systemd/system/qlo.service`:

```ini
[Unit]
Description=QLO scanner
After=network.target

[Service]
WorkingDirectory=/opt/qlo
ExecStart=/opt/qlo/venv/bin/uvicorn app:app --host 127.0.0.1 --port 8091
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now qlo
systemctl status qlo
```

Change `/opt/qlo` to wherever you cloned the repo.

### 6. Create the Helius webhook

```bash
./venv/bin/python setup_webhook.py
```

This registers a webhook on pool creations for the pump.fun AMM and points
it at your server. Check `setup_webhook.py` for the URL it uses and make
sure it matches your nginx path.

### 7. Connect the Telegram bot

Point Telegram at your server:

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://your.domain/qlo/tg"
```

### 8. Check it works

```bash
curl -s https://your.domain/qlo/health
```

You should get `{"ok": true, ...}`. The first alerts show up as soon as a
slow token graduates. On a quiet day that can take a while.

## Development and testing

Run the tests before deploying changes:

```bash
venv/bin/python -m unittest -v test_app.py
python3 -m py_compile app.py test_app.py
```

The tests cover migration verification, failed-transaction rejection,
webhook authentication, and PumpSwap buy/sell parsing from Helius transfer
payloads.

## Troubleshooting

Check service health:

```bash
curl -s http://127.0.0.1:8091/health
systemctl status qlo --no-pager
journalctl -u qlo -n 100 --no-pager
```

Repeated `POST /volume-hook 200 OK` lines mean Helius is delivering events;
they are not errors. `volume.events` counts received swaps, `volume.tokens`
counts swaps parsed with a token mint, and `volume.unparsed` counts events
that were intentionally rejected because direction or amount was ambiguous.

If DexScreener returns HTTP 403 or is unavailable, the bot should filter the
candidate rather than send an unverified alert. Check the service journal and
the provider response before relaxing any safety gate.

To stop the process without deleting webhooks:

```bash
systemctl stop qlo
```

To conserve Helius credits, also pause the SWAP webhook in the Helius
dashboard. Restart the service with `systemctl start qlo` when ready.

## Deploying an updated copy

Run the file copy from the computer that contains the repository, not from
inside the server SSH session:

```bash
rsync -avz \
  -e "ssh -o IdentitiesOnly=yes -i /home/mike/.ssh/codex" \
  /home/mike/Projects/qlo/app.py \
  /home/mike/Projects/qlo/config.example.json \
  /home/mike/Projects/qlo/test_app.py \
  root@YOUR_SERVER_IP:/opt/qlo/
```

On the server, run the tests and restart only after they pass:

```bash
cd /opt/qlo
venv/bin/python -m unittest -v test_app.py
systemctl restart qlo
systemctl is-active qlo
curl -s http://127.0.0.1:8091/health
journalctl -u qlo -f
```

Keep the graduation webhook pointed at `/qlo/hook`. If the momentum webhook
is enabled, point it at `/qlo/volume-hook` and configure its authorization
header to the exact value of `webhook_auth`.

## Using it

**As a channel owner**
- Alerts go to the channel set in `config.json`.
- Raise `min_age_hours` for fewer, stricter alerts. Lower it for more.
  Restart the service after any change:
  `systemctl restart qlo`

**As a subscriber**
1. Join the channel.
2. Open the bot and press **Start**.
3. Alerts arrive in DM while you stay a member.

**Reading an alert**
- `Token history >= 18.6 h` means the token spent at least that long
  on the curve before graduating.
- **Contract** is the token mint. Tap to copy.
- Buttons open the token on Pump.fun, DexScreener, Axiom and Solscan.

Always check the chart, holders and liquidity yourself before buying.

## Credits and costs

Each graduation costs a few Helius credits: the webhook event plus a
handful of history requests. The history walk stops early once the
threshold is reached. The free plan (1M credits a month) covers normal
traffic comfortably. Watch usage in the Helius dashboard for the first
few days.

## Troubleshooting

| problem | fix |
|---|---|
| `member list is inaccessible` | make the bot an admin of the channel |
| no alerts at all | check `systemctl status qlo`, `/health`, and that the Helius webhook points to the right URL |
| Helius shows failed deliveries | nginx path or HTTPS is wrong. Test the URL with `curl` |
| bot does not answer `/start` | re-run the `setWebhook` command from step 7 |

## Live version

The bot running in [@kotte_writes](https://t.me/kotte_writes) has more on
top of this code:

- **Real migration check.** The `migrate` / `migrate_v2` instruction is
  matched against the official pump.fun IDL, so pools made to look like a
  graduation are dropped.
- **Liquidity check.** At least 10 SOL of real money in the pool, checked
  at the migration transaction and again right before the alert.
- **Freshness.** An alert that would go out more than 180 seconds after
  the migration is dropped instead of sent late.
- **Socials.** X, Telegram, Discord and website pulled in parallel from
  token metadata, pump.fun and DexScreener, merged and deduplicated.
  Websites are shown by their real domain.
- **Dev profile.** Creator's pump.fun username, followers and linked
  socials.
- **Outcome tracking.** Every alert is measured at 15m, 1h, 6h and 24h.

## A note on PumpSwap prices

The PumpSwap docs say `virtual_quote_reserves` is 0 on all pools. We read
it straight from the pool accounts: it was non-zero on 28 of 40 pools we
checked, usually around 17.58 SOL.

If you price a pool from the quote vault balance alone, you are off by
1.3× to 15×. Adding the virtual reserve brought our price within 1–5% of
DexScreener:

```text
effective_quote = quote_vault_balance + Pool.virtual_quote_reserves
price           = effective_quote / base_vault_balance
```

The field is a signed i128 at bytes 245..261 of the Pool account.
Liquidity should still be checked on the real vault balance only.

## Disclaimer

Not financial advice. This is a filter over on-chain data. Most tokens,
including slow ones, lose most of their value. Links in token metadata are
set by the creator and are not verified. Trade only what you can afford
to lose.

## License

MIT
