# Per-phase PD — experimental build (not production-certified)

This branch contains the opt-in three-phase PD controller implementation.

## Configuration in Home Assistant

1. Back up Home Assistant and use a **test** instance or restorable setup.
2. Locate `PD L1 battery`, `PD L1 grid sensor`, etc. under Omnibattery entities.
   The battery name must exactly match the coordinator name within Omnibattery.
   Each grid sensor must be a Home Assistant `sensor.*` entity reporting
   **W or kW**, positive for import and negative for export.
3. Configure L1, L2 and L3 while their enable switches are off.
4. Review target, deadband, Kp, Kd and maximum command step per phase.
5. Do not enable on real hardware without staged validation.

The channels are **disabled by default**. Phase control attempts a zero-Watt
handoff before further commands. Commands use the existing battery writer.

## Isolated unit tests

```bash
python -m pip install pytest pytest-asyncio
python scripts/test_phase_isolated.py
```

These tests are not substitutes for comprehensive Home Assistant, driver,
manual override, stale telemetry, shutdown or hardware tests.

## Rollback

Disable phase switches before reverting to the stable Omnibattery version.
Verify setpoints at zero watts and actual AC power with the Shelly meter.


## Fallback des capteurs Shelly Pro 3EM

Pour les trois entrées MQTT définies ci-dessous, Omnibattery utilise la mesure
MQTT tant qu'elle est disponible, numérique, exprimée en W ou kW et âgée de
15 secondes au maximum. Sinon, le système essaie le capteur Shelly natif
correspondant, soumis aux mêmes contrôles de validité :

| Phase | Primaire MQTT | Secours Shelly natif |
| --- | --- | --- |
| L1 | `sensor.shelly_puissance_l1` | `sensor.shellypro3em_841fe8904c10_phase_a_puissance` |
| L2 | `sensor.shelly_puissance_l2` | `sensor.shellypro3em_841fe8904c10_phase_b_puissance` |
| L3 | `sensor.shelly_puissance_l3` | `sensor.shellypro3em_841fe8904c10_phase_c_puissance` |

Le secours automatique ne s'applique qu'aux trois ID MQTT exacts.
Pour tout autre capteur primaire, il n'y a pas de secours implicite.
Si les deux sources sont invalides, le contrôleur ne doit pas utiliser
de mesure pour calculer une nouvelle correction.

Le diagnostic `sensor.pd_l1_dry_run` (et L2/L3) expose
`meter_source: primary | fallback | unavailable`, `active_grid_sensor`,
`primary_meter_error` et `fallback_meter_error`.
Les deux sources doivent avoir la même convention de signe
(**positif = importation réseau**) et mesurer le même point électrique.
C'est indispensable avant toute activation du PD réel.


## Performance du PD par phase : réseau brut et régulation autorisée

Les entités `sensor.pd_l1_performance`, `sensor.pd_l2_performance` et
`sensor.pd_l3_performance` exposent des fenêtres glissantes de 5/15/60 min.

* **Réseau brut** : `rms_error_w`, `mean_abs_error_w`,
  `within_deadband_pct` ; toutes les mesures valides lorsque le PD est activé,
  y compris lorsque la batterie ne peut pas agir.
* **Régulation autorisée** : `eligible_rms_error_w`,
  `eligible_mean_abs_error_w`, `eligible_within_deadband_pct` ;
  uniquement les échantillons pour lesquels les protections Omnibattery
  autorisent une correction dans le sens nécessaire.
* **Transparence** : `eligible_samples`, `excluded_samples`,
  `eligible_pct` et `excluded_reasons` montrent les échantillons
  exclus. Aucun échantillon autorisé donne `null`, pas `0 W`.
* **Blocage instantané** : `regulation_eligible` et
  `regulation_block_reason` dans chaque `sensor.pd_lN_dry_run`.
  Les raisons SOC proviennent des bloqueurs réels du contrôleur.
* Dans la bande morte, la mesure reste comptée comme admissible : aucune
  correction n'est nécessaire à cet instant.

Les pourcentages représentent la proportion **d'échantillons**, non une durée
exacte pondérée. Les statistiques ne garantissent pas qu'une consigne soit
physiquement délivrée et sont effacées au rechargement de l'intégration.
Cette instrumentation est en lecture seule et ne change pas les commandes PD.
