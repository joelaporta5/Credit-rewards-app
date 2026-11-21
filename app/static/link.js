// Plaid Link glue. No secrets live here: the link_token is short-lived and single-purpose,
// and the access token never reaches the browser.
(function () {
  const status = (t) => { const el = document.getElementById("link-status"); if (el) el.textContent = t; };

  async function post(url, body) {
    const r = await fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(body || {}),
    });
    let data = {};
    try { data = await r.json(); } catch (e) { /* ignore */ }
    return { ok: r.ok, status: r.status, data };
  }

  async function finish(publicToken, metadata, itemId) {
    if (itemId) {
      status("Reconnected. Syncing…");
      await post("/plaid/items/" + itemId + "/reconnected", {});
      location.href = "/?msg=" + encodeURIComponent("Reconnected. Syncing in the background.");
      return;
    }
    const inst = (metadata && metadata.institution) || {};
    const body = { public_token: publicToken, institution_id: inst.institution_id || null, institution_name: inst.name || null };
    status("Saving connection…");
    let r = await post("/plaid/exchange", body);
    if (r.status === 409) {
      if (!window.confirm((r.data.detail || "Already connected.") + "\n\nConnect it again anyway?")) {
        status("Cancelled. Nothing was saved.");
        return;
      }
      body.confirm_duplicate = true;
      r = await post("/plaid/exchange", body);
    }
    if (!r.ok) { status("Couldn't save: " + (r.data.detail || r.status)); return; }
    location.href = "/accounts?msg=" + encodeURIComponent("Connected " + (r.data.institution || "account") +
      ". Now mark your primary checking and reserve accounts below. Transactions are loading in the background.");
  }

  function openWith(token, itemId, receivedRedirectUri) {
    const cfg = {
      token: token,
      onSuccess: (pt, md) => { sessionStorage.removeItem("plaid_link_token"); finish(pt, md, itemId); },
      onExit: (err) => { status(err ? ("Plaid closed: " + (err.display_message || err.error_code)) : "Closed."); },
    };
    if (receivedRedirectUri) cfg.receivedRedirectUri = receivedRedirectUri;
    Plaid.create(cfg).open();
  }

  async function start(itemId, accountSelection) {
    status("Opening Plaid…");
    const r = await post("/plaid/link-token", { item_id: itemId ? Number(itemId) : null, account_selection: !!accountSelection });
    if (!r.ok) { status("Couldn't start Plaid: " + (r.data.detail || r.status)); return; }
    sessionStorage.setItem("plaid_link_token", r.data.link_token);
    sessionStorage.setItem("plaid_item_id", itemId || "");
    openWith(r.data.link_token, itemId, null);
  }

  document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("connect-btn");
    if (btn) btn.addEventListener("click", () => {
      if (btn.dataset.plaidEnv === "production" &&
          !window.confirm("This connects a REAL bank and uses one of your Plaid Trial connections. Continue?")) return;
      start(null, false);
    });
    document.querySelectorAll(".reconnect-btn").forEach((b) =>
      b.addEventListener("click", () => start(b.dataset.itemId, b.dataset.accountSelection === "true")));
    if (document.body.dataset.oauthReturn) {
      const token = sessionStorage.getItem("plaid_link_token");
      if (!token) { status("Your Plaid session expired. Go back to Connect and try again."); return; }
      openWith(token, sessionStorage.getItem("plaid_item_id") || null, window.location.href);
    }
  });
})();
