# Sprint 2 backlog (proposed)

Ordered by value to the core promise ("never unable to pay my cards in full"):

1. **Alerts**: push/email when a connection needs re-authentication, safe-to-spend drops
   below a threshold, a statement is due and the reserve is short, or the review queue grows.
2. **Real-account burn-in**: a week of real use with a daily summary of matches, misses and
   corrections; tune thresholds (tie epsilon, tip %, merchant floor) from that data.
3. **Card payments from checking**: detect card payments on primary checking and treat them as
   "coverage" (so paying a card directly doesn't understate safe-to-spend).
4. **Bill payment detection**: match bills to checking outflows instead of assuming they're paid on
   the due date; recurring-bill suggestions from Plaid's recurring transactions.
5. **Better reserve coverage**: account for multiple payments since the statement, and project the
   *next* statement (current-cycle spend) against the reserve.
6. **Merchant categories UI**: let me set a merchant's category (restaurant/gas/…), which feeds
   matching tolerances now and rewards later.
7. **Rewards engine foundations** (cards and merchants already have their own tables): card reward
   rules, per-merchant category history, "best card for this merchant" lookups.
8. **Chrome extension API**: a read-only, separately keyed endpoint returning the best card for a
   site plus current safe-to-spend.
9. **Ops**: persistent login throttle, structured logs, uptime monitoring, documented backup/restore.
10. **Time-zone edge cases**: allow a −1 day tolerance in matching (medium confidence) once real
    data shows it's needed.
