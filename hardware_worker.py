#!/usr/bin/env python3
"""
============================================================
Forklift AI Safety System — Hardware Worker (Socket IPC Server)
============================================================
Standalone background process that bridges real-time socket
commands to the physical RM04U 4-Channel USB Relay board via serial.

Architecture Role: IPC SERVER + PURE OR-GATE SERIAL CONSUMER
  - Binds a Unix Domain Socket at /dev/shm/forklift_relay.sock
  - Accepts connections from the GUI client (hardware_manager.py)
  - Receives trigger commands via JSON-over-newline protocol
  - Applies a logical OR across all stations mapped to each relay
  - Fires physical relay commands via serial (N1-N4 / F1-F4)
    on a real edge transition (last_sent_state != OR result)
    OR when the channel state has not been retransmitted for
    HEARTBEAT_INTERVAL seconds (1 000 ms per-channel sync).
  - The heartbeat is transport-level only: it never alters, delays,
    debounces, or influences the OR decision in any way.
  - All application-level debounce / hold logic remains exclusively
    on the client side (hardware_manager.py / HMI).
  - Pushes hardware status updates to all connected clients

v3 ADDITION — AI/GUI LIVENESS WATCHDOG (fail-to-alarm):
  The heartbeat described above is a transport-level relay-sync signal
  only; it says nothing about whether the AI/GUI process is actually
  alive and running inference. Without a separate liveness check, a
  frozen or crashed ai_gui_system.py while the hazard zone was clear
  leaves every relay silently latched OFF forever — the system reports
  no error, but no detection logic is running behind it.
  This version adds an explicit AI-liveness watchdog: the GUI client
  must send {"cmd":"heartbeat"} at least once every AI_WATCHDOG_TIMEOUT
  seconds. If no client has ever connected, or the last heartbeat is
  older than AI_WATCHDOG_TIMEOUT (after an AI_WATCHDOG_CHECK_GRACE
  startup grace period), the watchdog trips and every relay channel
  listed in watchdog_covered_relays (config.json) is forced ON —
  fail-to-alarm, not fail-silent — until a heartbeat resumes. This is
  reported to clients as a separate "ai_watchdog_tripped" boolean on
  the status broadcast; hw_status (ONLINE/OFFLINE) still reflects only
  the physical serial connection, unchanged, so older clients remain
  compatible and simply ignore the new field until patched to read it.
============================================================
"""

import os
import sys
import json
import time
import glob
import threading
import socket

try:
    import serial
except ImportError:
    print("[HARDWARE] FATAL: pyserial not installed. Run: pip install pyserial")
    sys.exit(1)


# ============================================================
# CONFIGURATION
# ============================================================
SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
SOCKET_PATH     = "/dev/shm/forklift_relay.sock"
LOCK_PATH       = "/tmp/hardware_worker.lock"
UDEV_RELAY_PATH = "/dev/forklift_relay"   # Persistent udev symlink — primary source

BAUD_RATE          = 9600
SERIAL_TIMEOUT     = 1
POLL_INTERVAL      = 0.05    # 50ms polling loop
RECONNECT_INTERVAL = 3.0     # Seconds between reconnect attempts
SELF_TEST_PULSE    = 0.3     # Seconds each relay stays ON during self-test
HEARTBEAT_INTERVAL = 1.0     # Seconds between heartbeat retransmits (per channel)

# --- v3: AI/GUI liveness watchdog (fail-to-alarm) ---
AI_WATCHDOG_TIMEOUT      = 3.0   # Seconds without a client heartbeat before tripping
AI_WATCHDOG_CHECK_GRACE  = 5.0   # Seconds after worker boot before the watchdog is armed


# ============================================================
# PORT DETECTION
# ============================================================
def get_config_port():
    """Reads com_port from config.json with up to 10 retries for I/O contention."""
    config_path = os.path.join(SCRIPT_DIR, "config.json")
    for _ in range(10):
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
            port = data.get("settings", {}).get("com_port")
            if port:
                return port
        except (json.JSONDecodeError, IOError, OSError, PermissionError):
            time.sleep(0.01)
    return None


def get_serial_port():
    """
    Returns the serial port path in deterministic priority order:
      1. /dev/forklift_relay  — udev persistent symlink (canonical)
      2. config.json com_port — operator-configured path
      3. First /dev/ttyUSB* or /dev/ttyACM* device — last resort fallback
    """
    if os.path.exists(UDEV_RELAY_PATH):
        return UDEV_RELAY_PATH

    port = get_config_port()
    if port and not port.startswith("COM") and os.path.exists(port):
        return port

    ports = glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*")
    return ports[0] if ports else "/dev/ttyUSB0"


def is_port_present(port_name):
    """Checks whether a serial device node exists on the filesystem."""
    return bool(port_name) and os.path.exists(port_name)


def open_serial_connection(port_name):
    if not is_port_present(port_name):
        return None

    print(f"[HARDWARE] Port {port_name} detected. Opening at {BAUD_RATE} baud...")
    try:
        ser = serial.Serial(port_name, BAUD_RATE, timeout=SERIAL_TIMEOUT)
        time.sleep(2)  # CH340 firmware stabilisation delay
        if ser.is_open:
            print(f"[HARDWARE] Connected on {port_name}")
            return ser
        return None
    except serial.SerialException as e:
        print(f"[HARDWARE] SERIAL ERROR: {e}")
        return None
    except Exception as e:
        print(f"[HARDWARE] ERROR: {e}")
        return None


# ============================================================
# CONFIGURATION LOADER
# ============================================================
def load_initial_config():
    config_path = os.path.join(SCRIPT_DIR, "config.json")
    defaults = {
        "on_delay":              0.2,
        "off_delay":             1.5,
        "boot_self_test":        1,
        "hardware_coil_enabled": 1,
        "relay_mapping":         [1, 2, 3, 4],
        "ai_watchdog_enabled":   1,
        "watchdog_covered_relays": [1, 2, 3, 4],
    }
    try:
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                data = json.load(f)
            settings = data.get("settings", {})
            for key in ["on_delay", "off_delay", "boot_self_test", "hardware_coil_enabled",
                        "ai_watchdog_enabled"]:
                if key in settings:
                    defaults[key] = settings[key]
            if "relay_mapping" in settings:
                defaults["relay_mapping"] = settings["relay_mapping"]
            if "watchdog_covered_relays" in settings:
                defaults["watchdog_covered_relays"] = settings["watchdog_covered_relays"]
    except Exception as e:
        print(f"[HARDWARE] Warning: Could not read config.json: {e}")
    return defaults


# ============================================================
# THREAD-SAFE SHARED STATE
# ============================================================
class SharedState:
    def __init__(self):
        self._lock  = threading.Lock()
        config      = load_initial_config()
        mapping     = config.get("relay_mapping", [1, 2, 3, 4])
        while len(mapping) < 4:
            mapping.append(len(mapping) + 1)

        self._data = {
            "station_1_trigger":     0,
            "station_2_trigger":     0,
            "station_3_trigger":     0,
            "station_4_trigger":     0,
            "station_1_relay":       mapping[0],
            "station_2_relay":       mapping[1],
            "station_3_relay":       mapping[2],
            "station_4_relay":       mapping[3],
            "manual_test":           0,
            "boot_self_test":        config.get("boot_self_test", 1),
            "hardware_coil_enabled": config.get("hardware_coil_enabled", 1),
            "hw_status":             "OFFLINE",
        }

        # --- v3: AI/GUI liveness watchdog bookkeeping ---
        self._worker_start_time       = time.monotonic()
        self._last_client_heartbeat   = None   # monotonic ts of most recent heartbeat, any client
        self._connected_client_count  = 0
        self._any_client_ever_connected = False
        self._watchdog_tripped        = False
        self._ai_watchdog_enabled     = bool(int(config.get("ai_watchdog_enabled", 1)))
        covered = config.get("watchdog_covered_relays", [1, 2, 3, 4])
        self._watchdog_covered_relays = [int(c) for c in covered if 1 <= int(c) <= 4]

    def note_client_connected(self):
        with self._lock:
            self._connected_client_count += 1
            self._any_client_ever_connected = True

    def note_client_disconnected(self, remaining_clients):
        with self._lock:
            self._connected_client_count = remaining_clients

    def note_heartbeat(self):
        with self._lock:
            self._last_client_heartbeat = time.monotonic()

    def watchdog_should_trip(self):
        """
        Returns True if the AI/GUI liveness watchdog should be (or remain)
        tripped. Decision order:
          1. Watchdog disabled in config -> never trips.
          2. Still within startup grace period -> never trips (lets the
             GUI finish starting up and send its first heartbeat).
          3. No client has EVER connected -> trips (nothing is driving
             detection at all).
          4. A client connected but never sent a heartbeat -> trips.
          5. Otherwise: trips iff the most recent heartbeat is older than
             AI_WATCHDOG_TIMEOUT seconds.
        """
        with self._lock:
            if not self._ai_watchdog_enabled:
                return False
            now = time.monotonic()
            if now - self._worker_start_time < AI_WATCHDOG_CHECK_GRACE:
                return False
            if not self._any_client_ever_connected:
                return True
            if self._last_client_heartbeat is None:
                return True
            return (now - self._last_client_heartbeat) > AI_WATCHDOG_TIMEOUT

    def set_watchdog_tripped(self, tripped):
        """Returns True only when the tripped/clear state actually changed."""
        with self._lock:
            if self._watchdog_tripped == tripped:
                return False
            self._watchdog_tripped = tripped
            return True

    def is_watchdog_tripped(self):
        with self._lock:
            return self._watchdog_tripped

    def get_watchdog_covered_relays(self):
        with self._lock:
            return list(self._watchdog_covered_relays)

    def get_snapshot(self):
        with self._lock:
            return dict(self._data)

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key, value):
        with self._lock:
            self._data[key] = value

    def update(self, updates):
        with self._lock:
            self._data.update(updates)

    def set_hw_status(self, status_str):
        with self._lock:
            if self._data.get("hw_status") == status_str:
                return False
            self._data["hw_status"] = status_str
            return True

    def reset_triggers(self):
        with self._lock:
            for i in range(1, 5):
                self._data[f"station_{i}_trigger"] = 0
            self._data["manual_test"] = 0


# ============================================================
# UNIX DOMAIN SOCKET IPC SERVER
# ============================================================
class IPCServer:
    def __init__(self, socket_path, shared_state):
        self.socket_path   = socket_path
        self.state         = shared_state
        self.clients       = []
        self.clients_lock  = threading.Lock()
        self.server_socket = None
        self.running       = False

    def start(self):
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except Exception:
            pass

        self.server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server_socket.bind(self.socket_path)
        os.chmod(self.socket_path, 0o660)   # Owner + group only (hardened from 0o777)
        self.server_socket.listen(5)
        self.server_socket.settimeout(1.0)
        self.running = True

        print(f"[IPC] Unix Domain Socket server listening: {self.socket_path}")

        while self.running:
            try:
                client_sock, _ = self.server_socket.accept()
                threading.Thread(
                    target=self._handle_client, args=(client_sock,), daemon=True
                ).start()
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    print("[IPC] Server accept() error.")
                break

    def _handle_client(self, client_sock):
        with self.clients_lock:
            self.clients.append(client_sock)
        self.state.note_client_connected()

        self._send_to_client(client_sock, self._status_payload())

        recv_buffer = ""
        try:
            client_sock.settimeout(0.5)
            while self.running:
                try:
                    data = client_sock.recv(4096)
                    if not data:
                        break
                    recv_buffer += data.decode("utf-8", errors="replace")
                    while "\n" in recv_buffer:
                        line, recv_buffer = recv_buffer.split("\n", 1)
                        line = line.strip()
                        if line:
                            self._process_command(line, client_sock)
                except socket.timeout:
                    continue
                except (ConnectionResetError, BrokenPipeError):
                    break
        finally:
            with self.clients_lock:
                if client_sock in self.clients:
                    self.clients.remove(client_sock)
                remaining = len(self.clients)
            self.state.note_client_disconnected(remaining)
            try:
                client_sock.close()
            except Exception:
                pass

    def _process_command(self, raw_json, client_sock):
        try:
            msg = json.loads(raw_json)
        except json.JSONDecodeError:
            return

        cmd = msg.get("cmd")

        if cmd == "trigger":
            station = msg.get("station")
            if isinstance(station, int) and 1 <= station <= 4:
                self.state.set(f"station_{station}_trigger", int(msg.get("state", 0)))
                if msg.get("manual"):
                    self.state.set("manual_test", 1)
                elif not msg.get("any_manual_active", False):
                    self.state.set("manual_test", 0)
            self._send_ack(client_sock, cmd, True)

        elif cmd == "manual_test":
            self.state.set("manual_test", int(msg.get("enabled", 0)))
            self._send_ack(client_sock, cmd, True)

        elif cmd == "config":
            # on_delay / off_delay are client-side HMI timer values only.
            # The OR-gate worker does not consume them; only hardware_coil_enabled
            # and boot_self_test are relevant server-side settings.
            updates = {
                k: msg[k] for k in
                ["boot_self_test", "hardware_coil_enabled"]
                if k in msg
            }
            if updates:
                self.state.update(updates)
            self._send_ack(client_sock, cmd, True)

        elif cmd == "relay_mapping":
            mapping = msg.get("mapping", [1, 2, 3, 4])
            updates = {
                f"station_{i + 1}_relay": int(mapping[i])
                for i in range(min(4, len(mapping)))
            }
            if updates:
                self.state.update(updates)
            self._send_ack(client_sock, cmd, True)

        elif cmd == "heartbeat":
            self.state.note_heartbeat()
            self._send_ack(client_sock, cmd, True)

        elif cmd == "connect":
            self.state.reset_triggers()
            self.state.note_heartbeat()  # connect counts as the first heartbeat
            # Accept boot_self_test and hardware_coil_enabled from the connect
            # handshake.  on_delay / off_delay are intentionally ignored here —
            # they are client-side debounce timers, not server-side parameters.
            updates = {
                k: msg[k] for k in
                ["boot_self_test", "hardware_coil_enabled"]
                if k in msg
            }
            if "relay_mapping" in msg:
                mapping = msg["relay_mapping"]
                updates.update({
                    f"station_{i + 1}_relay": int(mapping[i])
                    for i in range(min(4, len(mapping)))
                })
            if updates:
                self.state.update(updates)
            self._send_ack(client_sock, cmd, True)
            self._send_to_client(client_sock, self._status_payload())

        elif cmd == "disconnect":
            self.state.reset_triggers()
            self._send_ack(client_sock, cmd, True)

        elif cmd == "status":
            self._send_to_client(client_sock, self._status_payload())

    def _status_payload(self):
        return {
            "type": "status",
            "hw_status": self.state.get("hw_status", "OFFLINE"),
            "ai_watchdog_tripped": self.state.is_watchdog_tripped(),
        }

    def _send_ack(self, client_sock, cmd, success):
        self._send_to_client(client_sock, {
            "type": "ack", "cmd": cmd, "success": success,
        })

    def _send_to_client(self, client_sock, msg_dict):
        try:
            client_sock.sendall((json.dumps(msg_dict) + "\n").encode("utf-8"))
        except Exception:
            pass

    def broadcast_status(self):
        data = (json.dumps(self._status_payload()) + "\n").encode("utf-8")

        with self.clients_lock:
            dead = []
            for c in self.clients:
                try:
                    c.sendall(data)
                except Exception:
                    dead.append(c)
            for c in dead:
                self.clients.remove(c)
                try:
                    c.close()
                except Exception:
                    pass

    def stop(self):
        self.running = False
        with self.clients_lock:
            for c in self.clients:
                try:
                    c.close()
                except Exception:
                    pass
            self.clients.clear()
        if self.server_socket:
            try:
                self.server_socket.close()
            except Exception:
                pass
        try:
            os.unlink(self.socket_path)
        except Exception:
            pass


# ============================================================
# BOOT SELF-TEST SEQUENCE
# ============================================================
def run_boot_self_test(ser):
    print("=" * 60)
    print("[HARDWARE] BOOT SELF-TEST: Sequential channel check...")
    print("=" * 60)

    for ch in range(1, 5):
        try:
            ser.write(f"N{ch}".encode("utf-8"))
            ser.flush()
            print(f"[SELF-TEST] Ch{ch} -> ON")
            time.sleep(SELF_TEST_PULSE)

            ser.write(f"F{ch}".encode("utf-8"))
            ser.flush()
            print(f"[SELF-TEST] Ch{ch} -> OFF")
            time.sleep(SELF_TEST_PULSE)

        except Exception as e:
            print(f"[SELF-TEST] FAILED on Ch{ch}: {e}")
            return False

    print("[HARDWARE] BOOT SELF-TEST: PASSED — All 4 channels healthy.")
    print("=" * 60)
    return True


# ============================================================
# PURE OR-GATE SERIAL WORKER  (zero internal timers)
# ============================================================
class HardwareWorker:
    """
    Software-defined OR gate with per-channel heartbeat retransmission.

    Every 50 ms cycle:
      1. Reads the current station trigger states from SharedState.
      2. OR-reduces them onto physical relay channels via relay_mapping.
      3. Transmits an ASCII serial command (N<ch> / F<ch>) when EITHER:
           a) Edge trigger   — the computed OR result differs from the
              last physically sent state (zero-latency, immediate).
           b) Heartbeat      — HEARTBEAT_INTERVAL seconds have elapsed
              since the last transmit for that channel and the channel
              has been transmitted at least once (transport-level sync).

    The heartbeat is transport-only.  It never modifies, delays, filters,
    or otherwise influences the OR decision logic.  All application-level
    debounce / hold logic remains exclusively on the client side
    (hardware_manager.py / HMI).

    Per-channel heartbeat rules:
      - Each channel keeps its own independent timer (_last_tx_time[ch]).
      - Any transmit() call (edge or heartbeat) resets that channel's timer.
      - Heartbeat fires at most once per HEARTBEAT_INTERVAL (flood-safe).
      - Heartbeat never fires for a channel that has never been transmitted
        (boot state, last_sent_state[ch] is None).
    """

    def __init__(self, serial_handle, shared_state, ipc_server):
        self.ser   = serial_handle
        self.state = shared_state
        self.ipc   = ipc_server

        # Tracks the last physically transmitted state per relay channel.
        # None = never transmitted (boot state).  0 = OFF.  1 = ON.
        # Drives the edge-trigger condition in Phase 3.
        self.last_sent_state = {1: None, 2: None, 3: None, 4: None}

        # Per-channel wall-clock timestamp of the most recent transmit().
        # Used exclusively by the heartbeat logic to measure elapsed time.
        # Initialised to -inf so that the first edge-transmit is never
        # delayed waiting for a heartbeat window to expire.
        self._last_tx_time = {1: float("-inf"),
                              2: float("-inf"),
                              3: float("-inf"),
                              4: float("-inf")}

    def transmit(self, channel, state_on):
        """Write one relay command to the serial port and update tracking state.

        Called for both edge-triggered and heartbeat retransmissions.
        Updates last_sent_state and _last_tx_time unconditionally so that
        every call correctly resets the heartbeat timer for that channel.

        Low-latency flushing strategy (Requirement 1):
          After pyserial's write()+flush(), an fcntl.ioctl(fd, TCSBRK, 1)
          syscall is issued to actively drain the Linux tty/serial kernel
          ring buffer, forcing immediate dispatch over the physical USB bus
          to the CH340 chip without OS-level text-mode aggregation delays.

        Exception shielding (Requirement 2):
          - POSIX ioctl call: wrapped in a standalone try/except that
            silently ignores any failure (non-POSIX platforms, restricted
            environments, or environments without fcntl/termios).
          - Physical write/port errors: caught, logged as
            [HARDWARE] TRANSMIT PHYSICAL ERROR: {error}, then re-raised
            as serial.SerialException to let the caller handle reconnect.

        State updates (Requirement 3):
          last_sent_state[channel] and _last_tx_time[channel] are updated
          unconditionally after a successful write, matching existing logic.
        """
        if self.ser is None or not self.ser.is_open:
            raise serial.SerialException("Serial handle is not open")

        cmd = ("N" if state_on else "F") + str(channel)

        # --- Physical write — catch and escalate hardware-level I/O errors ---
        try:
            self.ser.write(cmd.encode("utf-8"))
            self.ser.flush()
        except Exception as e:
            print(f"[HARDWARE] TRANSMIT PHYSICAL ERROR: {e}")
            raise serial.SerialException(str(e)) from e

        # --- POSIX kernel-level serial buffer purge ---
        # termios.TCSBRK with arg=1 is semantically equivalent to tcdrain():
        # it blocks until all data in the kernel output ring buffer has been
        # physically transmitted over the USB bus to the CH340 chip.
        # Invoking it via fcntl.ioctl bypasses any Python/pyserial buffering
        # and reaches the Linux tty subsystem directly, eliminating the
        # kernel text-mode aggregation that delays short serial byte writes
        # (e.g. 'N1', 'F1') during periodic heartbeat intervals.
        # Gracefully no-ops if fcntl or termios are unavailable (non-POSIX
        # platforms, containers with restricted syscall tables, etc.).
        try:
            import fcntl
            import termios
            fcntl.ioctl(self.ser.fileno(), termios.TCSBRK, 1)
        except Exception:
            pass  # Non-POSIX or restricted environment — silently ignored

        # --- Unconditional state tracking (Requirement 3) ---
        self.last_sent_state[channel] = 1 if state_on else 0
        self._last_tx_time[channel]   = time.monotonic()
        print(f"[HARDWARE] TX Ch{channel} -> {'ON' if state_on else 'OFF'} ({cmd})")

    def run(self):
        print("[HARDWARE] OR-Gate Consumer STARTED — 50ms poll / 1s heartbeat per channel")
        print("=" * 60)

        if self.state.set_hw_status("ONLINE"):
            self.ipc.broadcast_status()

        while True:
            try:
                now          = time.monotonic()
                config       = self.state.get_snapshot()
                coil_enabled = int(config.get("hardware_coil_enabled", 1)) == 1
                is_manual    = int(config.get("manual_test", 0)) == 1

                # --- Phase 1: Build an explicit per-station state registry ---
                # Each station's trigger value is read from SharedState and
                # normalised to a strict 0/1 integer every cycle.  A state=0
                # message from any station is reflected within this 50ms tick.
                station_states = {}
                for station in range(1, 5):
                    raw = config.get(f"station_{station}_trigger", 0)
                    if isinstance(raw, str):
                        station_states[station] = 1 if raw.strip().upper() == "ON" else 0
                    else:
                        station_states[station] = 1 if int(raw) == 1 else 0

                # --- Phase 2: OR-reduce station states onto physical relay targets ---
                # Relay target = 1  if ANY station mapped to that relay has state == 1
                # Relay target = 0  ONLY when ALL mapped stations have state == 0
                # This block is completely unchanged from the original logic.
                targets = {1: 0, 2: 0, 3: 0, 4: 0}
                if coil_enabled or is_manual:
                    for relay_ch in range(1, 5):
                        for station in range(1, 5):
                            mapped_relay = int(config.get(f"station_{station}_relay", station))
                            if mapped_relay == relay_ch and station_states[station] == 1:
                                targets[relay_ch] = 1
                                break  # OR satisfied — no need to check remaining stations

                # --- Phase 2b (v3): AI/GUI liveness watchdog override ---
                # If the GUI/AI process has stopped sending heartbeats
                # (crash, deadlock, or it never connected at all), force
                # every relay listed in watchdog_covered_relays ON —
                # fail-to-alarm, not fail-silent.
                #
                # Deliberately gated on `coil_enabled`, same as Phase 2:
                # hardware_coil_enabled=0 is an explicit human decision
                # (e.g. maintenance) to disable physical outputs. The
                # watchdog protects against an UNNOTICED AI failure; it
                # must never override a technician's deliberate, explicit
                # disable — forcing relays ON during a maintenance window
                # the technician intentionally opened would itself be an
                # unexpected-actuation hazard. Note this does NOT apply to
                # is_manual (manual_test), which is a momentary diagnostic
                # pulse, not a standing disable.
                tripped_now = self.state.watchdog_should_trip()
                if self.state.set_watchdog_tripped(tripped_now):
                    print(f"[HARDWARE] AI WATCHDOG "
                          f"{'TRIPPED — forcing covered relays ON' if tripped_now else 'CLEARED — AI heartbeat resumed'}")
                    self.ipc.broadcast_status()
                if tripped_now and coil_enabled:
                    for ch in self.state.get_watchdog_covered_relays():
                        targets[ch] = 1

                # --- Phase 3: Edge-triggered output + heartbeat retransmission ---
                #
                # Transmit on EITHER condition (evaluated independently):
                #
                #   (A) Edge trigger  — desired OR output differs from the last
                #       physically sent state.  This path has ZERO added latency:
                #       the transmit fires in the same 50 ms tick the state changes.
                #
                #   (B) Heartbeat     — OR output is stable AND the channel has
                #       already been transmitted at least once AND more than
                #       HEARTBEAT_INTERVAL seconds have elapsed since the last
                #       transmit for this channel.
                #
                # The two conditions are checked with an OR so that (A) always
                # takes priority: if the state just changed, the edge path fires
                # and resets _last_tx_time, which naturally defers the next
                # heartbeat by another full HEARTBEAT_INTERVAL.
                #
                # The OR decision (targets dict) is read here but NEVER written.
                # The heartbeat path only calls transmit() with the same `desired`
                # value that was already last_sent_state — it cannot change the
                # logical output.
                for ch in range(1, 5):
                    desired    = targets[ch]
                    is_edge    = (desired != self.last_sent_state[ch])
                    elapsed    = now - self._last_tx_time[ch]
                    is_hb      = (
                        self.last_sent_state[ch] is not None        # at least one prior TX
                        and elapsed >= HEARTBEAT_INTERVAL            # interval expired
                        and desired == self.last_sent_state[ch]      # state unchanged (belt+braces)
                    )

                    if is_edge:
                        # Edge-triggered: state changed — transmit immediately.
                        self.transmit(ch, bool(desired))
                    elif is_hb:
                        # Heartbeat: state stable — retransmit for relay sync.
                        self.transmit(ch, bool(desired))

                time.sleep(POLL_INTERVAL)

            except KeyboardInterrupt:
                # Safe shutdown: turn off any relay that is currently ON.
                for ch in range(1, 5):
                    if self.last_sent_state[ch] == 1:
                        try:
                            self.transmit(ch, False)
                        except Exception:
                            pass
                return True

            except serial.SerialException as e:
                print(f"[HARDWARE] SERIAL EXCEPTION: {e}")
                self._teardown()
                if self.state.set_hw_status("OFFLINE"):
                    self.ipc.broadcast_status()
                return False

            except Exception as e:
                print(f"[HARDWARE] WARNING: Unhandled exception in poll loop: {type(e).__name__}: {e}")
                time.sleep(POLL_INTERVAL)

    def _teardown(self):
        if self.ser is not None:
            try:
                if self.ser.is_open:
                    self.ser.close()
            except Exception:
                pass
            self.ser = None


# ============================================================
# RECONNECT LOOP
# ============================================================
def run_worker_thread(shared_state, ipc_server):
    while True:
        target_port = get_serial_port()
        ser         = open_serial_connection(target_port)

        if ser is None:
            if shared_state.set_hw_status("OFFLINE"):
                ipc_server.broadcast_status()
            time.sleep(RECONNECT_INTERVAL)
            continue

        if int(shared_state.get("boot_self_test", 0)) == 1:
            if not run_boot_self_test(ser):
                if shared_state.set_hw_status("OFFLINE"):
                    ipc_server.broadcast_status()
                try:
                    ser.close()
                except Exception:
                    pass
                time.sleep(RECONNECT_INTERVAL)
                continue

        if shared_state.set_hw_status("ONLINE"):
            ipc_server.broadcast_status()

        worker = HardwareWorker(ser, shared_state, ipc_server)

        try:
            clean_exit = worker.run()
        except KeyboardInterrupt:
            clean_exit = True
        finally:
            if shared_state.set_hw_status("OFFLINE"):
                ipc_server.broadcast_status()
            if ser is not None:
                try:
                    if ser.is_open:
                        ser.close()
                except Exception:
                    pass

        if clean_exit:
            break
        time.sleep(RECONNECT_INTERVAL)


# ============================================================
# SINGLETON GUARD
# ============================================================
def check_singleton():
    """Prevents duplicate hardware_worker instances from competing for the serial port."""
    try:
        import fcntl
        lock_file = open(LOCK_PATH, 'w')
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lock_file
    except IOError:
        print("[HARDWARE] FATAL: Another instance is already running. Exiting.")
        sys.exit(0)


# ============================================================
# ENTRY POINT
# ============================================================
def main():
    _singleton_lock = check_singleton()

    print("=" * 60)
    print(" Limitless Future — Hardware Worker (Socket IPC Server)")
    print(f" Protocol: RM04U ASCII (N1-N4 / F1-F4)")
    print(f" IPC:      Unix Domain Socket ({SOCKET_PATH})")
    print("=" * 60)

    shared_state = SharedState()
    ipc_server   = IPCServer(SOCKET_PATH, shared_state)

    threading.Thread(target=ipc_server.start, daemon=True).start()
    time.sleep(0.3)  # Allow socket to bind before worker thread starts

    worker_thread = threading.Thread(
        target=run_worker_thread, args=(shared_state, ipc_server), daemon=True
    )
    worker_thread.start()

    try:
        while worker_thread.is_alive():
            time.sleep(1.0)
    except KeyboardInterrupt:
        if shared_state.set_hw_status("OFFLINE"):
            ipc_server.broadcast_status()

    ipc_server.stop()
    print("[HARDWARE] Worker terminated.")


if __name__ == "__main__":
    main()
