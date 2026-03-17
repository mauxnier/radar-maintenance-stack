#!/usr/bin/env python3
"""
Simulateur de données radar — maintenance dashboard
Génère des métriques réalistes avec bruit, dérives thermiques, alarmes
et les insère dans TimescaleDB toutes les secondes.

Usage:
    pip install psycopg2-binary
    python simulator.py

    # Avec options
    python simulator.py --interval 2 --radar-id RADAR-01 --dsn "postgresql://user:pass@localhost:5432/radar"
"""

import argparse
import math
import random
import time
import signal
import sys
from datetime import datetime, timezone, timedelta

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("Installez psycopg2 : pip install psycopg2-binary")
    sys.exit(1)

# ----------------------------------------------------------------
#  Configuration par défaut
# ----------------------------------------------------------------
DEFAULT_DSN      = "postgresql://postgres:postgres@localhost:5432/radar"
DEFAULT_INTERVAL = 1.0   # secondes entre chaque INSERT
DEFAULT_RADAR_ID = "RADAR-01"

# ----------------------------------------------------------------
#  État interne du simulateur
# ----------------------------------------------------------------
class RadarState:
    def __init__(self):
        self.t = 0.0                  # temps simulation en secondes

        # Rotation
        self.rotation_state = "rotating"
        self.rpm            = 6.0     # RPM nominal
        self.azimuth        = 0.0     # degrés
        self.cumul_rotations = 0
        self._rotation_timer = 0.0
        self._fixed_duration = 0.0

        # Thermique — valeurs de base
        self.temp_cabin_base       = 28.0
        self.temp_motor_base       = 45.0
        self.temp_transmitter_base = 38.0
        self.temp_external_base    = 15.0

        # Dérive thermique lente (simulation de montée en température)
        self._thermal_drift = 0.0
        self._drift_dir     = 1

        # Alimentation
        self.gen1_state = "running"
        self.gen2_state = "running"
        self._gen_fault_countdown = 0
        self._gen2_fault_countdown = 0

        # Alarmes actives
        self.active_alarms = 0

        # Uptime (commence à une valeur aléatoire réaliste)
        self.uptime_hours = random.uniform(10, 500)

        # État opérationnel
        self.operational_state = "OPERATIONAL"

        # Compteurs
        self.inserts_total = 0

        # ---- Injection de pannes (fault injector) ----
        self._fault_overrides = {}   # dict des overrides actifs
        self._fault_expires   = None # datetime UTC d'expiration

    def apply_fault(self, command: str, expires_at):
        """Applique un scénario de panne injecté manuellement."""
        self._fault_expires = expires_at
        self._fault_overrides = {}
        if command == "gen1_fault":
            self._fault_overrides["gen1_fault"] = True
        elif command == "gen2_fault":
            self._fault_overrides["gen2_fault"] = True
        elif command == "blackout":
            self._fault_overrides["gen1_fault"] = True
            self._fault_overrides["gen2_fault"] = True
        elif command == "overheat":
            self._fault_overrides["overheat"] = True
        elif command == "vswr_spike":
            self._fault_overrides["vswr_spike"] = True
        elif command == "antenna_stop":
            self._fault_overrides["antenna_stop"] = True
        elif command == "maintenance":
            self._fault_overrides["maintenance"] = True

    def clear_fault(self):
        """Remet tous les overrides à zéro (reset)."""
        self._fault_overrides = {}
        self._fault_expires   = None

    def tick(self, dt: float):
        """Avance l'état de dt secondes."""
        self.t += dt
        self.uptime_hours += dt / 3600.0

        # --- Rotation ---
        self._rotation_timer += dt
        if self.rotation_state == "rotating":
            # Toutes les 2 à 10 min, passer en fixe quelques secondes
            if self._rotation_timer > random.uniform(120, 600):
                self.rotation_state  = "fixed"
                self._fixed_duration = random.uniform(5, 30)
                self._rotation_timer = 0.0
            else:
                # Variation réaliste du RPM (±0.3)
                self.rpm = max(0.5, self.rpm + random.gauss(0, 0.05))
                self.rpm = min(self.rpm, 12.0)
                # Avance azimut
                deg_per_sec     = self.rpm * 360.0 / 60.0
                self.azimuth    = (self.azimuth + deg_per_sec * dt) % 360.0
                self.cumul_rotations = int(self.uptime_hours * self.rpm * 60)

        elif self.rotation_state == "fixed":
            self.rpm = 0.0
            if self._rotation_timer > self._fixed_duration:
                self.rotation_state  = "rotating"
                self.rpm             = 6.0
                self._rotation_timer = 0.0

        # --- Dérive thermique lente (cycle ~10 min) ---
        self._thermal_drift = 8.0 * math.sin(self.t / 600.0 * math.pi)

        # --- Génératrices ---
        if self._gen_fault_countdown > 0:
            self._gen_fault_countdown -= dt
            if self._gen_fault_countdown <= 0:
                self.gen1_state = "running"
                self._gen_fault_countdown = 0
        else:
            if random.random() < 0.0002:
                self.gen1_state           = "fault"
                self._gen_fault_countdown = random.uniform(10, 60)

        if self._gen2_fault_countdown > 0:
            self._gen2_fault_countdown -= dt
            if self._gen2_fault_countdown <= 0:
                self.gen2_state = "running"
                self._gen2_fault_countdown = 0
        else:
            if random.random() < 0.0002:
                self.gen2_state            = "fault"
                self._gen2_fault_countdown = random.uniform(10, 60)

        # --- Overrides d'injection de pannes ---
        now_utc = datetime.now(timezone.utc)
        if self._fault_expires and now_utc >= self._fault_expires:
            self.clear_fault()

        if self._fault_overrides.get("gen1_fault"):
            self.gen1_state = "fault"
            # empêche le countdown normal de rétablir gen1
            if self._gen_fault_countdown <= 0:
                self._gen_fault_countdown = 1
        if self._fault_overrides.get("gen2_fault"):
            self.gen2_state = "fault"
            if self._gen2_fault_countdown <= 0:
                self._gen2_fault_countdown = 1
        if self._fault_overrides.get("antenna_stop"):
            self.rotation_state = "fixed"
            self.rpm = 0.0

        # --- Alarmes ---
        self.active_alarms = 0
        if self.gen1_state == "fault":
            self.active_alarms += 1
        if self.gen2_state == "fault":
            self.active_alarms += 1
        if self.temp_cabin() > 55:
            self.active_alarms += 1
        if self.humidity_internal() > 80:
            self.active_alarms += 1
        if self.vswr() > 2.0:
            self.active_alarms += 1

        # --- État opérationnel ---
        if self._fault_overrides.get("maintenance"):
            self.operational_state = "MAINTENANCE"
        elif self.active_alarms == 0:
            self.operational_state = "OPERATIONAL"
        elif self.active_alarms == 1:
            self.operational_state = "DEGRADED"
        else:
            self.operational_state = "FAULT"

    # --- Métriques calculées avec bruit gaussien ---

    def noise(self, sigma=0.1):
        return random.gauss(0, sigma)

    def temp_cabin(self):
        v = self.temp_cabin_base + self._thermal_drift + self.noise(0.3)
        if self.gen1_state == "fault":
            v += 5.0  # montée thermique si faute
        if self._fault_overrides.get("overheat"):
            v += 20.0  # surchauffe injectée
        return round(v, 2)

    def temp_motor(self):
        extra = 10.0 if self.rotation_state == "rotating" else 0.0
        return round(self.temp_motor_base + self._thermal_drift * 0.6 + extra + self.noise(0.5), 2)

    def temp_transmitter(self):
        return round(self.temp_transmitter_base + self._thermal_drift * 0.8 + self.noise(0.4), 2)

    def temp_external(self):
        # Cycle jour/nuit sur 24h
        hour_angle = (self.t % 86400) / 86400 * 2 * math.pi
        daily = 8.0 * math.sin(hour_angle - math.pi / 2)
        return round(self.temp_external_base + daily + self.noise(0.2), 2)

    def humidity_internal(self):
        v = 35.0 + 10.0 * math.sin(self.t / 1800.0) + self.noise(1.0)
        return round(max(5.0, min(95.0, v)), 1)

    def humidity_external(self):
        v = 60.0 + 15.0 * math.sin(self.t / 3600.0) + self.noise(2.0)
        return round(max(10.0, min(99.0, v)), 1)

    def vibration_g(self):
        base = 0.3 if self.rotation_state == "rotating" else 0.05
        return round(abs(base + self.noise(0.04)), 3)

    def gen1_power_w(self):
        if self.gen1_state == "fault":
            return 0.0
        base = 4500.0 if self.rotation_state == "rotating" else 2800.0
        return round(base + self.noise(80), 1)

    def gen1_voltage_v(self):
        if self.gen1_state == "fault":
            return 0.0
        return round(230.0 + self.noise(2.0), 2)

    def gen1_current_a(self):
        return round(self.gen1_power_w() / max(self.gen1_voltage_v(), 1), 2)

    def gen1_freq_hz(self):
        if self.gen1_state == "fault":
            return 0.0
        return round(50.0 + self.noise(0.05), 3)

    def gen2_power_w(self):
        if self.gen2_state == "fault":
            return 0.0
        if self.gen2_state != "running":
            return None
        base = 4200.0 if self.rotation_state == "rotating" else 2600.0
        return round(base + self.noise(80), 1)

    def gen2_voltage_v(self):
        if self.gen2_state == "fault":
            return 0.0
        if self.gen2_state != "running":
            return None
        return round(230.0 + self.noise(2.0), 2)

    def gen2_current_a(self):
        p = self.gen2_power_w()
        v = self.gen2_voltage_v()
        if p is None or v is None:
            return None
        return round(p / max(v, 1), 2)

    def gen2_freq_hz(self):
        if self.gen2_state == "fault":
            return 0.0
        if self.gen2_state != "running":
            return None
        return round(50.0 + self.noise(0.05), 3)

    def rf_power_peak_w(self):
        if self.operational_state == "FAULT":
            return round(random.uniform(0, 500), 1)
        return round(25000.0 + self.noise(300), 1)

    def rf_power_avg_w(self):
        peak = self.rf_power_peak_w()
        duty = 0.10  # duty cycle 10%
        return round(peak * duty + self.noise(20), 1)

    def vswr(self):
        if self._fault_overrides.get("vswr_spike"):
            return round(2.5 + abs(self.noise(0.15)), 3)  # spike injecté
        # Normalement proche de 1.05, pic aléatoire possible
        base = 1.05 + abs(self.noise(0.02))
        spike = 0
        if random.random() < 0.001:  # 0.1% de chance de spike
            spike = random.uniform(0.5, 2.0)
        return round(min(4.0, base + spike), 3)


# ----------------------------------------------------------------
#  Fonction d'insertion
# ----------------------------------------------------------------
INSERT_SQL = """
INSERT INTO radar_metrics (
    time, radar_id,
    rpm, azimuth_deg, rotation_state, cumul_rotations,
    vibration_g,
    temp_cabin, temp_motor, temp_transmitter, temp_external,
    humidity_internal, humidity_external,
    gen1_power_w, gen1_voltage_v, gen1_current_a, gen1_freq_hz, gen1_state,
    gen2_power_w, gen2_voltage_v, gen2_current_a, gen2_freq_hz, gen2_state,
    rf_power_peak_w, rf_power_avg_w, vswr,
    operational_state, active_alarms, uptime_hours
) VALUES (
    %(time)s, %(radar_id)s,
    %(rpm)s, %(azimuth_deg)s, %(rotation_state)s, %(cumul_rotations)s,
    %(vibration_g)s,
    %(temp_cabin)s, %(temp_motor)s, %(temp_transmitter)s, %(temp_external)s,
    %(humidity_internal)s, %(humidity_external)s,
    %(gen1_power_w)s, %(gen1_voltage_v)s, %(gen1_current_a)s, %(gen1_freq_hz)s, %(gen1_state)s,
    %(gen2_power_w)s, %(gen2_voltage_v)s, %(gen2_current_a)s, %(gen2_freq_hz)s, %(gen2_state)s,
    %(rf_power_peak_w)s, %(rf_power_avg_w)s, %(vswr)s,
    %(operational_state)s, %(active_alarms)s, %(uptime_hours)s
)
"""

INSERT_ALARM_SQL = """
INSERT INTO radar_alarms (time, radar_id, alarm_code, severity, metric_name, metric_value, threshold, message)
VALUES (%(time)s, %(radar_id)s, %(alarm_code)s, %(severity)s, %(metric_name)s, %(metric_value)s, %(threshold)s, %(message)s)
"""


def check_and_apply_commands(conn, state: RadarState, radar_id: str):
    """Vérifie les commandes en attente et les applique au state du simulateur."""
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, command, duration_s
                FROM radar_commands
                WHERE radar_id = %s AND applied_at IS NULL
                ORDER BY created_at DESC LIMIT 1
            """, (radar_id,))
            row = cur.fetchone()
            if not row:
                return
            cmd_id, command, duration_s = row
            now = datetime.now(timezone.utc)

            if command == "reset":
                state.clear_fault()
                expires_at = now
            else:
                expires_at = now + timedelta(seconds=duration_s)
                state.apply_fault(command, expires_at)

            cur.execute("""
                UPDATE radar_commands
                SET applied_at = %s, expires_at = %s
                WHERE id = %s
            """, (now, expires_at, cmd_id))
        conn.commit()
        print(f"[Fault Injector] Commande appliquée : {command} (durée {duration_s}s)")
    except Exception as e:
        conn.rollback()
        # Table peut ne pas encore exister au premier démarrage
        if "radar_commands" not in str(e):
            print(f"[ERREUR] check_commands: {e}")


def build_row(state: RadarState, radar_id: str) -> dict:
    return {
        "time":             datetime.now(timezone.utc),
        "radar_id":         radar_id,
        "rpm":              state.rpm,
        "azimuth_deg":      round(state.azimuth, 2),
        "rotation_state":   state.rotation_state,
        "cumul_rotations":  state.cumul_rotations,
        "vibration_g":      state.vibration_g(),
        "temp_cabin":       state.temp_cabin(),
        "temp_motor":       state.temp_motor(),
        "temp_transmitter": state.temp_transmitter(),
        "temp_external":    state.temp_external(),
        "humidity_internal": state.humidity_internal(),
        "humidity_external": state.humidity_external(),
        "gen1_power_w":     state.gen1_power_w(),
        "gen1_voltage_v":   state.gen1_voltage_v(),
        "gen1_current_a":   state.gen1_current_a(),
        "gen1_freq_hz":     state.gen1_freq_hz(),
        "gen1_state":       state.gen1_state,
        "gen2_power_w":     state.gen2_power_w(),
        "gen2_voltage_v":   state.gen2_voltage_v(),
        "gen2_current_a":   state.gen2_current_a(),
        "gen2_freq_hz":     state.gen2_freq_hz(),
        "gen2_state":       state.gen2_state,
        "rf_power_peak_w":  state.rf_power_peak_w(),
        "rf_power_avg_w":   state.rf_power_avg_w(),
        "vswr":             state.vswr(),
        "operational_state": state.operational_state,
        "active_alarms":    state.active_alarms,
        "uptime_hours":     round(state.uptime_hours, 4),
    }


def check_and_insert_alarms(cur, state: RadarState, row: dict, radar_id: str):
    """Insère une alarme si un seuil critique est franchi."""
    checks = [
        ("temp_cabin",       row["temp_cabin"],       55.0, 70.0,  "TEMP_CABIN",       "°C", "Température cabine"),
        ("temp_motor",       row["temp_motor"],        70.0, 90.0,  "TEMP_MOTOR",       "°C", "Température moteur"),
        ("humidity_internal", row["humidity_internal"], 80.0, 95.0, "HUMIDITY_HIGH",    "%",  "Hygrométrie interne"),
        ("vswr",             row["vswr"],               2.0,  3.0,  "VSWR_HIGH",        "",   "VSWR antenne"),
    ]
    now = row["time"]
    for metric, value, warn_thr, crit_thr, code, unit, label in checks:
        if value is None:
            continue
        if value >= crit_thr:
            cur.execute(INSERT_ALARM_SQL, {
                "time": now, "radar_id": radar_id,
                "alarm_code": f"{code}_CRIT", "severity": "CRITICAL",
                "metric_name": metric, "metric_value": value,
                "threshold": crit_thr,
                "message": f"{label} = {value}{unit} (seuil CRITIQUE {crit_thr}{unit})"
            })
        elif value >= warn_thr:
            cur.execute(INSERT_ALARM_SQL, {
                "time": now, "radar_id": radar_id,
                "alarm_code": f"{code}_WARN", "severity": "WARNING",
                "metric_name": metric, "metric_value": value,
                "threshold": warn_thr,
                "message": f"{label} = {value}{unit} (seuil WARNING {warn_thr}{unit})"
            })


# ----------------------------------------------------------------
#  Boucle principale
# ----------------------------------------------------------------
def run(dsn: str, interval: float, radar_id: str):
    print(f"[Simulateur] Connexion à {dsn}")
    conn = psycopg2.connect(dsn)
    conn.autocommit = False

    state = RadarState()
    running = True

    def on_sigint(sig, frame):
        nonlocal running
        print("\n[Simulateur] Arrêt...")
        running = False

    signal.signal(signal.SIGINT, on_sigint)
    signal.signal(signal.SIGTERM, on_sigint)

    print(f"[Simulateur] Démarré — radar={radar_id}, interval={interval}s")
    print("[Simulateur] Ctrl+C pour arrêter\n")

    while running:
        loop_start = time.monotonic()

        state.tick(interval)
        check_and_apply_commands(conn, state, radar_id)
        row = build_row(state, radar_id)

        try:
            with conn.cursor() as cur:
                cur.execute(INSERT_SQL, row)
                check_and_insert_alarms(cur, state, row, radar_id)
            conn.commit()
            state.inserts_total += 1

            # Log console toutes les 10 insertions
            if state.inserts_total % 10 == 0:
                print(
                    f"[{row['time'].strftime('%H:%M:%S')}] "
                    f"state={row['operational_state']:12s} "
                    f"rpm={row['rpm']:5.1f} "
                    f"T_cabin={row['temp_cabin']:5.1f}°C "
                    f"gen1={row['gen1_power_w']:6.0f}W "
                    f"alarms={row['active_alarms']} "
                    f"[total={state.inserts_total}]"
                )

        except Exception as e:
            conn.rollback()
            print(f"[ERREUR] Insert failed: {e}")

        # Attendre le reste de l'intervalle
        elapsed = time.monotonic() - loop_start
        sleep_time = max(0, interval - elapsed)
        if sleep_time > 0:
            time.sleep(sleep_time)

    conn.close()
    print(f"[Simulateur] Terminé — {state.inserts_total} insertions effectuées")


# ----------------------------------------------------------------
#  Entrypoint
# ----------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Simulateur de données radar pour TimescaleDB")
    parser.add_argument("--dsn",       default=DEFAULT_DSN,      help="DSN PostgreSQL")
    parser.add_argument("--interval",  default=DEFAULT_INTERVAL, type=float, help="Intervalle en secondes (défaut: 1)")
    parser.add_argument("--radar-id",  default=DEFAULT_RADAR_ID, help="Identifiant du radar")
    args = parser.parse_args()

    run(dsn=args.dsn, interval=args.interval, radar_id=args.radar_id)
