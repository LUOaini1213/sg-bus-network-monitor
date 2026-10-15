"""ETA accuracy, headway regularity, bunching and data quality from the Bus Arrival sweeps.

    git fetch origin data-collect
    git worktree add ../sgbus-data origin/data-collect        # or any checkout of the data-collect branch
    python src/busarrival_analysis.py --data ../sgbus-data/data/raw/busarrival [--days 2026-10-16 ...]

Writes outputs/busarrival_eta_accuracy.csv, busarrival_headways.csv, busarrival_bunching.csv,
busarrival_quality.json and busarrival_summary.json.

Observed arrivals are inferred, because DataMall publishes predictions only. For each stop and service the
sweeps give a list of up to three predicted arrival times (NextBus, NextBus2, NextBus3) every ~2 minutes, with
no vehicle id. The inference rule:

1. Tracking. Between two successive answers for a stop, the new list is aligned with the old one by a shift s:
   new[i] is the same bus as old[i + s], and the first s old buses have left. s is the shift with the smallest
   mean |new ETA - old ETA| over the aligned pairs; a shift that aligns nothing costs NO_MATCH_S (240 s), and
   every bus it would retire whose last ETA was more than GONE_SLACK_S (120 s) after the new answer adds
   IMPLAUSIBLE_S (600 s). Ties go to the smaller shift. When the service is no longer listed, every old bus has
   left (s = all).
2. Arrival. A bus's arrival interval is (t_prev, t], where t is the first answer at which either
   (a) the bus is monitored (Monitored = 1, a GPS-based estimate) and its ETA is at or before t (the ETA has
   crossed zero), or
   (b) the bus has left the list and its last ETA was no more than GONE_SLACK_S after t,
   and t_prev is the answer before. The observed arrival is the interval midpoint; half its width (about
   1 minute at the 2-minute cadence) is the resolution. A bus that leaves the list while it was still
   predicted to be more than GONE_SLACK_S away is "lost": no arrival is inferred, and the headway sequence is
   broken there. A gap of more than MAX_GAP_S between answers (a failed call or a skipped sweep) closes
   every open track without an arrival, and also breaks the headway sequence. An HTTP 200 answer that lists no
   service at all is a blank, treated as a missing answer. Rule (a) is the system's own
   estimate at the moment of arrival, so the 0-2 minute horizon is partly self-referential; the summary counts
   how many arrivals came from each rule.
3. Predictions. Every answer for that bus up to t_prev is one prediction: horizon = ETA - answer time,
   error = observed arrival - ETA (positive: the bus came later than predicted). Horizons are binned 0-2, 2-5,
   5-10 and 10-20 minutes; negative horizons (a stale ETA in the past) are counted in the quality report.
   A prediction counts only if its ETA is at least END_MARGIN_S (10 minutes) before the last answer for that
   stop: near the end of the observation, buses that come early are seen arriving and buses that come late are
   not, which would bias the long horizons towards "early".

Headways are differences between consecutive observed arrivals of a service at a stop, within an unbroken
sequence. Regularity is the coefficient of variation (sample standard deviation / mean) per service and stop,
with at least MIN_HEADWAYS headways. A headway is bunched when it is under 25% of the reference headway: the
scheduled headway band midpoint for the period (DataMall BusServices: AM peak 06:30-08:30, AM off-peak
08:31-16:59, PM peak 17:00-19:00, PM off-peak after 19:00), or, where the schedule has none, the median observed
headway of that service at that stop.
"""
import argparse
import datetime as dt
import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs"
SERVICES_CSV = ROOT / "data" / "reference" / "busarrival_services.csv"
STOPS_CSV = ROOT / "data" / "reference" / "busarrival_stops.csv"
SGT = dt.timezone(dt.timedelta(hours=8), "SGT")
NO_MATCH_S = 240
GONE_SLACK_S = 120
IMPLAUSIBLE_S = 600
MAX_GAP_S = 300
END_MARGIN_S = 600
MIN_HEADWAYS = 4
BUNCH_FRACTION = 0.25
BINS = [0, 2, 5, 10, 20]
BIN_LABELS = ["0-2", "2-5", "5-10", "10-20"]
SLOTS = ("NextBus", "NextBus2", "NextBus3")


# ---------------------------------------------------------------- loading

def read_sweeps(root, days=None):
    """Every stop answer in the sweep files under root/YYYY-MM-DD/, in time order."""
    root = Path(root)
    lines = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if days and d.name not in days:
            continue
        for f in sorted(d.glob("ba_*.jsonl.gz")):
            with gzip.open(f, "rt", encoding="utf-8") as z:
                lines += [json.loads(x) for x in z if x.strip()]
    return lines


def _ts(text):
    if not text:
        return None
    t = dt.datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=SGT)


def entries(service):
    """The listed buses of one service in one answer, sorted by ETA; empty slots dropped."""
    out = []
    for slot in SLOTS:
        b = service.get(slot) or {}
        eta = _ts(b.get("EstimatedArrival"))
        if eta is None:
            continue
        out.append({"eta": eta, "monitored": int(b.get("Monitored") or 0), "load": b.get("Load") or "",
                    "slot": slot})
    return sorted(out, key=lambda e: e["eta"])


def blank(answer):
    """An HTTP 200 answer that lists no service at all. DataMall returns these now and then for a stop that
    lists a dozen services a minute before and after; they are treated as missing answers, not as every bus
    having left."""
    return not answer.get("services")


def answers_by_group(lines):
    """{(stop, service): [(t, [entries]) ...]} over the successful, non-blank answers for that stop. An answer
    in which a service the stop has listed before is absent counts as an empty list for that service."""
    by_stop = {}
    for x in lines:
        if x.get("status") == "ok" and x.get("t") and not blank(x):
            by_stop.setdefault(x["stop"], []).append((_ts(x["t"]), x["services"]))
    groups = {}
    for stop, answers in by_stop.items():
        answers.sort(key=lambda a: a[0])
        seen = set()
        for t, services in answers:
            listed = {s["ServiceNo"]: entries(s) for s in services if s.get("ServiceNo")}
            seen |= set(listed)
            for svc in seen:
                groups.setdefault((stop, svc), []).append((t, listed.get(svc, [])))
    return groups


# ---------------------------------------------------------------- inference

def best_shift(old, new, t):
    """The number of buses at the front of `old` that have left, given the new list at time t."""
    if not new:
        return len(old)
    best, best_cost = 0, None
    for s in range(len(old) + 1):
        pairs = [(new[i]["eta"], old[i + s]["eta"]) for i in range(min(len(new), len(old) - s))]
        cost = np.mean([abs((a - b).total_seconds()) for a, b in pairs]) if pairs else NO_MATCH_S
        cost += IMPLAUSIBLE_S * sum((o["eta"] - t).total_seconds() > GONE_SLACK_S for o in old[:s])
        if best_cost is None or cost < best_cost:
            best, best_cost = s, cost
    return best


def track(answers):
    """Follow the buses of one stop-service through its answers.

    Returns (tracks, events). A track is {"preds": [(t, eta, monitored)], "lo", "hi", "how"}; lo/hi bound the
    arrival (None when no arrival was inferred), how is "zero", "gone", "lost" or "open". events is the time-ordered
    list of ("arrival", mid) and ("break", t) used for headways.
    """
    tracks, events, active, t_prev = [], [], [], None

    def close(trk, how, lo=None, hi=None):
        trk.update(how=how, lo=lo, hi=hi)
        if how in ("zero", "gone"):
            events.append(("arrival", lo + (hi - lo) / 2))
        elif how in ("lost", "gap"):
            events.append(("break", hi or lo))

    for t, new in answers:
        if t_prev is not None and (t - t_prev).total_seconds() > MAX_GAP_S:
            for trk in active:
                if trk["how"] == "active":
                    close(trk, "gap", lo=t_prev)
            if not active:
                events.append(("break", t_prev))
            active = []
        old = [trk for trk in active]
        if t_prev is None or not old:
            s = 0
        else:
            s = best_shift([{"eta": trk["last"]} for trk in old], new, t)
        for trk in old[:s]:  # left the list
            if trk["how"] == "active":
                if (trk["last"] - t).total_seconds() <= GONE_SLACK_S:
                    close(trk, "gone", t_prev, t)
                else:
                    close(trk, "lost", hi=t)
        matched = old[s:s + len(new)]
        for trk in old[s + len(new):]:  # fell off the tail of a shorter list: not an arrival, not a break
            if trk["how"] == "active":
                trk.update(how="open", lo=None, hi=None)
        active = []
        for i, e in enumerate(new):
            trk = matched[i] if i < len(matched) else None
            if trk is None:
                trk = {"preds": [], "how": "active", "lo": None, "hi": None, "first": t}
                tracks.append(trk)
            if trk["how"] == "active" and e["monitored"] == 1 and e["eta"] <= t:
                if trk["preds"]:
                    close(trk, "zero", t_prev, t)
                else:
                    trk.update(how="open")  # first seen already at the stop: no lower bound
            if trk["how"] == "active":
                trk["preds"].append((t, e["eta"], e["monitored"]))
            trk["last"] = e["eta"]
            active.append(trk)
        t_prev = t
    for trk in active:
        if trk["how"] == "active":
            trk["how"] = "open"
    return tracks, events


def infer(lines):
    """(predictions, headways, tracks) DataFrames from the raw answers."""
    preds, heads, trks = [], [], []
    for (stop, svc), answers in answers_by_group(lines).items():
        tracks, events = track(answers)
        horizon_end = answers[-1][0] - dt.timedelta(seconds=END_MARGIN_S)
        for k, trk in enumerate(tracks):
            trks.append({"stop": stop, "service": svc, "how": trk["how"], "n_preds": len(trk["preds"])})
            if trk["how"] not in ("zero", "gone"):
                continue
            mid = trk["lo"] + (trk["hi"] - trk["lo"]) / 2
            half = (trk["hi"] - trk["lo"]).total_seconds() / 2
            for t, eta, mon in trk["preds"]:
                if eta > horizon_end:  # only buses that would still have been seen arriving END_MARGIN_S late
                    continue
                preds.append({"stop": stop, "service": svc, "track": k, "t": t, "eta": eta, "monitored": mon,
                              "horizon_min": (eta - t).total_seconds() / 60,
                              "error_s": (mid - eta).total_seconds(), "resolution_s": half, "how": trk["how"]})
        last = None
        for kind, when in sorted(events, key=lambda e: e[1]):
            if kind == "break":
                last = None
            else:
                if last is not None:
                    heads.append({"stop": stop, "service": svc, "arrival": when,
                                  "headway_min": (when - last).total_seconds() / 60})
                last = when
    cols = ["stop", "service", "track", "t", "eta", "monitored", "horizon_min", "error_s", "resolution_s", "how"]
    return (pd.DataFrame(preds, columns=cols),
            pd.DataFrame(heads, columns=["stop", "service", "arrival", "headway_min"]),
            pd.DataFrame(trks, columns=["stop", "service", "how", "n_preds"]))


# ---------------------------------------------------------------- metrics

def eta_accuracy(preds, by=()):
    """Error by prediction horizon (and optionally by other columns, e.g. monitored)."""
    p = preds[(preds.horizon_min >= 0) & (preds.horizon_min < BINS[-1])].copy()
    p["horizon"] = pd.cut(p.horizon_min, BINS, right=False, labels=BIN_LABELS)
    a = p.error_s.abs()
    p["abs_s"] = a
    p["within_60"] = a <= 60
    p["within_120"] = a <= 120
    p["bus"] = list(zip(p.stop, p.service, p.track))
    g = p.groupby([*by, "horizon"], observed=True)
    out = g.agg(n=("error_s", "size"), buses=("bus", "nunique"), bias_s=("error_s", "mean"), median_error_s=("error_s", "median"), mae_s=("abs_s", "mean"),
                p90_abs_s=("abs_s", lambda s: float(np.percentile(s, 90))),
                within_60=("within_60", "mean"), within_120=("within_120", "mean"),
                resolution_s=("resolution_s", "median")).reset_index()
    return out


def regularity(heads, min_n=MIN_HEADWAYS):
    """Coefficient of variation of observed headways per stop and service (sample standard deviation)."""
    g = heads.groupby(["stop", "service"]).headway_min
    r = g.agg(n="size", mean_min="mean", sd_min=lambda s: s.std(ddof=1), median_min="median").reset_index()
    r = r[r.n >= min_n].copy()
    r["cv"] = r.sd_min / r.mean_min
    return r


def period_of(t):
    """DataMall's BusServices frequency period of a Singapore time."""
    m = t.hour * 60 + t.minute
    if 6 * 60 + 30 <= m <= 8 * 60 + 30:
        return "am_peak"
    if 8 * 60 + 30 < m < 17 * 60:
        return "am_offpeak"
    if 17 * 60 <= m <= 19 * 60:
        return "pm_peak"
    return "pm_offpeak"


def scheduled_headways(path=SERVICES_CSV):
    """{(stop, service): {period: minutes}} from the reference table; a service calling twice keeps the first."""
    s = pd.read_csv(path, dtype={"BusStopCode": str, "ServiceNo": str}).drop_duplicates(["BusStopCode", "ServiceNo"])
    cols = ["am_peak", "am_offpeak", "pm_peak", "pm_offpeak"]
    return {(r.BusStopCode, r.ServiceNo): {c: getattr(r, f"{c}_headway_min") for c in cols} for r in s.itertuples()}


def bunching(heads, schedule, fraction=BUNCH_FRACTION):
    """Each headway with its reference and bunched flag; the reference is the scheduled headway for the period
    of the arrival, or the median observed headway of the stop-service where the schedule has none."""
    h = heads.copy()
    if h.empty:
        return h.assign(reference_min=[], reference=[], bunched=[])
    med = h.groupby(["stop", "service"]).headway_min.transform("median")
    sched = [schedule.get((s, v), {}).get(period_of(a.astimezone(SGT))) for s, v, a in
             zip(h.stop, h.service, h.arrival)]
    sched = pd.Series(sched, index=h.index, dtype=float)
    h["reference"] = np.where(sched.notna(), "scheduled", "median")
    h["reference_min"] = sched.fillna(med)
    h["bunched"] = h.headway_min < fraction * h.reference_min
    return h


def quality(lines, tracks, preds):
    calls = pd.Series([x.get("status", "") for x in lines])
    status = calls.str.split(":").str[0].value_counts().to_dict()
    rows = []
    for x in lines:
        if x.get("status") != "ok":
            continue
        for s in x.get("services") or []:
            for slot in SLOTS:
                b = s.get(slot) or {}
                rows.append({"slot": slot, "eta": bool(b.get("EstimatedArrival")), "load": b.get("Load") or "",
                             "monitored": b.get("Monitored"), "type": b.get("Type") or "",
                             "latlon": bool(b.get("Latitude")) and str(b.get("Latitude")) not in ("0", "0.0")})
    e = pd.DataFrame(rows, columns=["slot", "eta", "load", "monitored", "type", "latlon"])
    listed = e[e.eta]
    first = e[e.slot == "NextBus"]
    q = {
        "answers": int(len(lines)), "answer_status": {k: int(v) for k, v in status.items()},
        "blank_answers": int(sum(1 for x in lines if x.get("status") == "ok" and blank(x))),
        "stop_service_answers": int(len(first)),
        "nextbus_missing_eta_share": float((~first.eta).mean()) if len(first) else None,
        "listed_buses": int(len(listed)),
        "load_share": {k: float(v) for k, v in listed.load.replace("", "missing").value_counts(normalize=True).items()},
        "monitored_share": float((listed.monitored == 1).mean()) if len(listed) else None,
        "type_share": {k: float(v) for k, v in listed.type.replace("", "missing").value_counts(normalize=True).items()},
        "missing_position_share_monitored": float((~listed[listed.monitored == 1].latlon).mean())
        if (listed.monitored == 1).any() else None,
        "tracks": {k: int(v) for k, v in tracks.how.value_counts().items()} if len(tracks) else {},
        "predictions": int(len(preds)),
        "negative_horizon_share": float((preds.horizon_min < 0).mean()) if len(preds) else None,
    }
    return q


def run(data, days=None, out=OUT):
    lines = read_sweeps(data, days)
    preds, heads, trks = infer(lines)
    acc = eta_accuracy(preds)
    acc_m = eta_accuracy(preds, by=("monitored",))
    reg = regularity(heads)
    bun = bunching(heads, scheduled_headways())
    q = quality(lines, trks, preds)
    times = sorted(_ts(x["t"]) for x in lines if x.get("t"))
    summary = {
        "first_answer": times[0].isoformat() if times else None, "last_answer": times[-1].isoformat() if times else None,
        "days": sorted({t.date().isoformat() for t in times}),
        "arrivals": {k: int(v) for k, v in trks.how.value_counts().items()},
        "eta": acc.to_dict("records"), "eta_by_monitored": acc_m.to_dict("records"),
        "headways": int(len(heads)), "stop_services_with_cv": int(len(reg)),
        "median_cv": float(reg.cv.median()) if len(reg) else None,
        "bunched_share": float(bun.bunched.mean()) if len(bun) else None,
        "bunched_share_scheduled_ref": float(bun[bun.reference == "scheduled"].bunched.mean())
        if (bun.reference == "scheduled").any() else None,
        "bunched_share_median_ref": float((bun.headway_min < BUNCH_FRACTION * bun.groupby(["stop", "service"])
                                           .headway_min.transform("median")).mean()) if len(bun) else None,
    }
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    pd.concat([acc.assign(monitored="all"), acc_m], ignore_index=True).to_csv(out / "busarrival_eta_accuracy.csv",
                                                                              index=False)
    reg.to_csv(out / "busarrival_headways.csv", index=False)
    stops = pd.read_csv(STOPS_CSV, dtype={"BusStopCode": str}).set_index("BusStopCode").corridor
    if len(bun):
        b = bun.assign(corridor=bun.stop.map(stops)).groupby("corridor").agg(
            headways=("bunched", "size"), bunched_share=("bunched", "mean"),
            scheduled_ref_share=("reference", lambda s: (s == "scheduled").mean())).reset_index()
    else:
        b = pd.DataFrame(columns=["corridor", "headways", "bunched_share", "scheduled_ref_share"])
    b.to_csv(out / "busarrival_bunching.csv", index=False)
    (out / "busarrival_quality.json").write_text(json.dumps(q, indent=2), encoding="utf-8")
    (out / "busarrival_summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary, q


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", required=True, help="the data/raw/busarrival folder of a data-collect checkout")
    ap.add_argument("--days", nargs="*", help="YYYY-MM-DD folders to use (default: all)")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()
    s, q = run(a.data, a.days, a.out)
    print(json.dumps({"summary": {k: v for k, v in s.items() if not k.startswith("eta")}, "quality": q}, indent=1,
                     default=str))
    print(pd.DataFrame(s["eta"]).to_string(index=False))
