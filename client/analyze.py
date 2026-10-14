import json
import math
import os
import sys
from collections import defaultdict

import numpy as np

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
RESULTS = os.environ.get("RESULTS_DIR", "/results")
NAN = float("nan")

C = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9"]
T975 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262}

plt.rcParams.update({"font.size": 11, "axes.grid": True, "grid.alpha": 0.3,
                     "axes.spines.top": False, "axes.spines.right": False,
                     "figure.dpi": 130, "savefig.bbox": "tight"})


def clean(xs):
    return [x for x in xs if x is not None and not (isinstance(x, float) and math.isnan(x))]


def mean_ci(xs):
    """среднее и полуширина 95 % доверительного интервала (t-Стьюдента) по сериям"""
    xs = clean(xs)
    n = len(xs)
    if n == 0:
        return NAN, NAN
    m = sum(xs) / n
    if n < 2:
        return m, NAN
    s = float(np.std(xs, ddof=1))
    return m, T975.get(n - 1, 1.96) * s / math.sqrt(n)


def pcts(xs):
    xs = clean(xs)
    if not xs:
        return NAN, NAN, NAN
    return tuple(float(np.percentile(xs, q)) for q in (50, 95, 99))


def jitter(rtts):
    """средняя абсолютная разность RTT соседних пакетов, мс"""
    d = [abs(b[1] - a[1]) for a, b in zip(rtts, rtts[1:]) if b[0] - a[0] == 1]
    return sum(d) / len(d) if d else NAN


def f(x, nd=1):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "–"
    return f"{x:.{nd}f}".replace(".", ",")


def fpm(m, ci, nd=1):
    if math.isnan(m):
        return "–"
    return f(m, nd) + ("" if math.isnan(ci) else " ± " + f(ci, nd))


LEVELS = CFG["levels"]
NAMES = [l["name"] for l in LEVELS]
PL = CFG["path_loss"]
for l in LEVELS:
    l["rssi"] = PL["p0_dbm"] - 10 * PL["exponent"] * math.log10(l["distance_m"])
LV = {l["name"]: l for l in LEVELS}


def load():
    path = os.path.join(RESULTS, "raw.jsonl")
    if not os.path.exists(path):
        sys.exit(f"Нет файла {path}. Сначала запустите measure.py")
    iperf, ping, mq, conn = defaultdict(dict), defaultdict(dict), defaultdict(lambda: defaultdict(dict)), defaultdict(dict)
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        s, lv, k, d = r["series"], r["level"], r["kind"], r["data"]
        if k == "iperf":
            iperf[lv][s] = d
        elif k == "ping":
            ping[lv][s] = d
        elif k.startswith("mqtt_qos"):
            mq[lv][int(k[-1])][s] = d
        elif k == "connect":
            conn[lv][s] = d
    return iperf, ping, mq, conn


IPERF, PING, MQ, CONN = load()
META = {}
if os.path.exists(os.path.join(RESULTS, "meta.json")):
    META = json.load(open(os.path.join(RESULTS, "meta.json"), encoding="utf-8"))
QOS = sorted({q for lv in MQ.values() for q in lv}) or [0, 1, 2]
NSER = max([len(v) for v in IPERF.values()] + [1])
LEVELS = [l for l in LEVELS if l["name"] in IPERF or l["name"] in PING]
NAMES = [l["name"] for l in LEVELS]
RSSI = [l["rssi"] for l in LEVELS]


def calc():
    R = {"thr": {}, "ping": {}, "mqtt": {}, "conn": {}}
    for l in LEVELS:
        n = l["name"]
        v = [d["mbps"] for d in IPERF[n].values()]
        m, ci = mean_ci(v)
        rt = [d["retransmits"] for d in IPERF[n].values() if d["retransmits"] is not None]
        R["thr"][n] = {"vals": clean(v), "mean": m, "ci": ci, "retr": sum(rt) / len(rt) if rt else NAN,
                       "fail": sum(1 for x in v if x is None)}
        # ping
        P = PING[n]
        pooled = [t for d in P.values() for _, t in d["rtts"]]
        loss = [100.0 * (d["sent"] - d["received"]) / d["sent"] for d in P.values() if d["sent"]]
        jit = [jitter(d["rtts"]) for d in P.values()]
        p50, p95, p99 = pcts(pooled)
        lm, lci = mean_ci(loss)
        jm, jci = mean_ci(jit)
        R["ping"][n] = {"rtts": sorted(pooled), "p50": p50, "p95": p95, "p99": p99,
                        "mean": float(np.mean(pooled)) if pooled else NAN,
                        "loss": lm, "loss_ci": lci, "jit": jm, "jit_ci": jci}
        # mqtt
        R["mqtt"][n] = {}
        for q in QOS:
            D = MQ[n].get(q, {})
            lat = [x for d in D.values() for x in d["latencies_ms"]]
            ls = [100.0 * (d["sent"] - d["received"]) / d["sent"] for d in D.values() if d["sent"]]
            lm_, lci_ = mean_ci(ls)
            p = pcts(lat)
            R["mqtt"][n][q] = {"loss": lm_, "loss_ci": lci_, "p50": p[0], "p95": p[1], "p99": p[2],
                               "late1s": 100.0 * sum(1 for x in lat if x > 1000) / len(lat) if lat else NAN,
                               "dups": sum(d["duplicates"] for d in D.values()), "lat": sorted(lat)}
        # connect
        R["conn"][n] = {}
        for mode in ("plain", "tls"):
            att = [x for d in CONN[n].values() for x in d[mode]]
            ok = [x for x in att if x]
            R["conn"][n][mode] = {
                "fail": 100.0 * (len(att) - len(ok)) / len(att) if att else NAN,
                **{k: float(np.median([x[k] for x in ok])) if ok else NAN
                   for k in ("dns_ms", "tcp_ms", "tls_ms", "mqtt_ms", "total_ms")},
                "p95": float(np.percentile([x["total_ms"] for x in ok], 95)) if ok else NAN}
    return R


R = calc()
A = CFG["assumptions"]
NFR = CFG["nfr"]
ASSUMED_MS = A["wifi_assoc_ms"] + A["dhcp_ms"]


def total_connect_s(n):
    """полное подключение, с: ассоциация + DHCP (допущение) + измеренные DNS/TCP/TLS/MQTT"""
    return (ASSUMED_MS + R["conn"][n]["tls"]["total_ms"]) / 1000.0


def nfr_table():
    rows = []
    for l in LEVELS:
        n = l["name"]
        q1 = R["mqtt"][n].get(1, {})
        checks = {
            "rtt_p99_ms": (R["ping"][n]["p99"], NFR["rtt_p99_ms"], lambda v, t: v <= t),
            "mqtt_qos1_loss_pct": (q1.get("loss", NAN), NFR["mqtt_qos1_loss_pct"], lambda v, t: v <= t),
            "mqtt_qos1_p99_ms": (q1.get("p99", NAN), NFR["mqtt_qos1_p99_ms"], lambda v, t: v <= t),
            "throughput_min_mbps": (R["thr"][n]["mean"], NFR["throughput_min_mbps"], lambda v, t: v >= t),
            "connect_total_s": (total_connect_s(n), NFR["connect_total_s"], lambda v, t: v <= t),
        }
        res = {k: (v, t, (not math.isnan(v)) and ok(v, t)) for k, (v, t, ok) in checks.items()}
        rows.append((l, res))
    return rows


NFR_ROWS = nfr_table()
first_fail = next((l for l, res in NFR_ROWS if not all(x[2] for x in res.values())), None)
last_ok = None
for l, res in NFR_ROWS:
    if all(x[2] for x in res.values()):
        last_ok = l
    else:
        break

def write_csv(name, header, rows):
    with open(os.path.join(RESULTS, name), "w", encoding="utf-8-sig") as fh:
        fh.write(";".join(header) + "\n")
        for r in rows:
            fh.write(";".join(str(x).replace(".", ",") if isinstance(x, float) else str(x) for x in r) + "\n")


write_csv("table_throughput.csv", ["level", "loss_pct", "rssi_dbm", "mbps_mean", "ci95", "retransmits_mean"],
          [(l["name"], l["loss_pct"], round(l["rssi"], 1), round(R["thr"][l["name"]]["mean"], 3),
            round(R["thr"][l["name"]]["ci"], 3), round(R["thr"][l["name"]]["retr"], 1)) for l in LEVELS])
write_csv("table_rtt.csv", ["level", "loss_pct", "rssi_dbm", "p50_ms", "p95_ms", "p99_ms", "jitter_ms", "ping_loss_pct"],
          [(l["name"], l["loss_pct"], round(l["rssi"], 1), *(round(R["ping"][l["name"]][k], 2) for k in
            ("p50", "p95", "p99", "jit", "loss"))) for l in LEVELS])
write_csv("table_mqtt.csv", ["level", "qos", "loss_pct", "p50_ms", "p95_ms", "p99_ms", "late_over_1s_pct", "duplicates"],
          [(l["name"], q, *(round(R["mqtt"][l["name"]][q][k], 2) for k in ("loss", "p50", "p95", "p99", "late1s")),
            R["mqtt"][l["name"]][q]["dups"]) for l in LEVELS for q in QOS])
write_csv("table_connect.csv", ["level", "mode", "dns_ms", "tcp_ms", "tls_ms", "mqtt_ms", "total_ms", "fail_pct"],
          [(l["name"], m, *(round(R["conn"][l["name"]][m][k], 2) for k in
            ("dns_ms", "tcp_ms", "tls_ms", "mqtt_ms", "total_ms", "fail"))) for l in LEVELS for m in ("plain", "tls")])


def xaxis_rssi(ax):
    ax.set_xlabel("RSSI (условный, по модели), дБм  →  сигнал слабее")
    ax.invert_xaxis()


def tag(ax):
    for l in LEVELS:
        ax.annotate(f"{l['name']}\n{l['distance_m']} м", (l["rssi"], ax.get_ylim()[0]), xytext=(0, 4),
                    textcoords="offset points", ha="center", fontsize=8, color="#555")


def fig1():
    d = np.linspace(1, 32, 200)
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot(d, PL["p0_dbm"] - 10 * PL["exponent"] * np.log10(d), color=C[0], lw=2, label="модель path loss")
    ax.scatter([l["distance_m"] for l in LEVELS], RSSI, color=C[1], zorder=3, s=45, label="уровни эксперимента")
    for l in LEVELS:
        ax.annotate(f"{l['name']}: потери {l['loss_pct']} %", (l["distance_m"], l["rssi"]),
                    xytext=(6, 6), textcoords="offset points", fontsize=9)
    ax.set_xlabel("Расстояние, м")
    ax.set_ylabel("RSSI, дБм")
    ax.set_title(f"RSSI(d): RSSI = {PL['p0_dbm']} − 10·{PL['exponent']}·lg(d)", fontsize=11)
    ax.legend(frameon=False)
    fig.savefig(os.path.join(RESULTS, "fig1_rssi_distance.png"))
    plt.close(fig)


def fig2():
    fig, ax = plt.subplots(figsize=(7, 4))
    m = [R["thr"][n]["mean"] for n in NAMES]
    ci = [0 if math.isnan(R["thr"][n]["ci"]) else R["thr"][n]["ci"] for n in NAMES]
    ax.errorbar(RSSI, m, yerr=ci, color=C[0], marker="o", capsize=4, lw=2)
    use_log = all(v > 0 for v in m if not math.isnan(v))
    if use_log:
        ax.set_yscale("log")
    ax.set_ylabel("Throughput TCP, Мбит/с" + (" (лог. шкала)" if use_log else ""))
    ax.set_title(f"Throughput от RSSI (среднее по {NSER} сериям, 95 % ДИ)", fontsize=11)
    xaxis_rssi(ax)
    for l, y in zip(LEVELS, m):
        ax.annotate(f"{l['name']} ({l['loss_pct']} %)", (l["rssi"], y), xytext=(0, 9),
                    textcoords="offset points", ha="center", fontsize=8)
    fig.savefig(os.path.join(RESULTS, "fig2_throughput_rssi.png"))
    plt.close(fig)


def fig3():
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot(RSSI, [l["loss_pct"] for l in LEVELS], "--", color="#888", label="задано в netem")
    def line(vals, cis, label, color, mk):
        ax.errorbar(RSSI, vals, yerr=[0 if math.isnan(c) else c for c in cis], color=color, marker=mk,
                    capsize=3, lw=1.8, label=label)
    line([R["ping"][n]["loss"] for n in NAMES], [R["ping"][n]["loss_ci"] for n in NAMES], "ping (ICMP)", C[0], "o")
    for q, c, mk in zip(QOS, C[1:], "svD"):
        line([R["mqtt"][n][q]["loss"] for n in NAMES], [R["mqtt"][n][q]["loss_ci"] for n in NAMES],
             f"MQTT QoS {q} (не доставлено)", c, mk)
    ax.set_ylabel("Потери, %")
    ax.set_title("Потери от RSSI (95 % ДИ по сериям)", fontsize=11)
    xaxis_rssi(ax)
    ax.legend(frameon=False, fontsize=9)
    fig.savefig(os.path.join(RESULTS, "fig3_loss_rssi.png"))
    plt.close(fig)


def fig4():
    fig, ax = plt.subplots(figsize=(7, 4.2))
    for i, l in enumerate(LEVELS):
        x = R["ping"][l["name"]]["rtts"]
        if not x:
            continue
        ax.plot(x, np.arange(1, len(x) + 1) / len(x), color=C[i % len(C)], lw=1.8,
                label=f"{l['name']}: {l['loss_pct']} %, {f(l['rssi'], 0)} дБм")
    ax.axhline(0.99, color="#888", ls=":", lw=1)
    ax.text(ax.get_xlim()[1], 0.99, " p99", va="bottom", ha="right", fontsize=9, color="#555")
    ax.set_xlabel("RTT, мс")
    ax.set_ylabel("Доля пакетов (CDF)")
    ax.set_title("Распределение RTT (ping, все серии)", fontsize=11)
    ax.legend(frameon=False, fontsize=9, loc="lower right")
    fig.savefig(os.path.join(RESULTS, "fig4_latency_cdf.png"))
    plt.close(fig)


def fig5():
    fig, axes = plt.subplots(1, len(QOS), figsize=(4.2 * len(QOS), 4), sharey=True)
    axes = np.atleast_1d(axes)
    for ax, q in zip(axes, QOS):
        for k, c, lab in (("p50", C[0], "p50"), ("p95", C[4], "p95"), ("p99", C[1], "p99")):
            ax.plot(RSSI, [R["mqtt"][n][q][k] for n in NAMES], marker="o", color=c, label=lab, lw=1.8)
        ax.set_yscale("log")
        ax.set_title(f"QoS {q}", fontsize=11)
        xaxis_rssi(ax)
        ax.set_xlabel("RSSI (усл.), дБм")
    axes[0].set_ylabel("Задержка доставки, мс (лог.)")
    axes[0].legend(frameon=False)
    fig.suptitle("Задержка доставки MQTT: publish() → получение подписчиком", fontsize=11)
    fig.savefig(os.path.join(RESULTS, "fig5_mqtt_latency_qos.png"))
    plt.close(fig)


def fig6():
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    parts = [("Wi-Fi ассоциация + DHCP (допущение)", None, "#BBBBBB"), ("DNS", "dns_ms", C[5]),
             ("TCP", "tcp_ms", C[0]), ("TLS", "tls_ms", C[2]), ("MQTT CONNECT", "mqtt_ms", C[1])]
    bottom = np.zeros(len(NAMES))
    for lab, key, col in parts:
        vals = np.array([ASSUMED_MS if key is None else R["conn"][n]["tls"][key] for n in NAMES], dtype=float)
        vals = np.nan_to_num(vals)
        ax.bar(NAMES, vals, bottom=bottom, color=col, label=lab, hatch="//" if key is None else None,
               edgecolor="white")
        bottom += vals
    ax.set_ylabel("Время подключения, мс (медиана)")
    ax.set_title("Полное подключение к брокеру по TLS", fontsize=11)
    ax.legend(frameon=False, fontsize=8)
    fig.savefig(os.path.join(RESULTS, "fig6_connect_time.png"))
    plt.close(fig)


for fn in (fig1, fig2, fig3, fig4, fig5, fig6):
    fn()


print("\nУровень  потери  RSSI   Мбит/с          RTT p50/p95/p99, мс      MQTT QoS1 потери/p99")
for l in LEVELS:
    n = l["name"]
    q1 = R["mqtt"][n].get(1, {})
    print(f"{n:6s} {l['loss_pct']:5d}% {l['rssi']:6.1f}  {fpm(R['thr'][n]['mean'], R['thr'][n]['ci'], 2):14s} "
          f"{f(R['ping'][n]['p50'])}/{f(R['ping'][n]['p95'])}/{f(R['ping'][n]['p99'])}".ljust(70)
          + f"{f(q1.get('loss', NAN), 2)}% / {f(q1.get('p99', NAN))}")
print()
if first_fail is None:
    print("NFR: выполнены на всех исследованных уровнях.")
else:
    ok = last_ok["name"] if last_ok else "нет"
    print(f"NFR: нарушены на уровне {first_fail['name']} (RSSI ≈ {first_fail['rssi']:.0f} дБм, условно "
          f"{first_fail['distance_m']} м); последний уровень, где всё выполнено: {ok}.")
print("Готово: графики fig1..fig6.png и таблицы table_*.csv в", RESULTS)