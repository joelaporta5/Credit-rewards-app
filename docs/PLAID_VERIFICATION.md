# Plaid verification log

Every Plaid endpoint, field and webhook the app uses was checked against Plaid's own sources
on 2026-11-21 (not from memory):

- **OpenAPI spec** `plaid/plaid-openapi` → `2020-09-14.yml`, version **2020-09-14_1.822.2**
  (the spec Plaid generates its client libraries from).
- **plaid.com/docs** pages: Transactions API, Transaction states (pending/posted), Items API
  (webhooks), Webhook verification, Link (web, update mode, OAuth, CSP directives), Billing/Trial
  plans, Sandbox test credentials.
- **Personal finance category taxonomy** CSV (`plaid.com/documents/pfc-taxonomy-all.csv`, v1 and v2).
- **plaid-python 47.0.0** model classes (field names and request classes import and construct).

| Used for | Endpoint / object | Fields relied on |
|---|---|---|
| Link | `/link/token/create` | `client_name` (≤30 chars), `language`, `country_codes`, `user.client_user_id`, `products`, `required_if_supported_products`, `transactions.days_requested`, `webhook`, `redirect_uri`, `access_token` (update mode), `update.account_selection_enabled` → `link_token`, `expiration` |
| Link | `/item/public_token/exchange` | `public_token` → `access_token`, `item_id` |
| Transactions | `/transactions/sync` | request `access_token`, `cursor` (omitted on first call), `count` (max 500), `options.include_original_description`; response `added`, `modified`, `removed[].transaction_id`, `next_cursor`, `has_more`, `accounts`, `transactions_update_status`; error `TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION` |
| Transactions | `Transaction` | `transaction_id`, `account_id`, `amount` (+ = money out), `iso_currency_code`, `unofficial_currency_code`, `date`, `authorized_date`, `authorized_datetime`, `datetime`, `name`, `merchant_name`, `merchant_entity_id`, `merchant_category_code` (beta), `pending`, `pending_transaction_id`, `payment_channel`, `personal_finance_category.{primary,detailed}`, `counterparties[].name` |
| Pending→posted | docs "Transaction states" | pending id appears in `removed`, posted in `added` with `pending_transaction_id`; "not guaranteed to be in the same page, but should happen within the same overall update"; pending may disappear without posting (authorization holds: gas, hotels, rental cars) |
| Refresh | `/transactions/refresh` | `access_token`; add-on; included in Trial plans (billing docs); results arrive via `SYNC_UPDATES_AVAILABLE` |
| Balance | `/accounts/balance/get` | `options.account_ids`; `accounts[].balances.{available,current,limit,iso_currency_code,unofficial_currency_code,last_updated_datetime}` (real-time; other endpoints may be cached) |
| Accounts | `/accounts/get` | `accounts[].{account_id,name,official_name,mask,type,subtype}`, `item.institution_name` |
| Liabilities | `/liabilities/get` | `liabilities.credit[].{account_id,last_statement_balance,last_statement_issue_date,next_payment_due_date,minimum_payment_amount,last_payment_amount,last_payment_date,is_overdue}` (no next statement *close* date, so it's entered manually or estimated) |
| Item status | `/item/get` | `item.error.error_code`, `item.consent_expiration_time`, `status.transactions.last_successful_update` |
| Webhook URL | `/item/webhook/update` | `access_token`, `webhook` |
| Webhook verification | `/webhook_verification_key/get` | `key_id` → `key.{alg,crv,kid,kty,use,x,y,created_at,expired_at}`; header `Plaid-Verification`; JWT `alg` ES256; claims `iat` (≤ 5 min), `request_body_sha256` |
| Webhooks | TRANSACTIONS | `SYNC_UPDATES_AVAILABLE` (`item_id`, `initial_update_complete`, `historical_update_complete`); legacy `INITIAL_UPDATE`, `HISTORICAL_UPDATE`, `DEFAULT_UPDATE`, `TRANSACTIONS_REMOVED` still sent |
| Webhooks | ITEM | `ERROR` (`error.error_code`, e.g. `ITEM_LOGIN_REQUIRED`), `PENDING_DISCONNECT` (US/CA, 7 days ahead), `PENDING_EXPIRATION` (EU/UK), `USER_PERMISSION_REVOKED`, `USER_ACCOUNT_REVOKED`, `LOGIN_REPAIRED`, `NEW_ACCOUNTS_AVAILABLE` |
| Webhooks | LIABILITIES | `DEFAULT_UPDATE` |
| Categories | PFC taxonomy | `FOOD_AND_DRINK_RESTAURANT` ("restaurants, bars, gastropubs"), `TRANSPORTATION_GAS`, `TRAVEL_LODGING`, `TRAVEL_RENTAL_CARS`, `LOAN_PAYMENTS_CREDIT_CARD_PAYMENT`, `TRANSFER_IN_*` |
| Sandbox (script only) | `/sandbox/public_token/create`, `/sandbox/item/fire_webhook`, `/sandbox/item/reset_login`, `/sandbox/transactions/create` | institution `ins_109508`, `override_username` `user_transactions_dynamic`; custom transactions are always posted and need non-future dates |
| Trial plan | billing docs | 10 Production Items; removing Items does not free slots; includes Balance, Liabilities, Transactions (incl. Refresh) |
| OAuth | Link OAuth docs | `redirect_uri` must be registered in the Dashboard; reinitialize Link with the same token and `receivedRedirectUri` |
| CSP | Link web docs | `script-src 'unsafe-inline' https://cdn.plaid.com/link/v2/stable/link-initialize.js`, `frame-src https://cdn.plaid.com/`, `connect-src` production/sandbox hosts |

Not verifiable without your account: the exact Plaid error code returned when Refresh isn't
enabled on a plan (the app falls back to a normal sync on *any* refresh error).
