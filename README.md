# QLO

Pump.fun graduation scanner.

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

1. A Helius webhook sends every pool creation on the pump.fun AMM
   (PumpSwap) to your server.
2. Pools that are not pump.fun graduations are dropped locally, with no
   API calls. That is about three quarters of the stream.
3. For each real graduation the bot walks the token's transaction history
   back to its first transaction and measures how long it sat on the curve.
   It stops paging as soon as the threshold is reached, to save credits.
4. If the token is older than your threshold, an alert goes to Telegram:
   ticker, name, time on the curve, contract and quick links.
5. Optionally, only members of your Telegram channel get alerts in DM.

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
git clone https://github.com/gustaffsonKotte/qlo.git
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

Never commit `config.json`. It is already in `.gitignore`.

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
