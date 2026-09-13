/* AP-CODEX live chat console.
 *
 * Streams the agent backend's POST /chat/stream (SSE) into a slide-up panel.
 * Native EventSource can't POST, so we use fetch + ReadableStream and parse the
 * SSE blocks ourselves. Falls back to the non-streaming POST /chat endpoint
 * when streaming isn't available.
 */
(function () {
  "use strict";

  var veil = document.getElementById("chatVeil");
  var panel = document.getElementById("chatPanel");
  var thread = document.getElementById("chatThread");
  var composer = document.getElementById("chatComposer");
  var input = document.getElementById("chatInput");
  var sendBtn = document.getElementById("chatSend");
  var closeBtn = document.getElementById("chatClose");
  var statusEl = document.getElementById("chatStatus");
  var welcome = document.getElementById("chatWelcome");

  var history = [];
  var state = "idle"; // idle | streaming
  var ctrl = null;

  /* ---------------- API base ---------------- */
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
  function originHint() {
    return apiBase ? apiBase : location.origin;
  }

  /* ---------------- tiny DOM helpers ---------------- */
  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  function setStatus(label, tone) {
    statusEl.dataset.tone = tone || "idle";
    statusEl.querySelector(".chat-status-label").textContent = label;
  }

  function statusLabel(state) {
    if (state === "parsing") return "parsing tasks";
    if (state === "running") return "running tools";
    if (state === "done") return "done";
    return "thinking";
  }

  function scrollThread() {
    thread.scrollTop = thread.scrollHeight;
  }

  /* ---------------- panel open/close ---------------- */
  function openPanel() {
    veil.classList.add("open");
    veil.setAttribute("aria-hidden", "false");
    panel.classList.add("open");
    document.body.style.overflow = "hidden";
  }

  function closePanel() {
    veil.classList.remove("open");
    panel.classList.remove("open");
    veil.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
    if (ctrl) {
      ctrl.abort();
      ctrl = null;
    }
    if (state !== "idle") {
      state = "idle";
      setStatus("idle", "idle");
    }
  }

  /* ---------------- message bubbles ---------------- */
  function userBubble(text) {
    if (welcome) welcome.style.display = "none";
    var bubble = el("div", "chat-msg user");
    bubble.appendChild(el("div", "chat-bubble", escapeHtml(text)));
    thread.appendChild(bubble);
    scrollThread();
  }

  function agentBubble() {
    var bubble = el("div", "chat-msg agent");
    var avatar = el("div", "agent-avatar");
    avatar.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
      '<path d="M4 4h16v6h-6v10H4V4Z" fill="currentColor" />' +
      '<path d="M14 14l4 4m0-4-4 4" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />' +
      "</svg>";
    var body = el("div", "chat-agent-body");
    body.appendChild(el("div", "agent-head", "AP-CODEX"));
    var tools = el("div", "chat-tools");
    body.appendChild(tools);
    var text = el("div", "chat-text");
    body.appendChild(text);
    var cursor = el("span", "chat-cursor");
    text.appendChild(cursor);

    bubble.appendChild(avatar);
    bubble.appendChild(body);
    bubble._tools = tools;
    bubble._text = text;
    bubble._cursor = cursor;
    thread.appendChild(bubble);
    scrollThread();
    return bubble;
  }

  function appendText(bubble, text) {
    if (!text) return;
    var last = bubble._text.lastChild;
    if (last && last.nodeType === 3) {
      last.nodeValue += text;
    } else {
      bubble._text.insertBefore(document.createTextNode(text), bubble._cursor);
    }
    scrollThread();
  }

  function finishCursor(bubble) {
    if (bubble._cursor && bubble._cursor.parentNode) {
      bubble._cursor.parentNode.removeChild(bubble._cursor);
    }
    bubble._cursor = null;
  }

  function errorRow(message) {
    var row = el("div", "chat-error",
      escapeHtml(message || "Something went wrong."));
    thread.appendChild(row);
    scrollThread();
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  /* ---------------- tool chips ---------------- */
  function renderTool(data, bubble) {
    var id = data.tool_call_id || data.name || "";
    var status = data.status;
    var label = (data.connector && data.action) ? data.connector + "." + data.action : data.name;

    if (status === "started") {
      var chip = el("div", "tool-chip running");
      chip.appendChild(el("span", "tool-spin"));
      chip.appendChild(document.createTextNode(" " + escapeHtml(label)));
      bubble._tools.appendChild(chip);
      bubble._tools.dataset[data.tool_call_id] = label;
      bubble._toolChips = bubble._toolChips || {};
      bubble._toolChips[id] = chip;
      if (bubble._tools.parentNode) bubble._tools.style.display = "";
      scrollThread();
      return;
    }

    var existing = bubble._toolChips && bubble._toolChips[id];
    if (!existing) {
      existing = el("div", "tool-chip");
      bubble._tools.appendChild(existing);
      bubble._toolChips = bubble._toolChips || {};
      bubble._toolChips[id] = existing;
    }
    existing.className = "tool-chip " + (status === "ok" ? "ok" : "error");

    var summary = (data.result && (data.result.summary || data.result.error)) || null;
    var detail = "";
    if (summary) {
      detail = " — " + String(summary).slice(0, 90);
      if (String(summary).length > 90) detail += "…";
    } else if (status === "error" && data.result && data.result.error) {
      detail = " — " + String(data.result.error).slice(0, 90);
    }
    existing.innerHTML =
      "<span class=\"tool-mark\">" + (status === "ok" ? "\u2713" : "\u2715") + "</span> " +
      escapeHtml(label) + escapeHtml(detail);
    scrollThread();
  }

  /* ---------------- SSE parsing ---------------- */
  function readStream(res, bubble) {
    var reader = res.body.getReader();
    var decoder = new TextDecoder("utf-8");
    var buffer = "";
    var reply = "";
    var finished = false;

    function handleBlock(block) {
      var event = "message";
      var dataLines = [];
      block.split("\n").forEach(function (line) {
        var value = line.slice(line.indexOf(":") + 1).trim();
        if (line.indexOf("event:") === 0) event = value;
        else if (line.indexOf("data:") === 0) dataLines.push(value);
      });
      if (!dataLines.length) return;
      var data;
      try {
        data = JSON.parse(dataLines.join("\n"));
      } catch (e) {
        return;
      }

      if (event === "delta") {
        reply += data.text || "";
        appendText(bubble, data.text || "");
      } else if (event === "tool") {
        renderTool(data, bubble);
      } else if (event === "status") {
        setStatus(statusLabel(data.state), data.state);
      } else if (event === "done") {
        finished = true;
        finishCursor(bubble);
        setStatus("done", "done");
        reply = data.reply || reply;
      } else if (event === "error") {
        finished = true;
        finishCursor(bubble);
        setStatus("error", "error");
        errorRow(data.message || "The agent loop hit an error.");
      }
    }

    function pump() {
      return reader.read().then(function (result) {
        if (result.done) return finished;
        buffer += decoder.decode(result.value, { stream: true });
        var idx;
        while ((idx = buffer.indexOf("\n\n")) !== -1) {
          handleBlock(buffer.slice(0, idx));
          buffer = buffer.slice(idx + 2);
        }
        return pump();
      });
    }

    return pump().catch(function () {
      return finished;
    }).then(function () {
      return reply;
    });
  }

  /* ---------------- execution ---------------- */
  function streamChat(bubble) {
    ctrl = new AbortController();
    setStatus("thinking", "thinking");
    history.push({ role: "assistant", content: "" });

    return fetch(endpoint("/chat/stream"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
      },
      body: JSON.stringify({ messages: history }),
      credentials: "same-origin",
      signal: ctrl.signal,
    })
      .then(function (res) {
        if (!res.ok || !res.body) {
          throw new Error("HTTP " + res.status);
        }
        return readStream(res, bubble);
      })
      .then(function (reply) {
        history[history.length - 1].content = reply;
        finishStream();
        return reply;
      })
      .catch(function (err) {
        if (err && err.name === "AbortError") return "";
        return fallbackChat(bubble, err);
      });
  }

  function fallbackChat(bubble, err) {
    finishCursor(bubble);
    return fetch(endpoint("/chat"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: history }),
      credentials: "same-origin",
    })
      .then(function (res) { return res.json(); })
      .then(function (body) {
        if (body && body.reply) {
          appendText(bubble, body.reply);
          history[history.length - 1].content = body.reply;
        } else {
          errorRow((body && body.error) || "No response from the engine.");
        }
        (body && body.tool_calls || []).forEach(function (t) {
          renderTool({
            tool_call_id: t.name,
            name: t.name,
            connector: t.connector,
            action: t.action,
            status: t.ok ? "ok" : "error",
            result: t,
          }, bubble);
        });
        setStatus("done", "done");
        finishStream();
      })
      .catch(function () {
        errorRow(
          "Engine not reachable. Start the backend and open this page from it:\n" +
          "  python -m uvicorn api.app:app --app-dir src --port 8123\n" +
          "  then visit http://127.0.0.1:8123/\n" +
          "(or open this page with ?api=http://127.0.0.1:8123 to point at a remote engine)"
        );
        setStatus("offline", "error");
        finishStream();
      });
  }

  function finishStream() {
    ctrl = null;
    state = "idle";
    sendBtn.disabled = false;
  }

  function send(message) {
    message = String(message || "").trim();
    if (!message || state !== "idle") return;

    state = "streaming";
    sendBtn.disabled = true;
    history.push({ role: "user", content: message });
    openPanel();
    userBubble(message);
    var bubble = agentBubble();
    streamChat(bubble);
  }

  /* ---------------- wiring ---------------- */
  composer.addEventListener("submit", function (e) {
    e.preventDefault();
    var message = input.value.trim();
    if (!message) return;
    input.value = "";
    send(message);
  });

  closeBtn.addEventListener("click", closePanel);
  veil.addEventListener("click", function (e) {
    if (e.target === veil) closePanel();
  });

  if (welcome) {
    welcome.addEventListener("click", function (e) {
      var chip = e.target;
      if (chip && chip.classList && chip.classList.contains("chat-suggest")) {
        if (state !== "idle") return;
        input.value = "";
        send(chip.textContent);
      }
    });
  }

  document.querySelectorAll(".hero-chips a.chip").forEach(function (chip) {
    chip.addEventListener("click", function (e) {
      e.preventDefault();
      send(chip.textContent);
    });
  });

  window.APCODEXChat = { send: send };
})();