# Test report

Run on 2026-11-21 in the development container: Python 3.12.3, PostgreSQL 16.15, schema created by the Alembic migrations, fake Plaid data (no network).

**Result: 70 passed in 7.47s.** (A retry test added afterwards brings the suite to 71; latest run: 71 passed.)

Command: `pytest -v`

## Required Sprint 1 tests

| # | Scenario | Test | Result |
|---|---|---|---|
| 1 | Apple Pay → exact pending → posted: counted once | `tests/test_scenarios.py::test_01_apple_pay_then_pending_then_posted_counted_once` | PASSED |
| 2 | Restaurant $40 tapped → $48 posted: matched, $8 tip recorded | `tests/test_scenarios.py::test_02_restaurant_tip_matched_and_recorded` | PASSED |
| 3 | Gas hold $100 → posted $42.17: matched, final amount used | `tests/test_scenarios.py::test_03_gas_hold_then_final_amount` | PASSED |
| 4 | Two identical same-day purchases: no double-matching; ambiguous → review | `tests/test_scenarios.py::test_04_two_identical_purchases_same_day_no_double_matching` | PASSED |
| 5 | Duplicate Shortcut delivery: counted once | `tests/test_scenarios.py::test_05_duplicate_shortcut_delivery_counted_once` | PASSED |
| 6 | Tap never posts: review after 5 days | `tests/test_scenarios.py::test_06_never_posts_goes_to_review_after_5_days` | PASSED |
| 7 | Online purchase seen only in Plaid: counted normally | `tests/test_scenarios.py::test_07_online_purchase_seen_only_in_plaid` | PASSED |
| 8 | Refund releases reserved amount | `tests/test_scenarios.py::test_08_refund_releases_reserved_amount` | PASSED |
| 9 | Pending removed without replacement: released and recorded | `tests/test_scenarios.py::test_09_pending_removed_without_replacement_releases_and_records` | PASSED |
| 10 | Reserve transfer marked → in transit → posted both sides: not double counted | `tests/test_scenarios.py::test_10_reserve_transfer_in_transit_not_double_counted` | PASSED |
| 11 | Stale balance: holdback applied, warning shown | `tests/test_scenarios.py::test_11_stale_balance_applies_holdback_and_warns` | PASSED |
| 12 | Unmapped Wallet card: counted, sent to review | `tests/test_scenarios.py::test_12_unmapped_wallet_card_counts_and_goes_to_review` | PASSED |
| 13 | Webhook with invalid signature: rejected | `tests/test_api.py::test_13_webhook_with_invalid_signature_is_rejected` | PASSED |
| 14 | Ledger integrity: dashboard totals = sum of ledger entries | `tests/test_scenarios.py::test_14_ledger_integrity_dashboard_equals_sum_of_entries` | PASSED |

Additional variants of the required scenarios: 1b (removal arrives before the posted version), 2b (only a posted charge with tip), 2c (non-restaurant mismatch does not match), 3b (tap amount differs from hold; posted version without pending link), 4b (identical purchases separated by authorization time), 7b (history before tracking start not counted).

## Full output (every test)

```
tests/test_api.py::test_every_non_webhook_endpoint_requires_auth PASSED
tests/test_api.py::test_browser_login_cookie_and_csrf_check PASSED
tests/test_api.py::test_wallet_endpoint_accepts_messy_input_and_dedupes PASSED
tests/test_api.py::test_13_webhook_with_invalid_signature_is_rejected PASSED
tests/test_api.py::test_item_error_webhook_flags_reauth_on_dashboard PASSED
tests/test_api.py::test_login_repaired_and_pending_disconnect PASSED
tests/test_api.py::test_link_exchange_stores_encrypted_token_and_syncs PASSED
tests/test_api.py::test_sync_restarts_pagination_after_mutation_error PASSED
tests/test_api.py::test_sync_error_marks_reauth_and_keeps_cursor PASSED
tests/test_api.py::test_refresh_falls_back_to_sync_when_not_on_plan PASSED
tests/test_api.py::test_dashboard_page_renders_breakdown PASSED
tests/test_api.py::test_review_page_actions PASSED
tests/test_api.py::test_account_roles_and_wallet_mapping_pages PASSED
tests/test_scenarios.py::test_01_apple_pay_then_pending_then_posted_counted_once PASSED
tests/test_scenarios.py::test_01b_removal_arriving_before_posted_version_is_not_double_counted PASSED
tests/test_scenarios.py::test_02_restaurant_tip_matched_and_recorded PASSED
tests/test_scenarios.py::test_02b_restaurant_posted_only_with_tip_matches_by_tip_rule PASSED
tests/test_scenarios.py::test_02c_non_restaurant_amount_mismatch_does_not_match PASSED
tests/test_scenarios.py::test_03_gas_hold_then_final_amount PASSED
tests/test_scenarios.py::test_03b_gas_tap_amount_differs_from_hold_still_matches PASSED
tests/test_scenarios.py::test_04_two_identical_purchases_same_day_no_double_matching PASSED
tests/test_scenarios.py::test_04b_same_amount_distinguished_by_authorization_time PASSED
tests/test_scenarios.py::test_05_duplicate_shortcut_delivery_counted_once PASSED
tests/test_scenarios.py::test_06_never_posts_goes_to_review_after_5_days PASSED
tests/test_scenarios.py::test_07_online_purchase_seen_only_in_plaid PASSED
tests/test_scenarios.py::test_07b_transactions_before_tracking_start_are_not_counted PASSED
tests/test_scenarios.py::test_08_refund_releases_reserved_amount PASSED
tests/test_scenarios.py::test_09_pending_removed_without_replacement_releases_and_records PASSED
tests/test_scenarios.py::test_10_reserve_transfer_in_transit_not_double_counted PASSED
tests/test_scenarios.py::test_11_stale_balance_applies_holdback_and_warns PASSED
tests/test_scenarios.py::test_12_unmapped_wallet_card_counts_and_goes_to_review PASSED
tests/test_scenarios.py::test_14_ledger_integrity_dashboard_equals_sum_of_entries PASSED
tests/test_units.py::test_parse_amount_variants[$12.34-1234] PASSED
tests/test_units.py::test_parse_amount_variants[12.34-1234_0] PASSED
tests/test_units.py::test_parse_amount_variants[12,34-1234] PASSED
tests/test_units.py::test_parse_amount_variants[1,234.56-123456] PASSED
tests/test_units.py::test_parse_amount_variants[1.234,56-123456] PASSED
tests/test_units.py::test_parse_amount_variants[$1,234-123400] PASSED
tests/test_units.py::test_parse_amount_variants[USD 5-500] PASSED
tests/test_units.py::test_parse_amount_variants[5.00 USD-500] PASSED
tests/test_units.py::test_parse_amount_variants[-$5.00--500] PASSED
tests/test_units.py::test_parse_amount_variants[(5.00)--500] PASSED
tests/test_units.py::test_parse_amount_variants[ $ 7.5 -750] PASSED
tests/test_units.py::test_parse_amount_variants[12.34-1234_1] PASSED
tests/test_units.py::test_parse_amount_variants[7-700] PASSED
tests/test_units.py::test_parse_amount_variants[0.1-10] PASSED
tests/test_units.py::test_parse_amount_variants[1 234,50-123450] PASSED
tests/test_units.py::test_parse_amount_variants[$0.99-99] PASSED
tests/test_units.py::test_parse_amount_variants[12.-1200] PASSED
tests/test_units.py::test_parse_amount_rejects_garbage[None] PASSED
tests/test_units.py::test_parse_amount_rejects_garbage[] PASSED
tests/test_units.py::test_parse_amount_rejects_garbage[abc] PASSED
tests/test_units.py::test_parse_amount_rejects_garbage[$] PASSED
tests/test_units.py::test_parse_amount_rejects_garbage[12.34.5x] PASSED
tests/test_units.py::test_parse_amount_rejects_garbage[True] PASSED
tests/test_units.py::test_parse_amount_rejects_garbage[1,23,4] PASSED
tests/test_units.py::test_parse_amount_flags_non_usd PASSED
tests/test_units.py::test_plaid_amount_uses_decimal_not_float_math PASSED
tests/test_units.py::test_fmt PASSED
tests/test_units.py::test_clean_merchant[SQ *BLUE BOTTLE COFFEE-BLUE BOTTLE COFFEE] PASSED
tests/test_units.py::test_clean_merchant[TST* Joe's Diner-JOES DINER] PASSED
tests/test_units.py::test_clean_merchant[PAYPAL *ETSY INC-ETSY] PASSED
tests/test_units.py::test_clean_merchant[STARBUCKS STORE #12345-STARBUCKS] PASSED
tests/test_units.py::test_clean_merchant[SHELL OIL 57444-SHELL OIL] PASSED
tests/test_units.py::test_similarity_tolerates_prefixes_and_noise PASSED
tests/test_units.py::test_amount_rules PASSED
tests/test_units.py::test_timestamp_parsing PASSED
tests/test_units.py::test_bill_occurrences PASSED
tests/test_units.py::test_statement_close_estimate_and_manual_override PASSED
tests/test_units.py::test_add_months_clamps PASSED
```

## Sandbox end-to-end (`scripts/sandbox_e2e.py`)

Run on 2026-11-21 against the real Plaid **Sandbox** API (plaid-python 47.0.0), First Platypus
Bank (`ins_109508`), test user `user_transactions_dynamic`, two Sandbox logins (the second one's
checking account plays the reserve).

**Result: 14/14 checks passed** (third run). Output of that run, unedited except for the long account list:

```
Database: postgresql://postgres@localhost:5432/rewards_e2e
Plaid: sandbox

[PASS] Sandbox Item created and token stored encrypted — products: transactions+liabilities
[PASS] Checking, reserve and credit card accounts found — checking=True reserve=True card=True
[PASS] /transactions/sync returned transactions and saved a cursor — 311 transactions after 1 sync(s), status INITIAL_UPDATE_COMPLETE, last messages []
[PASS] /accounts/balance/get stored a checking balance — available $420.23
[PASS] /liabilities/get stored card statement data — 1 snapshot(s)
[PASS] Apple Pay event matched to the Sandbox transaction — confidence high, after 1 sync(s); Plaid rows for it: 1
[PASS] Matched purchase counted once — 1 Plaid row, 1 active purchase, ledger total for it $12.34 (overall card spend moved $68.90 -> $143.97 because Sandbox also added unrelated charges)
[PASS] Duplicate Shortcut delivery counted once
[PASS] Marked transfer doesn't change safe-to-spend while pending — -$493.97 -> -$493.97
[PASS] Transfer seen leaving checking, recorded once in the ledger — status confirmed, ledger entries [-30000]
[PASS] Transfer confirmed on both accounts — status confirmed
[PASS] 'Refresh now' ran — Asked Plaid to check the bank now (/transactions/refresh). New transactions arrive by webhook within minutes.
[PASS] Ledger integrity: dashboard total == sum of entries; every purchase reconciles — sum $143.36; safe-to-spend $32.94

  Dashboard: verified cash $276.30 − card spend $143.36 − bills $0.00 − buffer $100.00 − holdback $0.00 = $32.94

sync item 1 failed at transactions_sync: transactions_sync: ITEM_LOGIN_REQUIRED
[PASS] ITEM_LOGIN_REQUIRED flags the connection for re-authentication — status needs_reauth (ITEM_LOGIN_REQUIRED)

14/14 checks passed
```

The "sync item 1 failed ... ITEM_LOGIN_REQUIRED" line is expected: the last step forces that
error on purpose (`/sandbox/item/reset_login`).

**Earlier runs (kept for honesty):**
- Run 1: the Plaid Python client ignored the container's HTTPS proxy, so nothing connected.
  Fixed: the gateway now honors the `HTTPS_PROXY` and CA-bundle environment variables.
- Run 2: 9/11. (a) The dynamic test user has only one bank account, so there was no reserve
  account; fixed in the script with a second Sandbox login. (b) Plaid returned
  `INTERNAL_SERVER_ERROR` on the sync right after a refresh; the app recorded it correctly, and
  it now **retries temporary Plaid errors** (5xx, API_ERROR, rate limits) up to 3 times with backoff
  (unit-tested).
- Run 3: 13/14. The "counted once" check compared total card spend before and after, but Sandbox
  added unrelated charges in between. The app was right: one purchase, one Plaid row, $12.34 in
  the ledger. The check now looks at that purchase directly. In that run `/transactions/refresh`
  also hit Sandbox's `TRANSACTIONS_LIMIT`, and the app fell back to a normal sync as designed.

Sandbox note: the dynamic test user's *available* checking balance changes on its own between
calls (e.g. $420.23 → $69.10 → $0.00 → $293.43 while *current* stayed $500.00). That's Plaid's
simulation, which is why the dashboard numbers above move.

**Not covered by this run:** delivery of Plaid webhooks (this development container has no public
address). After deploying, use **Connect → Send a test webhook (Sandbox)** and check that **Activity**
lists it as verified.
