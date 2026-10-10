/* Navigation Cohors — menu structuré (liens directs + sous-menus déroulants).
   Desktop : pilules avec sous-menus. Mobile (<=760px) : bouton ☰ + panneau latéral complet.
   Rendue automatiquement dans <nav class="tabs" id="navmain"></nav>. */
(function () {
  var MENU = [
    { label: "🙋 Moi", items: [
      { href: "/mespersos", label: "🪪 Mes personnages" },
      { href: "/mesrecettes", label: "📖 Mes recettes" },
      { href: "/messtats", label: "📊 Mes statistiques" },
      { href: "/alertes", label: "🔔 Alertes MM+" },
      { href: "/mesindispos", label: "🚫 Mes indispos" },
      { href: "/wishlist", label: "🎯 Ma wishlist" }
    ] },
    { label: "⚔️ Simulation", items: [
      { href: "/", label: "⚔️ Simulateur" },
      { href: "/gear", label: "🧰 Top Stuff" },
      { href: "/stuff", label: "🎒 Stuff conseillé" },
      { href: "/compare", label: "⚖️ Comparateur" }
    ] },
    { label: "👥 Guilde", items: [
      { href: "/characters", label: "👥 Personnages" },
      { href: "/mains", label: "⭐ Mains & alts" },
      { href: "/rankings", label: "🏆 Classements" },
      { href: "/fun", label: "🎉 Succès fun" },
      { href: "/craft", label: "🔨 Artisanat" },
      { href: "/guild", label: "🛡️ Infos & liens" }
    ] },
    { label: "⚔️ Raid/MM+", items: [
      { href: "/raids", label: "📊 Rapports" },
      { href: "/calendar", label: "🗓️ Calendrier" },
      { href: "/prep", label: "🧪 Préparation de raid" },
      { href: "/mplus", label: "⚔️ MM+" }
    ] },
    { label: "🎧 Vocal", items: [
      { href: "/voice", label: "🎧 Panneau vocal", vt: true },
      { href: "/music", label: "🎵 Musique", role: "officer" }
    ] },
    { href: "/help", label: "📖 Aide" }
  ];
  var nav = document.getElementById("navmain");
  if (!nav) return;
  var path = location.pathname.replace(/\/+$/, "") || "/";
  var isHere = function (i) {
    return i.href === path || (i.href === "/mains" && path.indexOf("/char/") === 0);
  };
  var inItems = function (items) { return items.some(isHere); };
  var MQ = window.matchMedia("(max-width: 760px)");
  var ROLE_OK = false;      // true si officier/admin (fixé par /api/me)
  var ROLE_KNOWN = false;

  var itemHTML = function (i, cls) {
    return '<a class="' + cls + (isHere(i) ? " active" : "") + '" href="' + i.href + '"' +
      (i.role ? ' data-role="' + i.role + '"' : "") + (i.vt ? ' data-vt="1"' : "") + '>' + i.label + '</a>';
  };

  /* ---------- Desktop : pilules ---------- */
  function buildPills() {
    var html = "";
    MENU.forEach(function (m) {
      if (m.items) {
        html += '<div class="tab menu' + (inItems(m.items) ? " active" : "") + '">' +
          '<button class="menubtn" type="button">' + m.label + '<span class="arr">▾</span></button>' +
          '<div class="dropdown">' + m.items.map(function (i) { return itemHTML(i, "ditem"); }).join("") + '</div></div>';
      } else {
        html += '<a class="tab' + (m.href === path ? " active" : "") + '" href="' + m.href + '">' + m.label + '</a>';
      }
    });
    html += '<button class="tab gsopen" type="button" title="Rechercher (Ctrl+K)" aria-label="Rechercher">🔎</button>';
    return html;
  }

  /* ---------- Mobile : bouton ☰ + panneau latéral ---------- */
  function buildMobile() {
    var rows = "";
    MENU.forEach(function (m) {
      if (m.items) {
        rows += '<div class="dsec' + (inItems(m.items) ? " here" : "") + '"><div class="dsec-t">' + m.label + '</div>' +
          m.items.map(function (i) { return itemHTML(i, "drow sub"); }).join("") + '</div>';
      } else {
        rows += itemHTML(m, "drow");
      }
    });
    return '' +
      '<button id="navburger" type="button" aria-label="Menu" aria-expanded="false">☰&nbsp;Menu</button>' +
      '<button class="gsopen gsopen-m" type="button" aria-label="Rechercher">🔎&nbsp;Rechercher</button>' +
      '<div id="navscrim"></div>' +
      '<aside id="navdrawer" aria-hidden="true">' +
        '<div class="dhead"><a class="dlogo" href="/dashboard" title="Tableau de bord" style="text-decoration:none;color:inherit;display:flex;align-items:center;gap:10px"><img src="/branding/logo" alt=""><span>Cohors</span></a>' +
          '<button id="navclose" type="button" aria-label="Fermer">✕</button></div>' +
        '<nav class="dlist">' + rows + '</nav>' +
        '<div class="dfoot">' +
          '<a class="drow" href="/settings">⚙️ Paramètres</a>' +
          '<a class="drow" href="#" id="navlogout">🚪 Déconnexion</a>' +
        '</div>' +
      '</aside>';
  }

  function closeDrawer() {
    document.body.classList.remove("navopen");
    var b = document.getElementById("navburger");
    if (b) b.setAttribute("aria-expanded", "false");
    var d = document.getElementById("navdrawer");
    if (d) d.setAttribute("aria-hidden", "true");
  }

  function openDrawer() {
    document.body.classList.add("navopen");
    var b = document.getElementById("navburger");
    if (b) b.setAttribute("aria-expanded", "true");
    var d = document.getElementById("navdrawer");
    if (d) d.setAttribute("aria-hidden", "false");
  }

  function applyRoles() {
    if (!ROLE_KNOWN) return;
    if (!ROLE_OK) {
      nav.querySelectorAll('[data-role="officer"]').forEach(function (x) { x.remove(); });
    }
    var al = document.getElementById("admin-link");
    if (al && ROLE_OK) al.classList.remove("hidden");
  }

  function attach() {
    // Vocal : dans le panneau vocal (page encadrée) → bascule le panneau ; sinon → page /voice.
    nav.querySelectorAll('a[href="/voice"]').forEach(function (voicelink) {
      voicelink.addEventListener("click", function (ev) {
        try {
          if (window.self !== window.top) {
            ev.preventDefault();
            window.top.postMessage({ type: "cohors-voice-toggle" }, "*");
          }
        } catch (e) {}
      });
    });
    // Sous-menus desktop
    nav.querySelectorAll(".tab.menu > .menubtn").forEach(function (btn) {
      btn.addEventListener("click", function (ev) {
        ev.stopPropagation();
        var m = btn.parentElement;
        var was = m.classList.contains("open");
        nav.querySelectorAll(".tab.menu.open").forEach(function (x) { x.classList.remove("open"); });
        if (!was) m.classList.add("open");
      });
    });
    // Tiroir mobile
    var burger = document.getElementById("navburger");
    if (burger) burger.addEventListener("click", function (ev) { ev.stopPropagation(); openDrawer(); });
    var scrim = document.getElementById("navscrim");
    if (scrim) scrim.addEventListener("click", closeDrawer);
    var close = document.getElementById("navclose");
    if (close) close.addEventListener("click", closeDrawer);
    var logout = document.getElementById("navlogout");
    if (logout) logout.addEventListener("click", function (ev) {
      ev.preventDefault();
      fetch("/api/logout", { method: "POST" }).catch(function () {}).then(function () { location.href = "/login"; });
    });
    var drawer = document.getElementById("navdrawer");
    if (drawer) drawer.querySelectorAll("a.drow").forEach(function (a) {
      a.addEventListener("click", function () { setTimeout(closeDrawer, 60); });
    });
    nav.querySelectorAll(".gsopen").forEach(function (b) {
      b.addEventListener("click", function (ev) { ev.stopPropagation(); openSearch(); });
    });
    applyRoles();
  }

  function render() {
    nav.innerHTML = MQ.matches ? buildMobile() : buildPills();
    closeDrawer();
    attach();
  }

  render();
  if (MQ.addEventListener) MQ.addEventListener("change", render);
  else if (MQ.addListener) MQ.addListener(render);
  document.addEventListener("keydown", function (ev) { if (ev.key === "Escape") closeDrawer(); });
  document.addEventListener("click", function () {
    nav.querySelectorAll(".tab.menu.open").forEach(function (x) { x.classList.remove("open"); });
  });

  /* ---------- Styles (desktop + mobile) ---------- */
  var css = document.createElement("style");
  css.textContent = [
    "#navmain { overflow:visible; }",
    "#navmain .tab.menu { position:relative; padding:0; display:inline-flex; }",
    "#navmain .tab.menu > .menubtn { display:inline-flex; align-items:center; gap:6px; padding:8px 16px;",
    "  border-radius:999px; color:inherit; font:inherit; font-weight:600; font-size:14px; line-height:1.55;",
    "  background:transparent; border:0; cursor:pointer; }",
    "#navmain .tab.menu:hover > .menubtn, #navmain .tab.menu.open > .menubtn { color:var(--fg,#e8edf7); }",
    "#navmain .tab.menu.active { border-color:var(--acc2,#dfa55a); }",
    "#navmain .tab.menu.active > .menubtn { color:var(--acc2,#dfa55a); }",
    "#navmain .tab.menu .arr { font-size:10px; opacity:.75; }",
    "#navmain .dropdown { display:none; position:absolute; top:calc(100% + 8px); left:0; min-width:235px;",
    "  background:var(--card2,#182130); border:1px solid var(--line,#2a3446); border-radius:12px; padding:8px;",
    "  z-index:80; box-shadow:0 12px 34px rgba(0,0,0,.5); }",
    "#navmain .tab.menu.open .dropdown { display:block; }",
    "#navmain .ditem { display:block; padding:9px 12px; border-radius:8px; color:var(--fg,#e8edf7);",
    "  text-decoration:none; font-size:14px; font-weight:500; white-space:nowrap; }",
    "#navmain .ditem:hover { background:rgba(255,255,255,.07); }",
    "#navmain .ditem.active { color:var(--acc2,#dfa55a); font-weight:700; }",
    "#topwrap { position:sticky; top:0; z-index:90; background:var(--bg,#0b0f17); padding-top:8px; margin-bottom:4px; }",
    "#topwrap.stuck { box-shadow:0 12px 26px rgba(0,0,0,.45); }",
    /* --- mobile --- */
    "#navmain .tab.gsopen { cursor:pointer; font:inherit; font-size:14px; padding:8px 14px; }",
    "#navmain .gsopen-m { display:none; }",
    "#gsearch { position:fixed; inset:0; z-index:400; background:rgba(4,3,2,.7); display:flex; justify-content:center;",
    "  align-items:flex-start; padding:9vh 16px 16px; }",
    "#gsearch[hidden] { display:none; }",
    "#gsearch .gsbox { width:min(640px, 100%); max-height:78vh; display:flex; flex-direction:column;",
    "  background:var(--card,#131a26); border:1px solid var(--line,#2a3446); border-radius:14px;",
    "  box-shadow:0 18px 50px rgba(0,0,0,.6); overflow:hidden; }",
    "#gsearch input { width:100%; box-sizing:border-box; border:0; border-bottom:1px solid var(--line,#2a3446);",
    "  background:transparent; color:var(--fg,#e8edf7); font:inherit; font-size:16px; padding:15px 18px; outline:none; }",
    "#gsearch .gsres { overflow-y:auto; padding:6px 8px 10px; }",
    "#gsearch .gsh { color:var(--mut,#8c96ad); font-size:11.5px; letter-spacing:1px; text-transform:uppercase; padding:10px 10px 4px; }",
    "#gsearch a.gsi { display:flex; align-items:baseline; gap:10px; padding:8px 10px; border-radius:8px;",
    "  color:var(--fg,#e8edf7); text-decoration:none; font-size:14.5px; }",
    "#gsearch a.gsi .gsn { font-weight:600; }",
    "#gsearch a.gsi .gsm { margin-left:auto; color:var(--mut,#8c96ad); font-size:12.5px; text-align:right; }",
    "#gsearch a.gsi.sel, #gsearch a.gsi:hover { background:rgba(255,255,255,.08); }",
    "#gsearch .gsempty, #gsearch .gsfoot { color:var(--mut,#8c96ad); font-size:13px; padding:10px; }",
    "#gsearch .gsfoot { border-top:1px solid var(--line,#2a3446); padding:8px 14px; font-size:12px; }",
    "#navburger, #navscrim, #navdrawer { display:none; }",
    "@media (max-width: 760px) {",
    "  #navmain { display:block; padding:0 !important; margin:0 0 8px !important; }",
    "  #navburger { display:inline-flex; align-items:center; gap:6px; padding:9px 18px; border-radius:999px;",
    "    border:1px solid var(--line,#2a3446); background:transparent; color:var(--fg,#e8edf7);",
    "    font-weight:600; font-size:14px; cursor:pointer; }",
    "  #navburger:active { border-color:var(--acc2,#dfa55a); }",
    "  #navmain .gsopen-m { display:inline-flex; align-items:center; margin-left:8px; padding:9px 16px; border-radius:999px;",
    "    border:1px solid var(--line,#2a3446); background:transparent; color:var(--fg,#e8edf7); font-weight:600; font-size:14px; cursor:pointer; }",
    "  #gsearch { padding-top:12px; }",
    "  #gsearch .gsbox { max-height:88vh; }",
    "  #navscrim { display:block; position:fixed; inset:0; background:rgba(4,3,2,.72); z-index:250;",
    "    opacity:0; pointer-events:none; transition:opacity .2s; }",
    "  #navdrawer { display:block; position:fixed; top:0; bottom:0; left:0; width:min(85vw, 330px); z-index:260;",
    "    background:#10151f; border-right:1px solid var(--line,#2a3446); transform:translateX(-102%);",
    "    transition:transform .22s ease; overflow-y:auto; padding:14px 12px calc(22px + env(safe-area-inset-bottom)); box-shadow:18px 0 40px rgba(0,0,0,.55);",
    "    -webkit-overflow-scrolling:touch; }",
    "  body.navopen { overflow:hidden; }",
    "  body.navopen #navscrim { opacity:1; pointer-events:auto; }",
    "  body.navopen #navdrawer { transform:translateX(0); }",
    "  #navdrawer .dhead { display:flex; align-items:center; gap:10px; padding:2px 6px 12px; border-bottom:1px solid var(--line,#2a3446); margin-bottom:6px; }",
    "  #navdrawer .dhead img { width:30px; height:30px; border-radius:8px; }",
    "  #navdrawer .dhead span { font-weight:700; font-size:17px; letter-spacing:.4px; }",
    "  #navdrawer .dhead button { margin-left:auto; background:transparent; border:0; color:var(--mut,#8c96ad);",
    "    font-size:18px; padding:6px 8px; cursor:pointer; }",
    "  #navdrawer .dsec-t { color:#ecc37e; font-size:12px; letter-spacing:1px; text-transform:uppercase;",
    "    padding:16px 12px 6px; }",
    "  #navdrawer .drow { display:flex; align-items:center; gap:10px; padding:12px 12px; border-radius:10px;",
    "    color:var(--fg,#e8edf7); text-decoration:none; font-size:15px; font-weight:500; }",
    "  #navdrawer .drow.sub { font-size:14.5px; padding-left:18px; font-weight:400; }",
    "  #navdrawer .drow:hover, #navdrawer .drow:active { background:rgba(255,255,255,.06); }",
    "  #navdrawer .drow.active { background:linear-gradient(135deg, var(--acc,#b1002e), #7d0020); color:#fff; font-weight:600; }",
    "  #navdrawer .dfoot { border-top:1px solid var(--line,#2a3446); margin-top:10px; padding-top:8px; }",
    "}"
  ].join("\n");
  document.head.appendChild(css);

  // En-tête + menu collants (« fixes ») et identiques sur toutes les pages.
  var top = document.querySelector(".topbar");
  if (top && top.parentElement && !document.getElementById("topwrap")) {
    var wrap = document.createElement("div");
    wrap.id = "topwrap";
    top.parentElement.insertBefore(wrap, top);
    wrap.appendChild(top);
    wrap.appendChild(nav);
    var onScroll = function () { wrap.classList.toggle("stuck", window.scrollY > 8); };
    window.addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  // Rôles : entrées officier/admin selon /api/me (réappliqué à chaque rendu).
  fetch("/api/me").then(function (r) { return r.ok ? r.json() : null; }).then(function (me) {
    ROLE_KNOWN = true;
    ROLE_OK = !!(me && (me.role === "admin" || me.role === "officer" || me.is_admin));
    applyRoles();
  }).catch(function () { ROLE_KNOWN = true; });

  // PWA — service worker (icône mobile, hors-ligne léger).
  if ("serviceWorker" in navigator &&
      (location.protocol === "https:" || location.hostname === "127.0.0.1" || location.hostname === "localhost")) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function () {});
    });
  }

  /* ---------- Recherche globale : bouton 🔎, Ctrl+K ou « / » ---------- */
  var GS = null, GS_SEQ = 0, GS_T = null, GS_SEL = -1;
  var CLS = { DeathKnight: "#C41E3A", DemonHunter: "#A330C9", Druid: "#FF7C0A", Evoker: "#33937F",
    Hunter: "#AAD372", Mage: "#3FC7EB", Monk: "#00FF98", Paladin: "#F48CBA", Priest: "#FFFFFF",
    Rogue: "#FFF468", Shaman: "#0070DD", Warlock: "#8788EE", Warrior: "#C69B6D" };
  var gEsc = function (v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  };
  var gNorm = function (v) {
    return String(v || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase()
      .replace(/’/g, "'").replace(/\s+/g, " ").trim();
  };
  var PAGES = [];
  MENU.forEach(function (m) { (m.items || [m]).forEach(function (i) { PAGES.push(i); }); });
  PAGES.push({ href: "/settings", label: "⚙️ Paramètres" });

  function gsPages(q) {
    return PAGES.filter(function (p) {
      if (p.role && !ROLE_OK) return false;
      var lab = window.__cohorsTr ? window.__cohorsTr(p.label) : p.label;
      return gNorm(p.label).indexOf(q) >= 0 || gNorm(lab).indexOf(q) >= 0;
    }).slice(0, 5);
  }

  function gsRow(href, name, meta, color) {
    return '<a class="gsi" href="' + href + '"><span class="gsn"' + (color ? ' style="color:' + color + '"' : "") + '>' +
      gEsc(name) + '</span><span class="gsm">' + meta + '</span></a>';
  }

  function gsRender(q, d) {
    var box = GS.querySelector(".gsres"), html = "";
    var pages = gsPages(gNorm(q));
    if (pages.length) {
      html += '<div class="gsh">Pages</div>' + pages.map(function (p) { return gsRow(p.href, p.label, "", ""); }).join("");
    }
    if (d && d.chars && d.chars.length) {
      html += '<div class="gsh">Personnages</div>' + d.chars.map(function (c) {
        return gsRow("/char/" + encodeURIComponent(c.realm) + "/" + encodeURIComponent(c.key), c.name,
          gEsc(c.spec || "") + (c.ilvl ? " · " + gEsc(c.ilvl) : ""), CLS[c.class_key] || "");
      }).join("");
    }
    if (d && d.items && d.items.length) {
      html += '<div class="gsh">Objets</div>' + d.items.map(function (it) {
        var meta = it.source ? gEsc(it.source.boss) + " · " + gEsc(it.source.instance) : gEsc(it.profession || "");
        return gsRow("/item/" + it.id, it.name, meta, "");
      }).join("");
    }
    if (d && d.recipes && d.recipes.length) {
      html += '<div class="gsh">Recettes</div>' + d.recipes.map(function (r) {
        return gsRow("/craft?q=" + encodeURIComponent(r.name), r.name, gEsc(r.profession || ""), "");
      }).join("");
    }
    if (!html) html = '<div class="gsempty">' + (d ? "Aucun résultat." : "Recherche…") + '</div>';
    box.innerHTML = html;
    GS_SEL = -1;
  }

  function gsQuery() {
    var q = GS.querySelector("input").value.trim(), seq = ++GS_SEQ;
    if (gNorm(q).length < 2) {
      GS.querySelector(".gsres").innerHTML = '<div class="gsempty">Tape au moins 2 lettres : personnage, objet, recette ou page.</div>';
      return;
    }
    gsRender(q, null);
    fetch("/api/search?q=" + encodeURIComponent(q)).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { if (seq === GS_SEQ) gsRender(q, d || { chars: [], items: [], recipes: [] }); })
      .catch(function () { if (seq === GS_SEQ) gsRender(q, { chars: [], items: [], recipes: [] }); });
  }

  function gsMove(delta) {
    var rows = GS.querySelectorAll("a.gsi");
    if (!rows.length) return;
    GS_SEL = (GS_SEL + delta + rows.length) % rows.length;
    rows.forEach(function (r, i) { r.classList.toggle("sel", i === GS_SEL); });
    rows[GS_SEL].scrollIntoView({ block: "nearest" });
  }

  function closeSearch() { if (GS) GS.hidden = true; }

  function openSearch() {
    closeDrawer();
    if (!GS) {
      GS = document.createElement("div");
      GS.id = "gsearch";
      GS.innerHTML = '<div class="gsbox" role="dialog" aria-label="Recherche">' +
        '<input type="search" autocomplete="off" placeholder="Rechercher un personnage, un objet, une recette, une page…">' +
        '<div class="gsres"></div>' +
        '<div class="gsfoot">↑ ↓ pour choisir · Entrée pour ouvrir · Échap pour fermer</div></div>';
      document.body.appendChild(GS);
      GS.addEventListener("click", function (ev) { if (ev.target === GS) closeSearch(); });
      var inp = GS.querySelector("input");
      inp.addEventListener("input", function () { clearTimeout(GS_T); GS_T = setTimeout(gsQuery, 200); });
      inp.addEventListener("keydown", function (ev) {
        if (ev.key === "ArrowDown") { ev.preventDefault(); gsMove(1); }
        else if (ev.key === "ArrowUp") { ev.preventDefault(); gsMove(-1); }
        else if (ev.key === "Enter") {
          var rows = GS.querySelectorAll("a.gsi");
          var r = rows[GS_SEL >= 0 ? GS_SEL : 0];
          if (r) { ev.preventDefault(); location.href = r.getAttribute("href"); }
        }
      });
    }
    GS.hidden = false;
    var i = GS.querySelector("input");
    i.focus();
    i.select();
    gsQuery();
  }

  document.addEventListener("keydown", function (ev) {
    var t = ev.target, typing = t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
    if ((ev.ctrlKey || ev.metaKey) && (ev.key === "k" || ev.key === "K")) { ev.preventDefault(); openSearch(); }
    else if (ev.key === "/" && !typing && !(GS && !GS.hidden)) { ev.preventDefault(); openSearch(); }
    else if (ev.key === "Escape") closeSearch();
  });

  /* Badge « notifications non lues » sur l'entrée 🙋 Moi. */
  var UNREAD = 0;
  function decorateNotifs(n) {
    nav.querySelectorAll("span[data-nbadge]").forEach(function (x) { x.remove(); });
    if (!n || n <= 0) return;
    var html = '<span data-nbadge="1" style="background:var(--acc,#b1002e);color:#fff;border-radius:999px;padding:0 7px;' +
      'margin-left:6px;font-size:11.5px;font-weight:700">' + n + '</span>';
    nav.querySelectorAll(".menubtn, .dsec-t, .ditem, .drow").forEach(function (b) {
      var t = b.textContent || "";
      if (t.indexOf("🙋") !== 0 && t.indexOf("🔔") !== 0) return;
      b.insertAdjacentHTML("beforeend", html);
      b.setAttribute("title", n + " notification(s) non lue(s)");
    });
  }
  window.__cohorsSetUnread = function (n) { UNREAD = n || 0; decorateNotifs(UNREAD); };
  fetch("/api/me/notifs").then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
    if (j && j.unread > 0) { UNREAD = j.unread; decorateNotifs(UNREAD); }
  }).catch(function () {});
})();
