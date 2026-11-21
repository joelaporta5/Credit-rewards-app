# Design decisions

A running log. Newest decisions at the bottom of each section.

## Platform

**D1. Hosting: Render.** Compared with Fly.io and Railway:
- Render deploys a GitHub repo from a `render.yaml` Blueprint, entirely in the browser,
  with managed PostgreSQL (with backups on paid plans), automatic HTTPS, a pre-deploy step for
  migrations and a web shell. No CLI to install on Windows.
- Fly.io needs the `flyctl` CLI and Docker/machine concepts, which is more setup for a non-engineer.
- Railway is similarly easy, but its usage-based billing is less predictable for an always-on service.
- We use **paid** Render plans: free web services sleep after 15 min idle (~1 min cold start, too
  slow for the Shortcut and webhooks), and free databases expire after 30 days (Render docs).

**D2. One process, in-process scheduler.** A 6-hour fallback sync and an hourly "missing
tap" check run as asyncio tasks inside the web process. No separate worker or paid cron.
Per-connection Postgres advisory locks make overlapping syncs safe if a second process ever runs.

**D3. Background work via FastAPI BackgroundTasks.** The Shortcut endpoint stores the raw event
(one insert) and returns `202` immediately; parsing into a purchase, matching and the ledger run
after the response. Webhooks likewise answer before syncing.

**D4. Tests run on real PostgreSQL**, with the schema built by the Alembic migrations, not
SQLite or `create_all`. The ledger's append-only trigger, partial unique indexes and advisory
locks are part of what's tested.

## Money and history

**D5. Integer cents everywhere.** Plaid's JSON numbers are converted via `Decimal(str(x))`,
never float arithmetic. The phone's amount strings go through a tolerant parser (`$12.34`,
`12,34`, `1.234,56`, `(5.00)`, `USD 5`). If both `,` and `.` appear, the last one is the decimal
separator. A single separator followed by exactly 3 digits is read as thousands (with a warning).

**D6. The ledger tracks only "card spend not yet covered."** Verified cash comes from
balance snapshots (append-only table), and bills, buffer and holdback come from settings
and the bills table. All are shown separately and never blended.

**D7. Target-and-delta ledger.** Each purchase and each reserve transfer has a *target*
contribution derived from current evidence (wallet tap, Plaid pending/posted/removed, review
decisions). When evidence changes we append one entry for `target − sum(existing entries)` with
a human-readable reason. Consequences:
- history is never edited, and corrections are new entries;
- the dashboard's figure is literally `max(0, SUM(ledger_entries.amount_cents))`;
- an integrity check (`purchase_sum == purchase_target` for every purchase) is easy to test.

**D8. Database-enforced append-only.** `ledger_entries`, `match_decisions`, `audit_log`,
`balance_snapshots`, `card_liability_snapshots` and `plaid_transaction_history` have a trigger
that rejects `UPDATE` and `DELETE`.

**D9. Over-funded reserve counts as $0, not negative.** If you moved more to the reserve than
you've spent, the extra sits in the reserve (not in checking), so it must not raise safe-to-spend.

**D10. Tracking start date.** Plaid returns ~90 days of history when you first connect. Card
charges before the tracking start date (default: the day it's first needed) aren't counted,
because your existing statements are assumed to be planned for. It can be moved earlier (charges
are then added), not later (that would rewrite history; use a manual ledger entry instead).

## Plaid

**D11. Verify against Plaid's own sources.** Endpoints and fields were checked against
Plaid's OpenAPI spec (2020-09-14, v1.822.2) and plaid.com/docs pages. See docs/PLAID_VERIFICATION.md.

**D12. Link products:** `products: ["transactions"]`, `required_if_supported_products:
["liabilities"]`. A checking-only bank still links, and cards get Liabilities. Balance needs no
initialization (Plaid docs). `client_user_id` is the constant `"owner"` (no PII).

**D13. Sync atomically.** We fetch *all* `/transactions/sync` pages first, restart from the
original cursor on `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION` (per Plaid docs), then apply
added → modified → removed **and save the new cursor in one DB transaction**.

**D14. Pending → posted updates the row in place.** If an added transaction has a
`pending_transaction_id` we already store, the existing row gets the new id (the old one is kept in
`replaced_pending_transaction_id`), so the linked purchase keeps its identity. A later "removed"
for the old pending id is recognized as superseded. Plaid says the pair isn't guaranteed to be on the
same page, and if the removal arrives in an *earlier* update, the row is released and then
*revived* when the posted version arrives. If Plaid can't link them (`pending_transaction_id`
null), the pending is released and the posted one is re-matched to the tap.

**D15. Balances: real-time `/accounts/balance/get`** after every sync and on Refresh (minimum
5 minutes apart unless forced). The `accounts` balances embedded in `/transactions/sync` may be
cached (Plaid docs), so they're not used for verified cash. Every result is stored.

**D16. Webhook verification exactly per Plaid docs.** We require `alg == ES256`, fetch the JWK by
`kid` (cached; an expired key is rejected), verify the signature, reject if `iat` is more than 5
minutes old, and compare the SHA-256 of the raw body in constant time. Rejected webhooks are
logged and get 401.

**D17. Webhook handling.** Any `TRANSACTIONS` webhook runs an (idempotent) sync, since Plaid still
sends the legacy codes alongside `SYNC_UPDATES_AVAILABLE`. `ITEM/ERROR` marks the connection
`needs_reauth` (for login-type codes) or `error`. `PENDING_DISCONNECT`/`PENDING_EXPIRATION`
mark it `expiring`, `USER_PERMISSION_REVOKED`/`USER_ACCOUNT_REVOKED` mark it `revoked`,
`LOGIN_REPAIRED` clears the flag, `NEW_ACCOUNTS_AVAILABLE` offers update mode with account
selection, and `LIABILITIES/DEFAULT_UPDATE` refetches liabilities.

**D18. Re-authentication uses Link update mode** (`access_token` in `/link/token/create`, no
products, no token exchange). This doesn't create a new Item, so it doesn't use a Trial slot.

**D19. Trial-slot protection.** The Connect page shows Production connections used out of 10. The
server returns 409 for a second connection to an institution already connected, and asks for
explicit confirmation **before** exchanging the public token (Plaid counts access tokens created).

**D20. Refresh.** "Refresh now" calls `/transactions/refresh` (Plaid docs: an add-on, included in
Trial plans), then syncs. On any error it still syncs and tells you refresh wasn't available.

**D21. Separate Sandbox and Production data.** Each connection records its environment; syncs only
touch connections from the current `PLAID_ENV`. The README recommends wiping the database
(guarded script) before going live.

## Matching

**D22. Purchases as the unit of counting.** A purchase is one real-world charge with evidence
from a wallet event and/or a Plaid transaction. Linking merges a Plaid-only purchase into the
tap's purchase (the merged one is released in the ledger), so a charge is never counted twice.

**D23. Global greedy matching with tie detection.** All open taps × open Plaid card charges
(same mapped card, Plaid date 0–3 days after the tap's *local* date, merchant similarity ≥ 0.55,
amount rule satisfied) are scored, sorted, and assigned one-to-one. If another unassigned pair
involving the same tap or charge scores within 0.015 of the best, the whole tied cluster is left
unlinked and goes to review.

**D24. Score** = 0.45 × merchant similarity + 0.35 × amount score + 0.20 × timing score.
High ≥ 0.85 auto-links. Medium ≥ 0.70 links and adds a review item. Below that, nothing is
linked and it goes to review. Amount score: exact (±1¢) 1.0; restaurant tip (posted up to 35%
higher) 0.8; gas/hotel/car-rental hold (any amount) 0.7. Timing: 0/1/2/3 days → 1.0/0.9/0.75/0.6,
refined by Plaid's `authorized_datetime` when present (this is what separates two identical
coffees bought hours apart).

**D25. Merchant category** comes from (in order) a manual category on the merchant, Plaid's
personal finance category (`FOOD_AND_DRINK_RESTAURANT`, which Plaid's taxonomy says covers
"restaurants, bars, gastropubs"; `TRANSPORTATION_GAS`; `TRAVEL_LODGING`; `TRAVEL_RENTAL_CARS`),
then the merchant category code (beta field; 5812/5813, 5541/5542, 7011/3501–3999, 7512/3351–3500),
then name keywords.

**D26. Merchant similarity** strips processor prefixes (`SQ *`, `TST*`, `PAYPAL *`, …), store
numbers and noise, then takes the best of RapidFuzz `token_set_ratio`, `WRatio` and a
space-insensitive ratio, against Plaid's `merchant_name`, `name` and counterparties.

**D27. Uncertainty is counted conservatively and labeled.** While an ambiguous or low-confidence
match waits for review, both the tap and the charge are counted. The dashboard shows "may be
counted twice: $X" so the number is visibly *lower*, never higher. Unmapped-card taps count too.

**D28. Duplicate Shortcut deliveries** are detected by (amount, cleaned merchant, card) within
120 seconds of arrival, or by the same client `id`, under a Postgres advisory lock so two
simultaneous deliveries can't both slip through.

**D29. Refunds vs payments on cards.** A negative card transaction is a **payment** (ignored, since
it's paid from the reserve, which is outside verified cash) if Plaid categorizes it as
`LOAN_PAYMENTS*`/`TRANSFER_IN*` or its description says PAYMENT/AUTOPAY/THANK YOU. Otherwise it's
a **refund**, which reduces card spend.

**D30. Missing taps.** A tap with no Plaid charge after 5 days goes to review ("possibly missing,
declined or refunded") and **stays counted** until you release it. A tap whose Plaid charge was
later *removed* (dropped pending) is already released and isn't flagged again.

## Reserve transfers

**D31. A transfer only reduces card spend once checking reflects it.** Clicking "I moved $X"
records a pending transfer and lowers the *suggested* transfer, but doesn't touch the ledger,
because the money is still in verified cash. When Plaid shows a matching outflow on primary
checking (same amount, from 3 days before the click) **and** the latest checking balance
was fetched after Plaid first showed that outflow, one `−$X` entry is written. That's the moment
verified cash drops by $X, so safe-to-spend never double-counts. It's *confirmed* when both
sides have posted. If the checking outflow disappears, the entry is reversed. Manual confirm
and cancel exist for banks whose transfers Plaid can't see.

## Web and security

**D32. Two keys.** `ADMIN_API_KEY` (dashboard/API; header or login cookie) and a separate
`WALLET_API_KEY` that can only submit wallet events, so a key stored on the phone can't read
financial data.

**D33. Browser sessions**: HMAC-signed cookie (30 days, HttpOnly, Secure on HTTPS, SameSite=Lax:
Lax so the OAuth return page works after the bank redirects back). Every cookie-authenticated
POST must also carry a same-origin `Origin`/`Referer`. Login failures are throttled per IP.

**D34. CSP.** A strict policy on all pages. The Link pages use Plaid's documented directives
(which require `'unsafe-inline'`), and only on those two pages.

**D35. Server-rendered Jinja pages**, mobile-first CSS, a tiny vanilla JS file only for Plaid Link.
