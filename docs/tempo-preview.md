# Prévision Tempo sur les batteries et recharge minimale par phase

## Mise à jour et affichage

Installer l'intégration depuis `main`, redémarrer Home Assistant puis recharger
complètement le navigateur (le module frontend passe à `tempo-preview.js?v=2`).
Aucun YAML, blueprint ou binding supplémentaire n'est nécessaire.

Dans **Omnibattery → Batteries**, chaque batterie possède maintenant un encart
**Prochaine recharge HC**, sous Santé et cellules / Hystérésis, avant Énergie
aujourd'hui. Le grand tableau a été retiré de Contrôle.

La valeur principale est l'**énergie estimée chargeable dans la batterie**, en
**kWh**, pour le prochain créneau Tempo **22:00 → 06:00 Europe/Paris**. Il ne
s'agit pas d'une puissance en kW. Les détails indiquent le besoin, le prélèvement
réseau estimé avec pertes, le motif, la qualité des données et le dernier calcul.
Le SOC de départ est projeté jusqu'au début des HC. Pendant une plage HC,
le reste à charger dans la plage en cours est séparé de la plage suivante.

Une donnée manquante reste **— kWh / Indisponible**, jamais un faux zéro.
Les batteries sont rapprochées des prévisions par entrée HA et identité/phase ;
un nom modifié par l'utilisateur ne doit pas mélanger deux installations.

## Recharge minimale indépendante par phase

Dans **Omnibattery → Contrôle → Recharge minimale par phase · heures creuses**,
chaque phase dispose de trois réglages, à enregistrer ensemble :

| Réglage | Défaut | Fonction |
|---|---:|---|
| Activer | **OFF** | Autorise cette recharge pour cette phase seulement |
| Déclencher sous | **8 %** | Déclenche strictement sous le seuil, pas à égalité |
| Recharger jusqu'à | **15 %** | Maintient la demande jusqu'à cette cible incluse |

Les seuils doivent vérifier `0 <= seuil < cible <= 100`. Le SOC maximal configuré
sur la batterie reste une limite : la récupération ne le relève pas. Le PD de la
phase doit être configuré et activé pour activer la fonction. Les réglages sont
persistés dans `phase_soc_recovery` de l'entrée de configuration, indépendamment
pour L1/L2/L3. Seul un administrateur HA peut les modifier.

Exemple : P3 est à 7 %, P1/P2 à 20 %. Avec la fonction activée sur P3, une demande
P3 est mémorisée. En HC, P3 recharge jusqu'à 15 %, même après être remontée à 9 %.
P1/P2 ne reçoivent **aucune recharge forcée liée à cette récupération** ; leur
régulation normale (et une éventuelle autre charge autorisée) continue.

Hors HC, la demande attend. À **06:00**, le dispatcher demande 0 W pour libérer
sa consigne forcée avant de reprendre la régulation normale au cycle suivant.
Si la cible n'est pas atteinte, la demande peut reprendre aux HC suivantes.
La désactivation ou l'atteinte de la cible retire également la consigne forcée.
Une absence d'acquittement entraîne des tentatives d'arrêt espacées ; elle n'est
jamais interprétée comme la preuve d'une puissance physique nulle.

L'hystérésis est conservée dans un Store HA, par entrée/phase/batterie/seuils.
Une réaffectation de batterie ou un changement de réglages invalide l'ancienne
demande. Des données SOC invalides ne déclenchent pas artificiellement de charge.

La récupération fonctionne indépendamment de l'activation de la charge
prédictive, mais **uniquement dans les HC Tempo**. Elle emprunte le dispatcher
existant, pas un second programme de commande.

## Priorités et protections

Ordre des demandes automatiques par phase :
**préparation Tempo rouge → charge complète hebdomadaire → récupération SOC →
charge prédictive ordinaire**. Les modes manuel, Pause VE, secours, propriétaires
externes, indisponibilités et blocages de sécurité restent prioritaires.
Les plafonds batterie/phase/contrat et la rampe existante sont conservés.
Le budget réseau réserve aussi une augmentation tentée dont l'acquittement est
incertain, y compris l'arrêt d'une décharge, avant de traiter la phase suivante.

Avec **pilotage Tempo et charge prédictive activés**, la logique existante vise
**100 % avant chaque jour rouge confirmé** : 22 h la veille à 6 h le jour rouge,
y compris les jours rouges consécutifs. La récupération à 15 % ne remplace pas
cet objectif. Cette évolution ne force pas silencieusement l'activation de Tempo.
Lorsque le solaire manque, la règle 100 % peut rester visible sans inventer un
chiffre en kWh. Une courbe solaire de l'après-midi suivant n'est pas exigée pour
projeter la recharge complète d'une plage HC déjà commencée.

**Cette fonction logicielle ne remplace pas les protections du BMS et ne garantit
pas l'absence de décharge profonde.** En HP, en cas de perte de communication,
de Pause VE, de mode manuel ou de blocage, elle ne forcera pas une recharge.

## Données et limites de la prévision

Le prévisionnel reste en lecture seule : il ne change ni les consignes actives
ni les demandes de récupération. Il partage un budget global puis le répartit
par phase ; un objectif de récupération est un minimum local, pas un deuxième
volume ajouté à une recharge déjà suffisante.

Le profil de parts de consommation conserve 14 jours d'observations ; les replis
(parts récentes, instantanées ou égales) sont signalés. Sans mesure photovoltaïque
par phase, le solaire est supposé réparti par tiers. La durée HC utilise UTC pour
les calculs : 7 ou 9 heures aux changements d'heure.

Pour AROME, une courbe marquée `complete: false` peut servir **seulement à cet
aperçu** si les intervalles futurs nécessaires sont réellement présents. Le
manque de données passées ne suffit plus à la rejeter. Tout trou dans la période
diurne future requise reste une erreur. Le lecteur de production et les états
HA originaux ne sont pas modifiés, et aucune production n'est inventée.

La quantité chargeable est une estimation sous les plafonds connus, pas une
promesse : température, BMS, rampe, nouvelles consommations, mises à jour météo
et autres blocages peuvent la réduire. La consommation nocturne est supposée
alimentée par le réseau ; une décharge après l'atteinte de la cible peut réduire
le SOC à 6 h. Après 22 h, la plage suivante nécessite parfois les prévisions de
J+2, pas encore publiées. Le créneau actuel reste traité séparément.

Première version : une flotte complète de trois batteries, chacune affectée à
L1/L2/L3 ; les mappings ambigus sont refusés. Le déficit prédictif est calculé
pour le mode Time Slot, pas pour une grille dynamique arbitraire.

## Tests et contrat frontend

```bash
python -m pip install pytest
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python scripts/test_phase_recovery_isolated.py
node --check custom_components/omnibattery/frontend/tempo-preview.js
```

Le lanceur isolé exécute les vrais planificateur, compteurs et dispatcher de
phase avec horloge/imports HA, persistance et matériel simulés. Les tests de
l'adaptateur utilisent des prévisions synthétiques. Cela n'est **pas** un test
d'installation HA complète ni une certification matérielle.

```bash
python -m pip install playwright
python -m playwright install chromium
python scripts/test_phase_recovery_frontend.py
```

Le navigateur utilise un panneau de test dont les points d'extension reproduisent
le contrat du panneau 1.6.0. Les hooks `_buildBatteryCard`, `_patchBatteries`,
`_renderControl`, `_patchControl` et `M.hyst` doivent être revérifiés lors des
backports majeurs. Aucun fichier principal de 580 ko n'est remplacé.
