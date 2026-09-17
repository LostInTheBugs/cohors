/* i18n FR/EN — LOTP Simulateur.
   Mécanique : les pages restent écrites en français ; ce fichier traduit le DOM
   à la volée en anglais quand la langue choisie est « en » (dictionnaire exact +
   règles pour les chaînes dynamiques), via un MutationObserver pour le contenu
   régénéré par JS. Le choix est mémorisé (localStorage). */
(() => {
  "use strict";
  const DICT = {
 "LOTP — Connexion": "LOTP — Sign in",
 "Connecte-toi pour lancer et consulter les simulations.": "Sign in to run and view your simulations.",
 "Se connecter": "Sign in",
 "E-mail": "Email",
 "Mot de passe": "Password",
 "Pas encore de compte ? L'accès se fait": "No account yet? Access is",
 "sur invitation": "by invitation",
 "— demande un lien à un officier de la guilde.": "— ask a guild officer for a link.",
 "Mot de passe oublié ? Demande un lien de réinitialisation à un officier.": "Forgot your password? Ask an officer for a reset link.",
 "Moteur : SimulationCraft · Données de jeu fournies par Blizzard Entertainment · Outil non affilié": "Engine: SimulationCraft · Game data provided by Blizzard Entertainment · Not affiliated",
 "Remplis l'e-mail et le mot de passe.": "Fill in your email and password.",
 "Connexion…": "Signing in…",
 "Erreur de connexion.": "Sign-in error.",
 "Erreur réseau — réessaie.": "Network error — try again.",
 "Chargement de l'invitation…": "Loading invitation…",
 "Lien invalide ou expiré": "Invalid or expired link",
 "Ce lien d'invitation n'est plus valable. Demande un nouveau lien à un officier de la guilde.": "This invitation link is no longer valid. Ask a guild officer for a new link.",
 "← Aller à la connexion": "← Go to sign in",
 "Crée ton compte pour accéder aux simulations.": "Create your account to access the simulations.",
 "Nom / perso": "Name / character",
 "Mot de passe (8 caractères minimum)": "Password (8 characters minimum)",
 "Confirme le mot de passe": "Confirm password",
 "Créer mon compte": "Create my account",
 "Le mot de passe doit faire au moins 8 caractères.": "Password must be at least 8 characters.",
 "Les deux mots de passe ne correspondent pas.": "Passwords do not match.",
 "Création du compte…": "Creating account…",
 "Erreur.": "Error.",
 "Compte créé — redirection…": "Account created — redirecting…",
 "Déconnexion": "Sign out",
 "Chargement…": "Loading…",
 "Erreur": "Error",
 "Erreur API": "API error",
 "⚔️ Simulateur": "⚔️ Simulator",
 "👥 Personnages": "👥 Characters",
 "📊 Rapports": "📊 Reports",
 "⚖️ Comparateur": "⚖️ Compare",
 "🧰 Top Stuff": "🧰 Top Stuff",
 "⚙️ Administration": "⚙️ Administration",
 "Simulateur": "Simulator",
 "SimulationCraft pour la guilde": "SimulationCraft for the guild",
 "Colle ton export": "Paste your export",
 "(en jeu : tape": "(in game: type",
 "puis copie tout le texte) et lance la simulation.": "then copy the whole text) and run the simulation.",
 "❓ Comment obtenir l'export /simc ?": "❓ How to get the /simc export?",
 "Installe l'addon": "Install the addon",
 "dans WoW (via": "in WoW (via",
 "ou WowUp, comme tes autres addons).": "or WowUp, like your other addons).",
 "En jeu, tape": "In game, type",
 "dans le chat : une fenêtre s'ouvre avec tout ton profil (équipement, talents, sacs et banque).": "in chat: a window opens with your full profile (gear, talents, bags and bank).",
 "Copie le texte (bouton": "Copy the text (",
 "dans la fenêtre), colle-le dans le champ ci-dessous, puis lance la simulation.": "button in the window), paste it into the field below, then run the simulation.",
 "Rien d'autre à installer : le moteur de simulation tourne entièrement sur le serveur.": "Nothing else to install: the simulation engine runs entirely on the server.",
 "Export /simc": "Export /simc",
 "SimC Addon export — colle ici…": "SimC addon export — paste here…",
 "nom du profil (pour enregistrer)": "profile name (to save)",
 "Nom / perso (optionnel)": "Name / character (optional)",
 "Type": "Type",
 "Dégâts (DPS)": "Damage (DPS)",
 "Poids de stats (optimiseur)": "Stat weights (optimizer)",
 "Précision": "Precision",
 "Rapide — 5 000 itérations": "Fast — 5,000 iterations",
 "Standard — 10 000 itérations": "Standard — 10,000 iterations",
 "Précis — 25 000 itérations": "Precise — 25,000 iterations",
 "Très précis — 50 000 itérations": "Very precise — 50,000 iterations",
 "▶ Lancer la simulation": "▶ Run simulation",
 "📁 Profil :": "📁 Profile:",
 "— charger un profil —": "— load a profile —",
 "💾 Enregistrer": "💾 Save",
 "Enregistrer l'export comme profil": "Save export as profile",
 "Supprimer le profil chargé": "Delete the loaded profile",
 "Partager avec la guilde": "Share with the guild",
 "Dernières simulations": "Recent simulations",
 "Derniers rapports de guilde sur Warcraft Logs. Clique sur un rapport pour voir les boss, les wipes/kills et les parses de chaque joueur.": "Latest guild reports on Warcraft Logs. Click a report to see bosses, wipes/kills and each player's parses.",
 "(image Docker officielle) · Données de jeu fournies par": "(official Docker image) · Game data provided by",
 "· Outil non affilié à Blizzard Entertainment ·": "· Not affiliated with Blizzard Entertainment ·",
 "à l'instant": "just now",
 "Puissance des sorts": "Spell power",
 "Puissance d'attaque": "Attack power",
 "Agilité": "Agility",
 "Hâte": "Haste",
 "Maîtrise": "Mastery",
 "Coups critiques": "Critical strike",
 "Polyvalence": "Versatility",
 "Intelligence": "Intelligence",
 "En simulation…": "Simulating…",
 "Terminé": "Done",
 "Échoué": "Failed",
 "Simulation": "Simulation",
 "Mes profils": "My profiles",
 "Partagés par la guilde": "Shared by the guild",
 "Colle d'abord un export /simc dans la zone de texte.": "Paste a /simc export into the text area first.",
 "Donne un nom au profil (champ « nom du profil »).": "Give the profile a name (« profile name » field).",
 "Profil supprimé.": "Profile deleted.",
 "Colle d'abord ton export /simc (le texte semble vide ou trop court).": "Paste your /simc export first (the text looks empty or too short).",
 "⚡ Résultat déjà en cache — simulation identique trouvée !": "⚡ Result already cached — identical simulation found!",
 "Échec — vérifie que ton export est complet (copié en entier depuis /simc).": "Failed — make sure your export is complete (copied entirely from /simc).",
 "· par": "· by",
 "Roster & équipement de la guilde": "Guild roster & gear",
 "Roster de la guilde (données live de l'API Battle.net). Clique sur un personnage pour voir son équipement.": "Guild roster (live from the Battle.net API). Click a character to see their gear.",
 "🪪 Mes personnages": "🪪 My characters",
 "Lie ton compte à tes personnages de guilde — ⭐ = ton main.": "Link your account to your guild characters — ⭐ = your main.",
 "⭐ en faire mon main": "⭐ set as my main",
 "📌 Lier à mon compte": "📌 Link to my account",
 "(cache 30 min) · Données de jeu fournies par": "(30 min cache) · Game data provided by",
 "Nom exact du personnage à lier (ex. Ganathielle)": "Exact character name to link (e.g. Ganathielle)",
 "Rechercher un personnage…": "Search a character…",
 "Refetch des données (max 1×/min)": "Refetch data (max 1×/min)",
 "Définir comme main": "Set as main",
 "Retirer de mon compte": "Remove from my account",
 "Chevalier de la mort": "Death Knight",
 "Chasseur de démons": "Demon Hunter",
 "Évocateur": "Evoker",
 "Prêtre": "Priest",
 "Démoniste": "Warlock",
 "Druide": "Druid",
 "Chasseur": "Hunter",
 "Voleur": "Rogue",
 "Chaman": "Shaman",
 "Guerrier": "Warrior",
 "Moine": "Monk",
 "Maître de guilde": "Guild Master",
 "Main mis à jour ⭐": "Main updated ⭐",
 "Personnage retiré.": "Character removed.",
 "✕ Retirer de mes personnages": "✕ Remove from my characters",
 "📌 C'est mon personnage": "📌 This is my character",
 "Retiré de tes personnages.": "Removed from your characters.",
 "Personnage lié ✓": "Character linked ✓",
 "Saisis le nom du personnage (comme dans le roster).": "Enter the character name (as in the roster).",
 "Lié ✓ — attention : ce personnage est aussi lié à un autre compte.": "Linked ✓ — note: this character is also linked to another account.",
 "Chargement de l'équipement…": "Loading gear…",
 "Rang": "Rank",
 "Nom": "Name",
 "Niv.": "Lvl",
 "Vu": "Seen",
 "Aucun personnage lié pour l'instant.": "No characters linked yet.",
 "Détails": "Details",
 "Armurerie ↗": "Armory ↗",
 "iLvl équipé": "iLvl equipped",
 "Warcraft Logs — parses de la guilde": "Warcraft Logs — guild parses",
 "Rapports récents": "Recent reports",
 "Données :": "Data:",
 "(API v2, cache 30 min) · Données de jeu fournies par": "(v2 API, 30 min cache) · Game data provided by",
 "Héroïque": "Heroic",
 "Mythique": "Mythic",
 "Défi (M+)": "Mythic+",
 "Défis": "Mythic+",
 "(sans titre)": "(untitled)",
 "Zone inconnue": "Unknown zone",
 "Ouvrir": "Open",
 "Ouvrir sur Warcraft Logs ↗": "Open on Warcraft Logs ↗",
 "Chargement du rapport…": "Loading report…",
 "✅ Tué": "✅ Killed",
 "Pas de parses pour ce pull.": "No parses for this pull.",
 "Personnages côte à côte — ilvl & parses": "Characters side by side — ilvl & parses",
 "Ajoute 2 à 6 personnages de la guilde (royaume Hyjal) pour les comparer : équipement (équipé, dernier jeu) et meilleurs parses Warcraft Logs sur le raid en cours —": "Add 2 to 6 guild characters (Hyjal realm) to compare them: gear (equipped, last played) and best Warcraft Logs parses on the current raid —",
 "Ajoute des personnages pour lancer la comparaison.": "Add characters to start the comparison.",
 "(cache serveur) · Données de jeu fournies par": "(server cache) · Game data provided by",
 "Nom du personnage (ex. Squall)": "Character name (e.g. Squall)",
 "Recharger les données": "Reload data",
 "Retirer": "Remove",
 "Pas de données Warcraft Logs.": "No Warcraft Logs data.",
 "Moy. des meilleurs": "Best avg",
 "Médiane": "Median",
 "équipé": "equipped",
 "Pas de parses sur ce raid.": "No parses on this raid.",
 "Quelle pièce te fait gagner le plus de DPS ?": "Which item gives you the most DPS?",
 ", ajoute les pièces à comparer (liens Wowhead ou identifiants) — chaque pièce est simulée sur ton personnage et classée par DPS.": ", add the items to compare (Wowhead links or IDs) — each item is simulated on your character and ranked by DPS.",
 "Ton export /simc": "Your /simc export",
 "Pièces à comparer": "Items to compare",
 "0 pièce (max 15)": "0 items (max 15)",
 "🧰 Comparer les pièces": "🧰 Compare items",
 "Résultat — classement par DPS": "Result — DPS ranking",
 "Un lien Wowhead par ligne, ex. https://www.wowhead.com/fr/item=251080 ou juste l'identifiant : 251080": "One Wowhead link per line, e.g.\nhttps://www.wowhead.com/item=251080\nor just the ID: 251080",
 "Colle d'abord ton export /simc (pièce du haut).": "Paste your /simc export first (top box).",
 "Ajoute au moins une pièce (lien Wowhead ou identifiant).": "Add at least one item (Wowhead link or ID).",
 "Terminé ✅": "Done ✅",
 "Équipement actuel": "Current gear",
 "référence": "reference",
 "Pièce": "Item",
 "Emplacement": "Slot",
 "Δ vs actuel": "Δ vs current",
 "Échec de la simulation — vérifie que ton export /simc est complet.": "Simulation failed — make sure your /simc export is complete.",
 "⚡ Résultat en cache — affiché ci-dessous.": "⚡ Cached result — shown below.",
 "Rapport complet →": "Full report →",
 "Gestion des invitations et des comptes — zone réservée": "Invitations and account management — restricted area",
 "← Retour à l'application": "← Back to the app",
 "✉️ Invitations": "✉️ Invitations",
 "👤 Comptes": "👤 Accounts",
 "Créer une invitation": "Create an invitation",
 "e-mail du membre (vide = lien libre)": "member email (empty = free link)",
 "note (optionnelle)": "note (optional)",
 "Saisis d'abord un e-mail": "Enter an email first",
 "✉️ Envoyer par e-mail": "✉️ Send by email",
 "Créer le lien": "Create the link",
 "Invitations": "Invitations",
 "Aucune invitation.": "No invitations.",
 "En attente": "Pending",
 "Utilisé": "Used",
 "Expiré": "Expired",
 "utilisé": "used",
 "Copié ✓": "Copied ✓",
 "Copie le lien :": "Copy the link:",
 "SMTP non configuré sur le serveur": "SMTP not configured on the server",
 "Révoquer cette invitation ?": "Revoke this invitation?",
 "Invitation révoquée": "Invitation revoked",
 "Copier le lien": "Copy link",
 "Révoquer": "Revoke",
 "✉️ Renvoyer": "✉️ Resend",
 "Lien à envoyer :": "Link to send:",
 "Copier": "Copy",
 "Comptes": "Accounts",
 "Désactiver": "Deactivate",
 "Réactiver": "Reactivate",
 "Désactivé": "Deactivated",
 "Lien de réinitialisation": "Reset link",
 "Supprimer": "Delete",
 "Compte mis à jour": "Account updated",
 "Lien de réinitialisation créé": "Reset link created",
 "Compte supprimé": "Account deleted",
 "Membre": "Member",
 "Officier": "Officer",
 "Administrateur": "Administrator",
 "Rôle mis à jour ✓": "Role updated ✓",
 "Annonce automatiquement les": "Automatically posts",
 "nouveaux rapports de raid": "new raid reports",
 "(Warcraft Logs) et les": "(Warcraft Logs) and",
 "mouvements de guilde": "guild roster changes",
 "(arrivées / départs) dans un salon Discord.": "(joins / leaves) in a Discord channel.",
 "❓ Créer l'application Discord (une seule fois)": "❓ Create the Discord application (once)",
 "Ouvre le": "Open the",
 "Portail développeurs Discord": "Discord Developer Portal",
 "(nom : « LOTP Simulateur »).": "(name: « LOTP Simulateur »).",
 "Copie l'": "Copy the",
 "(General Information) → colle-le ci-dessous.": "(General Information) → paste it below.",
 "Onglet": "Tab",
 "→ copie le": "→ copy the",
 "→ colle-le ci-dessous (secret).": "→ paste it below (secret).",
 "Clique": "Click",
 "📎 Lien d'invitation": "📎 Invite link",
 ", ouvre-le et ajoute le bot à ton serveur — il faut la permission « Gérer le serveur » sur le serveur visé (sinon, transmets le lien à un administrateur du serveur).": ", open it and add the bot to your server — the « Manage Server » permission is required on the target server (otherwise, forward the link to a server administrator).",
 "🔄 Charger les salons": "🔄 Load channels",
 ", choisis le salon,": ", pick the channel,",
 "puis": "then",
 "📨 Message de test": "📨 Test message",
 "Au premier passage, le bot pose ses références (aucune annonce rétroactive) : les annonces démarrent à partir de l'activation.": "On the first run the bot sets its baseline (no retroactive posts): announcements start from activation.",
 "Bot actif": "Bot active",
 "📊 Nouveaux rapports de raid": "📊 New raid reports",
 "👥 Mouvements de guilde": "👥 Guild roster changes",
 "Application ID (Portail développeurs)": "Application ID (Developer Portal)",
 "Token du bot": "Bot token",
 "Rôle du compte": "Account role",
 "🔴 Token refusé": "🔴 Token rejected",
 "⚪ Non configuré": "⚪ Not configured",
 "Token du bot (Portail développeurs → onglet Bot)": "Bot token (Developer Portal → Bot tab)",
 "Colle d'abord l'Application ID (Portail développeurs Discord)": "Paste the Application ID first (Discord Developer Portal)",
 "Configuration enregistrée ✓": "Configuration saved ✓",
 "Message de test envoyé 📨": "Test message sent 📨",
 "Lien créé — envoi e-mail échoué": "Link created — email sending failed",
 "Invitation créée": "Invitation created",
 "🤖 Bot Discord": "🤖 Discord bot",
 "Bot Discord": "Discord bot",
 "créé le": "created on",
 "dernière connexion": "last login",
 "Trop de tentatives — réessaie dans quelques minutes.": "Too many attempts — try again in a few minutes.",
 "E-mail ou mot de passe incorrect.": "Incorrect email or password.",
 "Ce compte est désactivé.": "This account is deactivated.",
 "Non connecté": "Not signed in",
 "Connexion requise": "Sign-in required",
 "Lien d'invitation invalide ou expiré.": "Invalid or expired invitation link.",
 "Un compte existe déjà avec cet e-mail — connecte-toi, ou demande un lien de réinitialisation.": "An account already exists with this email — sign in, or ask for a reset link.",
 "Réservé à l'administrateur": "Administrators only",
 "Réservé aux officiers et administrateurs": "Officers and administrators only",
 "Indique au moins une pièce (lien Wowhead ou identifiant).": "Provide at least one item (Wowhead link or ID).",
 "Personnage introuvable dans le roster de la guilde (vérifie l'orthographe, ou demande à un officier).": "Character not found in the guild roster (check the spelling, or ask an officer).",
 "Ce personnage est déjà lié à ton compte.": "This character is already linked to your account.",
 "Personnage non lié à ton compte.": "Character not linked to your account.",
 "Invitation inconnue.": "Unknown invitation.",
 "Invitation déjà utilisée ou expirée.": "Invitation already used or expired.",
 "Cette invitation est un lien libre (sans e-mail).": "This invitation is a free link (no email).",
 "Compte inconnu": "Unknown account",
 "Impossible de modifier ton propre compte.": "You cannot modify your own account.",
 "Impossible de supprimer ton propre compte.": "You cannot delete your own account.",
 "Impossible de modifier ton propre rôle.": "You cannot change your own role.",
 "Rôle inconnu (membre, officier ou administrateur).": "Unknown role (member, officer or administrator).",
 "Token du bot non configuré.": "Bot token not configured.",
 "Le bot n'est encore sur aucun serveur — utilise le lien d'invitation.": "The bot is not in any server yet — use the invite link.",
 "Configure d'abord le token et le salon (Enregistrer).": "Configure the token and the channel first (Save).",
 "Token du bot refusé par Discord (invalide ou révoqué ?).": "Bot token rejected by Discord (invalid or revoked?).",
 "Discord a refusé l'action (permissions du bot insuffisantes ?).": "Discord refused the action (insufficient bot permissions?).",
 "LOTP — Simulateur": "LOTP — Simulator",
 "LOTP — Personnages": "LOTP — Characters",
 "LOTP — Rapports de raid": "LOTP — Raid reports",
 "LOTP — Comparateur": "LOTP — Compare",
 "LOTP — Inscription": "LOTP — Sign-up",
 "LOTP — Top Stuff": "LOTP — Top Stuff",
 "LOTP — Administration": "LOTP — Admin",
 "· Moteur : SimulationCraft · Outil non affilié à Blizzard Entertainment": "· Engine: SimulationCraft · Not affiliated with Blizzard Entertainment"
};
  const ATTRS = ["placeholder", "title", "aria-label"];

  const RULES = [
    [/il y a (\d+) min\b/g, "$1 min ago"],
    [/il y a (\d+) h\b/g, "$1 h ago"],
    [/il y a (\d+) j\b/g, "$1 d ago"],
    [/(\d[\d\u00a0 ]*) membres\b/g, "$1 members"],
    [/actualisé/g, "refreshed"],
    [/à l'instant/g, "just now"],
    [/rapports au total/g, "reports total"],
    [/points de hauts faits/g, "achievement points"],
    [/(\d+) tué\(s\)/g, "$1 killed"],
    [/\bRang (\d+)\b/g, "Rank $1"],
    [/\(moyen ([\d\u00a0 ,]+)\)/g, "(avg $1)"],
    [/itérations/g, "iterations"],
    [/Simulation :/g, "Simulation:"],
    [/Simulation en cours/g, "Simulation running"],
    [/ça peut prendre 1 à 2 minutes selon le nombre de pièces…/g, "this can take 1–2 min depending on the number of items…"],
    [/la simulation démarre dans quelques instants…/g, "simulation starting shortly…"],
    [/En file \(position/g, "Queued (position"],
    [/^vu /g, "seen "],
    [/Invitation pour /g, "Invitation for "],
    [/— choisis ton mot de passe pour activer ton compte\./g, "— choose your password to activate your account."],
    [/Profil « (.*?) » chargé ✅/g, "Profile « $1 » loaded ✅"],
    [/Profil « (.*?) » mis à jour ✅/g, "Profile « $1 » updated ✅"],
    [/Profil « (.*?) » enregistré ✅/g, "Profile « $1 » saved ✅"],
    [/Supprimer le profil « (.*?) » \?/g, "Delete profile « $1 » ?"],
    [/Supprimer définitivement le compte « (.*?) » \?/g, "Permanently delete account « $1 » ?"],
    [/« (.*?) » lié à ton compte ✓/g, "« $1 » linked to your account ✓"],
    [/Renvoyer par e-mail à /g, "Resend by email to "],
    [/expiré le /g, "expired on "],
    [/expire le /g, "expires on "],
    [/Relance envoyée à (.*?) ✓/g, "Reminder sent to $1 ✓"],
    [/Invitation envoyée à (.*?) ✓/g, "Invitation sent to $1 ✓"],
    [/Token actuel : …(.*?) \(laisser vide = inchangé\)/g, "Current token: …$1 (leave empty = unchanged)"],
    [/meilleur parse \(médiane (.*?)\)/g, "best parse (median $1)"],
    [/Pas dans le roster de guilde : /g, "Not in the guild roster: "],
    [/vérifie l'orthographe \(la comparaison reste possible\)\./g, "check the spelling (comparison still possible)."],
    [/Erreur de chargement du roster : /g, "Error loading roster: "],
    [/Erreur de chargement : /g, "Error loading: "],
    [/Erreur : /g, "Error: "],
    [/Lien de réinitialisation/g, "Reset link"],
    [/créé le /g, "created on "],
    [/dernière connexion /g, "last login "],
    [/\(sans titre\)/g, "(untitled)"],
    [/Zone inconnue/g, "Unknown zone"],
    [/Token refusé par Discord — /g, "Token rejected by Discord — "],
    [/Discord injoignable \(/g, "Discord unreachable ("],
    [/· par /g, "· by "],
    [/· créé /g, "· created "],
    [/utilisé(?![eé])/g, "used"],
    [/Moteur :/g, "Engine:"],
    [/Outil non affilié à Blizzard Entertainment/g, "Not affiliated with Blizzard Entertainment"],
  ];

  const norm = (s) => s.replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();

  function tr(text) {
    const key = norm(text);
    if (!key) return null;
    if (Object.prototype.hasOwnProperty.call(DICT, key)) return DICT[key];
    let out = text;
    for (const [re, rep] of RULES) out = out.replace(re, rep);
    return out !== text ? out : null;
  }

  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "CODE", "PRE"]);

  function translateTextNode(node) {
    const raw = node.nodeValue;
    if (!raw || !raw.trim()) return;
    const p = node.parentNode;
    if (p && (SKIP.has(p.tagName) || p.id === "langbox")) return;
    const out = tr(raw);
    if (out === null || out === raw) return;
    const lead = raw.match(/^\s*/)[0];
    const trail = raw.match(/\s*$/)[0];
    node.nodeValue = lead + out + trail;
  }

  function translateElement(el) {
    for (const a of ATTRS) {
      const v = el.getAttribute && el.getAttribute(a);
      if (v && v.trim()) {
        const out = tr(v);
        if (out !== null && out !== v) el.setAttribute(a, out);
      }
    }
  }

  function walk(root) {
    if (!root) return;
    if (root.nodeType === 3) { translateTextNode(root); return; }
    if (root.nodeType !== 1 && root.nodeType !== 9 && root.nodeType !== 11) return;
    if (root.nodeType === 1) {
      if (root.id === "langbox" || SKIP.has(root.tagName)) return;
      translateElement(root);
    }
    const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
      acceptNode: (n) => (n.nodeType === 1 && (SKIP.has(n.tagName) || n.id === "langbox"))
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
    });
    let n;
    while ((n = tw.nextNode())) {
      if (n.nodeType === 3) translateTextNode(n);
      else translateElement(n);
    }
  }

  // ---------------- langue
  const STORE = "lotp_lang";
  function resolveLang() {
    try {
      const saved = localStorage.getItem(STORE);
      if (saved === "fr" || saved === "en") return saved;
    } catch (e) {}
    const nav = (navigator.languages && navigator.languages[0]) || navigator.language || "fr";
    return String(nav).toLowerCase().startsWith("en") ? "en" : "fr";
  }
  const LANG = resolveLang();
  if (LANG !== "en") { injectBox(); return; }

  document.documentElement.lang = "en";

  // dates/nombres en locale anglaise pour les appels codés en fr-FR
  try {
    const _d = Date.prototype.toLocaleDateString, _t = Date.prototype.toLocaleTimeString, _s = Date.prototype.toLocaleString;
    Date.prototype.toLocaleDateString = function (l, o) { return _d.call(this, l === "fr-FR" ? "en-GB" : l, o); };
    Date.prototype.toLocaleTimeString = function (l, o) { return _t.call(this, l === "fr-FR" ? "en-GB" : l, o); };
    Date.prototype.toLocaleString = function (l, o) { return _s.call(this, l === "fr-FR" ? "en-GB" : l, o); };
    const _n = Number.prototype.toLocaleString;
    Number.prototype.toLocaleString = function (l, o) { return _n.call(this, l === "fr-FR" ? "en-GB" : l, o); };
  } catch (e) {}

  function translateTitle() {
    const t = document.title;
    const out = tr(t);
    if (out !== null) document.title = out;
  }

  function startObserver() {
    try {
      const obs = new MutationObserver((muts) => {
        for (const m of muts) {
          if (m.type === "characterData") translateTextNode(m.target);
          else if (m.type === "attributes") translateElement(m.target);
          else for (const n of m.addedNodes) walk(n);
        }
      });
      obs.observe(document.body, { childList: true, characterData: true, subtree: true,
        attributes: true, attributeFilter: ATTRS });
    } catch (e) {}
  }

  function apply() {
    translateTitle();
    walk(document.body);
    startObserver();
    injectBox();
  }

  // ---------------- sélecteur de langue
  function injectBox() {
    if (document.getElementById("langbox")) return;
    const el = document.createElement("span");
    el.id = "langbox";
    el.style.cssText = "display:inline-flex;gap:4px;align-items:center;margin-left:10px;font-size:12.5px;";
    el.innerHTML = "";
    for (const [code, label] of [["fr", "🇫🇷 FR"], ["en", "🇬🇧 EN"]]) {
      const b = document.createElement("a");
      b.textContent = label;
      b.href = "#";
      b.style.cssText = "cursor:pointer;text-decoration:none;padding:2px 8px;border-radius:999px;border:1px solid "
        + (code === LANG ? "var(--acc2,#dfa55a);color:var(--acc2,#dfa55a)" : "var(--line,#2a3446);color:var(--mut,#8b96ad)");
      b.addEventListener("click", (ev) => {
        ev.preventDefault();
        if (code === LANG) return;
        try { localStorage.setItem(STORE, code); } catch (e) {}
        location.reload();
      });
      el.appendChild(b);
    }
    const box = document.querySelector("header.topbar .userbox");
    if (box) { box.appendChild(el); }
    else {
      el.style.position = "fixed"; el.style.top = "10px"; el.style.right = "12px"; el.style.zIndex = "99";
      el.style.background = "var(--card, #131a26)"; el.style.padding = "3px 8px"; el.style.borderRadius = "999px";
      el.style.border = "1px solid var(--line,#2a3446)";
      document.body.appendChild(el);
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", apply);
  else apply();
})();
