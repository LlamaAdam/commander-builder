// FP-011 Settings panel — drives GET/PUT /api/config.
//
// The Anthropic token is never returned by the server, so the key field
// starts blank and a "(set · …last4)" hint shows whether one is stored.
// Leaving it blank on save keeps the existing token; typing a new value
// replaces it. Non-secret fields round-trip normally.
(function () {
  "use strict";

  function $(id) { return document.getElementById(id); }

  var dialog = $("settings-dialog");
  if (!dialog) { return; }

  var statusEl = $("settings-status");

  function setStatus(msg, isError) {
    statusEl.textContent = msg || "";
    statusEl.style.color = isError ? "#c0392b" : "";
  }

  // Populate the form from the redacted config payload.
  function fillForm(cfg) {
    cfg = cfg || {};
    $("settings-api-key").value = ""; // never prefill a secret
    var keyState = $("settings-key-state");
    if (cfg.anthropic_api_key_set) {
      keyState.textContent = "(set · " + (cfg.anthropic_api_key_hint || "…") + ")";
    } else {
      keyState.textContent = "(not set)";
    }
    $("settings-bracket").value = cfg.default_bracket != null ? cfg.default_bracket : "";
    $("settings-model").value = cfg.model || "";
    $("settings-moxfield").value = cfg.moxfield_user || "";
  }

  function openSettings() {
    setStatus("loading…", false);
    fetch("/api/config")
      .then(function (r) { return r.json(); })
      .then(function (cfg) { fillForm(cfg); setStatus("", false); })
      .catch(function () { setStatus("could not load config", true); });
    if (typeof dialog.showModal === "function") {
      dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
  }

  function closeSettings() {
    if (typeof dialog.close === "function") { dialog.close(); }
    else { dialog.removeAttribute("open"); }
  }

  // Build a sparse update: only send fields the user actually set.
  // Empty string for a non-secret field means "clear it"; empty key
  // field means "leave the token unchanged" (so we omit it).
  function collectUpdate() {
    var update = {};
    var key = $("settings-api-key").value.trim();
    if (key) { update.anthropic_api_key = key; }

    var bracket = $("settings-bracket").value.trim();
    if (bracket !== "") { update.default_bracket = parseInt(bracket, 10); }

    update.model = $("settings-model").value.trim();
    update.moxfield_user = $("settings-moxfield").value.trim();
    return update;
  }

  function saveSettings() {
    setStatus("saving…", false);
    fetch("/api/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectUpdate()),
    })
      .then(function (r) {
        return r.json().then(function (body) { return { ok: r.ok, body: body }; });
      })
      .then(function (res) {
        if (!res.ok) {
          var detail = (res.body.details && res.body.details.join("; ")) ||
                       res.body.error || "save failed";
          setStatus(detail, true);
          return;
        }
        fillForm(res.body.config);
        setStatus("saved", false);
        setTimeout(closeSettings, 600);
      })
      .catch(function () { setStatus("save failed", true); });
  }

  var btn = $("btn-settings");
  if (btn) { btn.addEventListener("click", openSettings); }
  var cancel = $("settings-cancel");
  if (cancel) { cancel.addEventListener("click", closeSettings); }
  var save = $("settings-save");
  if (save) { save.addEventListener("click", saveSettings); }
})();
