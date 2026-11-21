# Known limitations (Sprint 1)

**Data and timing**
- **Plaid isn't real time.** Plaid checks most banks 1–4 times a day. "Refresh now" asks for
  an immediate check, but the bank itself may lag. Apple Pay taps fill the gap only for taps,
  not online or card-number purchases.
- **Apple Pay taps can be late, missing or for declined cards.** The Wallet automation is known
  to time out or fire late on some phones, and is reported to fire on declined transactions. A
  missing tap is harmless (Plaid catches the charge). A declined or never-posted tap stays counted
  until you release it from the review queue (it's flagged after 5 days).
- **Two identical purchases within 2 minutes** (same amount, merchant and card) are treated as
  one duplicate delivery. Send an `id` from the Shortcut (e.g. *Generate UUID*) to avoid this.
- **Matching window is 0–3 days after the tap's local date** (per spec). A bank that dates a
  charge the day *before* your local tap (time-zone edge cases) won't auto-match. You'll see
  both, and after 5 days the tap is flagged for review.
- **Ambiguous matches are counted twice until reviewed** (conservative; shown on the dashboard).
- **Restaurant tips** are recognized only when Plaid categorizes the merchant as a restaurant/bar
  (or MCC 5812/5813, or a restaurant-like name). Tips at coffee shops or salons need a manual
  confirm from the review queue if the posted amount differs.
- **Gas, hotel and car-rental holds**: while pending, the *hold* amount is reserved
  (conservative), and the final amount replaces it when it posts.

**Safe-to-spend model**
- Bills are assumed paid from checking on their due date. A bill due today still counts. From
  tomorrow it's assumed reflected in the checking balance (if your biller is slow, it may be
  briefly under-counted).
- Card payments are assumed to come from the reserve account. If you pay a card from primary
  checking instead, checking drops while "card spend not yet covered" doesn't, so safe-to-spend
  is understated (safe direction) until you record a reserve transfer or a manual ledger entry.
- Reserve transfers are detected by **exact amount** on primary checking (outflow) and reserve
  (inflow). Banks that split or rename transfers may need "Confirm manually".
- Statement "still owed" uses Plaid's last payment only if it's after the statement date, so
  several partial payments may be undercounted.
- Next statement **close** date isn't provided by Plaid. It's entered by you or estimated
  (last statement date + 1 month) and always labeled.
- Only USD is handled. Other currencies are stored but flagged and not converted.

**Operations**
- One process and an in-memory login throttle. Restarting clears the throttle.
- The scheduler runs inside the web process. If the service is down, the next start
  syncs within ~90 seconds.
- Webhook delivery to a local PC needs a tunnel (README §3). Plaid's Sandbox can't deliver
  webhooks to the development container used to build this, so real webhook delivery is checked
  after deploying with the "Send a test webhook" button.
- No email/push alerts yet (the dashboard is the only place you see a broken connection).
- No automatic backups beyond what your Render database plan provides.
