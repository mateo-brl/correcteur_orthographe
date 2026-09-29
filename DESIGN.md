# Direction de design

Je lis ça comme : utilitaire de correction intégré au système, pour un usage quotidien sur des PC parfois modestes,
en langage visuel d'outil système sobre, curseurs ENERGY 1 / RHYTHM 1 / MOTION 1.

Les valeurs exactes des couleurs sont dans [`src/correcteur/ui/theme.py`](src/correcteur/ui/theme.py), qui fait foi ;
ce document n'en recopie aucune.

## Décisions

- Police du système : l'application doit se fondre dans Windows et Linux.
- Les couleurs vives sont réservées aux quatre catégories de fautes (rouge pour l'orthographe, bleu pour la
  grammaire, orange pour la ponctuation, violet pour le style), parce qu'ici la couleur porte une information.
- Les boutons, la première suggestion et le focus utilisent une couleur d'action neutre, « encre », pour qu'on ne
  la confonde pas avec une catégorie.
- Les fautes sont soulignées d'un trait ondulé, parce que c'est la convention connue des traitements de texte.
- La barre colorée à gauche de chaque carte indique la catégorie de la faute. C'est une information, pas un décor.
- Les thèmes clair et sombre suivent le réglage du système ; on peut en forcer un dans les paramètres.
- Aucune animation (MOTION 1). Les états « vérification en cours » et « moteur indisponible » sont écrits en
  toutes lettres dans la fenêtre plutôt que signalés par un mouvement ou une couleur seule.
- Les contrastes respectent le niveau WCAG AA, et des tests le vérifient (`tests/test_theme.py`).
