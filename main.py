import os
import sys
import time
import subprocess
import ctypes
import threading
import json
import urllib.parse
from ctypes import wintypes
from datetime import datetime
from http.server import BaseHTTPRequestHandler
import socketserver
import sqlite3
import customtkinter as ctk
from tkinter import messagebox

try:
    import win32com.client
    import pythoncom
except Exception:
    win32com = None
    pythoncom = None

try:
    import win32evtlog
except Exception:
    win32evtlog = None
import xml.etree.ElementTree as ET

# ========================================================================================
# CONFIGURAÇÕES DE TEMA E CORES PROFISSIONAIS (DARK SLATE / FINTECH)
# ========================================================================================
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

PORT = 15000
PC_NAME = os.environ.get("COMPUTERNAME", "MinhaRig")
TEMPO_DE_ESPERA = 600

THEME = {
    "bg_main": "#0B0F17",       # Fundo principal ultra escuro
    "bg_card": "#111827",       # Superfície dos cards
    "bg_subcard": "#0D131F",    # Sub-blocos internos
    "border": "#1E293B",        # Bordas finas (1px)
    "text_primary": "#F8FAFC",  # Branco gelo para números principais
    "text_sec": "#94A3B8",      # Cinza azulado para rótulos
    "text_muted": "#64748B",    # Cinza escuro para subtítulos
    "emerald": "#10B981",       # Verde sucesso / lucro
    "emerald_bg": "#064E3B",
    "blue": "#3B82F6",          # Azul informativo
    "amber": "#F59E0B",         # Alerta / Aviso
    "amber_bg": "#451A03",
    "crimson": "#EF4444",       # Erro / Custo / Crítico
    "crimson_bg": "#450A0A"
}

ultimo_restart = 0
httpd = None
app = None
restart_lock = threading.Lock()
balance_tracker_lock = threading.RLock()

BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
CONFIG_FILE = os.path.join(BASE_DIR, 'config_energia.json')
LOG_FILE = os.path.join(BASE_DIR, 'motor_kryptex.log')
KRYPTEX_DB_FILE = os.path.expandvars(r'%APPDATA%\Kryptex\kryptex.db')
BALANCE_TRACKER_DB = os.path.expandvars(
    r'%APPDATA%\Kryptex\motor_balance_panorama.db'
)
_instance_mutex_handle = None


def acquire_single_instance():
    global _instance_mutex_handle
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    mutex = kernel32.CreateMutexW(
        None, True, "Local\\KryptexMonitorProPanorama"
    )
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel32.CloseHandle(mutex)
        ctypes.WinDLL("user32").MessageBoxW(
            None,
            "O Panorama Kryptex já está aberto. Feche a outra janela antes de abrir novamente.",
            "Kryptex Monitor",
            0x40
        )
        return False
    _instance_mutex_handle = mutex
    return True


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class nvmlMemory_t(ctypes.Structure):
    _fields_ = [
        ('total', ctypes.c_ulonglong),
        ('free', ctypes.c_ulonglong),
        ('used', ctypes.c_ulonglong),
    ]


class NVMLMonitor:
    """Mantém o handle da NVML aberto sem reinicializar a DLL a cada leitura."""
    def __init__(self):
        self.nvml = None
        self.device = ctypes.c_void_p()
        self._init_nvml()

    def _init_nvml(self):
        try:
            self.nvml = ctypes.CDLL('nvml.dll')
            self.nvml.nvmlInit()
            self.nvml.nvmlDeviceGetHandleByIndex(0, ctypes.byref(self.device))
        except Exception:
            self.nvml = None

    def get_metrics(self):
        if not self.nvml:
            self._init_nvml()
        if self.nvml:
            try:
                mem = nvmlMemory_t()
                self.nvml.nvmlDeviceGetMemoryInfo(self.device, ctypes.byref(mem))
                vram_used_gb = mem.used / (1024**3)
                vram_total_gb = mem.total / (1024**3)
                vram_pct = (mem.used / mem.total * 100.0) if mem.total > 0 else 0.0

                temp = ctypes.c_uint()
                self.nvml.nvmlDeviceGetTemperature(self.device, 0, ctypes.byref(temp))

                power = ctypes.c_uint()
                self.nvml.nvmlDeviceGetPowerUsage(self.device, ctypes.byref(power))
                power_w = power.value / 1000.0

                pcie = ctypes.c_uint()
                self.nvml.nvmlDeviceGetPcieReplayCounter(self.device, ctypes.byref(pcie))

                return {
                    "vram_used_gb": vram_used_gb,
                    "vram_total_gb": vram_total_gb,
                    "vram_pct": vram_pct,
                    "temp_c": int(temp.value),
                    "power_w": power_w,
                    "pcie_replay": int(pcie.value)
                }
            except Exception:
                self.nvml = None
        return None


gpu_monitor = NVMLMonitor()


def get_ram_info():
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(stat)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    used_gb = (stat.ullTotalPhys - stat.ullAvailPhys) / (1024**3)
    total_gb = stat.ullTotalPhys / (1024**3)
    return {
        "percent": int(stat.dwMemoryLoad),
        "used_gb": used_gb,
        "total_gb": total_gb
    }


def get_gpu_health_and_power():
    metrics = gpu_monitor.get_metrics()
    if metrics:
        return metrics

    try:
        CREATE_NO_WINDOW = 0x08000000
        out = subprocess.check_output(
            ['nvidia-smi', '--query-gpu=power.draw,temperature.gpu,memory.used,memory.total', '--format=csv,noheader,nounits'],
            creationflags=CREATE_NO_WINDOW, encoding='utf-8', timeout=2
        )
        parts = [p.strip() for p in out.strip().split(',')]
        if len(parts) >= 4:
            p_w = float(parts[0])
            t_c = int(parts[1])
            m_u = float(parts[2]) / 1024.0
            m_t = float(parts[3]) / 1024.0
            return {
                "vram_used_gb": m_u,
                "vram_total_gb": m_t,
                "vram_pct": (m_u / m_t * 100.0) if m_t > 0 else 0.0,
                "temp_c": t_c,
                "power_w": p_w,
                "pcie_replay": 0
            }
    except Exception:
        pass

    return {
        "vram_used_gb": 0.0, "vram_total_gb": 0.0, "vram_pct": 0.0,
        "temp_c": 0, "power_w": 0.0, "pcie_replay": 0
    }


def get_cpu_power(wmi_obj=None):
    if wmi_obj is not None:
        try:
            for item in wmi_obj.InstancesOf('Win32_PerfFormattedData_PowerMeterCounter_PowerMeter'):
                if 'PKG' in item.Name:
                    val = float(item.Power) / 1000.0
                    if val > 0:
                        return val
        except Exception:
            pass
    return 45.0


def get_latest_hardware_record_id():
    if not win32evtlog:
        return 0
    query = '''
    <QueryList>
      <Query Id='0' Path='System'>
        <Select Path='System'>*[System[Provider[@Name='nvlddmkm' or @Name='Microsoft-Windows-WHEA-Logger']]]</Select>
      </Query>
    </QueryList>
    '''
    hQuery = None
    try:
        flags = win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryReverseDirection
        hQuery = win32evtlog.EvtQuery('System', flags, query)
        events = win32evtlog.EvtNext(hQuery, 1)
        if events:
            try:
                xml_str = win32evtlog.EvtRender(events[0], win32evtlog.EvtRenderEventXml)
                root = ET.fromstring(xml_str)
                ns = {'ns': 'http://schemas.microsoft.com/win/2004/08/events/event'}
                return int(root.find('ns:System/ns:EventRecordID', ns).text)
            finally:
                win32evtlog.EvtClose(events[0])
    except Exception:
        pass
    finally:
        if hQuery:
            try:
                win32evtlog.EvtClose(hQuery)
            except Exception:
                pass
    return 0


def check_new_hardware_errors(since_record_id):
    if not win32evtlog or since_record_id <= 0:
        return []
    query = f'''
    <QueryList>
      <Query Id='0' Path='System'>
        <Select Path='System'>*[System[(Provider[@Name='nvlddmkm' or @Name='Microsoft-Windows-WHEA-Logger']) and EventRecordID &gt; {since_record_id}]]</Select>
      </Query>
    </QueryList>
    '''
    results = []
    hQuery = None
    try:
        flags = win32evtlog.EvtQueryChannelPath | win32evtlog.EvtQueryReverseDirection
        hQuery = win32evtlog.EvtQuery('System', flags, query)
        events = win32evtlog.EvtNext(hQuery, 10)
        for evt in events:
            try:
                xml_str = win32evtlog.EvtRender(evt, win32evtlog.EvtRenderEventXml)
                root = ET.fromstring(xml_str)
                ns = {'ns': 'http://schemas.microsoft.com/win/2004/08/events/event'}
                rec_id = int(root.find('ns:System/ns:EventRecordID', ns).text)
                provider = root.find('ns:System/ns:Provider', ns).attrib.get('Name')
                event_id = root.find('ns:System/ns:EventID', ns).text
                results.append({'record_id': rec_id, 'provider': provider, 'event_id': event_id})
            finally:
                win32evtlog.EvtClose(evt)
    except Exception:
        pass
    finally:
        if hQuery:
            try:
                win32evtlog.EvtClose(hQuery)
            except Exception:
                pass
    return results


def get_kryptex_local_data():
    db_path = KRYPTEX_DB_FILE
    if not os.path.exists(db_path):
        return {"online": False, "reason": "Banco de dados do Kryptex não encontrado"}

    conn = None
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=1.0)
        cur = conn.cursor()

        cur.execute("SELECT id, timestamp FROM reading ORDER BY id DESC LIMIT 1")
        row_r = cur.fetchone()
        if not row_r:
            return {"online": False, "reason": "Sem leituras registradas no Kryptex"}

        reading_id, reading_ts = row_r
        age_s = (time.time() * 1000 - reading_ts) / 1000.0
        if age_s > 45:
            return {"online": False, "reason": f"Sem atualização no Kryptex há {int(age_s)}s"}

        cur.execute("SELECT id, pid, running_at, stopped_at, exit_error FROM process ORDER BY id DESC LIMIT 1")
        row_p = cur.fetchone()
        if not row_p or row_p[3] is not None or row_p[4] is not None:
            return {"online": False, "reason": "Processo minerador parado ou com erro"}

        cur.execute("""
            SELECT coin_algorithm_id, hashrate, shares_accepted_total, shares_rejected_total 
            FROM process_device_reading 
            WHERE reading_id = ?
        """, (reading_id,))
        row_pdr = cur.fetchone()
        if not row_pdr or row_pdr[1] <= 0:
            return {"online": False, "reason": "Hashrate zerado"}

        coin_algo_id, hashrate, shares_acc, shares_rej = row_pdr

        total_shares = (shares_acc or 0) + (shares_rej or 0)
        rejection_rate = (shares_rej / total_shares * 100.0) if total_shares > 0 else 0.0

        cur.execute("SELECT profitability FROM coin_algorithm_profitability WHERE coin_algorithm_id = ?", (coin_algo_id,))
        prof_row = cur.fetchone()
        prof_factor = prof_row[0] if prof_row else 0.0

        cur.execute("SELECT rate FROM currency_rate WHERE currency_id = 14")
        brl_row = cur.fetchone()
        if not brl_row or brl_row[0] <= 0:
            return {"online": False, "reason": "Cotação do real indisponível"}
        brl_rate = brl_row[0]

        profit_usd = hashrate * prof_factor
        profit_brl = profit_usd * brl_rate

        cur.execute("SELECT total, withdrawable, updated_at FROM balance LIMIT 1")
        bal_row = cur.fetchone()
        if bal_row:
            saldo_total_usd = bal_row[0] or 0.0
            saldo_withdrawable_usd = bal_row[1] or 0.0
            saldo_total_brl = saldo_total_usd * brl_rate
            saldo_withdrawable_brl = saldo_withdrawable_usd * brl_rate
        else:
            saldo_total_usd = 0.0
            saldo_withdrawable_usd = 0.0
            saldo_total_brl = 0.0
            saldo_withdrawable_brl = 0.0

        return {
            "online": True,
            "reason": "OK",
            "age_s": age_s,
            "hashrate": hashrate,
            "shares_acc": shares_acc or 0,
            "shares_rej": shares_rej or 0,
            "total_shares": total_shares,
            "rejection_rate": rejection_rate,
            "profit_usd": profit_usd,
            "profit_brl": profit_brl,
            "brl_rate": brl_rate,
            "saldo_total_usd": saldo_total_usd,
            "saldo_total_brl": saldo_total_brl,
            "saldo_withdrawable_usd": saldo_withdrawable_usd,
            "saldo_withdrawable_brl": saldo_withdrawable_brl
        }
    except Exception as e:
        return {"online": False, "reason": str(e)}
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def read_balance_tracking_sample():
    if not os.path.exists(KRYPTEX_DB_FILE):
        raise FileNotFoundError("Banco de dados do Kryptex não encontrado")

    conn = sqlite3.connect(
        f'file:{KRYPTEX_DB_FILE}?mode=ro', uri=True, timeout=1.0
    )
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT total, withdrawable FROM balance ORDER BY id DESC LIMIT 1"
        )
        balance_row = cur.fetchone()
        if not balance_row:
            raise RuntimeError("Saldo não encontrado no banco do Kryptex")

        cur.execute(
            "SELECT rate FROM currency_rate WHERE currency_id = 14"
        )
        brl_row = cur.fetchone()
        cur.execute(
            "SELECT rate FROM currency_rate WHERE currency_id = 2"
        )
        usd_row = cur.fetchone()
        if not brl_row or not usd_row or brl_row[0] <= 0 or usd_row[0] <= 0:
            raise RuntimeError("Cotação USD/BRL não encontrada ou inválida")

        expected_usd_per_day = 0.0
        cur.execute("SELECT id, timestamp FROM reading ORDER BY id DESC LIMIT 1")
        reading_row = cur.fetchone()
        cur.execute(
            "SELECT stopped_at, exit_error FROM process ORDER BY id DESC LIMIT 1"
        )
        process_row = cur.fetchone()

        if (
            reading_row
            and process_row
            and process_row[0] is None
            and process_row[1] is None
            and (time.time() * 1000 - reading_row[1]) <= 45_000
        ):
            cur.execute(
                """
                SELECT coin_algorithm_id, SUM(hashrate)
                FROM process_device_reading
                WHERE reading_id = ? AND hashrate > 0
                GROUP BY coin_algorithm_id
                """,
                (reading_row[0],)
            )
            hashrates = cur.fetchall()
            cur.execute(
                "SELECT coin_algorithm_id, profitability "
                "FROM coin_algorithm_profitability"
            )
            profitability = dict(cur.fetchall())

            expected_usd_per_day = sum(
                hashrate * profitability[algorithm_id] * usd_row[0]
                for algorithm_id, hashrate in hashrates
                if algorithm_id in profitability
            )

        return {
            "observed_at": int(time.time() * 1000),
            "total_usd": float(balance_row[0]),
            "withdrawable_usd": float(balance_row[1]),
            "brl_rate": float(brl_row[0]),
            "expected_usd_per_day": expected_usd_per_day
        }
    finally:
        conn.close()


def _ensure_balance_tracker_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tracker_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_total_usd REAL NOT NULL,
            last_sample_at INTEGER NOT NULL,
            pending_expected_usd REAL NOT NULL DEFAULT 0,
            tracking_started_at INTEGER NOT NULL,
            initial_total_usd REAL NOT NULL,
            initial_brl_rate REAL NOT NULL,
            fx_baseline_total_usd REAL NOT NULL,
            fx_baseline_brl_rate REAL NOT NULL,
            fx_baseline_flow_value_brl REAL NOT NULL DEFAULT 0
        )
        """
    )
    state_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(tracker_state)")
    }
    if "fx_baseline_total_usd" not in state_columns:
        conn.execute(
            "ALTER TABLE tracker_state ADD COLUMN "
            "fx_baseline_total_usd REAL NOT NULL DEFAULT 0"
        )
        conn.execute(
            "UPDATE tracker_state SET fx_baseline_total_usd = initial_total_usd"
        )
    if "fx_baseline_brl_rate" not in state_columns:
        conn.execute(
            "ALTER TABLE tracker_state ADD COLUMN "
            "fx_baseline_brl_rate REAL NOT NULL DEFAULT 0"
        )
        conn.execute(
            "UPDATE tracker_state SET fx_baseline_brl_rate = initial_brl_rate"
        )
    if "fx_baseline_flow_value_brl" not in state_columns:
        conn.execute(
            "ALTER TABLE tracker_state ADD COLUMN "
            "fx_baseline_flow_value_brl REAL NOT NULL DEFAULT 0"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS balance_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at INTEGER NOT NULL,
            local_day TEXT NOT NULL,
            previous_total_usd REAL NOT NULL,
            total_usd REAL NOT NULL,
            delta_usd REAL NOT NULL,
            brl_rate REAL NOT NULL,
            expected_usd_since_previous REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_balance_events_day "
        "ON balance_events(local_day)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS minute_expectations (
            minute_start INTEGER PRIMARY KEY,
            local_day TEXT NOT NULL,
            expected_usd REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_minute_expectations_day "
        "ON minute_expectations(local_day)"
    )


def record_balance_tracking_sample(sample, tracker_db_path=BALANCE_TRACKER_DB):
    with balance_tracker_lock:
        return _record_balance_tracking_sample(sample, tracker_db_path)


def _record_balance_tracking_sample(sample, tracker_db_path):
    os.makedirs(os.path.dirname(os.path.abspath(tracker_db_path)), exist_ok=True)
    conn = sqlite3.connect(tracker_db_path, timeout=2.0)
    try:
        _ensure_balance_tracker_schema(conn)
        now_ms = sample["observed_at"]
        local_day = datetime.fromtimestamp(now_ms / 1000).astimezone().date().isoformat()
        minute_start = (now_ms // 60_000) * 60_000
        expected_rate = max(0.0, sample["expected_usd_per_day"])

        state = conn.execute(
            "SELECT last_total_usd, last_sample_at, pending_expected_usd, "
            "tracking_started_at "
            "FROM tracker_state WHERE id = 1"
        ).fetchone()

        if state is None:
            conn.execute(
                "INSERT INTO tracker_state "
                "(id, last_total_usd, last_sample_at, tracking_started_at, "
                "initial_total_usd, initial_brl_rate, fx_baseline_total_usd, "
                "fx_baseline_brl_rate, fx_baseline_flow_value_brl) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    sample["total_usd"], now_ms, now_ms,
                    sample["total_usd"], sample["brl_rate"],
                    sample["total_usd"], sample["brl_rate"]
                )
            )
            conn.commit()
            return {"baseline": True, "observed_at": now_ms}

        previous_usd, previous_at, pending_usd, _ = state
        elapsed_s = (now_ms - previous_at) / 1000.0
        if 0 < elapsed_s <= 5:
            expected_usd = expected_rate * elapsed_s / 86400.0
            pending_usd += expected_usd
            conn.execute(
                """
                INSERT INTO minute_expectations
                    (minute_start, local_day, expected_usd)
                VALUES (?, ?, ?)
                ON CONFLICT(minute_start) DO UPDATE SET
                    expected_usd = expected_usd + excluded.expected_usd
                """,
                (minute_start, local_day, expected_usd)
            )
        elif elapsed_s > 5 or elapsed_s <= 0:
            pending_usd = 0.0

        delta_usd = sample["total_usd"] - previous_usd
        event = None
        if abs(delta_usd) >= 0.00000001:
            conn.execute(
                """
                INSERT INTO balance_events (
                    observed_at, local_day, previous_total_usd, total_usd,
                    delta_usd, brl_rate, expected_usd_since_previous
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now_ms, local_day, previous_usd, sample["total_usd"],
                    delta_usd, sample["brl_rate"], pending_usd
                )
            )
            event = {
                "observed_at": now_ms,
                "delta_usd": delta_usd,
                "brl_rate": sample["brl_rate"],
                "total_usd": sample["total_usd"],
                "expected_usd": pending_usd
            }
            pending_usd = 0.0

        conn.execute(
            """
            UPDATE tracker_state SET
                last_total_usd = ?, last_sample_at = ?,
                pending_expected_usd = ?
            WHERE id = 1
            """,
            (sample["total_usd"], now_ms, pending_usd)
        )
        conn.commit()
        return event
    finally:
        conn.close()


def reset_balance_fx_variation(sample, tracker_db_path=BALANCE_TRACKER_DB):
    os.makedirs(os.path.dirname(os.path.abspath(tracker_db_path)), exist_ok=True)
    conn = sqlite3.connect(tracker_db_path, timeout=2.0)
    try:
        _ensure_balance_tracker_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        state = conn.execute(
            "SELECT 1 FROM tracker_state WHERE id = 1"
        ).fetchone()
        if state is None:
            now_ms = sample["observed_at"]
            conn.execute(
                """
                INSERT INTO tracker_state (
                    id, last_total_usd, last_sample_at, pending_expected_usd,
                    tracking_started_at, initial_total_usd, initial_brl_rate,
                    fx_baseline_total_usd, fx_baseline_brl_rate,
                    fx_baseline_flow_value_brl
                ) VALUES (1, ?, ?, 0, ?, ?, ?, ?, ?, 0)
                """,
                (
                    sample["total_usd"], now_ms, now_ms,
                    sample["total_usd"], sample["brl_rate"],
                    sample["total_usd"], sample["brl_rate"]
                )
            )

        flow_value_brl = conn.execute(
            "SELECT COALESCE(SUM(delta_usd * brl_rate), 0) "
            "FROM balance_events"
        ).fetchone()[0]
        conn.execute(
            """
            UPDATE tracker_state SET
                fx_baseline_total_usd = ?,
                fx_baseline_brl_rate = ?,
                fx_baseline_flow_value_brl = ?
            WHERE id = 1
            """,
            (
                sample["total_usd"], sample["brl_rate"], flow_value_brl
            )
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_balance_tracking_summary(
    current_sample, tracker_db_path=BALANCE_TRACKER_DB
):
    if not os.path.exists(tracker_db_path):
        return None

    conn = sqlite3.connect(
        f'file:{tracker_db_path}?mode=ro', uri=True, timeout=1.0
    )
    try:
        local_day = datetime.now().astimezone().date().isoformat()
        state = conn.execute(
            "SELECT tracking_started_at, initial_total_usd, initial_brl_rate, "
            "fx_baseline_total_usd, fx_baseline_brl_rate, "
            "fx_baseline_flow_value_brl "
            "FROM tracker_state WHERE id = 1"
        ).fetchone()
        if not state:
            return None

        net_usd, increases_usd, decreases_usd, flow_value_brl, event_count = conn.execute(
            """
            SELECT
                COALESCE(SUM(delta_usd), 0),
                COALESCE(SUM(CASE WHEN delta_usd > 0 THEN delta_usd ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN delta_usd < 0 THEN delta_usd ELSE 0 END), 0),
                COALESCE(SUM(delta_usd * brl_rate), 0),
                COUNT(*)
            FROM balance_events
            """
        ).fetchone()
        expected_usd = conn.execute(
            "SELECT COALESCE(SUM(expected_usd), 0) FROM minute_expectations"
        ).fetchone()[0]
        elapsed_seconds = max(
            0.0, (int(time.time() * 1000) - state[0]) / 1000.0
        )
        fx_change_brl = (
            current_sample["total_usd"] * current_sample["brl_rate"]
            - state[3] * state[4]
            - (flow_value_brl - state[5])
        )
        return {
            "local_day": local_day,
            "net_usd": net_usd,
            "increases_usd": increases_usd,
            "decreases_usd": decreases_usd,
            "expected_usd": expected_usd,
            "difference_usd": increases_usd - expected_usd,
            "adherence_pct": (
                increases_usd / expected_usd * 100.0
                if expected_usd > 0 and elapsed_seconds >= 300
                else None
            ),
            "fx_change_brl": fx_change_brl,
            "current_total_usd": current_sample["total_usd"],
            "current_brl_rate": current_sample["brl_rate"],
            "elapsed_seconds": elapsed_seconds,
            "event_count": event_count,
            "tracking_started_at": state[0]
        }
    finally:
        conn.close()


def stop_all_miners():
    CREATE_NO_WINDOW = 0x08000000
    # 1. Encerra cirurgicamente os PIDs ativos recentes registrados no banco
    conn = None
    try:
        db_path = KRYPTEX_DB_FILE
        if os.path.exists(db_path):
            conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=1.0)
            cur = conn.cursor()
            recent_cutoff_ms = int((time.time() - 600) * 1000)
            cur.execute(
                "SELECT pid FROM process WHERE running_at >= ? AND stopped_at IS NULL ORDER BY id DESC LIMIT 3",
                (recent_cutoff_ms,)
            )
            pids = [row[0] for row in cur.fetchall() if row[0]]
            for p in pids:
                try:
                    subprocess.run(["taskkill", "/F", "/PID", str(p), "/T"],
                                   creationflags=CREATE_NO_WINDOW,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass

    # 2. Encerra o processo pai Kryptex.exe e processos de suporte
    miners = [
        "Kryptex.exe", "kryptex3.exe", "kryptex4.exe", "kryptex7.exe",
        "SRBMiner-MULTI.exe", "kryptex_SRBMiner-MULTI.exe"
    ]
    for m in miners:
        try:
            subprocess.run(["taskkill", "/F", "/IM", m, "/T"],
                           creationflags=CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
        except Exception:
            pass


def start_kryptex_process():
    caminho_kryptex1 = os.path.expandvars(r"%LOCALAPPDATA%\Programs\Kryptex\Kryptex.exe")
    caminho_kryptex2 = r"C:\Program Files\Kryptex\Kryptex.exe"
    caminho_kryptex = caminho_kryptex1 if os.path.exists(caminho_kryptex1) else caminho_kryptex2

    if os.path.exists(caminho_kryptex):
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        subprocess.Popen(
            [caminho_kryptex, "--disable-gpu"],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True
        )
        return True, caminho_kryptex
    return False, caminho_kryptex


def restart_kryptex(force=False, motivo="Queda ou Shares Negados"):
    global ultimo_restart
    # Usa acquire com timeout de 3s para evitar travamento mas permitir execução
    if not restart_lock.acquire(timeout=3.0):
        return False

    try:
        agora = time.time()
        if not force and (agora - ultimo_restart) < TEMPO_DE_ESPERA:
            tempo_restante = int(TEMPO_DE_ESPERA - (agora - ultimo_restart))
            if app:
                app.log(f"ALERTA IGNORADO: Carência ativa ({tempo_restante}s restantes)")
            return False

        ultimo_restart = agora
        if app:
            app.offline_consecutive_count = 0
            app.warmup_until = agora + 80.0
            app.update_status(f"INTERVENÇÃO: {motivo.upper()}", THEME["crimson"])
            app.log(f"Intervenção Preventiva iniciada ({motivo}).")
            app.log("Encerrando processos mineradores para liberar VRAM e descarregar GPU...")

        stop_all_miners()

        if app:
            app.log("Processos encerrados. Aguardando 5s para estabilização térmica e reset de driver...")
        time.sleep(5)

        ok, path = start_kryptex_process()
        ultimo_restart = time.time()
        if ok:
            if app:
                app.offline_consecutive_count = 0
                app.warmup_until = time.time() + 75.0
                app.log("Kryptex reiniciado com sucesso (--disable-gpu ativo). Aguardando alocação do DAG...")
            return True
        else:
            if app:
                app.log(f"ERRO: Executável não encontrado em {path}")
            return False
    finally:
        restart_lock.release()


def load_config():
    default_config = {
        'tarifa': 0.80,
        'descanso_ativo': True,
        'descanso_intervalo_h': 8.0,
        'descanso_duracao_m': 2.0
    }
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                default_config.update(data)
    except Exception:
        pass
    return default_config


def save_config(cfg_dict):
    try:
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg_dict, f, indent=2)
    except Exception:
        pass


def calcular_confiabilidade_expectativas():
    """Compara, por período, a referência do Kryptex com a produção estimada."""
    db_path = os.path.expandvars(r'%APPDATA%\Kryptex\kryptex.db')
    if not os.path.exists(db_path):
        return None

    conn = None
    try:
        conn = sqlite3.connect(f'file:{db_path}?mode=ro', uri=True, timeout=2.0)
        cur = conn.cursor()

        cur.execute('SELECT rate FROM currency_rate WHERE currency_id = 14')
        brl_row = cur.fetchone()
        cur.execute('SELECT rate FROM currency_rate WHERE currency_id = 2')
        usd_row = cur.fetchone()
        if (
            not brl_row or brl_row[0] <= 0
            or not usd_row or usd_row[0] <= 0
        ):
            return None

        brl_rate = brl_row[0]
        usd_rate = usd_row[0]

        cur.execute('SELECT total, withdrawable, updated_at FROM balance LIMIT 1')
        bal_row = cur.fetchone()
        saldo_usd = (bal_row[0] or 0.0) if bal_row else 0.0
        withdrawable_usd = (bal_row[1] or 0.0) if bal_row else 0.0
        saldo_updated_at = bal_row[2] if bal_row else 0

        saldo_brl = saldo_usd * brl_rate
        withdrawable_brl = withdrawable_usd * brl_rate

        cur.execute('SELECT coin_algorithm_id, profitability FROM coin_algorithm_profitability')
        prof_map = dict(cur.fetchall())

        cur.execute('''
            SELECT coin_algorithm_id, hashrate 
            FROM device_server_coin_algorithm_estimate
            WHERE hashrate > 0
        ''')
        bench_map = dict(cur.fetchall())

        now_ms = time.time() * 1000
        periods = {
            'hoje': now_ms - (24 * 3600 * 1000),
            '7d': now_ms - (7 * 24 * 3600 * 1000),
            '30d': now_ms - (30 * 24 * 3600 * 1000),
            'inicio': 0
        }

        resultado = {
            'saldo_brl': saldo_brl,
            'saldo_usd': saldo_usd,
            'withdrawable_usd': withdrawable_usd,
            'withdrawable_brl': withdrawable_brl,
            'updated_at': saldo_updated_at,
            'brl_rate': brl_rate
        }

        MAX_GAP_SECONDS = 600.0
        DEFAULT_SAMPLE_SECONDS = 300.0

        for key, start_ms in periods.items():
            cur.execute('''
                SELECT 
                    r.timestamp,
                    pdr.hashrate,
                    pdr.coin_algorithm_id,
                    pdr.shares_accepted_total,
                    pdr.shares_rejected_total
                FROM reading r
                JOIN process_device_reading pdr ON r.id = pdr.reading_id
                WHERE r.timestamp >= ? AND pdr.hashrate > 0
                ORDER BY r.timestamp ASC
            ''', (start_ms,))

            rows = cur.fetchall()
            if not rows:
                resultado[key] = {
                    'exp_brl': 0.0, 'real_brl': 0.0, 'exp_usd': 0.0, 'real_usd': 0.0,
                    'diff_pct': 0.0, 'precisao': 100.0, 'horas': 0.0, 'dias': 0.0,
                    'media_exp_dia': 0.0, 'media_real_dia': 0.0, 'rend_pct': 100.0
                }
                continue

            sum_real_brl = 0.0
            sum_exp_brl = 0.0
            sum_real_usd = 0.0
            sum_exp_usd = 0.0
            total_active_seconds = 0.0

            prev_ts = None
            prev_acc = 0
            prev_rej = 0

            for ts, hashrate, algo_id, acc, rej in rows:
                acc = acc or 0
                rej = rej or 0

                if prev_ts is not None:
                    dt_s = (ts - prev_ts) / 1000.0
                    if dt_s <= 0 or dt_s > MAX_GAP_SECONDS:
                        dt_s = DEFAULT_SAMPLE_SECONDS
                else:
                    dt_s = DEFAULT_SAMPLE_SECONDS
                prev_ts = ts

                day_fraction = dt_s / 86400.0
                total_active_seconds += dt_s

                d_acc = (acc - prev_acc) if acc >= prev_acc else acc
                d_rej = (rej - prev_rej) if rej >= prev_rej else rej
                prev_acc, prev_rej = acc, rej

                tot_s = d_acc + d_rej
                eff = (d_acc / tot_s) if tot_s > 0 else 1.0

                prof = prof_map.get(algo_id, 0.0)

                real_usd_slice = ((hashrate or 0.0) * prof * usd_rate) * day_fraction * eff
                real_brl_slice = real_usd_slice * brl_rate

                bench_hr = bench_map.get(algo_id, hashrate or 0.0)
                exp_usd_slice = (bench_hr * prof * usd_rate) * day_fraction
                exp_brl_slice = exp_usd_slice * brl_rate

                sum_real_brl += real_brl_slice
                sum_exp_brl += exp_brl_slice
                sum_real_usd += real_usd_slice
                sum_exp_usd += exp_usd_slice

            horas_ativas = total_active_seconds / 3600.0
            dias_ativos = max(0.04, horas_ativas / 24.0)

            media_exp_dia = sum_exp_brl / dias_ativos if dias_ativos > 0 else 0.0
            media_real_dia = sum_real_brl / dias_ativos if dias_ativos > 0 else 0.0
            rend_pct = (sum_real_brl / sum_exp_brl * 100.0) if sum_exp_brl > 0 else 100.0
            diff_pct = ((sum_real_brl - sum_exp_brl) / sum_exp_brl * 100.0) if sum_exp_brl > 0 else 0.0
            precisao = max(0.0, 100.0 - abs(diff_pct))

            resultado[key] = {
                'exp_brl': sum_exp_brl,
                'real_brl': sum_real_brl,
                'exp_usd': sum_exp_usd,
                'real_usd': sum_real_usd,
                'diff_pct': diff_pct,
                'precisao': precisao,
                'horas': round(horas_ativas, 1),
                'dias': dias_ativos,
                'media_exp_dia': media_exp_dia,
                'media_real_dia': media_real_dia,
                'rend_pct': rend_pct
            }

        exp_inicio = resultado['inicio']['exp_brl']
        real_inicio = resultado['inicio']['real_brl']
        horas_inicio = resultado['inicio']['horas']
        dias_inicio = resultado['inicio']['dias']

        # Rendimento global real (somatoria efetiva / somatoria prevista pelo benchmark)
        rendimento_total_pct = (real_inicio / exp_inicio * 100.0) if exp_inicio > 0 else 100.0
        media_diaria_gerada = resultado['inicio']['media_real_dia']

        resultado['rendimento_total_pct'] = rendimento_total_pct
        resultado['media_diaria_saldo_real'] = media_diaria_gerada
        resultado['dias_ativos'] = dias_inicio
        resultado['total_produzido_brl'] = real_inicio

        return resultado
    except Exception:
        return None
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


def disable_quickedit():
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        mode.value &= ~0x0040
        kernel32.SetConsoleMode(handle, mode)
    except Exception:
        pass


class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        path = parsed_path.path

        try:
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b"OK")
        except Exception:
            pass

        if path == '/restart':
            threading.Thread(target=restart_kryptex, kwargs={"force": True, "motivo": "Comando Remoto /restart"}, daemon=True).start()

        elif path == '/online':
            if app:
                app.last_extension_ping = time.time()
                app.update_status("ONLINE • SINCRONIZADO VIA EXTENSÃO", THEME["emerald"])
                query = urllib.parse.parse_qs(parsed_path.query)
                if 'profit_d' in query:
                    profit_d = float(query['profit_d'][0])
                    watts = float(query.get('watts', [0])[0])
                    app.update_extension_data(profit_d, watts)

        elif path == '/warning':
            if app:
                app.last_extension_ping = time.time()
                app.update_status("AVISO • INSTABILIDADE DETECTADA", THEME["amber"])
                app.log("ALERTA: Queda ou erro detectado na extensão. Aguardando confirmação...")

        elif path == '/notfound':
            if app:
                app.last_extension_ping = time.time()
                app.update_status(f"AVISO • RIG '{PC_NAME}' NÃO ENCONTRADO", THEME["amber"])

        elif path == '/exit':
            if app:
                app.root.after(300, app.root.destroy)
            threading.Thread(target=lambda: (time.sleep(0.5), os._exit(0))).start()

    def log_message(self, format, *args):
        pass


class LocalTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def run_server():
    global httpd
    try:
        disable_quickedit()
        httpd = LocalTCPServer(("127.0.0.1", PORT), RequestHandler)
        if app:
            app.log(f"Servidor local ativo em 127.0.0.1:{PORT}")
            app.log("Escudo Anti-BSOD (WHEA / TDR) e telemetria NVML inicializados.")
        httpd.serve_forever()
    except Exception as e:
        if app:
            app.log(f"ERRO AO INICIAR SERVIDOR LOCAL: Porta {PORT} em uso ({e})")
            app.update_status("ERRO DE PORTA LOCAL", THEME["crimson"])


class KryptexGUI:
    def __init__(self, root):
        global app
        app = self
        self.root = root
        self.root.title("Kryptex Monitor Pro — Hardware & Financial Telemetry")
        self.root.geometry("980x790")
        self.root.minsize(920, 720)
        self.root.configure(fg_color=THEME["bg_main"])

        self.cfg = load_config()
        self._save_after_id = None
        self.fx_reset_in_progress = False

        self.last_profit_d = 0.0
        self.last_ext_watts = 0.0
        self.last_extension_ping = 0.0
        self.offline_consecutive_count = 0
        self.rejected_consecutive_count = 0
        self.warmup_until = time.time() + 45.0

        self.last_brl_rate = 0.0

        self.real_gpu_w = 0.0
        self.real_cpu_w = 0.0
        self.real_total_w = 0.0

        self.last_event_record_id = get_latest_hardware_record_id()
        self.vram_crit_count = 0

        self.last_rest_time = time.time()
        self.in_rest_pause = False

        self._build_ui()

        threading.Thread(target=run_server, daemon=True).start()
        threading.Thread(target=self.autonomous_monitor, daemon=True).start()
        threading.Thread(target=self.hardware_and_shield_monitor, daemon=True).start()
        threading.Thread(target=self.rest_cycle_monitor, daemon=True).start()
        threading.Thread(target=self.balance_tracker_monitor, daemon=True).start()

    def _build_ui(self):
        # 1. TOP BAR (Cabeçalho Executivo Compacto)
        self.topbar = ctk.CTkFrame(
            self.root, fg_color=THEME["bg_card"], corner_radius=10,
            border_width=1, border_color=THEME["border"]
        )
        self.topbar.pack(fill="x", padx=18, pady=(12, 5))

        title_box = ctk.CTkFrame(self.topbar, fg_color="transparent")
        title_box.pack(side="left", padx=16, pady=8)

        ctk.CTkLabel(
            title_box, text="KRYPTEX MONITOR PRO",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
            text_color=THEME["text_primary"]
        ).pack(anchor="w")

        ctk.CTkLabel(
            title_box, text=f"Rig: {PC_NAME}  •  API: 127.0.0.1:15000",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=THEME["text_muted"]
        ).pack(anchor="w")

        # Botões de Ação Manual (Direita)
        actions_box = ctk.CTkFrame(self.topbar, fg_color="transparent")
        actions_box.pack(side="right", padx=14, pady=8)

        self.btn_restart = ctk.CTkButton(
            actions_box, text="↻ Reiniciar Minerador", width=145, height=30,
            fg_color="#1E293B", hover_color="#334155",
            border_width=1, border_color="#334155",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=lambda: threading.Thread(
                target=restart_kryptex, kwargs={"force": True, "motivo": "Acionamento Manual"}, daemon=True
            ).start()
        )
        self.btn_restart.pack(side="right", padx=(6, 0))

        durac_init = float(self.cfg.get('descanso_duracao_m', 2.0))
        durac_txt = f"{int(durac_init)}" if durac_init.is_integer() else f"{durac_init:.1f}"
        self.btn_rest_now = ctk.CTkButton(
            actions_box, text=f"⏸ Pausar Agora ({durac_txt} min)", width=145, height=30,
            fg_color="#1E293B", hover_color="#334155",
            border_width=1, border_color="#334155",
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self.acionar_descanso_manual
        )
        self.btn_rest_now.pack(side="right", padx=6)

        # Pill de Status
        self.status_pill = ctk.CTkFrame(
            self.topbar, fg_color=THEME["bg_subcard"], corner_radius=16,
            border_width=1, border_color=THEME["border"]
        )
        self.status_pill.pack(side="right", padx=10, pady=8)

        self.status_label = ctk.CTkLabel(
            self.status_pill, text="● CONECTANDO AO BANCO LOCAL...",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=THEME["amber"]
        )
        self.status_label.pack(padx=12, pady=4)

        # 2. BARRA DE PARÂMETROS OPERACIONAIS
        self.ctrl_bar = ctk.CTkFrame(
            self.root, fg_color=THEME["bg_card"], corner_radius=10,
            border_width=1, border_color=THEME["border"]
        )
        self.ctrl_bar.pack(fill="x", padx=18, pady=4)

        ctk.CTkLabel(
            self.ctrl_bar, text="⚡ Tarifa (R$/kWh):",
            font=ctk.CTkFont(size=11, weight="bold"), text_color=THEME["text_sec"]
        ).pack(side="left", padx=(14, 4), pady=6)

        self.tarifa_var = ctk.StringVar(value=str(self.cfg.get('tarifa', 0.80)))
        self.tarifa_entry = ctk.CTkEntry(
            self.ctrl_bar, textvariable=self.tarifa_var, width=58, height=26,
            fg_color=THEME["bg_subcard"], border_color=THEME["border"],
            font=ctk.CTkFont(family="Consolas", size=11, weight="bold"), justify="center"
        )
        self.tarifa_entry.pack(side="left", padx=(0, 10))
        self.tarifa_var.trace_add("write", lambda *args: self.agendar_salvamento())

        ctk.CTkFrame(self.ctrl_bar, width=1, height=18, fg_color=THEME["border"]).pack(side="left", padx=4)

        self.descanso_var = ctk.BooleanVar(value=bool(self.cfg.get('descanso_ativo', True)))
        self.descanso_cb = ctk.CTkCheckBox(
            self.ctrl_bar, text="Auto-Cooldown Anti-BSOD:",
            variable=self.descanso_var, command=self.agendar_salvamento,
            checkbox_width=16, checkbox_height=16,
            font=ctk.CTkFont(size=11, weight="bold"), text_color=THEME["text_sec"]
        )
        self.descanso_cb.pack(side="left", padx=(6, 4), pady=6)

        ctk.CTkLabel(self.ctrl_bar, text="a cada", font=ctk.CTkFont(size=11), text_color=THEME["text_muted"]).pack(side="left", padx=2)
        self.intervalo_var = ctk.StringVar(value=str(self.cfg.get('descanso_intervalo_h', 8.0)))
        ctk.CTkEntry(
            self.ctrl_bar, textvariable=self.intervalo_var, width=42, height=26,
            fg_color=THEME["bg_subcard"], border_color=THEME["border"],
            font=ctk.CTkFont(family="Consolas", size=11), justify="center"
        ).pack(side="left", padx=2)
        self.intervalo_var.trace_add("write", lambda *args: self.agendar_salvamento())

        ctk.CTkLabel(self.ctrl_bar, text="h pausar", font=ctk.CTkFont(size=11), text_color=THEME["text_muted"]).pack(side="left", padx=2)
        self.duracao_var = ctk.StringVar(value=str(self.cfg.get('descanso_duracao_m', 2.0)))
        ctk.CTkEntry(
            self.ctrl_bar, textvariable=self.duracao_var, width=42, height=26,
            fg_color=THEME["bg_subcard"], border_color=THEME["border"],
            font=ctk.CTkFont(family="Consolas", size=11), justify="center"
        ).pack(side="left", padx=2)
        self.duracao_var.trace_add("write", lambda *args: self.agendar_salvamento())
        ctk.CTkLabel(self.ctrl_bar, text="min", font=ctk.CTkFont(size=11), text_color=THEME["text_muted"]).pack(side="left", padx=(2, 8))

        self.shares_lbl = ctk.CTkLabel(
            self.ctrl_bar, text="Shares: Monitorando rejeições...",
            font=ctk.CTkFont(family="Consolas", size=11, weight="bold"),
            text_color=THEME["text_sec"]
        )
        self.shares_lbl.pack(side="right", padx=14, pady=6)

        # 3. GRID DE 4 KPIS FINANCEIROS
        self.kpi_grid = ctk.CTkFrame(self.root, fg_color="transparent")
        self.kpi_grid.pack(fill="x", padx=18, pady=4)
        self.kpi_grid.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="kpi")

        self.kpi_saldo, self.lbl_saldo, self.lbl_saldo_mid, self.lbl_saldo_sub = self._create_kpi_card(
            self.kpi_grid, 0, "SALDO ACUMULADO (KRYPTEX)", "R$ 0.00",
            "Gerado no PC: R$ 0.00", "Disponível: R$ 0.00", THEME["emerald"], THEME["emerald"]
        )
        self.kpi_bruto, self.lbl_bruto, self.lbl_bruto_mid, self.lbl_bruto_sub = self._create_kpi_card(
            self.kpi_grid, 1, "RECEITA BRUTA ESTIMADA", "R$ 0.00 / dia",
            "Cotação Kryptex aplicada", "Projeção Bruta: R$ 0.00 / mês", THEME["blue"], THEME["emerald"]
        )
        self.kpi_custo, self.lbl_custo, self.lbl_custo_mid, self.lbl_consumo = self._create_kpi_card(
            self.kpi_grid, 2, "CUSTO DE ENERGIA (TOMADA)", "- R$ 0.00 / dia",
            "Custo Mensal: - R$ 0.00 / mês", "Consumo: 0 W • 0.0 kWh/dia", THEME["crimson"], THEME["text_sec"]
        )
        self.kpi_liquido, self.lbl_liquido, self.lbl_liquido_conv, self.lbl_liquido_sub = self._create_kpi_card(
            self.kpi_grid, 3, "LUCRO LÍQUIDO ESTIMADO", "R$ 0.00 / dia",
            "Conversão cambial incluída", "Líquido: R$ 0.00 / mês", THEME["emerald"], THEME["text_sec"]
        )

        # 4. TELEMETRIA DE HARDWARE & ESCUDO ANTI-BSOD
        self.hw_card = ctk.CTkFrame(
            self.root, fg_color=THEME["bg_card"], corner_radius=10,
            border_width=1, border_color=THEME["border"]
        )
        self.hw_card.pack(fill="x", padx=18, pady=4)

        hw_top = ctk.CTkFrame(self.hw_card, fg_color="transparent")
        hw_top.pack(fill="x", padx=14, pady=(6, 2))

        ctk.CTkLabel(
            hw_top, text="TELEMETRIA DE HARDWARE & ESCUDO ANTI-BSOD (NVML / WHEA / TDR)",
            font=ctk.CTkFont(size=11, weight="bold"), text_color=THEME["text_sec"]
        ).pack(side="left")

        self.hw_status = ctk.CTkLabel(
            hw_top, text="● INTEGRIDADE NORMAL",
            font=ctk.CTkFont(size=11, weight="bold"), text_color=THEME["emerald"]
        )
        self.hw_status.pack(side="right")

        hw_bars = ctk.CTkFrame(self.hw_card, fg_color="transparent")
        hw_bars.pack(fill="x", padx=14, pady=(2, 8))
        hw_bars.grid_columnconfigure((0, 1, 2), weight=1, uniform="hw")

        self.lbl_gpu_temp, self.bar_gpu_temp = self._create_telemetry_gauge(hw_bars, 0, "Temperatura GPU / Potência", "0°C • 0 W")
        self.lbl_vram, self.bar_vram = self._create_telemetry_gauge(hw_bars, 1, "Alocação de VRAM", "0.0 / 0.0 GB (0%)")
        self.lbl_ram, self.bar_ram = self._create_telemetry_gauge(hw_bars, 2, "Memória RAM do Sistema", "0.0 / 0.0 GB (0%)")

        # 5. AUDITORIA FINANCEIRA E REGISTRO DO SALDO OBSERVADO
        self.audit_card = ctk.CTkFrame(
            self.root, fg_color=THEME["bg_card"], corner_radius=10,
            border_width=1, border_color=THEME["border"]
        )
        self.audit_card.pack(fill="x", padx=18, pady=4)

        audit_top = ctk.CTkFrame(self.audit_card, fg_color="transparent")
        audit_top.pack(fill="x", padx=14, pady=(6, 2))

        ctk.CTkLabel(
            audit_top,
            text="PROJEÇÃO KRYPTEX vs. CRÉDITOS OBSERVADOS (USD)",
            font=ctk.CTkFont(size=11, weight="bold"), text_color=THEME["text_sec"]
        ).pack(side="left")

        self.conf_badge = ctk.CTkLabel(
            audit_top, text="● INICIANDO MEDIÇÃO...",
            font=ctk.CTkFont(size=11, weight="bold"), text_color=THEME["text_sec"]
        )
        self.conf_badge.pack(side="right")

        balance_grid = ctk.CTkFrame(self.audit_card, fg_color="transparent")
        balance_grid.pack(fill="x", padx=10, pady=(5, 8))
        balance_grid.grid_columnconfigure((0, 1, 2, 3), weight=1, uniform="balance")

        self.balance_expected = self._create_kpi_card(
            balance_grid, 0, "PROJEÇÃO KRYPTEX (USD)", "US$ 0.000000",
            "Acumulando expectativa", "Desde o início da coleta",
            THEME["blue"], THEME["text_sec"], card_color=THEME["bg_subcard"]
        )
        self.balance_net = self._create_kpi_card(
            balance_grid, 1, "CRÉDITOS OBSERVADOS (USD)", "US$ +0.000000",
            "Créditos: US$ +0.000000", "Saques/reduções: US$ 0.000000",
            THEME["emerald"], THEME["text_sec"], card_color=THEME["bg_subcard"]
        )
        self.balance_difference = self._create_kpi_card(
            balance_grid, 2, "DIFERENÇA (USD)", "US$ +0.000000",
            "Créditos observados menos projeção", "Cumprimento após 5 min",
            THEME["text_primary"], THEME["text_sec"], card_color=THEME["bg_subcard"]
        )
        self.balance_fx = self._create_kpi_card(
            balance_grid, 3, "VARIAÇÃO CAMBIAL", "R$ +0.00",
            "Não é produção", "Saldo USD convertido pela cotação",
            THEME["amber"], THEME["text_sec"], card_color=THEME["bg_subcard"]
        )
        self.btn_reset_fx = ctk.CTkButton(
            self.balance_fx[0],
            text="Zerar variação",
            width=112,
            height=22,
            fg_color="#1E293B",
            hover_color="#334155",
            border_width=1,
            border_color=THEME["border"],
            font=ctk.CTkFont(size=9, weight="bold"),
            command=self.confirm_reset_fx_variation
        )
        self.btn_reset_fx.pack(anchor="e", padx=10, pady=(0, 6))
        for card_data in (
            self.balance_expected, self.balance_net,
            self.balance_difference, self.balance_fx
        ):
            card_data[1].configure(
                font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold")
            )
            card_data[2].configure(wraplength=200)
            card_data[3].configure(wraplength=200)
        ctk.CTkLabel(
            self.audit_card,
            text=(
                "A projeção usa a rentabilidade e o hashrate atuais do Kryptex. "
                "O câmbio é exibido separadamente."
            ),
            font=ctk.CTkFont(size=9),
            text_color=THEME["text_muted"],
            anchor="w",
            wraplength=900
        ).pack(fill="x", padx=14, pady=(0, 8))

        # 6. CONSOLE DE EVENTOS
        log_frame = ctk.CTkFrame(
            self.root, fg_color=THEME["bg_card"], corner_radius=10,
            border_width=1, border_color=THEME["border"]
        )
        log_frame.pack(fill="both", expand=True, padx=18, pady=(4, 12))

        log_header = ctk.CTkFrame(log_frame, fg_color="transparent")
        log_header.pack(fill="x", padx=14, pady=(5, 0))
        ctk.CTkLabel(
            log_header, text="REGISTRO DE EVENTOS E INTERVENÇÕES (MÁX 300 LINHAS)",
            font=ctk.CTkFont(size=10, weight="bold"), text_color=THEME["text_muted"]
        ).pack(side="left")

        self.log_area = ctk.CTkTextbox(
            log_frame, fg_color=THEME["bg_subcard"], text_color=THEME["text_sec"],
            font=ctk.CTkFont(family="Consolas", size=10), corner_radius=6,
            border_width=1, border_color=THEME["border"]
        )
        self.log_area.pack(fill="both", expand=True, padx=10, pady=(3, 8))
        self.log_area.insert("end", "Console de telemetria e integridade inicializado.\n")
        self.log_area.configure(state="disabled")

    def _create_kpi_card(
        self, parent, col, title, main_val, mid_val, sub_val,
        accent_color, mid_color=None, card_color=None
    ):
        padx = (0, 4) if col == 0 else ((4, 0) if col == 3 else 4)
        card = ctk.CTkFrame(
            parent, fg_color=card_color or THEME["bg_card"], corner_radius=10,
            border_width=1, border_color=THEME["border"]
        )
        card.grid(row=0, column=col, padx=padx, sticky="nsew")

        ctk.CTkLabel(
            card, text=title, font=ctk.CTkFont(size=10, weight="bold"),
            text_color=THEME["text_muted"]
        ).pack(anchor="w", padx=12, pady=(7, 0))

        lbl_main = ctk.CTkLabel(
            card, text=main_val, font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            text_color=accent_color
        )
        lbl_main.pack(anchor="w", padx=12, pady=0)

        lbl_mid = ctk.CTkLabel(
            card, text=mid_val, font=ctk.CTkFont(size=10, weight="bold"),
            text_color=mid_color or THEME["text_sec"]
        )
        lbl_mid.pack(anchor="w", padx=12, pady=0)

        lbl_sub = ctk.CTkLabel(
            card, text=sub_val, font=ctk.CTkFont(size=9),
            text_color=THEME["text_sec"]
        )
        lbl_sub.pack(anchor="w", padx=12, pady=(0, 7))
        return card, lbl_main, lbl_mid, lbl_sub

    def _create_telemetry_gauge(self, parent, col, title, initial_val):
        padx = (0, 5) if col == 0 else ((5, 0) if col == 2 else 5)
        box = ctk.CTkFrame(
            parent, fg_color=THEME["bg_subcard"], corner_radius=8,
            border_width=1, border_color=THEME["border"]
        )
        box.grid(row=0, column=col, padx=padx, sticky="nsew")

        row_top = ctk.CTkFrame(box, fg_color="transparent")
        row_top.pack(fill="x", padx=10, pady=(5, 1))

        ctk.CTkLabel(
            row_top, text=title, font=ctk.CTkFont(size=10),
            text_color=THEME["text_sec"]
        ).pack(side="left")

        lbl_val = ctk.CTkLabel(
            row_top, text=initial_val, font=ctk.CTkFont(family="Consolas", size=10, weight="bold"),
            text_color=THEME["text_primary"]
        )
        lbl_val.pack(side="right")

        bar = ctk.CTkProgressBar(
            box, height=5, corner_radius=3,
            fg_color="#1E293B", progress_color=THEME["blue"]
        )
        bar.set(0.0)
        bar.pack(fill="x", padx=10, pady=(1, 6))
        return lbl_val, bar

    def _create_audit_col(self, parent, col, title):
        box = ctk.CTkFrame(
            parent, fg_color=THEME["bg_subcard"], corner_radius=8,
            border_width=1, border_color=THEME["border"]
        )
        box.grid(row=0, column=col, padx=3, sticky="nsew")

        ctk.CTkLabel(
            box, text=title, font=ctk.CTkFont(size=9, weight="bold"),
            text_color=THEME["text_muted"]
        ).pack(anchor="w", padx=10, pady=(5, 1))

        lbl_vals = ctk.CTkLabel(
            box, text="Ref: -- | Est: --",
            font=ctk.CTkFont(family="Consolas", size=10, weight="bold"),
            text_color=THEME["text_primary"]
        )
        lbl_vals.pack(anchor="w", padx=10, pady=0)

        lbl_pct = ctk.CTkLabel(
            box, text="Vs. referência: --",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=THEME["emerald"]
        )
        lbl_pct.pack(anchor="w", padx=10, pady=0)

        lbl_sub = ctk.CTkLabel(
            box, text="Média: --",
            font=ctk.CTkFont(size=9),
            text_color=THEME["text_sec"]
        )
        lbl_sub.pack(anchor="w", padx=10, pady=(0, 5))

        return {"vals": lbl_vals, "pct": lbl_pct, "sub": lbl_sub}

    def agendar_salvamento(self):
        if self._save_after_id:
            self.root.after_cancel(self._save_after_id)
        self._save_after_id = self.root.after(400, self.salvar_todas_configs)

    def salvar_todas_configs(self):
        self._save_after_id = None
        try:
            tarifa = float(self.tarifa_var.get().replace(',', '.'))
        except ValueError:
            tarifa = 0.80
        try:
            interv_h = float(self.intervalo_var.get().replace(',', '.'))
        except ValueError:
            interv_h = 8.0
        try:
            durac_m = float(self.duracao_var.get().replace(',', '.'))
        except ValueError:
            durac_m = 2.0

        self.cfg = {
            "tarifa": tarifa,
            "descanso_ativo": bool(self.descanso_var.get()),
            "descanso_intervalo_h": interv_h,
            "descanso_duracao_m": durac_m
        }
        durac_txt = f"{int(durac_m)}" if float(durac_m).is_integer() else f"{durac_m:.1f}"
        self.btn_rest_now.configure(text=f"⏸ Pausar Agora ({durac_txt} min)")
        save_config(self.cfg)
        self.recalculate()

    def acionar_descanso_manual(self):
        if not self.in_rest_pause:
            durac_m = self.cfg.get("descanso_duracao_m", 2.0)
            threading.Thread(target=self._ciclo_descanso_thread, args=(durac_m,), daemon=True).start()

    def _ciclo_descanso_thread(self, durac_m):
        self.in_rest_pause = True
        try:
            self.executar_descanso(durac_m)
        finally:
            self.last_rest_time = time.time()
            self.in_rest_pause = False

    def update_shares_display(self, acc, rej, tot, rate):
        color = THEME["emerald"] if rate < 5.0 else (THEME["amber"] if rate < 10.0 else THEME["crimson"])
        self.shares_lbl.configure(text=f"Shares: {acc} OK  •  {rej} Rej ({rate:.1f}%)", text_color=color)

    def update_hw_ui(self, status_text, status_color, temp_c, gpu_w, vram_used, vram_tot, vram_pct, ram_used, ram_tot, ram_pct):
        self.hw_status.configure(text=status_text, text_color=status_color)

        # Barra Temperatura GPU (escala até 90°C)
        self.lbl_gpu_temp.configure(text=f"{temp_c}°C • {gpu_w:.0f} W")
        self.bar_gpu_temp.set(min(1.0, max(0.0, temp_c / 90.0)))
        self.bar_gpu_temp.configure(
            progress_color=THEME["crimson"] if temp_c >= 82 else (THEME["amber"] if temp_c >= 75 else THEME["emerald"])
        )

        # Barra VRAM
        self.lbl_vram.configure(text=f"{vram_used:.1f}/{vram_tot:.1f} GB ({vram_pct:.0f}%)")
        self.bar_vram.set(min(1.0, max(0.0, vram_pct / 100.0)))
        self.bar_vram.configure(
            progress_color=THEME["crimson"] if vram_pct >= 92 else (THEME["amber"] if vram_pct >= 85 else THEME["blue"])
        )

        # Barra RAM
        self.lbl_ram.configure(text=f"{ram_used:.1f}/{ram_tot:.1f} GB ({ram_pct}%)")
        self.bar_ram.set(min(1.0, max(0.0, ram_pct / 100.0)))
        self.bar_ram.configure(
            progress_color=THEME["crimson"] if ram_pct >= 88 else (THEME["amber"] if ram_pct >= 80 else THEME["blue"])
        )

    def update_saldo_display(
        self, saldo_tot, saldo_with, saldo_usd, withdrawable_usd, brl_rate
    ):
        self.last_brl_rate = brl_rate
        if saldo_tot > 0:
            self.lbl_saldo.configure(text=f"R$ {saldo_tot:.2f}")
            self.lbl_saldo_mid.configure(
                text=f"Carteira Kryptex: US$ {saldo_usd:.4f}",
                text_color=THEME["emerald"]
            )
            self.lbl_saldo_sub.configure(
                text=f"Disponível: R$ {saldo_with:.2f} "
                f"(US$ {withdrawable_usd:.2f}) • R$ {brl_rate:.4f}/US$"
            )

    def update_balance_audit_ui(self, summary):
        if not summary:
            return
        started = datetime.fromtimestamp(
            summary["tracking_started_at"] / 1000
        ).astimezone().strftime("%d/%m/%y %H:%M")
        elapsed_minutes = summary["elapsed_seconds"] / 60.0
        if summary["adherence_pct"] is None:
            adherence = (
                f"aguardando (mínimo 5 min; coleta em "
                f"{elapsed_minutes:.1f} min)"
            )
            color = THEME["amber"]
        else:
            adherence = f"{summary['adherence_pct']:.1f}%"
            color = (
                THEME["emerald"]
                if 90 <= summary["adherence_pct"] <= 110
                else THEME["amber"]
            )
        self.conf_badge.configure(
            text=f"● CUMPRIMENTO: {adherence}",
            text_color=color
        )
        self.balance_expected[1].configure(
            text=f"US$ {summary['expected_usd']:.6f}"
        )
        self.balance_expected[2].configure(
            text=f"Coletando há {elapsed_minutes:.1f} min"
        )
        self.balance_expected[3].configure(
            text=f"Início: {started} • expectativa variável"
        )

        self.balance_net[1].configure(
            text=f"US$ {summary['increases_usd']:+.6f}",
            text_color=THEME["emerald"]
        )
        self.balance_net[2].configure(
            text=f"Créditos: US$ {summary['increases_usd']:+.6f}"
        )
        self.balance_net[3].configure(
            text=f"Saques/reduções: US$ {summary['decreases_usd']:+.6f}"
        )

        difference_color = (
            THEME["emerald"]
            if summary["difference_usd"] >= 0
            else THEME["crimson"]
        )
        self.balance_difference[1].configure(
            text=f"US$ {summary['difference_usd']:+.6f}",
            text_color=difference_color
        )
        self.balance_difference[2].configure(
            text=f"Créditos observados − projeção"
        )
        self.balance_difference[3].configure(
            text=f"Cumprimento: {adherence}"
        )

        fx_color = (
            THEME["emerald"] if summary["fx_change_brl"] >= 0 else THEME["crimson"]
        )
        self.balance_fx[1].configure(
            text=f"R$ {summary['fx_change_brl']:+.2f}",
            text_color=fx_color
        )
        self.balance_fx[2].configure(
            text=f"Cotação: R$ {summary['current_brl_rate']:.4f}/US$"
        )
        self.balance_fx[3].configure(
            text=f"Carteira: US$ {summary['current_total_usd']:.4f}"
        )

    def confirm_reset_fx_variation(self):
        if self.fx_reset_in_progress:
            return
        confirmed = messagebox.askyesno(
            "Zerar variação cambial",
            "A variação será zerada com o saldo e a cotação atuais. "
            "O histórico de créditos, projeções e saques será preservado. "
            "Deseja continuar?",
            parent=self.root
        )
        if not confirmed:
            return

        self.fx_reset_in_progress = True
        self.btn_reset_fx.configure(state="disabled", text="Zerando...")
        threading.Thread(
            target=self._reset_fx_variation_thread, daemon=True
        ).start()

    def _reset_fx_variation_thread(self):
        try:
            with balance_tracker_lock:
                sample = read_balance_tracking_sample()
                record_balance_tracking_sample(sample)
                reset_balance_fx_variation(sample)
                summary = get_balance_tracking_summary(sample)
            self.root.after(
                0, self._complete_fx_variation_reset, summary, None
            )
        except Exception as exc:
            self.root.after(
                0, self._complete_fx_variation_reset, None, str(exc)
            )

    def _complete_fx_variation_reset(self, summary, error):
        self.fx_reset_in_progress = False
        self.btn_reset_fx.configure(
            state="normal", text="Zerar variação"
        )
        if error:
            self.log(f"Erro ao zerar variação cambial: {error}")
            messagebox.showerror(
                "Erro ao zerar variação cambial",
                error,
                parent=self.root
            )
            return

        self.update_balance_audit_ui(summary)
        self.log(
            "Variação cambial zerada com saldo "
            f"US$ {summary['current_total_usd']:.4f} e cotação "
            f"R$ {summary['current_brl_rate']:.4f}/US$. "
            "Histórico de créditos, projeções e saques preservado."
        )

    def balance_tracker_monitor(self):
        last_error = None
        last_ui_update = 0.0
        while True:
            try:
                with balance_tracker_lock:
                    sample = read_balance_tracking_sample()
                    event = record_balance_tracking_sample(sample)
                if event and event.get("baseline"):
                    started = datetime.fromtimestamp(
                        event["observed_at"] / 1000
                    ).astimezone().strftime("%d/%m/%y %H:%M:%S")
                    self.log(
                        f"Registro de saldo iniciado em {started}; "
                        f"saldo-base US$ {sample['total_usd']:.8f}."
                    )
                elif event:
                    observed = datetime.fromtimestamp(
                        event["observed_at"] / 1000
                    ).astimezone().strftime("%d/%m/%y %H:%M:%S")
                    direction = "aumentou" if event["delta_usd"] > 0 else "diminuiu"
                    self.log(
                        f"Saldo Kryptex {direction} em {observed}: "
                        f"US$ {event['delta_usd']:+.8f} "
                        f"(cotação R$ {event['brl_rate']:.4f}/US$); "
                        f"expectativa do intervalo US$ "
                        f"{event['expected_usd']:.8f}."
                    )

                now = time.monotonic()
                if now - last_ui_update >= 15:
                    summary = get_balance_tracking_summary(sample)
                    if summary:
                        self.root.after(
                            0, self.update_balance_audit_ui, summary
                        )
                    last_ui_update = now
                last_error = None
            except Exception as exc:
                message = str(exc)
                if message != last_error:
                    self.log(f"Erro ao registrar saldo do Kryptex: {message}")
                    last_error = message
            time.sleep(1)

    def rest_cycle_monitor(self):
        time.sleep(15)
        while True:
            try:
                if self.cfg.get("descanso_ativo", True) and not self.in_rest_pause:
                    agora = time.time()
                    interv_h = float(self.cfg.get("descanso_intervalo_h", 8.0))
                    durac_m = float(self.cfg.get("descanso_duracao_m", 2.0))

                    intervalo_s = max(1800.0, interv_h * 3600.0)
                    if (agora - self.last_rest_time) >= intervalo_s:
                        self._ciclo_descanso_thread(durac_m)
            except Exception:
                pass
            time.sleep(10)

    def executar_descanso(self, duracao_minutos):
        global ultimo_restart
        if not restart_lock.acquire(timeout=5.0):
            return
        try:
            duracao_s = int(max(30, duracao_minutos * 60))
            self.log(f"COOLDOWN PREVENTIVO ({duracao_minutos:.1f} min): Pausando mineradores para limpeza de VRAM...")
            stop_all_miners()

            for restante in range(duracao_s, 0, -1):
                mins, segs = divmod(restante, 60)
                self.update_status(f"● COOLDOWN PREVENTIVO ({mins:02d}:{segs:02d})", THEME["amber"])
                time.sleep(1)

            self.log("Cooldown concluído. Reiniciando Kryptex com VRAM limpa...")
            ok, _ = start_kryptex_process()
            ultimo_restart = time.time()
            self.offline_consecutive_count = 0
            self.warmup_until = time.time() + 75.0
            if ok:
                self.update_status("● RETOMADO PÓS-COOLDOWN • ALOCANDO DAG", THEME["emerald"])
                self.log("Kryptex retomado com sucesso. Aguardando estabilização do DAG (75s)...")
        finally:
            restart_lock.release()

    def autonomous_monitor(self):
        time.sleep(2)
        while True:
            try:
                if not self.in_rest_pause:
                    now = time.time()
                    local_data = get_kryptex_local_data()

                    if local_data["online"]:
                        self.warmup_until = 0.0
                        acc = local_data.get("shares_acc", 0)
                        rej = local_data.get("shares_rej", 0)
                        tot = local_data.get("total_shares", 0)
                        rate = local_data.get("rejection_rate", 0.0)

                        self.root.after(0, self.update_shares_display, acc, rej, tot, rate)

                        saldo_tot = local_data.get("saldo_total_brl", 0.0)
                        saldo_with = local_data.get("saldo_withdrawable_brl", 0.0)
                        saldo_usd = local_data.get("saldo_total_usd", 0.0)
                        withdrawable_usd = local_data.get("saldo_withdrawable_usd", 0.0)
                        brl_rate = local_data.get("brl_rate", 0.0)
                        if saldo_tot > 0:
                            self.root.after(
                                0, self.update_saldo_display,
                                saldo_tot, saldo_with, saldo_usd,
                                withdrawable_usd, brl_rate
                            )

                        if tot >= 25 and rate >= 10.0:
                            self.rejected_consecutive_count += 1
                            if self.rejected_consecutive_count == 1:
                                self.log(f"ALERTA VRAM: Taxa elevada de shares rejeitados ({rate:.1f}%).")
                                self.update_status(f"● ALERTA • SHARES REJEITADOS ({rate:.1f}%)", THEME["amber"])
                            elif self.rejected_consecutive_count >= 4:
                                self.rejected_consecutive_count = 0
                                self.log(f"CRÍTICO: Shares rejeitados em {rate:.1f}%. Reiniciando Kryptex...")
                                restart_kryptex(force=True, motivo="Excesso de Shares Rejeitados")
                        else:
                            self.rejected_consecutive_count = 0

                        if (now - self.last_extension_ping) >= 12:
                            self.offline_consecutive_count = 0
                            self.last_profit_d = local_data["profit_brl"]
                            if self.rejected_consecutive_count == 0:
                                self.update_status("● ONLINE • MONITORAMENTO DIRETO DB", THEME["emerald"])
                            self.root.after(0, self.recalculate)
                    else:
                        if now < self.warmup_until:
                            self.offline_consecutive_count = 0
                            restante_w = max(1, int(self.warmup_until - now))
                            self.update_status(f"● CARREGANDO DAG KRYPTEX ({restante_w}s)", THEME["amber"])
                        elif (now - self.last_extension_ping) >= 12:
                            self.offline_consecutive_count += 1
                            if self.offline_consecutive_count == 1:
                                self.update_status(f"● AVISO • {local_data['reason'].upper()}", THEME["amber"])
                                self.log(f"Alerta Local: {local_data['reason']}. Verificando persistência...")
                            elif self.offline_consecutive_count >= 3:
                                self.offline_consecutive_count = 0
                                self.log(f"Offline confirmado ({local_data['reason']}). Reiniciando...")
                                restart_kryptex(force=False, motivo=local_data['reason'])
                        else:
                            self.offline_consecutive_count = 0
            except Exception:
                pass
            time.sleep(4)

    def hardware_and_shield_monitor(self):
        wmi_obj = None
        if pythoncom and win32com:
            try:
                pythoncom.CoInitialize()
                wmi_obj = win32com.client.GetObject("winmgmts:")
            except Exception:
                wmi_obj = None

        time.sleep(3)
        while True:
            try:
                gpu_data = get_gpu_health_and_power()
                cpu_w = get_cpu_power(wmi_obj)
                ram_data = get_ram_info()

                gpu_w = gpu_data["power_w"]
                base_w = 45.0 if (gpu_w > 0 or cpu_w > 0) else 0.0
                psu_eff = 0.90
                total_w = ((gpu_w + cpu_w + base_w) / psu_eff) if (gpu_w > 0 or cpu_w > 0) else 0.0

                self.real_gpu_w = gpu_w
                self.real_cpu_w = cpu_w
                self.real_total_w = total_w

                vram_pct = gpu_data["vram_pct"]
                vram_used = gpu_data["vram_used_gb"]
                vram_tot = gpu_data["vram_total_gb"]
                temp_c = gpu_data["temp_c"]
                ram_pct = ram_data["percent"]
                ram_used = ram_data["used_gb"]
                ram_tot = ram_data["total_gb"]

                new_errors = check_new_hardware_errors(self.last_event_record_id)
                status_text = "● INTEGRIDADE NORMAL"
                status_color = THEME["emerald"]

                if new_errors and not self.in_rest_pause:
                    self.last_event_record_id = max(e['record_id'] for e in new_errors)
                    for err in new_errors:
                        provider = err['provider']
                        ev_id = err['event_id']
                        if 'nvlddmkm' in provider:
                            status_text = "● TDR INTERCEPTADO (DRIVER GPU)"
                            status_color = THEME["crimson"]
                            self.log(f"ESCUDO ANTI-BSOD: Erro de driver interceptado ({provider} ID {ev_id}).")
                            restart_kryptex(force=True, motivo=f"Escudo Anti-BSOD: {provider} ID {ev_id}")
                            break
                        elif 'WHEA' in provider:
                            status_text = "● ALERTA WHEA BARRAMENTO/PCIE"
                            status_color = THEME["amber"]
                            self.log(f"ESCUDO ANTI-BSOD: Evento WHEA detectado ({provider} ID {ev_id}).")
                            restart_kryptex(force=True, motivo=f"Escudo Anti-BSOD: WHEA ID {ev_id}")
                            break

                if not self.in_rest_pause:
                    if vram_pct >= 95.0:
                        self.vram_crit_count += 1
                        status_text = "● VRAM CRÍTICA (>95%)"
                        status_color = THEME["crimson"]
                        if self.vram_crit_count >= 2:
                            self.vram_crit_count = 0
                            self.log(f"VRAM Esgotada ({vram_pct:.1f}%). Reiniciando preventivamente...")
                            restart_kryptex(force=True, motivo="VRAM Esgotada")
                    else:
                        self.vram_crit_count = 0

                    if temp_c >= 88:
                        status_text = "● LIMITE TÉRMICO GPU ATINGIDO"
                        status_color = THEME["crimson"]
                        self.log(f"TEMPERATURA CRÍTICA NA GPU ({temp_c}°C)! Intervenção imediata...")
                        restart_kryptex(force=True, motivo=f"GPU Superaquecida ({temp_c}°C)")
                    elif temp_c >= 82 and status_color == THEME["emerald"]:
                        status_text = "● TEMPERATURA GPU ELEVADA"
                        status_color = THEME["amber"]

                    if ram_pct >= 90:
                        status_text = "● MEMÓRIA RAM CRÍTICA"
                        status_color = THEME["crimson"]
                        self.log(f"RAM Crítica ({ram_pct}%). Reiniciando minerador para liberar memória...")
                        restart_kryptex(force=True, motivo="RAM Esgotada (OOM)")

                self.root.after(
                    0, self.update_hw_ui,
                    status_text, status_color, temp_c, gpu_w,
                    vram_used, vram_tot, vram_pct, ram_used, ram_tot, ram_pct
                )
                self.root.after(0, self.recalculate)

            except Exception:
                pass
            time.sleep(3)

    def update_extension_data(self, profit_d, watts):
        self.last_profit_d = profit_d
        self.last_ext_watts = watts
        self.root.after(0, self.recalculate)

    def recalculate(self):
        tarifa = float(self.cfg.get("tarifa", 0.80))

        if self.real_total_w > 0:
            watts = self.real_total_w
            consumo_sub = f"~{watts:.0f}W (GPU {self.real_gpu_w:.0f}W + Sis)"
        elif self.last_ext_watts > 0:
            watts = self.last_ext_watts
            consumo_sub = f"Estimado: ~{watts:.0f} W"
        else:
            watts = 0.0
            consumo_sub = "Tomada: 0 W"

        profit_d = self.last_profit_d
        if watts == 0 and profit_d == 0:
            return

        kwh_dia = (watts / 1000.0) * 24.0
        custo_dia = kwh_dia * tarifa
        custo_mes = custo_dia * 30.0
        bruto_dia = profit_d
        bruto_mes = bruto_dia * 30.0
        liquido_dia = bruto_dia - custo_dia
        liquido_mes = liquido_dia * 30.0

        self.lbl_bruto.configure(text=f"R$ {bruto_dia:.2f} / dia")
        cotacao_txt = (
            f"Cotação: R$ {self.last_brl_rate:.4f}/US$"
            if self.last_brl_rate > 0
            else "Conversão já incluída no valor"
        )
        self.lbl_bruto_mid.configure(text=cotacao_txt)
        self.lbl_bruto_sub.configure(text=f"Projeção Bruta: R$ {bruto_mes:.2f} / mês")

        self.lbl_custo.configure(text=f"- R$ {custo_dia:.2f} / dia" if custo_dia > 0 else "R$ 0.00 / dia")
        self.lbl_custo_mid.configure(text=f"Custo Mensal: - R$ {custo_mes:.2f} / mês")
        self.lbl_consumo.configure(text=f"{consumo_sub} • {kwh_dia:.1f} kWh/d")

        cor_liq = THEME["emerald"] if liquido_dia >= 0 else THEME["crimson"]
        self.lbl_liquido.configure(text=f"R$ {liquido_dia:.2f} / dia", text_color=cor_liq)
        self.lbl_liquido_conv.configure(
            text="Conversão cambial já incluída",
            text_color=THEME["text_sec"]
        )
        self.lbl_liquido_sub.configure(
            text=f"Líquido: R$ {liquido_mes:.2f} / mês"
        )

    def log(self, msg):
        self.root.after(0, self._log_thread_safe, msg)

    def _log_thread_safe(self, msg):
        timestamp_ui = time.strftime('%H:%M:%S')
        timestamp_file = time.strftime('%d/%m/%Y %H:%M:%S')

        self.log_area.configure(state="normal")
        num_lines = int(self.log_area.index('end-1c').split('.')[0])
        if num_lines > 300:
            self.log_area.delete("1.0", f"{num_lines - 300}.0")

        self.log_area.insert("end", f"[{timestamp_ui}]  {msg}\n")
        self.log_area.see("end")
        self.log_area.configure(state="disabled")

        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{timestamp_file}] {msg}\n")
        except Exception:
            pass

    def update_status(self, text, color):
        self.root.after(0, lambda: self.status_label.configure(text=text, text_color=color))


if __name__ == "__main__":
    if not acquire_single_instance():
        sys.exit(0)
    root = ctk.CTk()
    app = KryptexGUI(root)
    root.mainloop()
