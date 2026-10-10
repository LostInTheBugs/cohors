/* Cohors — fonctions communes aux pages (chargé juste avant le script de la page).
   Définies comme propriétés de window : une page qui garde sa propre version (const locale) la masque
   sans conflit. Déconnexion : le lien #logout est branché ici. */
(function () {
  "use strict";
  // Sélecteur court.
  window.$ = (s) => document.querySelector(s);
  // GET JSON ; en cas d'erreur HTTP, lève une Error portant le message de l'API (detail) et le statut.
  window.jget = async (u) => { const r = await fetch(u);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { const err = new Error(j.detail || "Erreur API"); err.status = r.status; throw err; } return j; };
  // Nombre au format français (d décimales au plus) ; « — » si absent.
  window.fmt = (n, d = 0) => n == null ? "—" : new Intl.NumberFormat("fr-FR", { maximumFractionDigits: d }).format(n);
  // Ancienneté relative d'un horodatage (secondes) : « à l'instant », « il y a 5 min », « il y a 3 h », « il y a 2 j ».
  window.ago = (ts) => { if (!ts) return "—"; const s = Math.floor(Date.now() / 1000 - ts);
    if (s < 60) return "à l'instant";
    if (s < 3600) return `il y a ${Math.floor(s / 60)} min`;
    if (s < 86400) return `il y a ${Math.floor(s / 3600)} h`;
    return `il y a ${Math.floor(s / 86400)} j`; };
  // Couleurs de classe (clés indépendantes de la langue, cf. CLASS_KEY_FR côté serveur).
  window.CLASS_COLORS = { DeathKnight: "#C41E3A", DemonHunter: "#A330C9", Druid: "#FF7C0A", Evoker: "#33937F",
    Hunter: "#AAD372", Mage: "#3FC7EB", Monk: "#00FF98", Paladin: "#F48CBA", Priest: "#FFFFFF",
    Rogue: "#FFF468", Shaman: "#0070DD", Warlock: "#8788EE", Warrior: "#C69B6D" };
  // Déconnexion depuis l'en-tête.
  var bindLogout = function () {
    var lo = document.getElementById("logout");
    if (!lo) return;
    lo.addEventListener("click", async (ev) => {
      ev.preventDefault();
      try { await fetch("/api/logout", { method: "POST" }); } catch (e) { /* on redirige quand même */ }
      location.href = "/login";
    });
  };
  if (document.getElementById("logout")) bindLogout();
  else document.addEventListener("DOMContentLoaded", bindLogout);
})();
