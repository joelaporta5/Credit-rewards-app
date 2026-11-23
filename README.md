# Rewards Autopilot — Sprint 1: Cash Reserve Engine

A private, single-user web app that watches your credit cards and checking account and
tells you how much cash is **safe to spend** without leaving yourself unable to pay your
cards in full.

- **Apple Pay taps** arrive instantly from an iPhone Shortcut (provisional).
- **Plaid** (read-only) is the source of truth and corrects or replaces them.
- An **append-only ledger** makes every number on the dashboard traceable.
- The app **never moves money**. It has no payment or transfer code at all.

```
safe_to_spend = verified checking cash
              − card spend not yet covered by your reserve account
              − bills due in the next N days
              − minimum buffer
              − uncertainty holdback (only when data is stale or a connection is broken)
```

It's an estimate, never a guarantee, and the dashboard says so.

| Document | What's in it |
|---|---|
| [DECISIONS.md](DECISIONS.md) | Design choices and why |
| [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) | What it can't do (yet) and where it may be wrong |
| [docs/SPRINT2_BACKLOG.md](docs/SPRINT2_BACKLOG.md) | Next steps |
| [docs/TEST_REPORT.md](docs/TEST_REPORT.md) | Latest test run |
| [docs/PLAID_VERIFICATION.md](docs/PLAID_VERIFICATION.md) | Every Plaid endpoint/field used and where it was verified |

---

## Contents

1. [Set up on Windows (local)](#1-set-up-on-windows-local)
2. [Plaid Sandbox setup](#2-plaid-sandbox-setup)
3. [Test webhooks locally with a tunnel](#3-test-webhooks-locally-with-a-tunnel)
4. [Deploy to Render](#4-deploy-to-render)
5. [Build the iPhone Shortcut (iOS 27)](#5-build-the-iphone-shortcut-ios-27)
6. [Switching to your real accounts (Plaid Trial)](#6-switching-to-your-real-accounts-plaid-trial)
7. [Using the app day to day](#7-using-the-app-day-to-day)
8. [Running the tests](#8-running-the-tests)
9. [Security notes](#9-security-notes)

---

## 1. Set up on Windows (local)

You only need this to run things on your own PC (tests, trying it out). If you just want it
running on the internet, you can skip to [Deploy to Render](#4-deploy-to-render).

**Install these once** (accept the defaults unless noted):

1. **Python 3.12**: https://www.python.org/downloads/windows/ → "Windows installer (64-bit)" for 3.12.x.
   On the first screen **tick "Add python.exe to PATH"**.
2. **Git for Windows**: https://git-scm.com/download/win
3. **PostgreSQL 16**: https://www.postgresql.org/download/windows/ → "Download the installer".
   Write down the password you choose for the `postgres` user. Keep port 5432.

**Get the code and install** (open *PowerShell* from the Start menu):

```powershell
cd $HOME
git clone https://github.com/joelaporta5/credit-rewards-app.git
cd credit-rewards-app
git checkout claude/rewards-autopilot-sprint-1-9rw0qy

py -3.12 -m venv .venv
# If the next line says "running scripts is disabled", run this once and answer Y:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
```

**Create the databases** (enter your postgres password when asked):

```powershell
& "C:\Program Files\PostgreSQL\16\bin\createdb.exe" -U postgres rewards_dev
& "C:\Program Files\PostgreSQL\16\bin\createdb.exe" -U postgres rewards_test
```

**Configure:**

```powershell
copy .env.example .env
python scripts\generate_keys.py
notepad .env
```

Paste the four generated lines into `.env` (replacing the empty ones), put your postgres
password into `DATABASE_URL`, and add your Plaid Sandbox keys (next section).

**Create the tables and start the app:**

```powershell
alembic upgrade head
uvicorn app.main:app --reload
```

Open http://localhost:8000 and log in with your `ADMIN_API_KEY`. Each time you come back
later, run `.\.venv\Scripts\Activate.ps1` first, then `uvicorn app.main:app --reload`.

---

## 2. Plaid Sandbox setup

Sandbox is Plaid's fake bank with test data. Use it until everything works.

1. Sign up at https://dashboard.plaid.com and verify your email.
2. **Developers → Keys**: copy the **client_id** and the **Sandbox secret**.
3. In `.env`, set `PLAID_ENV=sandbox`, `PLAID_CLIENT_ID=...`, `PLAID_SECRET=<the Sandbox secret>`.
4. Restart the app, open **Connect → Connect account**, pick any bank (e.g. "First Platypus
   Bank"), and log in with **username `user_good`, password `pass_good`**.
5. On **Accounts & cards**, mark one checking account as **Primary checking** and another
   depository account as **Card-payment reserve**. Credit cards are detected automatically.

**Automatic end-to-end check** (uses its own throwaway database, `rewards_e2e`):

```powershell
& "C:\Program Files\PostgreSQL\16\bin\createdb.exe" -U postgres rewards_e2e
$env:E2E_DATABASE_URL="postgresql://postgres:YOUR_PASSWORD@localhost:5432/rewards_e2e"
python scripts\sandbox_e2e.py
```

It creates a Sandbox connection, syncs, sends an Apple Pay event, creates the matching
charge in Sandbox, checks they match once, checks duplicate delivery, a reserve transfer,
"Refresh now", ledger integrity, and a forced "login required" error. Each step prints PASS or FAIL.

---

## 3. Test webhooks locally with a tunnel

Plaid needs a public HTTPS address to send webhooks to. A free Cloudflare quick tunnel
gives your PC one temporarily.

```powershell
winget install --id Cloudflare.cloudflared
cloudflared tunnel --url http://localhost:8000
```

It prints an address like `https://something-random.trycloudflare.com`. Put that in `.env`
as `PUBLIC_BASE_URL`, restart the app, and connect a Sandbox bank. New connections send
webhooks there. On **Connect**, click **Send a test webhook (Sandbox)**; within seconds the
**Activity** page should list a `TRANSACTIONS / SYNC_UPDATES_AVAILABLE` webhook as *verified*.

The quick-tunnel address changes every time. After moving to Render, click **"Point all
connections' webhooks at this server"** on the Connect page.

---

## 4. Deploy to Render

**Why Render:** you deploy from GitHub entirely in the browser, with no command-line tools
to install on Windows, and you get HTTPS and a managed PostgreSQL database with backups. The
included `render.yaml` sets everything up in one go. See DECISIONS.md for the comparison
with Fly.io and Railway.

**Cost:** the Blueprint uses a paid *Starter* web service and a paid *basic-256mb* database.
The free tier doesn't work here: free web services sleep after 15 minutes and take about a
minute to wake (the Shortcut would time out), and free databases are deleted after 30 days.
Check current prices at https://render.com/pricing before you confirm.

Steps:

1. Create an account at https://render.com (sign in with GitHub is easiest).
2. Make sure the code you want is on GitHub (merge this branch into `main`, or deploy the
   branch directly).
3. Render Dashboard → **New → Blueprint** → choose the `credit-rewards-app` repository
   (and branch). Render reads `render.yaml`.
4. Render asks for the values marked "sync: false":
   - `PLAID_CLIENT_ID` and `PLAID_SECRET`: your **Sandbox** keys for now.
   - `PUBLIC_BASE_URL`: enter `https://rewards-autopilot.onrender.com` for now. Once the
     service exists, copy its real address from the top of its page, update this variable
     (Environment tab) and save. Render redeploys.
5. Click **Apply**. The first deploy takes a few minutes; database tables are created automatically.
6. Open the service → **Environment**. Copy `ADMIN_API_KEY` (to log in) and `WALLET_API_KEY`
   (for the iPhone Shortcut). Store both in a password manager.
7. Visit your address, log in, and connect a Sandbox bank. Then click **Send a test webhook
   (Sandbox)** on the Connect page and check **Activity** for a *verified* webhook.

**Never change `TOKEN_ENCRYPTION_KEY`** after connecting accounts; stored connections become unreadable.

---

## 5. Build the iPhone Shortcut (iOS 27)

Every time you pay with Apple Pay, your iPhone sends the amount, the store and the card name
to your app. You build this once in the **Shortcuts** app (it's preinstalled; search for it if
you don't see it).

**Have these ready** (copy them into Notes on your iPhone first, so you can paste them):
- your **app address**, e.g. `https://rewards-autopilot-xxxx.onrender.com`. Add `/events/wallet` to
  the end: `https://rewards-autopilot-xxxx.onrender.com/events/wallet`
- your **WALLET_API_KEY** (Render → rewards-autopilot → Environment). Not the admin key.

Apple occasionally renames buttons. If a label below doesn't match exactly, look for the closest one.

### Part 1 — the trigger ("when I pay")

1. Open **Shortcuts** → tap **Automation** at the bottom.
2. Tap **+** (top right). You'll see a list of triggers.
3. Scroll down and tap **Wallet** (on older iOS it's called **Transaction**).
4. Under cards, tap **Choose** and select **every card** you pay with. Leave the other options as they are.
5. At the bottom, choose **Run Immediately** (so it doesn't ask you each time). Turn **Notify When Run** off if you like.
6. Tap **Next**, then **Create New Shortcut** (or **New Blank Automation**).

### Part 2 — the action ("send it to my app")

7. In the search bar at the bottom ("Search for apps and actions"), type **Get Contents of URL**
   and tap it. A block appears saying *Get contents of URL*.
8. Tap the blue word **URL** in that block and **paste your address** ending in `/events/wallet`.
9. Tap the small **arrow (›)** on the right edge of the block to open its options.
10. **Method**: tap **GET** and change it to **POST**.
11. **Request Body**: make sure it says **JSON**. Ignore **Headers**; you don't need them.
12. Tap **Add new field** → **Text**, four times. For each one, type the **Key**, then fill the **Value**:

| Key (type exactly) | Value |
|---|---|
| `key` | paste your **WALLET_API_KEY** |
| `amount` | tap the Value box → in the bar above the keyboard tap **Shortcut Input** → tap the blue **Shortcut Input** bubble you just added → choose **Amount** |
| `merchant` | same way, but choose **Merchant** |
| `card` | same way, but choose the card's **Name** (it may be called **Card or Pass** or **Name**) |

    If you don't see "Shortcut Input" above the keyboard, tap **Select Variable** and pick
    **Shortcut Input**, then tap it to choose the detail (Amount, Merchant, Name).

13. Tap **Done** (top right).

### Part 3 — try it

14. Pay for something small with Apple Pay.
15. In your app, open **Activity**. Under **Apple Pay events** you should see the purchase within
    a few seconds. If it says "timestamp missing; used time received", that's fine.
16. Open **Accounts & cards** → **Wallet card mapping**. The card name your phone sent is listed.
    Pick which card it is and tap **Save**. Do this once per card.

**If nothing shows up in Activity:** open Shortcuts → Automation, tap your automation and check the
address (it must end in `/events/wallet`), that the Method is **POST**, and that `key` holds the
**wallet** key. Also check that the Shortcuts app is allowed to use mobile data (Settings → Cellular).

### Optional — describe it instead (iOS 27)

iOS 27 can build the action from a description: in the new automation, choose **Describe a Shortcut**
and paste:

> Send a POST request with Get Contents of URL to https://YOUR-APP.onrender.com/events/wallet with a JSON
> body containing "amount" set to the shortcut input's Amount, "merchant" set to its Merchant and "card"
> set to its card name.

Leave the key out of the description (descriptions may be processed by Apple's cloud). Afterwards
add the `key` field by hand (step 12) and compare the result with Part 2.

### Good to know

- Any field may be missing or oddly formatted (`$12.34`, `12,34`…); the app copes. If the same
  purchase arrives twice within 2 minutes, it's counted once.
- Optional extras: a `timestamp` field (**Format Date** action → ISO 8601, include time → use
  **Formatted Date**), and an `id` field (**Generate UUID**) so a repeat is always recognized.
- Known iPhone quirks: the automation sometimes runs late, asks "Allow" again, or times out, and it
  reportedly also fires for **declined** cards. Missing taps are harmless (Plaid catches the charge).
  A declined tap shows up in **Review** after 5 days; tap **Never charged / refunded** to release it.
- The key can also be sent as an `X-API-Key` header instead of the `key` field.

Test from a Windows PC without buying anything (PowerShell):

```powershell
Invoke-RestMethod -Method Post -Uri "https://YOUR-APP.onrender.com/events/wallet" `
  -ContentType "application/json" `
  -Body '{"key":"YOUR_WALLET_API_KEY","amount":"$1.23","merchant":"Test Shop","card":"Test Card"}'
```

(That test shows up in your review queue; tap **Duplicate** to remove it.)

---

## 6. Switching to your real accounts (Plaid Trial)

**Only after the tests and the Sandbox end-to-end check pass.** Trial allows **10
Production connections in total**, and deleting one does **not** give the slot back.

1. Plaid Dashboard: apply for / confirm the **Trial plan**, then copy your **Production secret**.
2. Optional but recommended for phones: in the Plaid Dashboard → Developers → API → *Allowed
   redirect URIs*, add `https://YOUR-APP.onrender.com/link/oauth-return`, and set the same
   value as `PLAID_REDIRECT_URI` on Render. Some banks (e.g. Chase) log you in on their own site
   ("OAuth") and send you back to this page.
3. Start with an empty database so test data doesn't mix with real data. In the Render
   service → **Shell**, run:
   `python scripts/wipe_database.py --i-understand-this-deletes-everything`
   (It refuses to run if any real connection is stored.)
4. Render → Environment: set `PLAID_ENV=production` and `PLAID_SECRET=<Production secret>`. Save.
5. Open **Connect**. It now shows **production** and a "connections used: N of 10" counter.
   Connect **each bank once**: your checking bank (checking + reserve accounts) and each card
   issuer. If a connection later needs attention, use its **Reconnect** button. That repairs
   the same connection and doesn't use a new slot. The app warns you before connecting the
   same bank twice.
6. On **Accounts & cards**, mark primary checking and reserve, set statement-close dates,
   and map Wallet card names. On **Settings**, pick a buffer, holdback and tracking start date.
   On **Bills**, add your bills.

---

## 7. Using the app day to day

- **Dashboard**: safe-to-spend, the full breakdown, data freshness, per-card spend
  (provisional vs posted), statement balances, due dates, next close (*estimate* or *entered by you*).
- **Reserve workflow**: the dashboard suggests a transfer (= card spend not yet covered). Make
  that transfer **yourself** in your bank app, then click **I moved it**. The amount stays
  counted until Plaid shows it leaving checking *and* the checking balance reflects it, and it's
  confirmed once it posts on both accounts. It's never subtracted twice.
- **Review queue**: confirm or reject uncertain matches, pick the right tap when two
  identical purchases happened, release taps that never posted, and mark duplicates.
- **Ledger**: every change to "card spend not yet covered", with the reason. Corrections are
  new entries; the database refuses edits and deletions.
- **Refresh now**: asks Plaid to check your banks immediately (`/transactions/refresh`, which
  the Trial plan includes), then syncs. If your plan doesn't include it, it runs a normal
  sync and says so.
- Automatic: webhooks trigger syncs, a fallback sync runs every 6 hours, and unmatched taps
  are checked hourly.

---

## 8. Running the tests

Tests use a real PostgreSQL database (wiped on every run; its name must contain "test") and
fake Plaid data. They never call Plaid.

```powershell
.\.venv\Scripts\Activate.ps1
$env:TEST_DATABASE_URL="postgresql://postgres:YOUR_PASSWORD@localhost:5432/rewards_test"
pytest -v
```

---

## 9. Security notes

- Read-only Plaid products only (Transactions, Balance, Liabilities). No payment or transfer API is used.
- Bank passwords are typed into Plaid's own window; this app never sees them, nor full card
  numbers or CVVs (Plaid doesn't provide those for these products).
- Plaid access tokens are encrypted at rest (Fernet / AES + HMAC) with `TOKEN_ENCRYPTION_KEY`.
  They're never logged (a logging filter also redacts anything token-shaped), never shown,
  and never sent to the browser.
- Every endpoint except `/healthz`, `/login`, the Shortcut endpoint (wallet key) and Plaid
  webhooks (signature-verified: ES256 JWT, body SHA-256, 5-minute replay window) needs the
  admin key or the login cookie (HttpOnly, SameSite=Lax, plus a same-origin check on every form).
- Money is stored as integer cents everywhere.
