import argparse
import datetime as dt
import json
import os
import re
import socket
import ssl
import subprocess
import threading
import time
import uuid

import paho.mqtt.client as mqtt

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
RESULTS = os.environ.get("RESULTS_DIR", "/results")
IPERF_HOST = os.environ.get("IPERF_HOST", "iperf-server")
BROKER = os.environ.get("BROKER_HOST", "mosquitto")
CA_FILE = os.environ.get("CA_FILE", "/certs/server.crt")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def iface_for(host):
    ip = socket.gethostbyname(host)
    out = subprocess.check_output(["ip", "-o", "route", "get", ip], text=True)
    return re.search(r"dev (\S+)", out).group(1)


def set_netem(dev, lvl):
    base = ["tc", "qdisc", "replace", "dev", dev, "root", "netem",
            "limit", "10000", "delay", f"{lvl['delay_ms']}ms", f"{lvl['jitter_ms']}ms"]
    tail = ["loss", f"{lvl['loss_pct']}%"] if lvl["loss_pct"] > 0 else []
    r = subprocess.run(base + ["distribution", "normal"] + tail, capture_output=True, text=True)
    if r.returncode != 0:  # на случай, если нет таблицы распределений
        r = subprocess.run(base + tail, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("tc netem не применился (нужен cap_add: NET_ADMIN): " + r.stderr)


def clear_netem(dev):
    subprocess.run(["tc", "qdisc", "del", "dev", dev, "root"], capture_output=True)


def run_iperf(seconds):
    for _ in range(3):
        try:
            p = subprocess.run(
                ["iperf3", "-c", IPERF_HOST, "-t", str(seconds), "-J", "--connect-timeout", "5000"],
                capture_output=True, text=True, timeout=seconds + 60)
            j = json.loads(p.stdout)
            if "error" in j:
                continue
            e = j["end"]
            return {"mbps": e["sum_received"]["bits_per_second"] / 1e6,
                    "retransmits": e["sum_sent"].get("retransmits")}
        except Exception:
            continue
    return {"mbps": None, "retransmits": None}


def parse_ping(text):
    rtts = [(int(s), float(t)) for s, t in re.findall(r"icmp_seq=(\d+) .*?time=([\d.]+) ms", text)]
    m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", text)
    sent, got = (int(m.group(1)), int(m.group(2))) if m else (0, len(rtts))
    return {"sent": sent, "received": got, "rtts": rtts}


def run_ping(count, interval):
    p = subprocess.run(["ping", "-c", str(count), "-i", str(interval), "-W", "2", IPERF_HOST],
                       capture_output=True, text=True, timeout=count * (interval + 2) + 60)
    return parse_ping(p.stdout)


def run_mqtt(qos, n, rate, drain):
    """Публикует n сообщений и считает долю недоставленных и задержку доставки.
    Публикатор и подписчик в одном процессе – задержка считается по одним часам."""
    topic = f"lab2/{uuid.uuid4().hex[:8]}"
    recv, dups, lock = {}, [0], threading.Lock()
    sub_ready, sub_conn, pub_conn = threading.Event(), threading.Event(), threading.Event()
    V2 = mqtt.CallbackAPIVersion.VERSION2

    sub = mqtt.Client(V2, client_id="sub-" + uuid.uuid4().hex[:8])
    pub = mqtt.Client(V2, client_id="pub-" + uuid.uuid4().hex[:8])

    def sub_on_connect(c, u, flags, rc, props=None):
        sub_conn.set()
        c.subscribe(topic, qos)

    def sub_on_subscribe(c, u, mid, rcs, props=None):
        sub_ready.set()

    def sub_on_message(c, u, msg):
        t = time.perf_counter()
        seq_s, tp_s = msg.payload.split(b",")[:2]
        seq, tp = int(seq_s), float(tp_s)
        with lock:
            if seq in recv:
                dups[0] += 1
            else:
                recv[seq] = (t - tp) * 1000.0

    sub.on_connect, sub.on_subscribe, sub.on_message = sub_on_connect, sub_on_subscribe, sub_on_message
    pub.on_connect = lambda c, u, f, rc, p=None: pub_conn.set()
    pub.max_inflight_messages_set(100)
    pub.max_queued_messages_set(0)

    sub.connect(BROKER, 1883, keepalive=60)
    pub.connect(BROKER, 1883, keepalive=60)
    sub.loop_start()
    pub.loop_start()
    try:
        if not (sub_conn.wait(60) and sub_ready.wait(60) and pub_conn.wait(60)):
            raise RuntimeError("MQTT: не удалось подключиться/подписаться за 60 с")
        refused = 0
        t0 = time.perf_counter()
        for i in range(n):
            wait = t0 + i / rate - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
            payload = f"{i},{time.perf_counter()!r},".encode().ljust(100, b"x")
            info = pub.publish(topic, payload, qos)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                refused += 1
        deadline = time.perf_counter() + drain
        while time.perf_counter() < deadline:
            with lock:
                if len(recv) >= n:
                    break
            time.sleep(0.1)
    finally:
        pub.loop_stop()
        sub.loop_stop()
        pub.disconnect()
        sub.disconnect()
    with lock:
        lat = [recv[k] for k in sorted(recv)]
    return {"sent": n, "received": len(lat), "duplicates": dups[0], "refused": refused,
            "latencies_ms": lat}


def mqtt_connect_packet(client_id: bytes):
    var = b"\x00\x04MQTT\x04\x02\x00\x3c"          # MQTT 3.1.1, clean session, keepalive 60
    payload = len(client_id).to_bytes(2, "big") + client_id
    return b"\x10" + bytes([len(var) + len(payload)]) + var + payload


def recv_exact(s, n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("соединение закрыто")
        buf += chunk
    return buf


def connect_once(port, tls_ctx, idx):
    """Время этапов подключения в мс или None при сбое."""
    s, out = None, {}
    try:
        t0 = time.perf_counter()
        addr = socket.getaddrinfo(BROKER, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
        t1 = time.perf_counter()
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(20)
        s.connect(addr)
        t2 = time.perf_counter()
        if tls_ctx is not None:
            s = tls_ctx.wrap_socket(s, server_hostname=BROKER)   # рукопожатие TLS внутри
        t3 = time.perf_counter()
        s.sendall(mqtt_connect_packet(f"lab2c{os.getpid()}-{idx}".encode()))
        ack = recv_exact(s, 4)
        t4 = time.perf_counter()
        if ack[0] != 0x20 or ack[3] != 0:
            return None
        out = {"dns_ms": (t1 - t0) * 1e3, "tcp_ms": (t2 - t1) * 1e3,
               "tls_ms": (t3 - t2) * 1e3 if tls_ctx is not None else 0.0,
               "mqtt_ms": (t4 - t3) * 1e3, "total_ms": (t4 - t0) * 1e3}
        s.sendall(b"\xe0\x00")                                    # DISCONNECT
        return out
    except Exception:
        return None
    finally:
        try:
            if s is not None:
                s.close()
        except Exception:
            pass


def run_connect(repeats):
    ctx = ssl.create_default_context(cafile=CA_FILE)
    return {"plain": [connect_once(1883, None, i) for i in range(repeats)],
            "tls": [connect_once(8883, ctx, i) for i in range(repeats)]}


def tool_versions():
    def first(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True).stdout.splitlines()[0]
        except Exception:
            return "?"
    return {"iperf3": first(["iperf3", "--version"]), "ping": first(["ping", "-V"]),
            "paho": getattr(mqtt, "__version__", "2.x")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="быстрая проверка: 1 серия, меньше замеров")
    ap.add_argument("--series", type=int)
    ap.add_argument("--fresh", action="store_true", help="удалить старые raw.jsonl перед стартом")
    args = ap.parse_args()

    m = dict(CFG["measure"])
    if args.quick:
        m.update(series=1, iperf_seconds=4, ping_count=50, mqtt_messages=200,
                 mqtt_drain_s=5, connect_repeats=8)
    if args.series:
        m["series"] = args.series

    os.makedirs(RESULTS, exist_ok=True)
    raw_path = os.path.join(RESULTS, "raw.jsonl")
    if args.fresh and os.path.exists(raw_path):
        os.remove(raw_path)
    dev = iface_for(IPERF_HOST)
    log(f"интерфейс: {dev}; серий: {m['series']}; уровней: {len(CFG['levels'])}")

    with open(os.path.join(RESULTS, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"started": dt.datetime.now().isoformat(timespec="seconds"), "quick": args.quick,
                   "measure": m, "tools": tool_versions()}, f, ensure_ascii=False, indent=2)

    out = open(raw_path, "a", encoding="utf-8")

    def save(series, level, kind, data):
        out.write(json.dumps({"series": series, "level": level, "kind": kind, "data": data},
                             ensure_ascii=False) + "\n")
        out.flush()

    try:
        for s in range(1, m["series"] + 1):
            for lvl in CFG["levels"]:
                name = lvl["name"]
                log(f"=== серия {s}/{m['series']}, {name}: потери {lvl['loss_pct']} %, "
                    f"задержка {lvl['delay_ms']}±{lvl['jitter_ms']} мс")
                set_netem(dev, lvl)
                time.sleep(1)

                d = run_iperf(m["iperf_seconds"])
                save(s, name, "iperf", d)
                log(f"  iperf3: {d['mbps'] if d['mbps'] is None else round(d['mbps'], 2)} Мбит/с")

                d = run_ping(m["ping_count"], m["ping_interval_s"])
                save(s, name, "ping", d)
                log(f"  ping: получено {d['received']}/{d['sent']}")

                for q in m["mqtt_qos"]:
                    try:
                        d = run_mqtt(q, m["mqtt_messages"], m["mqtt_rate_per_s"], m["mqtt_drain_s"])
                    except Exception as e:
                        log(f"  MQTT QoS {q}: ошибка {e}")
                        d = {"sent": m["mqtt_messages"], "received": 0, "duplicates": 0,
                             "refused": 0, "latencies_ms": [], "error": str(e)}
                    save(s, name, f"mqtt_qos{q}", d)
                    log(f"  MQTT QoS {q}: доставлено {d['received']}/{d['sent']}")

                d = run_connect(m["connect_repeats"])
                save(s, name, "connect", d)
                ok = sum(1 for x in d["tls"] if x)
                log(f"  подключение TLS: успешно {ok}/{len(d['tls'])}")
    finally:
        clear_netem(dev)
        out.close()
        log("netem снят. Данные: " + raw_path)


if __name__ == "__main__":
    main()
