# iPhone Duo stock checker

Watches Apple Store pickup availability for the **iPhone Duo** (Night Sky and
Star White, 256GB and 512GB) at the two Apple Stores nearest you. It texts you
when one can be picked up.

It uses only Python 3.9+ and the standard library, so there's nothing to `pip install`.

## What it checks

The part numbers are found automatically on apple.com. As of November 2026, the US unlocked models are:

| Part        | Model                         |
|-------------|-------------------------------|
| MK1E4LL/A   | iPhone Duo 256GB Star White   |
| MK1F4LL/A   | iPhone Duo 256GB Night Sky    |
| MK1G4LL/A   | iPhone Duo 512GB Star White   |
| MK1H4LL/A   | iPhone Duo 512GB Night Sky    |

## Setup

1. Make your config file:

   ```bash
   cp .env.example .env
   ```

2. Put your `ZIP_CODE` in `.env`, then find your stores:

   ```bash
   python3 stock_checker.py --list-stores
   ```

   Copy the two store numbers you want (they look like `R123`) into
   `STORE_NUMBERS=R123,R456`. If you leave it blank, the checker uses the 2 stores nearest your ZIP code.

3. Set up texting. Pick one of the options in `.env.example`:
   - **Twilio**: real SMS for about 1 cent per text. Sign up at twilio.com and buy a number.
     US numbers have to be registered (A2P 10DLC or toll-free verification) before
     texts get delivered.
   - **Carrier email-to-text**: free, sent from your Gmail using an App Password.
     It works for Verizon (`@vtext.com`) and T-Mobile (`@tmomail.net`), but not AT&T.
   - **ntfy**: a free push notification to the ntfy phone app. It isn't SMS, but it's the easiest to set up.

   Check that texts arrive:

   ```bash
   python3 stock_checker.py --test-sms
   ```

4. Run it:

   ```bash
   python3 stock_checker.py --loop 120
   ```

   This checks every ~2 minutes and keeps running until you press Ctrl+C. You get one
   text when a phone comes into stock, and another only if it is still in stock
   `REALERT_MINUTES` later. If it sells out and comes back, you get a new text.

### Keep it running

Leave it running on a computer or Raspberry Pi at home, or schedule single checks with cron (`crontab -e`):

```
*/3 * * * * cd /path/to/Iphone_stock && /usr/bin/python3 stock_checker.py >> checker.log 2>&1
```

## Notes

- Run it from your **home internet connection**. Apple blocks automated requests
  from many cloud/datacenter IPs with `HTTP 541`, and it may block yours too if you
  check too often. Every 2–5 minutes is reasonable.
- If Apple changes its page and auto-discovery breaks, set `PART_NUMBERS` in
  `.env` to the list above.
- Run the tests:

  ```bash
  python3 -m unittest discover -s tests
  ```
