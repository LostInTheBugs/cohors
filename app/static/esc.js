/* Cohors — échappement HTML partagé (helper unique, chargé avant les scripts de page).
   Remplace la copie locale « const esc » qui existait dans 23 pages (v2026.09.142).
   window.esc (et non un const global) pour ne pas entrer en collision avec d'éventuels
   scripts de page. */
window.esc = (t) => String(t == null ? "" : t).replace(/[<>&"]/g, (c) => ({
  "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;",
}[c]));
