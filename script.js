(function () {
  "use strict";

  var reducingMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---------- Rotating typing effect ---------- */
  var input = document.getElementById("promptInput");
  var typing = document.getElementById("promptTyping");
  var phrases = [
    "Create slides for tomorrow's pitch",
    "Build a website for my startup",
    "Design a logo for my brand",
    "Create a 2D game in the browser",
    "Turn this data into a report",
    "Automate my weekly email digest"
  ];

  var phraseIndex = 0;
  var charIndex = 0;
  var deleting = false;

  function typeLoop() {
    if (!typing) return;
    var phrase = phrases[phraseIndex];

    if (!deleting) {
      charIndex++;
      typing.textContent = phrase.slice(0, charIndex);
      if (charIndex === phrase.length) {
        deleting = true;
        setTimeout(typeLoop, 2200);
        return;
      }
      setTimeout(typeLoop, 55);
    } else {
      charIndex--;
      typing.textContent = phrase.slice(0, charIndex);
      if (charIndex === 0) {
        deleting = false;
        phraseIndex = (phraseIndex + 1) % phrases.length;
      }
      setTimeout(typeLoop, 28);
    }
  }

  input.addEventListener("input", function () {
    typing.style.opacity = input.value.length > 0 ? 0 : 1;
  });

  typeLoop();

  /* ---------- Prompt submit ---------- */
  var form = document.getElementById("promptForm");
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var message = input.value.trim();
    if (!message) return;

    if (window.APCODEXChat && window.APCODEXChat.send) {
      window.APCODEXChat.send(message);
      input.value = "";
      typing.style.opacity = 1;
      return;
    }

    input.value = "";
    typing.style.opacity = 0;
    input.placeholder = "Working on \u201C" + message + "\u201D...";
    setTimeout(function () {
      input.placeholder = "Ask AP-CODEX to do anything...";
    }, 1800);
  });

  /* ---------- Scroll reveal ---------- */
  var revealEls = document.querySelectorAll(".reveal");
  var revealObserver;

  function observeReveals() {
    if (reducingMotion) {
      revealEls.forEach(function (el) { el.classList.add("visible"); });
      return;
    }
    revealObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add("visible");
          revealObserver.unobserve(entry.target);
        }
      });
    }, { threshold: 0.15, rootMargin: "0px 0px -40px 0px" });
    revealEls.forEach(function (el) { revealObserver.observe(el); });
  }

  /* Stagger cards */
  document.querySelectorAll(".features-grid, .hero-chips, .stats").forEach(function (group) {
    Array.prototype.forEach.call(group.children, function (child, i) {
      child.style.setProperty("--reveal-delay", (i * 0.08).toFixed(2) + "s");
    });
  });

  /* ---------- Navbar scrolled state + active link ---------- */
  var navWrap = document.querySelector(".nav-wrap");
  var sections = ["features", "solutions", "pricing"];
  var navLinks = document.querySelectorAll(".nav-link");

  function updateNav() {
    navWrap.classList.toggle("scrolled", window.scrollY > 8);
    var pos = window.scrollY + 120;
    var current = "";
    sections.forEach(function (id) {
      var sec = document.getElementById(id);
      if (sec && sec.offsetTop <= pos) current = id;
    });
    navLinks.forEach(function (link) {
      link.classList.toggle("active", link.dataset.target === current);
    });
  }

  window.addEventListener("scroll", updateNav, { passive: true });
  updateNav();

  /* ---------- Ripple on buttons ---------- */
  document.querySelectorAll(".btn, .prompt-submit, .chip").forEach(function (btn) {
    btn.addEventListener("click", function (e) {
      if (reducingMotion) return;
      var rect = btn.getBoundingClientRect();
      var r = document.createElement("span");
      var size = Math.max(rect.width, rect.height);
      r.className = "ripple";
      r.style.width = r.style.height = size + "px";
      r.style.left = e.clientX - rect.left - size / 2 + "px";
      r.style.top = e.clientY - rect.top - size / 2 + "px";
      btn.appendChild(r);
      setTimeout(function () { r.remove(); }, 650);
    });
  });

  /* ---------- Animated counters ---------- */
  var counters = document.querySelectorAll("[data-count]");
  var counterObserver = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      var el = entry.target;
      counterObserver.unobserve(el);
      var target = parseFloat(el.dataset.count);
      var suffix = el.dataset.suffix || "";
      var duration = 1500;
      var start = null;

      function tick(ts) {
        if (!start) start = ts;
        var progress = Math.min((ts - start) / duration, 1);
        var eased = 1 - Math.pow(1 - progress, 3);
        var value = (target * eased).toFixed(1);
        el.textContent = value.replace(/\.0$/, "") + suffix;
        if (progress < 1) requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
    });
  }, { threshold: 0.6 });
  counters.forEach(function (el) { counterObserver.observe(el); });

  /* ---------- 3D tilt on demo frame ---------- */
  var tiltFrame = document.getElementById("tiltFrame");
  if (tiltFrame && !reducingMotion && window.matchMedia("(pointer: fine)").matches) {
    var raf = null;
    tiltFrame.addEventListener("mousemove", function (e) {
      if (raf) return;
      raf = requestAnimationFrame(function () {
        raf = null;
        var rect = tiltFrame.getBoundingClientRect();
        var px = (e.clientX - rect.left) / rect.width - 0.5;
        var py = (e.clientY - rect.top) / rect.height - 0.5;
        tiltFrame.style.transform = "perspective(1200px) rotateX(" + (-py * 4) + "deg) rotateY(" + (px * 4) + "deg)";
      });
    });
    tiltFrame.addEventListener("mouseleave", function () {
      tiltFrame.style.transform = "perspective(1200px) rotateX(0) rotateY(0)";
    });
  }

  /* ---------- Parallax hero orbs ---------- */
  if (!reducingMotion && window.matchMedia("(pointer: fine)").matches) {
    window.addEventListener("mousemove", function (e) {
      var x = e.clientX / window.innerWidth - 0.5;
      var y = e.clientY / window.innerHeight - 0.5;
      document.querySelectorAll(".orb").forEach(function (orb, i) {
        var mult = (i + 1) * 12;
        orb.style.translate = (x * mult * -1) + "px " + (y * mult * -1) + "px";
      });
    });
  }

  /* ---------- Mobile nav ---------- */
  var toggle = document.querySelector(".nav-toggle");
  var links = document.querySelector(".nav-links");
  if (toggle && links) {
    toggle.addEventListener("click", function () {
      var open = links.classList.toggle("open");
      toggle.classList.toggle("open", open);
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    links.querySelectorAll("a").forEach(function (link) {
      link.addEventListener("click", function () {
        links.classList.remove("open");
        toggle.classList.remove("open");
      });
    });
  }

  observeReveals();
})();