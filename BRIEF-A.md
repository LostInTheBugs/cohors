Travaille exclusivement dans /home/administrator/work/cohors/. N'écris rien en dehors.

Étape 0 — Repartir propre
Sauvegarde ton travail en cours, sans le réutiliser : git stash push -u -m "abandon-essai-secrets".
Mets-toi à jour : git fetch origin && git checkout main && git reset --hard origin/main. Vérifie que VERSION = 2026.09.155-c2.
Crée la branche hash-session-tokens.
Enregistre ce brief tel quel dans /home/administrator/work/cohors/BRIEF-A.md. Relis-le avant chaque étape. Si un message te semble coupé ou ambigu, ne demande rien : suis BRIEF-A.md.
Tâche — Partie A seulement : tokens de session hashés

Ne touche ni aux clés API, ni au bot Discord, ni au SMTP, ni au chiffrement. Ce sera une PR séparée.

Aujourd'hui, sessions.token stocke en clair le token du cookie (app/main.py, table vers la ligne 147).

Ajoute un helper unique _session_key(token: str) -> str qui renvoie hashlib.sha256(token.encode()).hexdigest().
La base stocke _session_key(token). Le cookie garde le token brut. Utilise le helper partout où un token de session sert dans une requête SQL :
_new_session (vers la ligne 658) : insère le hash, renvoie le token brut ;
_get_session_user, SELECT et UPDATE (vers 677-686) ;
logout (vers 1721-1724) ;
« déconnecter mes autres sessions » (vers 8000-8003, token != ?). Lance grep -n "sessions" app/main.py et vérifie chaque ligne. Le proxy vocal (vers 8159) relit le cookie : vérifie qu'il ne compare rien en base sans passer par le helper.
Migration, dans le style des migrations existantes (PRAGMA table_info + ALTER TABLE, vers 398-423) :
si la colonne hashed n'existe pas, ajoute hashed INTEGER NOT NULL DEFAULT 0 ;
pour chaque ligne où hashed = 0, remplace token par _session_key(token) et mets hashed = 1, en Python ligne par ligne ;
_new_session insère avec hashed = 1. C'est idempotent : un second passage ne trouve plus de ligne à 0. La table garde le nom sessions. Personne n'est déconnecté, puisque le cookie existant, une fois hashé, retrouve sa ligne.
Ne touche pas aux tokens d'invitation.
Tests (dans tests/, style test_api.py)
Après login, la valeur en base ≠ la valeur du cookie, et = sha256(cookie).
Une ligne insérée « à l'ancienne » (token en clair, hashed absent ou 0), puis migration : le cookie correspondant authentifie toujours. Une seconde migration ne change rien.
Logout supprime bien la session.
« Déconnecter mes autres sessions » garde la session courante et supprime les autres.

Lance toute la suite Python. Elle doit passer.

Version et livraison
Nouvelle fonctionnalité, donc nouveau numéro YYYY.MM.NNN. Première version d'octobre : regarde dans git log / git tag comment le compteur repart à chaque mois (probablement 2026.10.001).
Mets à jour VERSION, app/VERSION, CACHE dans sw.js et les ?v= des HTML. Les tests de cohérence te diront si tu en oublies.
Entrée CHANGELOG en anglais, section ### Security : « Session tokens are stored hashed (SHA-256); existing sessions are migrated in place — nobody is signed out. »
Commit, push, gh pr create vers main, en anglais. Ne merge pas.
Rapport

Écris /home/administrator/work/cohors/RAPPORT-A.md avec :

les lignes modifiées (fichier:ligne) ;
la sortie des tests ;
l'URL de la PR.

Réponds ensuite en 5 lignes maximum.
