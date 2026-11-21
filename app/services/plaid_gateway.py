"""Thin wrapper around the official Plaid Python client (plaid-python 47).

Read-only products only: Link, Transactions (/transactions/sync, /transactions/refresh),
Balance (/accounts/balance/get), Liabilities (/liabilities/get), Item status and
webhook verification keys. There is deliberately NO payment, transfer or
money-movement call anywhere in this codebase.

Every method returns plain JSON-like dicts so the rest of the app (and the tests'
fake gateway) never depend on the client's model classes.

Endpoints and field names were checked against Plaid's OpenAPI spec 2020-09-14
v1.822.2 and plaid.com/docs (see docs/PLAID_VERIFICATION.md).
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, Protocol

from app.config import get_settings


class PlaidError(Exception):
    def __init__(self, error_code: str, error_type: str = "", message: str = "", display_message: str | None = None, status: int | None = None):
        super().__init__(f"{error_type}/{error_code}: {message}")
        self.error_code = error_code
        self.error_type = error_type
        self.message = message
        self.display_message = display_message
        self.status = status


# Error codes meaning the person must re-authenticate through Link update mode.
REAUTH_ERROR_CODES = {
    "ITEM_LOGIN_REQUIRED",
    "PENDING_EXPIRATION",
    "PENDING_DISCONNECT",
    "INVALID_CREDENTIALS",
    "INSUFFICIENT_CREDENTIALS",
    "INVALID_MFA",
    "INVALID_UPDATED_USERNAME",
    "ITEM_LOCKED",
    "USER_SETUP_REQUIRED",
    "MFA_NOT_SUPPORTED",
    "NO_ACCOUNTS",
    "ACCESS_NOT_GRANTED",
}


class Gateway(Protocol):
    def link_token_create(self, *, webhook: str, redirect_uri: str | None, access_token: str | None = None,
                          account_selection: bool = False) -> dict: ...
    def public_token_exchange(self, public_token: str) -> dict: ...
    def transactions_sync(self, access_token: str, cursor: str | None, count: int = 500) -> dict: ...
    def transactions_refresh(self, access_token: str) -> dict: ...
    def accounts_get(self, access_token: str) -> dict: ...
    def accounts_balance_get(self, access_token: str, account_ids: list[str] | None = None) -> dict: ...
    def liabilities_get(self, access_token: str) -> dict: ...
    def item_get(self, access_token: str) -> dict: ...
    def item_webhook_update(self, access_token: str, webhook: str) -> dict: ...
    def webhook_verification_key_get(self, key_id: str) -> dict: ...


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if hasattr(obj, "to_dict"):
        return _jsonable(obj.to_dict())
    if hasattr(obj, "value") and obj.__class__.__module__.startswith("plaid.model"):
        return obj.value
    return obj


class PlaidGateway:
    """Real Plaid client. Credentials come from PLAID_CLIENT_ID / PLAID_SECRET."""

    def __init__(self) -> None:
        import plaid
        from plaid.api import plaid_api

        s = get_settings()
        if not s.plaid_client_id or not s.plaid_secret:
            raise RuntimeError("PLAID_CLIENT_ID and PLAID_SECRET are not set")
        host = plaid.Environment.Production if s.plaid_env == "production" else plaid.Environment.Sandbox
        cfg = plaid.Configuration(
            host=host,
            api_key={"clientId": s.plaid_client_id, "secret": s.plaid_secret, "plaidVersion": "2020-09-14"},
        )
        self._client = plaid_api.PlaidApi(plaid.ApiClient(cfg))
        self.env = s.plaid_env

    # -- helpers ----------------------------------------------------------
    def _call(self, fn, request) -> dict:
        from plaid.exceptions import ApiException

        try:
            resp = fn(request)
        except ApiException as exc:
            try:
                body = json.loads(exc.body or "{}")
            except (TypeError, ValueError):
                body = {}
            raise PlaidError(
                body.get("error_code", "UNKNOWN"),
                body.get("error_type", ""),
                body.get("error_message", str(exc.reason)),
                body.get("display_message"),
                exc.status,
            ) from None  # don't chain: the original carries request details
        return _jsonable(resp)

    # -- Link ---------------------------------------------------------------
    def link_token_create(self, *, webhook: str, redirect_uri: str | None, access_token: str | None = None,
                          account_selection: bool = False) -> dict:
        from plaid.model.country_code import CountryCode
        from plaid.model.link_token_create_request import LinkTokenCreateRequest
        from plaid.model.link_token_create_request_update import LinkTokenCreateRequestUpdate
        from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
        from plaid.model.link_token_transactions import LinkTokenTransactions
        from plaid.model.products import Products

        s = get_settings()
        kwargs: dict[str, Any] = dict(
            client_name=s.plaid_client_name[:30],
            language="en",
            country_codes=[CountryCode("US")],
            # A fixed opaque id: single-user app, and Plaid asks for no PII here.
            user=LinkTokenCreateRequestUser(client_user_id="owner"),
            webhook=webhook,
        )
        if redirect_uri:
            kwargs["redirect_uri"] = redirect_uri
        if access_token:
            # Update mode (re-authentication): products must be omitted.
            kwargs["access_token"] = access_token
            if account_selection:
                kwargs["update"] = LinkTokenCreateRequestUpdate(account_selection_enabled=True)
        else:
            kwargs["products"] = [Products("transactions")]
            # Liabilities only where the institution/account supports it (credit cards);
            # a checking-only bank still links. Balance needs no initialization.
            kwargs["required_if_supported_products"] = [Products("liabilities")]
            kwargs["transactions"] = LinkTokenTransactions(days_requested=s.plaid_days_requested)
        return self._call(self._client.link_token_create, LinkTokenCreateRequest(**kwargs))

    def public_token_exchange(self, public_token: str) -> dict:
        from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest

        return self._call(self._client.item_public_token_exchange, ItemPublicTokenExchangeRequest(public_token=public_token))

    # -- Transactions -------------------------------------------------------
    def transactions_sync(self, access_token: str, cursor: str | None, count: int = 500) -> dict:
        from plaid.model.transactions_sync_request import TransactionsSyncRequest
        from plaid.model.transactions_sync_request_options import TransactionsSyncRequestOptions

        kwargs: dict[str, Any] = dict(
            access_token=access_token,
            count=count,
            options=TransactionsSyncRequestOptions(include_original_description=False),
        )
        if cursor:
            kwargs["cursor"] = cursor
        return self._call(self._client.transactions_sync, TransactionsSyncRequest(**kwargs))

    def transactions_refresh(self, access_token: str) -> dict:
        from plaid.model.transactions_refresh_request import TransactionsRefreshRequest

        return self._call(self._client.transactions_refresh, TransactionsRefreshRequest(access_token=access_token))

    # -- Accounts / Balance / Liabilities ------------------------------------
    def accounts_get(self, access_token: str) -> dict:
        from plaid.model.accounts_get_request import AccountsGetRequest

        return self._call(self._client.accounts_get, AccountsGetRequest(access_token=access_token))

    def accounts_balance_get(self, access_token: str, account_ids: list[str] | None = None) -> dict:
        from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
        from plaid.model.accounts_balance_get_request_options import AccountsBalanceGetRequestOptions

        kwargs: dict[str, Any] = dict(access_token=access_token)
        if account_ids:
            kwargs["options"] = AccountsBalanceGetRequestOptions(account_ids=account_ids)
        return self._call(self._client.accounts_balance_get, AccountsBalanceGetRequest(**kwargs))

    def liabilities_get(self, access_token: str) -> dict:
        from plaid.model.liabilities_get_request import LiabilitiesGetRequest

        return self._call(self._client.liabilities_get, LiabilitiesGetRequest(access_token=access_token))

    # -- Item -------------------------------------------------------------------
    def item_get(self, access_token: str) -> dict:
        from plaid.model.item_get_request import ItemGetRequest

        return self._call(self._client.item_get, ItemGetRequest(access_token=access_token))

    def item_webhook_update(self, access_token: str, webhook: str) -> dict:
        from plaid.model.item_webhook_update_request import ItemWebhookUpdateRequest

        return self._call(self._client.item_webhook_update, ItemWebhookUpdateRequest(access_token=access_token, webhook=webhook))

    def webhook_verification_key_get(self, key_id: str) -> dict:
        from plaid.model.webhook_verification_key_get_request import WebhookVerificationKeyGetRequest

        return self._call(self._client.webhook_verification_key_get, WebhookVerificationKeyGetRequest(key_id=key_id))

    # -- Sandbox-only helpers (used by scripts/sandbox_e2e.py) ---------------
    def _require_sandbox(self) -> None:
        if self.env != "sandbox":
            raise RuntimeError("Sandbox helper called outside the Sandbox environment")

    def sandbox_public_token_create(self, institution_id: str, products: list[str], webhook: str | None,
                                    override_username: str | None = None) -> dict:
        from plaid.model.products import Products
        from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
        from plaid.model.sandbox_public_token_create_request_options import SandboxPublicTokenCreateRequestOptions

        self._require_sandbox()
        opts: dict[str, Any] = {}
        if webhook:
            opts["webhook"] = webhook
        if override_username:
            opts["override_username"] = override_username
            opts["override_password"] = "pass_good"
        req = SandboxPublicTokenCreateRequest(
            institution_id=institution_id,
            initial_products=[Products(p) for p in products],
            options=SandboxPublicTokenCreateRequestOptions(**opts),
        )
        return self._call(self._client.sandbox_public_token_create, req)

    def sandbox_fire_webhook(self, access_token: str, webhook_code: str, webhook_type: str | None = None) -> dict:
        from plaid.model.sandbox_item_fire_webhook_request import SandboxItemFireWebhookRequest

        self._require_sandbox()
        kwargs: dict[str, Any] = dict(access_token=access_token, webhook_code=webhook_code)
        if webhook_type:
            from plaid.model.webhook_type import WebhookType

            kwargs["webhook_type"] = WebhookType(webhook_type)
        return self._call(self._client.sandbox_item_fire_webhook, SandboxItemFireWebhookRequest(**kwargs))

    def sandbox_reset_login(self, access_token: str) -> dict:
        from plaid.model.sandbox_item_reset_login_request import SandboxItemResetLoginRequest

        self._require_sandbox()
        return self._call(self._client.sandbox_item_reset_login, SandboxItemResetLoginRequest(access_token=access_token))

    def sandbox_transactions_create(self, access_token: str, transactions: list[dict]) -> dict:
        from plaid.model.custom_sandbox_transaction import CustomSandboxTransaction
        from plaid.model.sandbox_transactions_create_request import SandboxTransactionsCreateRequest

        self._require_sandbox()
        txns = [
            CustomSandboxTransaction(
                date_transacted=t["date_transacted"],
                date_posted=t["date_posted"],
                amount=float(t["amount"]),  # the Plaid client requires a float here (Sandbox test data only)
                description=t["description"],
                **({"account_id": t["account_id"]} if t.get("account_id") else {}),
            )
            for t in transactions
        ]
        return self._call(
            self._client.sandbox_transactions_create,
            SandboxTransactionsCreateRequest(access_token=access_token, transactions=txns),
        )


_gateway_override: Gateway | None = None


def get_gateway() -> Gateway:
    if _gateway_override is not None:
        return _gateway_override
    return PlaidGateway()


def set_gateway(gw: Gateway | None) -> None:
    """Tests install a fake gateway here."""
    global _gateway_override
    _gateway_override = gw
