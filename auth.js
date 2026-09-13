/* AP-CODEX account drawer: local sign-up/login + "Continue with Google".
 *
 * Sessions are bearer tokens kept in localStorage and sent as
 * `Authorization: Bearer <token>`. The Google OAuth callback redirects back
 * to the homepage with `?auth=<token>`, which we consume and strip from the URL.
 */
(function () {
  "use strict";

  var TOKEN_KEY = "ap_codex_token";
  var NAME_KEY = "ap_codex_name";

  /* ---------------- API base (matches chat.js) ---------------- */
  var apiBase = "";
  (function () {
    var q = /[?&]api=([^&]+)/.exec(location.search);
    if (q) apiBase = decodeURIComponent(q[1]).replace(/\/+$/, "");
    if (window.AP_CODEX_API_BASE) {
      apiBase = String(window.AP_CODEX_API_BASE).replace(/\/+$/, "");
    }
  })();
  function endpoint(path) {
    return apiBase ? apiBase + path : path;
  }

  /* ---------------- DOM refs ---------------- */
  var veil = document.getElementById("authVeil");
  var panel = document.querySelector(".auth-panel");
  var closeBtn = document.getElementById("authClose");
  var signinBtn = document.getElementById("authSigninBtn");
  var signupBtn = document.getElementById("authSignupBtn");
  var googleBtn = document.getElementById("authGoogle");
  var tabs = document.querySelectorAll("[data-auth-tab]");
  var forms = document.querySelectorAll("[data-auth-form]");
  var signinForm = document.getElementById("signinForm");
  var signupForm = document.getElementById("signupForm");
  var signinError = document.getElementById("signinError");
  var signupError = document.getElementById("signupError");
  var navAuth = document.getElementById("navAuth");
  var navUser = document.getElementById("navUser");
  var navUserName = document.getElementById("navUserName");
  var signoutBtn = document.getElementById("authSignout");

  /* ---------------- modal controls ---------------- */
  function openModal(tab) {
    switchTab(tab || "signin");
    veil.setAttribute("aria-hidden", "false");
    veil.classList.add("open");
    document.body.style.overflow = "hidden";
    var first = veil.querySelector("input");
    if (first) first.focus();
  }

  function closeModal() {
    veil.classList.remove("open");
    veil.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
  }

  function switchTab(name) {
    tabs.forEach(function (tab) {
      tab.classList.toggle("active", tab.dataset.authTab === name);
    });
    forms.forEach(function (form) {
      form.hidden = form.dataset.authForm !== name;
    });
    setError(signinError, "");
    setError(signupError, "");
  }

  function setError(el, message) {
    if (!el) return;
    el.textContent = message || "";
  }

  if (signinBtn) signinBtn.addEventListener("click", function (e) { e.preventDefault(); openModal("signin"); });
  if (signupBtn) signupBtn.addEventListener("click", function (e) { e.preventDefault(); openModal("signup"); });
  if (closeBtn) closeBtn.addEventListener("click", closeModal);
  if (veil) veil.addEventListener("click", function (e) {
    if (e.target === veil) closeModal();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && veil && veil.classList.contains("open")) closeModal();
  });
  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () { switchTab(tab.dataset.authTab); });
  });

  /* ---------------- session helpers ---------------- */
  function applyUser(user) {
    if (user) {
      localStorage.setItem(TOKEN_KEY, user.token || "");
      localStorage.setItem(NAME_KEY, user.user ? user.user.name : "");
    }
    var token = localStorage.getItem(TOKEN_KEY);
    var name = localStorage.getItem(NAME_KEY) || "";
    if (navAuth) navAuth.hidden = !!token;
    if (navUser) navUser.hidden = !token;
    if (navUserName) navUserName.textContent = name || "Account";
  }

  function requireToken() {
    return localStorage.getItem(TOKEN_KEY) || "";
  }

  /* ---------------- requests ---------------- */
  function postJSON(url, body, token) {
    var headers = { "Content-Type": "application/json" };
    if (token) headers["Authorization"] = "Bearer " + token;
    return fetch(endpoint(url), {
      method: "POST",
      headers: headers,
      body: JSON.stringify(body),
    }).then(function (resp) { return resp.json(); });
  }

  function handleAuthResult(data) {
    if (!data || !data.ok) {
      throw new Error((data && data.error) || "request failed");
    }
    var user = { token: data.token, user: data.user };
    applyUser(user);
    return user;
  }

  function onError(formErrorEl) {
    return function (err) {
      setError(formErrorEl, (err && err.message) ? err.message : "Something went wrong. Try again.");
    };
  }

  if (signinForm) signinForm.addEventListener("submit", function (e) {
    e.preventDefault();
    setError(signinError, "");
    var fd = new FormData(signinForm);
    postJSON("/auth/login", {
      email: fd.get("email"),
      password: fd.get("password"),
    }).then(handleAuthResult).then(function () {
      closeModal();
      signinForm.reset();
    }).catch(onError(signinError));
  });

  if (signupForm) signupForm.addEventListener("submit", function (e) {
    e.preventDefault();
    setError(signupError, "");
    var fd = new FormData(signupForm);
    postJSON("/auth/signup", {
      name: fd.get("name"),
      email: fd.get("email"),
      password: fd.get("password"),
    }).then(handleAuthResult).then(function () {
      closeModal();
      signupForm.reset();
    }).catch(onError(signupError));
  });

  if (signoutBtn) signoutBtn.addEventListener("click", function () {
    postJSON("/auth/logout", {}, requireToken())
      .catch(function () { /* best-effort */ })
      .then(function () {
        localStorage.removeItem(TOKEN_KEY);
        localStorage.removeItem(NAME_KEY);
        applyUser(null);
      });
  });

  if (googleBtn) googleBtn.addEventListener("click", function () {
    window.location.href = endpoint("/auth/google/authorize");
  });

  /* ---------------- restore session + consume ?auth= token ---------------- */
  (function () {
    var m = /[?&]auth=([^&]+)/.exec(location.search);
    if (m) {
      localStorage.setItem(TOKEN_KEY, decodeURIComponent(m[1]));
      var name = /[?&]name=([^&]+)/.exec(location.search);
      if (name) localStorage.setItem(NAME_KEY, decodeURIComponent(name[1]));
      var clean = location.pathname + (location.hash || "");
      history.replaceState(null, "", clean);
    }

    var token = requireToken();
    if (token) {
      fetch(endpoint("/auth/me"), { headers: { Authorization: "Bearer " + token } })
        .then(function (resp) { return resp.json(); })
        .then(function (data) {
          if (data && data.ok) {
            if (data.user) localStorage.setItem(NAME_KEY, data.user.name);
            applyUser(null);
          } else {
            localStorage.removeItem(TOKEN_KEY);
            localStorage.removeItem(NAME_KEY);
            applyUser(null);
          }
        })
        .catch(function () { applyUser(null); });
    } else {
      applyUser(null);
    }
  })();
})();