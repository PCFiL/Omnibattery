# Prévision de recharge Tempo par phase

## Emplacement et activation

Mettre à jour l'intégration depuis `main`, redémarrer Home Assistant puis
actualiser complètement le navigateur. Dans **Omnibattery → Contrôle → Charge
prédictive**, un bloc **Prochaine recharge · heures creuses Tempo** apparaît.
Aucun YAML Lovelace ni nouvelle automatisation n'est nécessaire.

Un capteur **Tempo charge preview**, identifié par l'entrée de configuration,
porte les attributs `current_window` et `next_window`. Le premier désigne le
créneau en cours (reste à charger), le second le créneau strictement suivant.
Les lignes sont ordonnées P1/P2/P3 et utilisent les associations batterie/phase
existantes ; elles ne sont pas codées en dur pour des numéros de série.

La projection est séparée du pilotage : elle ne remplace ni le calendrier actif,
ni les objectifs SOC exécutés, et n'appelle aucune commande matérielle. Un SOC
cible futur affiché est une estimation, pas la promesse que la régulation le
sélectionnera au début de la nuit. La cible active, lorsqu'elle existe pendant
le créneau courant, est conservée dans `runtime_target_soc`.

## Calcul et données

- Fenêtre Tempo 22:00 inclus → 06:00 exclus, Europe/Paris. Les durées sont
  calculées en UTC : 7/9 heures lors des changements d'heure, pas toujours 8.
- Le SOC de départ est projeté depuis le SOC actuel et les flux de consommation
  et de production jusqu'au début de la fenêtre, avec seuils SOC et puissances.
- Le profil global existant de consommation est utilisé par dates et quarts
  d'heure. Un seul budget global utilise le calcul pur des échéances
  `pricing.chronological.build_energy_deadlines`, puis il est réparti par phase.
- Les données AROME aujourd'hui/demain ne sont acceptées qu'avec des périodes
  datées. Un manque de couverture diurne ne devient jamais une production de
  zéro. Un vrai zéro fourni par le producteur est accepté. Les courbes datant
  de plus de 24 h, les valeurs invalides et les chevauchements sont rejetés.
- La marge solaire est appliquée une fois par jour au total PV, pas trois fois.
- Priorité d'affichage : préparation d'un jour rouge confirmé, puis charge
  hebdomadaire dans sa fenêtre, puis déficit prédictif. Les bornes hebdomadaires
  HH:MM sont respectées ; l'inclusion de la minute de fin suit le code existant.
- La quantité stockée et le prélèvement réseau estimé sont distincts : la
  constante prudente `CHARGE_EFFICIENCY` de l'intégration est réutilisée.
- La faisabilité tient compte des plafonds configurés de la batterie, de la
  phase et du contrat réseau, après la consommation prévue du foyer. Un seul
  budget réseau est partagé, sans créditer les exports prévus d'autres phases.

## Profil de consommation par phase

Un profil de parts de consommation, propre à cette vue et à chaque entrée HA,
est conservé sur 14 jours. Il ne modifie pas les profils ni les consignes du
contrôleur. La charge domestique est reconstruite à partir des compteurs de
phase, de la puissance AC des batteries et du solaire global.

**Sans mesure PV par phase, le solaire est supposé réparti par tiers.** Cette
hypothèse est affichée. Le profil n'est pas alimenté avec des mesures absentes,
périmées ou incohérentes, et les interruptions ne sont pas remplies de zéros.

Chaque quart d'heure utilise des observations de jours précédents lorsqu'elles
existent ; trois jours suffisamment couverts donnent le libellé « appris ».
Sinon, le repli est explicitement signalé : profil partiel, parts récentes,
parts instantanées, puis parts égales. La confiance initiale est donc faible.
Le changement d'affectation des batteries ou des compteurs invalide ce profil ;
un simple réglage de Kp ne l'invalide pas.

## Limites importantes

- Première version destinée à une flotte complète de trois batteries affectées
  à L1/L2/L3. Les mappings incomplets, ambigus et flottes mixtes sont signalés.
- Le déficit prédictif utilise le mode **Time Slot**. Les calendriers de prix
  dynamiques et prix temps réel ne sont pas remplacés par un faux plan Tempo.
- Il s'agit d'une projection de recharge : la consommation nocturne est supposée
  alimentée par le réseau. Le **SOC max estimé** n'est pas une garantie de SOC
  à 06:00 si le contrôleur décharge après avoir atteint une cible. Les phases
  peuvent également avoir un besoin propre que le seul budget global ne couvre
  pas : l'état `phase_constraint` le rend visible sans accroître ce budget.
- La puissance chargeable est une **borne supérieure sous les plafonds connus**.
  Température, BMS, rampe, réévaluation du contrôleur, tarification, blocages
  temporaires et délai solaire hebdomadaire peuvent encore la réduire. Pas de
  garantie d'heure précise d'achèvement ni de simulation complète de la boucle PD.
- La Pause VE est affichée comme suspension ; le mode manuel et les autres
  propriétaires sont exclus. La projection future suppose les modes observés
  maintenus et n'invente pas les interventions futures d'un utilisateur.
- Après 22 h, le prochain créneau vise le jour après demain. Si le fournisseur
  ne publie pas encore sa courbe, ce créneau reste indisponible, tandis que le
  créneau en cours peut être calculé normalement.
- Mise à jour toutes les 60 s, et sur les mises à jour batterie regroupées avec
  un espacement d'environ 30 s. Les gros attributs ne sont pas enregistrés par
  Recorder. Le petit profil par phase dispose de son propre Store.

## Frontend et compatibilité

Le module `frontend/tempo-preview.js` est servi par le chemin statique existant
et enregistré dans les modules supplémentaires HA. Il ajoute seulement une vue
au panneau 1.6.0, sans remplacer son fichier principal. Deux points d'extension
privés du panneau (`_renderControl` et `_patchControl`) sont utilisés ; leur
contrat et le sélecteur `diagPredictive` devront être revérifiés à chaque backport
majeur du frontend. Aucun service HA n'est appelé par le module. Plusieurs
entrées de configuration sont rendues séparément.

## Validation

```bash
python -m pip install pytest
python scripts/test_tempo_preview_isolated.py
node --check custom_components/omnibattery/frontend/tempo-preview.js
```

Avec Python 3.14 et les dépendances HA :

```bash
python -m pip install -r requirements-test.txt
python -m pytest -p no:homeassistant tests/test_tempo_preview.py \
  tests/test_tempo_preview_adapter.py tests/test_tempo_preview_sensor.py
```

Le workflow **Tempo preview validation** contient un job pur et un job de
contrats avec les dépendances HA réelles ; les nouveaux tests sont également
collectés par le workflow général. Les fixtures ne commandent aucun matériel.

Validation locale lors de l'implémentation : **48 tests purs**, **26 contrats
adaptateur**, **6 contrats de cycle de vie**, tous réussis sous Python 3.13.
Les deux dernières séries utilisent localement des frontières HA/upstream
simulées ; elles ne constituent pas une exécution complète de Home Assistant.
Le rendu a aussi été vérifié dans Chromium sur ordinateur et mobile avec des
états synthétiques : indépendance des entrées, valeurs nulles distinctes de
zéro, échappement des noms d'appareils et absence d'appels de services.
Aucun fonctionnement matériel ni succès de la CI distante n'est présumé.
