/* Navigation LOTP — menu structuré (liens directs + sous-menus déroulants).
   Rendue automatiquement dans <nav class="tabs" id="navmain"></nav>. */
(function () {
  var MENU = [
    { href: "/dashboard", label: "🏠 Tableau de bord" },
    { label: "⚔️ Simulation", items: [
      { href: "/", label: "⚔️ Simulateur" },
      { href: "/gear", label: "🧰 Top Stuff" },
      { href: "/compare", label: "⚖️ Comparateur" }
    ] },
    { label: "👥 Guilde", items: [
      { href: "/characters", label: "👥 Personnages" },
      { href: "/raids", label: "📊 Rapports" },
      { href: "/calendar", label: "🗓️ Calendrier" },
      { href: "/guild", label: "🛡️ Infos & liens" }
    ] },
    { href: "/help", label: "📖 Aide" }
  ];
  var nav = document.getElementById("navmain");
  if (!nav) return;
  var path = location.pathname.replace(/\/+$/, "") || "/";
  var inItems = function (items) {
    return items.some(function (i) { return i.href === path; });
  };

  var html = "";
  MENU.forEach(function (m) {
    if (m.items) {
      html += '<div class="tab menu' + (inItems(m.items) ? " active" : "") + '">' +
        '<button class="menubtn" type="button">' + m.label + '<span class="arr">▾</span></button>' +
        '<div class="dropdown">' +
        m.items.map(function (i) {
          return '<a class="ditem' + (i.href === path ? " active" : "") + '" href="' + i.href + '">' + i.label + '</a>';
        }).join("") +
        '</div></div>';
    } else {
      html += '<a class="tab' + (m.href === path ? " active" : "") + '" href="' + m.href + '">' + m.label + '</a>';
    }
  });
  html += '<a class="tab admin hidden" id="admin-link" href="/admin">⚙️ Administration</a>';
  nav.innerHTML = html;

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
    "#navmain .ditem.active { color:var(--acc2,#dfa55a); font-weight:700; }"
  ].join("\n");
  document.head.appendChild(css);

  nav.querySelectorAll(".tab.menu > .menubtn").forEach(function (btn) {
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      var m = btn.parentElement;
      var was = m.classList.contains("open");
      nav.querySelectorAll(".tab.menu.open").forEach(function (x) { x.classList.remove("open"); });
      if (!was) m.classList.add("open");
    });
  });
  document.addEventListener("click", function () {
    nav.querySelectorAll(".tab.menu.open").forEach(function (x) { x.classList.remove("open"); });
  });
})();
