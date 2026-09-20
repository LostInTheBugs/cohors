/* Cohors — identité de la guilde (v2026.09.111) : logo, nom et fond personnalisables.
   Applique /api/branding sur toutes les pages (connexion incluse).
   Réversible : si l'identité revient aux valeurs par défaut, les remplacements
   sont annulés (les valeurs d'origine sont mémorisées par élément). */
(function () {
  var CF = null, LAST = "";
  var BGP = ["background-image", "background-size", "background-position",
             "background-repeat", "background-attachment"];

  function apply() {
    if (!CF) return;
    var short = CF.short || "", full = CF.name || "";
    try {
      var ap = document.querySelector('meta[name="apple-mobile-web-app-title"]');
      if (short && short !== "Cohors") {
        LAST = short;
        document.querySelectorAll(".brandname").forEach(function (el) {
          var t = el.childNodes[0];
          if (t && t.nodeType === 3) {
            if (el.dataset.borig == null) el.dataset.borig = t.nodeValue;
            t.nodeValue = el.dataset.borig.replace("Cohors", short);
          }
        });
        document.querySelectorAll(".dhead span").forEach(function (s) {
          if (s.dataset.borig == null) s.dataset.borig = s.textContent;
          if (s.dataset.borig.trim() === "Cohors") s.textContent = s.dataset.borig.replace("Cohors", short);
        });
        if (document.title.indexOf("Cohors") !== -1) document.title = document.title.replace("Cohors", short);
        if (ap && ap.getAttribute("content") === "Cohors") ap.setAttribute("content", short);
      } else {
        document.querySelectorAll(".brandname").forEach(function (el) {
          var t = el.childNodes[0];
          if (t && t.nodeType === 3 && el.dataset.borig != null) t.nodeValue = el.dataset.borig;
        });
        document.querySelectorAll(".dhead span").forEach(function (s) {
          if (s.dataset.borig != null) s.textContent = s.dataset.borig;
        });
        if (LAST && document.title.indexOf(LAST) !== -1) document.title = document.title.replace(LAST, "Cohors");
        if (ap && LAST && ap.getAttribute("content") === LAST) ap.setAttribute("content", "Cohors");
        LAST = "";
      }
      document.querySelectorAll("img.crest").forEach(function (im) {
        if (im.dataset.borigalt == null) im.dataset.borigalt = im.getAttribute("alt") || "";
        im.alt = full ? full : im.dataset.borigalt;
      });
      if (CF.bg) {
        document.body.style.setProperty("background-image", "url('" + CF.bg + "')", "important");
        document.body.style.setProperty("background-size", "cover", "important");
        document.body.style.setProperty("background-position", "center", "important");
        document.body.style.setProperty("background-repeat", "no-repeat", "important");
        document.body.style.setProperty("background-attachment", "fixed", "important");
      } else {
        BGP.forEach(function (p) { document.body.style.removeProperty(p); });
      }
      if (CF.bg_color) document.body.style.setProperty("background-color", CF.bg_color, "important");
      else document.body.style.removeProperty("background-color");
    } catch (e) {}
  }

  window.__cohorsBranding = { refresh: load, get: function () { return CF; } };
  function load() {
    fetch("/api/branding", { cache: "no-store" }).then(function (r) { return r.json(); })
      .then(function (d) { CF = d || {}; apply(); setTimeout(apply, 400); })
      .catch(function () {});
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", load);
  else load();
  window.addEventListener("load", function () { setTimeout(apply, 600); });
})();
