
import os
import json
import time
import math
import random
from datetime import datetime
from dateutil import tz

import numpy as np
import pandas as pd
import requests
import streamlit as st

from streamlit_autorefresh import st_autorefresh
import folium
from streamlit_folium import st_folium

# ============================================================
# GAMIFIED CARPOOL DRIVER PANEL (Prototype)
# - 500 passenger samples within 100 km of office; regenerated on office/home GPS change
# - "Clustering" distance: distance to centroid (K=1) where centroid = driver home
# - Filter on pickup radius + days + time tolerance; highlight top 5 closest among filtered
# - Tooltips show dist_to_driver for every passenger
# - NO passenger payment capping (removed)
# - More robust Commute Scoreboard: Coins/XP never stuck at 0
# ============================================================

APP_TZ = tz.gettz("Asia/Kolkata")
REFRESH_SECONDS = 15 * 60

N_SAMPLES = 500
SAMPLE_RADIUS_KM = 100
TOP_K = 5

DEFAULT_COST_EUR_PER_KM = 0.4327
DEFAULT_OSRM_BASE = "https://router.project-osrm.org"  # can be changed to http://localhost:5000

OUTBOX_DIR = "outbox"
os.makedirs(OUTBOX_DIR, exist_ok=True)

# --- Platform-wide persistent stats ---
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)
PLATFORM_STATS_PATH = os.path.join(DATA_DIR, "platform_stats.json")
CO2_PER_KM_KG = 0.19  # kg CO2 per km saved (passenger car estimate)

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

# ------------------------------------------------------------
# Utilities
# ------------------------------------------------------------

def now_local():
    return datetime.now(tz=APP_TZ)



# ============================================================
# Platform-wide persistence helpers (defined early, before init_state)
# ============================================================

def _default_platform_stats():
    return {
        "version": 1,
        "updated_at": None,
        "drivers": {},
        "totals": {
            "drivers_sent_requests": 0,
            "drivers_confirmed": 0,
            "requests_sent": 0,
            "requests_confirmed": 0,
            "revenue_eur": 0.0,
            "coins": 0.0,
            "co2_saved_kg": 0.0,
        },
        "leaders": {
            "top_earner_driver_id": None,
            "top_earner_revenue_eur": 0.0,
            "co2_hero_driver_id": None,
            "co2_hero_saved_kg": 0.0,
        },
    }


def load_platform_stats():
    if os.path.exists(PLATFORM_STATS_PATH):
        try:
            with open(PLATFORM_STATS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for key in ("drivers", "totals", "leaders"):
                    if key not in data:
                        data[key] = _default_platform_stats()[key]
                return data
        except Exception:
            pass
    return _default_platform_stats()


def save_platform_stats(stats):
    stats["updated_at"] = now_local().isoformat()
    tmp = PLATFORM_STATS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PLATFORM_STATS_PATH)


def _ensure_driver(stats, driver_id):
    if driver_id not in stats["drivers"]:
        stats["drivers"][driver_id] = {
            "driver_id": driver_id,
            "requests_sent": 0,
            "requests_confirmed": 0,
            "revenue_eur": 0.0,
            "coins": 0.0,
            "co2_saved_kg": 0.0,
        }
    stats["totals"]["drivers_sent_requests"] = len(
        [d for d in stats["drivers"].values() if d.get("requests_sent", 0) > 0]
    )
    stats["totals"]["drivers_confirmed"] = len(
        [d for d in stats["drivers"].values() if d.get("requests_confirmed", 0) > 0]
    )


def register_requests_sent(stats, driver_id, n_requests):
    _ensure_driver(stats, driver_id)
    stats["drivers"][driver_id]["requests_sent"] += int(n_requests)
    stats["totals"]["requests_sent"] += int(n_requests)
    stats["totals"]["drivers_sent_requests"] = len(
        [d for d in stats["drivers"].values() if d.get("requests_sent", 0) > 0]
    )


def register_confirmation(stats, driver_id, payment_eur, coins_val, co2_saved_kg):
    _ensure_driver(stats, driver_id)
    d = stats["drivers"][driver_id]
    d["requests_confirmed"] += 1
    d["revenue_eur"] += float(payment_eur or 0.0)
    d["coins"] += float(coins_val or 0.0)
    d["co2_saved_kg"] += float(co2_saved_kg or 0.0)
    stats["totals"]["requests_confirmed"] += 1
    stats["totals"]["revenue_eur"] += float(payment_eur or 0.0)
    stats["totals"]["coins"] += float(coins_val or 0.0)
    stats["totals"]["co2_saved_kg"] += float(co2_saved_kg or 0.0)
    stats["totals"]["drivers_confirmed"] = len(
        [dd for dd in stats["drivers"].values() if dd.get("requests_confirmed", 0) > 0]
    )
    if d["revenue_eur"] > float(stats["leaders"].get("top_earner_revenue_eur", 0.0) or 0.0):
        stats["leaders"]["top_earner_driver_id"] = driver_id
        stats["leaders"]["top_earner_revenue_eur"] = d["revenue_eur"]
    if d["co2_saved_kg"] > float(stats["leaders"].get("co2_hero_saved_kg", 0.0) or 0.0):
        stats["leaders"]["co2_hero_driver_id"] = driver_id
        stats["leaders"]["co2_hero_saved_kg"] = d["co2_saved_kg"]


def format_badges(driver_rollup):
    rev = float(driver_rollup.get("revenue_eur", 0.0) or 0.0)
    conf = int(driver_rollup.get("requests_confirmed", 0) or 0)
    co2 = float(driver_rollup.get("co2_saved_kg", 0.0) or 0.0)
    badges = []
    if rev >= 100: badges.append("💎 100€ Club")
    elif rev >= 50: badges.append("🥇 50€ Earner")
    elif rev >= 20: badges.append("🥈 20€ Earner")
    elif rev > 0: badges.append("🥉 First Earnings")
    if conf >= 20: badges.append("🔥 20 Confirmations Streak")
    elif conf >= 10: badges.append("🚀 10 Confirmations")
    elif conf >= 5: badges.append("⭐ 5 Confirmations")
    elif conf > 0: badges.append("✅ First Confirmation")
    if co2 >= 50: badges.append("🌍 CO₂ Hero (50kg+)")
    elif co2 >= 20: badges.append("🌱 CO₂ Saver (20kg+)")
    elif co2 > 0: badges.append("🍃 First CO₂ Saved")
    return badges


def parse_hhmm_to_minutes(s: str) -> int:
    hh, mm = s.split(":")
    return int(hh) * 60 + int(mm)


def minutes_to_hhmm(m: int) -> str:
    m = m % (24 * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def random_point_within_radius_km(lat, lon, radius_km, rng: random.Random):
    """Uniform random point within a circle on Earth (approx)."""
    R = 6371.0
    bearing = rng.uniform(0, 2 * math.pi)
    d = radius_km * math.sqrt(rng.random())
    dr = d / R

    lat1 = math.radians(lat)
    lon1 = math.radians(lon)

    lat2 = math.asin(math.sin(lat1) * math.cos(dr) + math.cos(lat1) * math.sin(dr) * math.cos(bearing))
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(dr) * math.cos(lat1),
        math.cos(dr) - math.sin(lat1) * math.sin(lat2),
    )

    return (math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180)


def write_outbox_email(kind: str, payload: dict):
    ts = now_local().strftime("%Y%m%d_%H%M%S")
    fn = os.path.join(OUTBOX_DIR, f"{ts}_{kind}.json")
    with open(fn, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return fn


def fairness_message(alpha: float) -> str:
    # Payment formula: payment = (1-alpha)*solo_avoided_cost + detour_cost
    # Higher alpha reduces (1-alpha) term -> passenger-friendly.
    if alpha < 0.25:
        return "⚠️ α is low → passengers contribute more of their avoided solo cost. If declines increase, move α toward ~0.5."
    if 0.25 <= alpha < 0.40:
        return "🙂 Slightly driver-favorable. Often ok if detours are meaningful."
    if 0.40 <= alpha <= 0.60:
        return "✅ Balanced α range. Typically perceived as fair → higher acceptance probability."
    if 0.60 < alpha <= 0.80:
        return "🙂 Passenger-friendly. Helps acceptance; ensure it still feels sustainable for you."
    return "⚠️ α is very high → passengers contribute little of avoided solo cost. Great for acceptance, but may reduce your benefit."


def acceptance_probability(alpha: float, passenger_profile: dict) -> float:
    deal_sensitivity = passenger_profile.get("deal_sensitivity", 0.5)
    penalty = abs(alpha - 0.55) * 1.1
    base = 0.82 - penalty
    p = base - (deal_sensitivity * abs(alpha - 0.55) * 0.5)
    return float(np.clip(p, 0.10, 0.95))


# ------------------------------------------------------------
# Routing (optional): used for driver->office display and economics
# ------------------------------------------------------------

def _verify_opt(ca_bundle_path: str, insecure_skip_verify: bool):
    if insecure_skip_verify:
        return False
    if ca_bundle_path and ca_bundle_path.strip():
        return ca_bundle_path.strip()
    return True


@st.cache_data(show_spinner=False, ttl=60 * 60)
def osrm_route(osrm_base: str, src_lat, src_lon, dst_lat, dst_lon,
               ca_bundle_path: str = "", insecure_skip_verify: bool = False):
    url = f"{osrm_base.rstrip('/')}/route/v1/driving/{src_lon},{src_lat};{dst_lon},{dst_lat}"
    params = {"overview": "full", "geometries": "geojson", "steps": "false"}

    verify = _verify_opt(ca_bundle_path, insecure_skip_verify)
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, timeout=20, verify=verify,
                             headers={"User-Agent": "gamified-carpool-demo/1.0"})
            r.raise_for_status()
            data = r.json()
            if data.get("code") != "Ok" or not data.get("routes"):
                raise RuntimeError(f"OSRM error: {data.get('code')}")
            route = data["routes"][0]
            dist_km = route["distance"] / 1000.0
            coords = route["geometry"]["coordinates"]
            latlon = [(c[1], c[0]) for c in coords]
            return float(dist_km), latlon, True
        except Exception as e:
            last_err = e
            time.sleep(0.4 * (attempt + 1))

    raise RuntimeError(f"OSRM routing failed after retries: {last_err}")


def safe_route(osrm_base, src, dst, ca_bundle_path="", insecure_skip_verify=False):
    (lat1, lon1), (lat2, lon2) = src, dst
    try:
        d, geom, used = osrm_route(osrm_base, lat1, lon1, lat2, lon2,
                                  ca_bundle_path=ca_bundle_path,
                                  insecure_skip_verify=insecure_skip_verify)
        return d, geom, used
    except Exception:
        d = haversine_km(lat1, lon1, lat2, lon2)
        return float(d), [src, dst], False


@st.cache_data(show_spinner=False, ttl=60 * 60)
def route_km_only(osrm_base, a, b, ca_bundle_path="", insecure_skip_verify=False):
    d, _, used = safe_route(osrm_base, a, b, ca_bundle_path, insecure_skip_verify)
    return float(d), bool(used)


# ------------------------------------------------------------
# Passenger generation (500 within 100 km of office)
# ------------------------------------------------------------

def generate_passengers_500(office_lat, office_lon, n=N_SAMPLES, radius_km=SAMPLE_RADIUS_KM, seed=7):
    rng = random.Random(seed)
    np.random.seed(seed)

    age_groups = ["18–25", "26–35", "36–45", "46–60"]
    sexes = ["F", "M", "X"]
    domains = ["Engineering", "Finance", "Design", "HR", "Data", "Operations"]
    interests = ["Music", "Fitness", "Tech", "Food", "Travel", "Books", "Movies"]

    rows = []
    for i in range(n):
        lat, lon = random_point_within_radius_km(office_lat, office_lon, radius_km, rng)
        days = rng.sample(DAYS, k=rng.randint(2, 5))
        start_min = rng.choice([7 * 60 + 30, 8 * 60, 8 * 60 + 30, 9 * 60, 9 * 60 + 30, 10 * 60])
        start_min += rng.randint(-10, 15)
        wait_tol = rng.choice([5, 10, 15, 20, 25])

        rows.append({
            "passenger_id": f"P{i+1:03d}",
            "home_lat": float(lat),
            "home_lon": float(lon),
            "office_lat": float(office_lat),
            "office_lon": float(office_lon),
            "days": ",".join(days),
            "trip_start_time": minutes_to_hhmm(start_min),
            "tolerance_wait_min": int(wait_tol),
            "age_group": rng.choice(age_groups),
            "sex": rng.choice(sexes),
            "domain": rng.choice(domains),
            "interests": ", ".join(rng.sample(interests, k=2)),
            "deal_sensitivity": float(np.clip(np.random.normal(0.5, 0.18), 0, 1)),
            "uses_carpool": False,
            "owns_car": False,
        })

    return pd.DataFrame(rows)


# ------------------------------------------------------------
# Filtering rules
# ------------------------------------------------------------

def filter_by_day(df, driver_days_set):
    if not driver_days_set:
        return df.copy()

    def overlaps(pass_days):
        p = set([d.strip() for d in str(pass_days).split(",") if d.strip()])
        return len(p.intersection(driver_days_set)) > 0

    return df[df["days"].apply(overlaps)].copy()


def filter_by_time(df, driver_start_min, driver_wait_tol):
    def ok(row):
        p_start = parse_hhmm_to_minutes(row["trip_start_time"])
        p_tol = int(row["tolerance_wait_min"])
        delta = abs(p_start - driver_start_min)
        return delta <= max(driver_wait_tol, p_tol)

    return df[df.apply(ok, axis=1)].copy()


# ------------------------------------------------------------
# Clustering distance to centroid (K=1), centroid fixed at driver home
# ------------------------------------------------------------

def add_centroid_distance(df, centroid):
    lat0, lon0 = centroid
    out = df.copy()
    out["dist_to_driver_km"] = out.apply(lambda r: haversine_km(lat0, lon0, r["home_lat"], r["home_lon"]), axis=1)
    return out


def add_detour_distance(df, driver_home, office):
    """
    Calculate the detour (additional distance) for each passenger pickup using Haversine distance.
    Detour = (home->pickup + pickup->office) - (home->office)
    Uses fast Haversine calculations instead of OSRM routing for initial filtering.
    """
    out = df.copy()
    
    # Get direct route distance from driver home to office (Haversine)
    base_km = haversine_km(driver_home[0], driver_home[1], office[0], office[1])
    
    detours = []
    for _, row in out.iterrows():
        passenger_pickup = (row["home_lat"], row["home_lon"])
        
        # Calculate using Haversine (fast, no API calls)
        home_to_pickup = haversine_km(driver_home[0], driver_home[1], passenger_pickup[0], passenger_pickup[1])
        pickup_to_office = haversine_km(passenger_pickup[0], passenger_pickup[1], office[0], office[1])
        # Detour = additional distance incurred
        detour = (home_to_pickup + pickup_to_office) - base_km
        
        detours.append(float(max(0.0, detour)))  # Ensure non-negative
    
    out["detour_km"] = detours
    return out


def top_k_nearest(df, k=TOP_K):
    if len(df) == 0:
        return df.copy()
    return df.sort_values("dist_to_driver_km", ascending=True).head(k).copy()


def top_k_least_detour(df, k=TOP_K):
    """Sort by minimal detour (passengers most on-the-way first)."""
    if len(df) == 0:
        return df.copy()
    if "detour_km" not in df.columns:
        return top_k_nearest(df, k)
    return df.sort_values("detour_km", ascending=True).head(k).copy()


# ------------------------------------------------------------
# Best route + per-stop detour allocation (for economics)
# ------------------------------------------------------------



def optimal_best_order_indices(osrm_base, start, stops, end, ca_bundle_path="", insecure_skip_verify=False, max_exact_stops=12):
    """Return visiting order (indices) that MINIMIZES total distance.

    Objective: start -> stops (in some order) -> end.
    Distances use route_km_only (OSRM when available, else fallback).

    Exact Held–Karp DP for n <= max_exact_stops, else greedy + 2-opt.

    Returns: (order_idx_list, total_km, used_all, is_exact)
    """
    n = len(stops)
    if n == 0:
        d_end, used_end = route_km_only(osrm_base, start, end, ca_bundle_path, insecure_skip_verify)
        return [], float(d_end), bool(used_end), True

    def dist(a, b):
        return route_km_only(osrm_base, a, b, ca_bundle_path, insecure_skip_verify)

    used_all = True

    d_start = [0.0] * n
    for i in range(n):
        d, used = dist(start, stops[i])
        d_start[i] = float(d)
        used_all = used_all and bool(used)

    d_mat = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            d, used = dist(stops[i], stops[j])
            d_mat[i][j] = float(d)
            used_all = used_all and bool(used)

    d_end = [0.0] * n
    for i in range(n):
        d, used = dist(stops[i], end)
        d_end[i] = float(d)
        used_all = used_all and bool(used)

    if n <= int(max_exact_stops):
        size = 1 << n
        INF = 1e18
        dp = [[INF] * n for _ in range(size)]
        parent = [[-1] * n for _ in range(size)]

        for i in range(n):
            dp[1 << i][i] = d_start[i]

        for mask in range(size):
            for last in range(n):
                cur = dp[mask][last]
                if cur >= INF or not (mask & (1 << last)):
                    continue
                rem = (~mask) & (size - 1)
                j = rem
                while j:
                    lsb = j & -j
                    nxt = lsb.bit_length() - 1
                    new_mask = mask | lsb
                    cand = cur + d_mat[last][nxt]
                    if cand < dp[new_mask][nxt]:
                        dp[new_mask][nxt] = cand
                        parent[new_mask][nxt] = last
                    j -= lsb

        full = size - 1
        best_total = INF
        best_last = -1
        for last in range(n):
            cand = dp[full][last] + d_end[last]
            if cand < best_total:
                best_total = cand
                best_last = last

        order = []
        mask = full
        last = best_last
        while last != -1:
            order.append(last)
            pl = parent[mask][last]
            mask ^= (1 << last)
            last = pl
        order.reverse()

        return order, float(best_total), bool(used_all), True

    # heuristic
    remaining = list(range(n))
    cur_pt = start
    order = []
    while remaining:
        best_idx = None
        best_d = None
        for idx in remaining:
            d, _used = dist(cur_pt, stops[idx])
            if best_d is None or d < best_d:
                best_d = d
                best_idx = idx
        order.append(best_idx)
        remaining.remove(best_idx)
        cur_pt = stops[best_idx]

    def route_len(ord_idx):
        total = 0.0
        cur = start
        for k in ord_idx:
            total += float(dist(cur, stops[k])[0])
            cur = stops[k]
        total += float(dist(cur, end)[0])
        return float(total)

    best_len = route_len(order)
    improved = True
    while improved:
        improved = False
        for i in range(n - 1):
            for j in range(i + 1, n):
                new = order[:i] + list(reversed(order[i:j+1])) + order[j+1:]
                new_len = route_len(new)
                if new_len + 1e-9 < best_len:
                    order = new
                    best_len = new_len
                    improved = True
                    break
            if improved:
                break

    return order, float(best_len), bool(used_all), False

def greedy_best_order(osrm_base, start, stops, end, ca_bundle_path="", insecure_skip_verify=False):
    """Backward-compatible name.

    IMPORTANT: This now returns the order that minimizes TOTAL distance to office
    (exact DP for small N, heuristic otherwise).

    Returns: (ordered_stops, total_km, used_all)
    """
    order_idx, total, used_all, _is_exact = optimal_best_order_indices(
        osrm_base, start, stops, end, ca_bundle_path, insecure_skip_verify
    )
    ordered = [stops[i] for i in order_idx]
    return ordered, float(total), bool(used_all)

def allocate_detours_by_stop(osrm_base, driver_home, ordered_stops, office,
                             ca_bundle_path="", insecure_skip_verify=False):
    points = [driver_home] + ordered_stops + [office]

    def d(p, q):
        return route_km_only(osrm_base, p, q, ca_bundle_path, insecure_skip_verify)[0]

    detours = []
    for i in range(1, len(points) - 1):
        prev_p = points[i - 1]
        stop_p = points[i]
        next_p = points[i + 1]
        incr = d(prev_p, stop_p) + d(stop_p, next_p) - d(prev_p, next_p)
        detours.append(float(max(0.0, incr)))

    return detours


# ------------------------------------------------------------
# Pricing / economics (NO CAP)
# payment_i = (1-alpha)*solo_avoided_cost_i + detour_cost_i
# ------------------------------------------------------------

def compute_pricing_and_economics(osrm_base, cost_per_km, alpha,
                                 driver_home, office, selected_profiles,
                                 ca_bundle_path="", insecure_skip_verify=False):
    selected_ids = list(selected_profiles.keys())

    base_km, _, base_used = safe_route(osrm_base, driver_home, office, ca_bundle_path, insecure_skip_verify)
    solo_cost_driver = base_km * cost_per_km

    if len(selected_ids) == 0:
        return {
            "base_km": float(base_km),
            "best_total_km": float(base_km),
            "solo_cost_driver": float(solo_cost_driver),
            "shared_cost_driver": float(solo_cost_driver),
            "incremental_cost_driver": 0.0,
            "revenue": 0.0,
            "driver_benefit_vs_solo": 0.0,
            "offset_pct": 0.0,
            "passenger_breakdown": {},
            "route_used_all": bool(base_used),
        }

    stops = [(float(selected_profiles[pid]["home_lat"]), float(selected_profiles[pid]["home_lon"])) for pid in selected_ids]
    ordered_stops, best_total_km, used_all = greedy_best_order(
        osrm_base, driver_home, stops, office, ca_bundle_path, insecure_skip_verify
    )
    detour_km_list = allocate_detours_by_stop(osrm_base, driver_home, ordered_stops, office, ca_bundle_path, insecure_skip_verify)

    # map ordered stop -> passenger id by nearest coordinate match (robust for duplicates)
    remaining = set(selected_ids)
    stop_to_pid = []
    for stop in ordered_stops:
        best_pid, best_h = None, 1e18
        for pid in list(remaining):
            pr = selected_profiles[pid]
            h = haversine_km(stop[0], stop[1], float(pr["home_lat"]), float(pr["home_lon"]))
            if h < best_h:
                best_h, best_pid = h, pid
        stop_to_pid.append(best_pid)
        remaining.remove(best_pid)

    passenger_breakdown = {}
    revenue = 0.0

    for idx, pid in enumerate(stop_to_pid):
        pr = selected_profiles[pid]
        p_home = (float(pr["home_lat"]), float(pr["home_lon"]))

        p_solo_km, _ = route_km_only(osrm_base, p_home, office, ca_bundle_path, insecure_skip_verify)
        solo_avoided_cost = float(p_solo_km * cost_per_km)

        detour_km = float(detour_km_list[idx])
        detour_cost = float(detour_km * cost_per_km)

        payment = float((1.0 - alpha) * solo_avoided_cost + detour_cost)

        passenger_breakdown[pid] = {
            "passenger_solo_km": float(p_solo_km),
            "solo_avoided_cost": solo_avoided_cost,
            "assigned_detour_km": detour_km,
            "detour_cost": detour_cost,
            "payment": payment,
        }
        revenue += payment

    shared_cost_driver = float(best_total_km * cost_per_km)
    incremental_cost_driver = float(max(0.0, shared_cost_driver - solo_cost_driver))
    driver_benefit_vs_solo = float(revenue - incremental_cost_driver)
    offset_pct = float((revenue / solo_cost_driver) * 100.0) if solo_cost_driver > 1e-9 else 0.0

    return {
        "base_km": float(base_km),
        "best_total_km": float(best_total_km),
        "solo_cost_driver": float(solo_cost_driver),
        "shared_cost_driver": float(shared_cost_driver),
        "incremental_cost_driver": float(incremental_cost_driver),
        "revenue": float(revenue),
        "driver_benefit_vs_solo": float(driver_benefit_vs_solo),
        "offset_pct": float(offset_pct),
        "passenger_breakdown": passenger_breakdown,
        "route_used_all": bool(used_all and base_used),
    }


# ------------------------------------------------------------
# Emails
# ------------------------------------------------------------

def compose_passenger_email(passenger, driver_start_time, office_label, alpha, pickup_radius_km, driver_benefit_hint=None, cost_breakdown=None):
    pid = passenger["passenger_id"]
    interests = passenger.get("interests", "shared interests")

    fairness_line = (
        "I’m proposing a balanced split so it feels fair for both of us."
        if 0.40 <= alpha <= 0.60
        else "I’m flexible on cost-sharing so we can find a fair middle ground."
    )

    benefit_line = ""
    if driver_benefit_hint is not None:
        benefit_line = "This pricing keeps the ride sustainable, which improves reliability for everyone." if driver_benefit_hint >= 0 else "If you accept, we can tune the split slightly so both sides feel good."

    # Cost breakdown section (gamified)
    cost_section = ""
    if cost_breakdown:
        solo_km = cost_breakdown.get("passenger_solo_km", 0)
        solo_cost = cost_breakdown.get("solo_avoided_cost", 0)
        payment = cost_breakdown.get("payment", 0)
        savings = solo_cost - payment

        cost_section = f"""
💰 COST BREAKDOWN:
┌─────────────────────────────┐
│ Solo trip cost:    €{solo_cost:.2f}  │
│ Your carpool cost: €{payment:.2f}  │
│ Your savings:      €{savings:.2f}  │
└─────────────────────────────┘
""".strip()

    subject = f"🚗 Carpool invite to {office_label} — quick confirm?"
    body = f"""
Hi {pid},

I noticed our commute overlaps. Would you like to share a ride to {office_label}?

🕒 Start time: {driver_start_time}
📍 Pickup: within ~{pickup_radius_km} km
🤝 Cost sharing: α = {alpha:.2f}. {fairness_line}
{cost_section}

{benefit_line}

Why it could be nice:
• Less stress: shared commute routine
• You mentioned interests like {interests}
• Small change → meaningful reduction in solo commuting

Please reply with one click:
✅ Accept   |   ❌ Decline

Thanks!
— Driver
""".strip()

    return subject, body


def compose_driver_update_email(passenger_id, status, office_label, start_time_str):
    subject = f"Update: {passenger_id} has {status} your request"
    body = f"✅ {passenger_id} accepted." if status == "accepted" else f"❌ {passenger_id} declined."
    body += f"\n\nOffice: {office_label}\nStart: {start_time_str}\n"
    return subject, body


# ------------------------------------------------------------
# Session state
# ------------------------------------------------------------

def init_state():
    if "passenger_pool" not in st.session_state:
        st.session_state.passenger_pool = None
    if "gps_key" not in st.session_state:
        st.session_state.gps_key = None
    if "last_refresh_epoch" not in st.session_state:
        st.session_state.last_refresh_epoch = int(time.time())
    if "driver_id" not in st.session_state:
        st.session_state.driver_id = f"D{random.randint(1000, 9999)}"
    if "driver_sex" not in st.session_state:
        st.session_state.driver_sex = random.choice(["Male", "Female"])
    if "selected_passengers" not in st.session_state:
        st.session_state.selected_passengers = set()
    if "selected_profiles" not in st.session_state:
        st.session_state.selected_profiles = {}
    if "pending_requests" not in st.session_state:
        st.session_state.pending_requests = []
    if "alerts" not in st.session_state:
        st.session_state.alerts = []
    if "alpha" not in st.session_state:
        st.session_state.alpha = 0.50
    if "last_sent_email_previews" not in st.session_state:
        st.session_state.last_sent_email_previews = []
    if "accepted_passengers" not in st.session_state:
        st.session_state.accepted_passengers = set()
    if "declined_passengers" not in st.session_state:
        st.session_state.declined_passengers = set()
    if "passenger_breakdown" not in st.session_state:
        st.session_state.passenger_breakdown = {}
    if "cumulative_confirmed_count" not in st.session_state:
        st.session_state.cumulative_confirmed_count = 0
    if "cumulative_revenue" not in st.session_state:
        st.session_state.cumulative_revenue = 0.0
    if "cumulative_coins" not in st.session_state:
        st.session_state.cumulative_coins = 0.0
    if "cumulative_co2_saved_kg" not in st.session_state:
        st.session_state.cumulative_co2_saved_kg = 0.0
    if "platform_stats" not in st.session_state:
        st.session_state.platform_stats = load_platform_stats()
    if "driver_sessions" not in st.session_state:
        st.session_state.driver_sessions = []  # Track each driver's session results


init_state()


# ------------------------------------------------------------
# UI styling (robust scoreboard)
# ------------------------------------------------------------

st.set_page_config(page_title="Gamified Carpool Driver Panel", layout="wide")

st.markdown(
    """
<style>
.block-container { padding-top: 1rem; }

.card {
  border-radius: 16px;
  padding: 14px;
  border: 1px solid #00000014;
  background: linear-gradient(135deg, #0B1A2A 0%, #1B3A57 100%);
  color: #ffffff;
  box-shadow: 0 8px 24px rgba(0,0,0,0.12);
}

.card h3 { margin: 0 0 8px 0; }
.muted { opacity: 0.85; font-size: 12px; }

.metricRow {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.metricBox {
  background: #ffffff10;
  border: 1px solid #ffffff22;
  border-radius: 14px;
  padding: 10px 12px;
  min-width: 140px;
}
.metricLabel { font-size: 12px; opacity: 0.85; }
.metricValue { font-size: 18px; font-weight: 800; }

.badge {
  display:inline-block;
  padding: 6px 10px;
  border-radius: 999px;
  background: #00C853;
  color: white;
  font-weight: 800;
  font-size: 12px;
  margin-right: 6px;
}
.badge.warn { background:#FF6D00; }
.badge.cool { background:#7B1FA2; }
.badge.bad { background:#D32F2F; }

hr { border: none; border-top: 1px solid #ffffff22; margin: 10px 0; }
</style>
""",
    unsafe_allow_html=True,
)


# ------------------------------------------------------------
# Timer / refresh (only auto-refresh if pending requests exist)
# ------------------------------------------------------------

# Only auto-refresh if there are pending requests that might resolve soon
pending_resolving = [r for r in st.session_state.pending_requests if r["status"] == "pending"]
should_auto_refresh = len(pending_resolving) > 0

if should_auto_refresh:
    # Faster refresh when waiting for responses (5 seconds)
    st_autorefresh(interval=5_000, key="tick5s")
else:
    # Slower refresh for passive mode (30 seconds) - just checking for manually triggered actions
    st_autorefresh(interval=30_000, key="tick30s")

elapsed = int(time.time()) - st.session_state.last_refresh_epoch
remaining = max(0, REFRESH_SECONDS - elapsed)

h1, h2, h3 = st.columns([2, 3, 2])
with h1:
    st.markdown("### 🚗 Gamified Driver Carpool Dashboard")
with h2:
    st.info(f"⏳ Next passenger refresh in **{remaining//60:02d}:{remaining%60:02d}** (mm:ss)")
with h3:
    if st.button("🔄 Refresh now", use_container_width=True):
        st.session_state.last_refresh_epoch = int(time.time())
        st.rerun()


# ------------------------------------------------------------
# Welcome banner with driver sex icon
# ------------------------------------------------------------
_welcome_icon = "👨" if st.session_state.driver_sex == "Male" else "👩"
_welcome_bg = "linear-gradient(135deg, #1a73e8 0%, #4fc3f7 100%)" if st.session_state.driver_sex == "Male" else "linear-gradient(135deg, #e91e8f 0%, #f48fb1 100%)"
st.markdown(
    f"""
    <div style="
        background: {_welcome_bg};
        border-radius: 14px;
        padding: 14px 24px;
        margin-bottom: 12px;
        display: flex;
        align-items: center;
        gap: 14px;
        box-shadow: 0 4px 16px rgba(0,0,0,0.10);
    ">
        <span style="font-size: 44px;">{_welcome_icon}</span>
        <div>
            <div style="font-size: 22px; font-weight: 800; color: white;">Welcome {st.session_state.driver_id}</div>
            <div style="font-size: 13px; color: #ffffffcc;">Your carpool journey starts here. Select passengers, earn rewards, save the planet!</div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)

if remaining == 0:
    st.session_state.last_refresh_epoch = int(time.time())


# ------------------------------------------------------------
# Process pending requests (simulated email confirmations)
# ------------------------------------------------------------

def process_pending_requests():
    now_ts = time.time()
    updated = []
    for req in st.session_state.pending_requests:
        if req["status"] != "pending":
            updated.append(req)
            continue

        if now_ts >= req["resolve_at_epoch"]:
            passenger_profile = req["passenger_profile"]
            alpha = req["alpha"]
            p_accept = acceptance_probability(alpha, passenger_profile)
            accepted = random.random() < p_accept

            if accepted:
                req["status"] = "accepted"
                st.session_state.accepted_passengers.add(req['passenger_id'])
                st.session_state.cumulative_confirmed_count += 1  # Track cumulative
                # --- Record realized earnings + CO2 savings (only on acceptance) ---
                _payment = float(req.get("payment_eur", 0.0) or 0.0)
                _coins_v = float(req.get("coins_val", 0.0) or 0.0)
                _co2 = float(req.get("co2_saved_kg", 0.0) or 0.0)
                st.session_state.cumulative_revenue += _payment
                st.session_state.cumulative_coins += _coins_v
                st.session_state.cumulative_co2_saved_kg += _co2
                _pstats = load_platform_stats()
                register_confirmation(_pstats, req.get("driver_id", st.session_state.driver_id), _payment, _coins_v, _co2)
                save_platform_stats(_pstats)
                st.session_state.platform_stats = _pstats
                st.session_state.alerts.append(f"✅ {req['passenger_id']} accepted your request.")
                subj, body = compose_driver_update_email(req["passenger_id"], "accepted", req["office_label"], req["driver_start_time"])
                write_outbox_email("to_driver_ACCEPTED", {"driver_id": req["driver_id"], "passenger_id": req["passenger_id"], "alpha": alpha, "subject": subj, "body": body, "time": now_local().isoformat()})
            else:
                req["status"] = "declined"
                st.session_state.declined_passengers.add(req['passenger_id'])
                st.session_state.alerts.append(f"❌ {req['passenger_id']} declined. You can pick a replacement.")

                if st.session_state.passenger_pool is not None:
                    st.session_state.passenger_pool = pd.concat([st.session_state.passenger_pool, pd.DataFrame([passenger_profile])], ignore_index=True)

                subj, body = compose_driver_update_email(req["passenger_id"], "declined", req["office_label"], req["driver_start_time"])
                write_outbox_email("to_driver_DECLINED", {"driver_id": req["driver_id"], "passenger_id": req["passenger_id"], "alpha": alpha, "subject": subj, "body": body, "time": now_local().isoformat()})

        updated.append(req)

    st.session_state.pending_requests = updated


process_pending_requests()
for msg in st.session_state.alerts[-4:]:
    st.warning(msg)


# ------------------------------------------------------------
# Layout
# ------------------------------------------------------------

left, main, right = st.columns([1.2, 2.2, 1.25], gap="large")


# ------------------------------------------------------------
# LEFT: Driver inputs
# ------------------------------------------------------------

with left:
    st.markdown("#### 🎮 Driver Setup")

    st.caption("🏢 Office location (GPS)")
    office_lat = st.number_input("Office latitude", value=12.971600, format="%.6f")
    office_lon = st.number_input("Office longitude", value=77.594600, format="%.6f")
    office_label = "Office (GPS)"

    st.caption("🏠 Driver home/pickup location (GPS)")
    home_lat = st.number_input("Home latitude", value=float(office_lat + 0.050000), format="%.6f")
    home_lon = st.number_input("Home longitude", value=float(office_lon - 0.050000), format="%.6f")

    pickup_radius_km = st.slider("📍 PICKUP_RADIUS_KM", min_value=1, max_value=100, value=10)
    seats = st.slider("🪑 NUM_OF_SEATS_AVAIL", min_value=1, max_value=6, value=3)

    st.caption("📅 Visiting days")
    selected_days = []
    d1, d2 = st.columns(2)
    for i, d in enumerate(DAYS):
        with (d1 if i % 2 == 0 else d2):
            if st.checkbox(d, value=(d in ["Mon", "Tue", "Wed", "Thu", "Fri"]), key=f"day_{d}"):
                selected_days.append(d)
    driver_days = set(selected_days)

    start_time_str = st.selectbox("🕒 TRIP_START_TIME", ["07:30", "08:00", "08:30", "09:00", "09:30", "10:00"], index=2)
    driver_start_min = parse_hhmm_to_minutes(start_time_str)

    waiting_tol = st.slider("⏱️ TOLERANCE_FOR_WAITING_AT_PICKUP (min)", min_value=0, max_value=30, value=15)

    cost_per_km = st.number_input("💶 Cost per km (EUR)", value=float(DEFAULT_COST_EUR_PER_KM), step=0.01)

    st.markdown("---")
    st.markdown("#### 🧭 Routing (optional)")

    routing_mode = st.radio("Routing mode", ["Public OSRM (internet)", "Local OSRM (recommended)"], index=0)
    osrm_base = st.text_input("OSRM base URL", value=("http://localhost:5000" if routing_mode.startswith("Local") else DEFAULT_OSRM_BASE))

    ca_bundle_path = st.text_input("🔐 CA bundle path (optional)", value="")
    insecure_skip_verify = st.checkbox("⚠️ Demo-only: disable SSL verification", value=False)
    if insecure_skip_verify:
        st.error("SSL verification is disabled. Use only for local/testing demos.")

    if st.button("🧪 Test OSRM connectivity", use_container_width=True):
        try:
            test_url = f"{osrm_base.rstrip('/')}/route/v1/driving/77.5946,12.9716;77.60,12.98?overview=false"
            verify = _verify_opt(ca_bundle_path, insecure_skip_verify)
            r = requests.get(test_url, timeout=10, verify=verify, headers={"User-Agent": "gamified-carpool-demo/1.0"})
            if r.status_code == 200 and '"code":"Ok"' in r.text:
                st.success("OSRM reachable ✅")
            else:
                st.error(f"OSRM responded but not OK ❌ (status={r.status_code})")
        except Exception as e:
            st.error(f"OSRM not reachable ❌: {e}")

    st.markdown("---")
    st.caption(f"Driver ID: {st.session_state.driver_id}")


driver_home = (float(home_lat), float(home_lon))
office = (float(office_lat), float(office_lon))


# ------------------------------------------------------------
# Regenerate 500 samples whenever office/home GPS changes
# ------------------------------------------------------------

def refresh_samples_if_needed(office_lat, office_lon, home_lat, home_lon):
    key = (round(float(office_lat), 6), round(float(office_lon), 6), round(float(home_lat), 6), round(float(home_lon), 6))
    if st.session_state.gps_key != key or st.session_state.passenger_pool is None:
        st.session_state.gps_key = key
        seed = abs(hash(key)) % (2**31 - 1)
        st.session_state.passenger_pool = generate_passengers_500(float(office_lat), float(office_lon), n=N_SAMPLES, radius_km=SAMPLE_RADIUS_KM, seed=seed)
        st.session_state.selected_passengers = set()
        st.session_state.selected_profiles = {}


refresh_samples_if_needed(office_lat, office_lon, home_lat, home_lon)

pool = st.session_state.passenger_pool.copy()

# Compute centroid distance for all 500
all_with_dist = add_centroid_distance(pool, driver_home)

# Add detour distance for all 500 (prioritize passengers on-the-way to office)
all_with_dist = add_detour_distance(all_with_dist, driver_home, office)

nearest5_overall = top_k_least_detour(all_with_dist, k=TOP_K)


# ------------------------------------------------------------
# Filtering on parameter changes
# ------------------------------------------------------------

# Stage 1: Distance filter
filtered_by_distance = all_with_dist[all_with_dist["dist_to_driver_km"] <= float(pickup_radius_km)].copy()

# Stage 2: Day filter
filtered_by_day = filter_by_day(filtered_by_distance, driver_days)

# Stage 3: Time filter
filtered = filter_by_time(filtered_by_day, driver_start_min, waiting_tol)

# Determine what to show
use_fallback = False
fallback_reason = ""
has_secondary = False  # Flag to indicate if we're showing secondary (next best) passengers

if len(filtered) > 0:
    highlight5 = top_k_least_detour(filtered, k=TOP_K)
    primary_ids = set(highlight5["passenger_id"].tolist())
    
    # If fewer than 5 filtered matches, fill with next best from overall sample
    if len(highlight5) < TOP_K:
        remaining_needed = TOP_K - len(highlight5)
        # Get passengers from all_with_dist that are not in filtered results
        not_in_filtered = all_with_dist[~all_with_dist["passenger_id"].isin(primary_ids)].copy()
        # Get the ones with least detour
        secondary = top_k_least_detour(not_in_filtered, k=remaining_needed)
        highlight5_secondary = pd.concat([highlight5, secondary], ignore_index=True)
        has_secondary = True
    else:
        highlight5_secondary = highlight5.copy()
else:
    highlight5 = nearest5_overall.copy()
    highlight5_secondary = highlight5.copy()
    primary_ids = set()
    use_fallback = True
    
    # Analyze which filter is blocking passengers
    if len(filtered_by_distance) == 0:
        fallback_reason = "distance"
    elif len(filtered_by_day) == 0:
        fallback_reason = "day"
    elif len(filtered) == 0:
        fallback_reason = "time"

base_km, base_geom, base_used = safe_route(osrm_base, driver_home, office, ca_bundle_path, insecure_skip_verify)


def get_filter_suggestions(all_passengers, filtered_dist, filtered_day, filtered_time, driver_days, pickup_radius_km, waiting_tol, driver_start_min):
    """Generate helpful suggestions for improving filter results."""
    suggestions = []
    
    # Check distance filter
    if len(filtered_dist) == 0:
        closest_passenger = all_passengers.nsmallest(1, "dist_to_driver_km")
        if len(closest_passenger) > 0:
            closest_km = closest_passenger.iloc[0]["dist_to_driver_km"]
            suggestions.append(f"📍 **Increase pickup radius** from {pickup_radius_km} km to at least {int(closest_km) + 1} km")
    
    # Check day filter
    if len(filtered_day) == 0 and len(filtered_dist) > 0:
        available_days = set()
        for days_str in filtered_dist["days"]:
            available_days.update([d.strip() for d in str(days_str).split(",") if d.strip()])
        missing_days = available_days - driver_days
        if missing_days:
            suggestions.append(f"📅 **Add more commute days**: Try including {', '.join(sorted(missing_days))} to match more passengers")
    
    # Check time filter
    if len(filtered_time) == 0 and len(filtered_day) > 0:
        # Suggest increasing time tolerance
        suggestions.append(f"🕒 **Increase wait tolerance** from {waiting_tol} min to 20-30 min")
        # Or suggest different start time
        suggestions.append(f"🕐 **Try a different start time** - passengers prefer commuting between 7:30-9:30 AM")
    
    return suggestions


with st.expander("🔍 Matching debug", expanded=False):
    st.write("**Filtering stages:**")
    st.write(f"1️⃣ Total samples: {len(all_with_dist)}")
    st.write(f"2️⃣ Within pickup radius ({pickup_radius_km} km): {len(filtered_by_distance)}")
    st.write(f"3️⃣ Matching your days ({', '.join(sorted(driver_days))}): {len(filtered_by_day)}")
    st.write(f"4️⃣ Matching time window (±{waiting_tol} min around {start_time_str}): {len(filtered)}")
    
    if has_secondary:
        num_primary = len(filtered)
        num_secondary = len(highlight5_secondary) - num_primary
        st.write(f"\n**Result:** ✅ Found {num_primary} matched + {num_secondary} suggested alternatives")
    else:
        st.write(f"\n**Result:** {'✅ Showing matched passengers' if not use_fallback else f'❌ No matches - blocking filter: {fallback_reason}'}")
    
    st.write(f"**Shown:** {len(highlight5_secondary)} passengers total")


# ------------------------------------------------------------
# MAIN: Map
# ------------------------------------------------------------

with main:
    st.markdown("#### 🗺️ Overall passengers (gray) + Matched (🟠 orange) + Suggested (🟡 yellow)")
    
    # Show status message
    if use_fallback:
        st.warning(
            "⚠️ **No passengers matched your filters.** Showing the 5 nearest passengers overall as fallback.",
            icon="⚠️"
        )
        suggestions = get_filter_suggestions(all_with_dist, filtered_by_distance, filtered_by_day, filtered, driver_days, pickup_radius_km, waiting_tol, driver_start_min)
        if suggestions:
            st.info("💡 **To find more passengers, try:**\n" + "\n".join(suggestions))
    elif has_secondary:
        num_matched = len(filtered)
        num_suggested = len(highlight5_secondary) - len(filtered)
        st.success(f"✅ Found {num_matched} passengers matching your filters + {num_suggested} suggested alternatives!", icon="✅")
        st.info(f"🟠 **Orange markers** = matched your filters | 🟡 **Yellow markers** = next best from all samples (may not match your day/time)", icon="ℹ️")
    else:
        st.success(f"✅ Found {len(highlight5)} passengers matching your filters!", icon="✅")

    m = folium.Map(location=[office[0], office[1]], zoom_start=10, tiles="OpenStreetMap")

    folium.Marker([driver_home[0], driver_home[1]], tooltip="Driver home/pickup (centroid)",
                  icon=folium.Icon(color="blue", icon="home")).add_to(m)
    folium.Marker([office[0], office[1]], tooltip=f"{office_label}",
                  icon=folium.Icon(color="green", icon="briefcase")).add_to(m)

    folium.PolyLine(base_geom, color="#00C853", weight=4, opacity=0.75, tooltip="Driver → Office").add_to(m)

    highlight_ids = set(highlight5_secondary["passenger_id"].tolist())
    primary_highlight_ids = primary_ids if has_secondary or not use_fallback else set()

    for _, row in all_with_dist.iterrows():
        pid = row["passenger_id"]
        d = float(row["dist_to_driver_km"])
        if pid in highlight_ids:
            continue
        folium.CircleMarker(
            location=[float(row["home_lat"]), float(row["home_lon"])],
            radius=4,
            color="#9E9E9E",
            fill=True,
            fill_opacity=0.55,
            tooltip=f"{pid} | dist_to_driver={d:.2f} km"
        ).add_to(m)

    # Draw primary passengers (matched filters) in orange
    for _, row in highlight5_secondary.iterrows():
        pid = row["passenger_id"]
        d = float(row["dist_to_driver_km"])
        tooltip = f"{pid} | dist_to_driver={d:.2f} km | start={row['trip_start_time']} | days={row['days']}"
        
        # Determine color based on whether it's primary or secondary
        is_primary = pid in primary_highlight_ids
        marker_color = "#FF6D00" if is_primary else "#FFD600"  # Orange for matched, Yellow for suggested
        radius = 8
        
        folium.CircleMarker(
            location=[float(row["home_lat"]), float(row["home_lon"])],
            radius=radius,
            color=marker_color,
            fill=True,
            fill_opacity=0.95,
            tooltip=tooltip
        ).add_to(m)

        folium.PolyLine([(driver_home[0], driver_home[1]), (float(row["home_lat"]), float(row["home_lon"]))],
                        color=marker_color, weight=2, opacity=0.35).add_to(m)

    # Add routes for selected passengers
    # IMPORTANT: use deterministic passenger-ID order to keep route + any numbering consistent
    selected_ids = list(st.session_state.selected_profiles.keys()) if st.session_state.selected_profiles else []
    if len(selected_ids) > 0 and st.session_state.selected_profiles:
        selected_stops = [(float(st.session_state.selected_profiles[pid]["home_lat"]),
                          float(st.session_state.selected_profiles[pid]["home_lon"])) for pid in selected_ids]
        try:
            order_idx, best_total_km, used_all, _is_exact = optimal_best_order_indices(
                osrm_base, driver_home, selected_stops, office, ca_bundle_path, insecure_skip_verify
            )
            ordered_stops = [selected_stops[i] for i in order_idx]
            st.session_state.selected_route_order = [selected_ids[i] for i in order_idx]
            # Draw the optimized route
            route_coords = [driver_home]
            for stop in ordered_stops:
                route_coords.append(stop)
            route_coords.append(office)
            
            # Get actual route geometry if possible
            for i in range(len(route_coords) - 1):
                d, geom, used = safe_route(osrm_base, route_coords[i], route_coords[i+1], ca_bundle_path, insecure_skip_verify)
                folium.PolyLine(geom, color="#2196F3", weight=3, opacity=0.9, tooltip="Selected route").add_to(m)
        except Exception:
            # Fallback: simple line path through selected passengers
            route_coords = [driver_home] + ordered_stops + [office]
            folium.PolyLine(route_coords, color="#2196F3", weight=3, opacity=0.7, tooltip="Selected route (approx)").add_to(m)
    
    st_folium(m, width=None, height=560)

    st.markdown("#### 👥 Highlighted passengers (Top 5 nearest to centroid)")
    
    # Display passengers as cards instead of table
    # Calculate available seats based on current DRIVER'S confirmed and pending passengers (filter by driver_id)
    current_driver_accepted = len([r for r in st.session_state.pending_requests if r["driver_id"] == st.session_state.driver_id and r["status"] == "accepted"])
    current_driver_pending = len([r for r in st.session_state.pending_requests if r["driver_id"] == st.session_state.driver_id and r["status"] == "pending"])
    available_seats = seats - current_driver_accepted - current_driver_pending
    
    st.caption(f"Select up to {seats} passengers | This driver - Confirmed: {current_driver_accepted}, Pending: {current_driver_pending}, Available: {available_seats}")
    
    # Show capacity status with logic
    if current_driver_accepted > 0 and available_seats == 1:
        st.info(f"🎯 Capacity Alert: {current_driver_accepted} passengers confirmed. You can select only 1 more passenger.")
    elif current_driver_accepted > 0 and available_seats == 0:
        st.warning(f"🚗 Vehicle Full: {current_driver_accepted}/{seats} passengers confirmed + {current_driver_pending} pending. You cannot select more until decisions are made.")
    
    current = set(st.session_state.selected_passengers)

    sorted_highlight5 = highlight5_secondary.sort_values("dist_to_driver_km")

    for _, row in sorted_highlight5.iterrows():
        pid = row["passenger_id"]
        checked = pid in current
        
        # Updated disability logic: account for available seats
        num_new_selections = len(current) if not checked else len(current) - 1
        disabled = (num_new_selections >= available_seats) and (not checked)

        # Determine if this is a primary or suggested passenger
        is_primary = pid in primary_highlight_ids
        card_background = "#f1d1b8" if is_primary else "#fffbea"
        card_border = "#FF6D00" if is_primary else "#ffd60080"

        # Parse age safely (handles en-dash, em-dash, hyphen)
        age_raw = str(row.get("age_group", ""))
        age_display = age_raw
        for sep in ["–", "-", "—"]:
            if sep in age_raw:
                parts = age_raw.split(sep)
                age_display = f"{parts[0].strip()}–{parts[1].strip()}"
                break

        # Build badge HTML separately (avoids nested f-string issues)
        badge_html = ""
        if not is_primary:
            badge_html = ('<span style="background:#ffd600;color:#333;'
                        'padding:2px 8px;border-radius:12px;font-size:10px;'
                        'font-weight:bold;">💡 Suggested alternative</span>')

        # Build full card HTML as a single string variable
        card_html = (
            f'<div style="border:2px solid {card_border};border-radius:8px;'
            f'padding:12px;margin-bottom:8px;background:{card_background};">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">'
            f'<span><b>{pid}</b> · {age_display} · {row["sex"]}</span>'
            f'{badge_html}'
            f'</div>'
            f'<span style="font-size:12px;color:#666;">Domain:</span> <b>{row["domain"]}</b><br/>'
            f'<span style="font-size:12px;color:#666;">Interests:</span> {row["interests"]}<br/>'
            f'<span style="font-size:11px;color:#888;">'
            f'📍 Distance: {row["dist_to_driver_km"]:.2f} km | '
            f'�️ Detour: {row.get("detour_km", 0):.2f} km | '
            f'🕒 Start: {row["trip_start_time"]} | '
            f'📅 Days: {row["days"]}</span><br/>'
            f'<span style="font-size:11px;color:#999;font-style:italic;">'
            f'✓ On-the-way passenger: minimal route deviation to pickup</span>'
            f'</div>'
        )

        col1, col2 = st.columns([0.05, 0.95])
        with col1:
            new_checked = st.checkbox("", value=checked, disabled=disabled, key=f"sel_{pid}")
        with col2:
            st.markdown(card_html, unsafe_allow_html=True)

        if new_checked and not checked:
            current.add(pid)
            st.session_state.selected_profiles[pid] = pool[pool["passenger_id"] == pid].iloc[0].to_dict()
        if (not new_checked) and checked:
            current.remove(pid)
            st.session_state.selected_profiles.pop(pid, None)

    st.session_state.selected_passengers = current

    if not base_used:
        st.warning("Routing server not reachable — using fallback (straight line) for driver→office in display/economics.")


# ------------------------------------------------------------
# RIGHT: Robust Scoreboard + pricing
# ------------------------------------------------------------

with right:
    st.markdown("#### 💰 Earnings & Negotiation")

    selected_profiles = st.session_state.selected_profiles

    st.session_state.alpha = st.slider("🎚️ Cost negotiation (α)", 0.0, 1.0, float(st.session_state.alpha), 0.05)
    alpha = float(st.session_state.alpha)
    st.write(fairness_message(alpha))

    if len(selected_profiles) == 0:
        st.info("Select passengers (from highlighted top 5) to see earnings and payment breakdown.")
    else:
        econ = compute_pricing_and_economics(
            osrm_base=osrm_base,
            cost_per_km=float(cost_per_km),
            alpha=alpha,
            driver_home=driver_home,
            office=office,
            selected_profiles=selected_profiles,
            ca_bundle_path=ca_bundle_path,
            insecure_skip_verify=insecure_skip_verify,
        )

        base_km = econ["base_km"]
        best_total_km = econ["best_total_km"]
        solo_cost = econ["solo_cost_driver"]
        shared_cost = econ["shared_cost_driver"]
        incremental_cost = econ["incremental_cost_driver"]
        revenue = econ["revenue"]
        benefit = econ["driver_benefit_vs_solo"]
        offset_pct = econ["offset_pct"]

        # --- Robust gamification ---
        # Fairness score: 1.0 at alpha=0.5, 0.0 at extremes
        fairness_score = float(max(0.0, 1.0 - abs(alpha - 0.5) / 0.5))

        # Coins: depend mainly on revenue (always >=0), plus bonus when benefit is positive
        coins = float(max(0.0, revenue) + max(0.0, benefit) * 0.5)

        # XP: always non-zero, depends on: passengers selected, offset, fairness, and positive benefit
        xp = int(
            25
            + 20 * len(selected_profiles)
            + min(75, offset_pct)
            + 40 * fairness_score
            + (15 if benefit > 0 else 0)
        )

        # Level system
        level = xp // 100 + 1
        xp_in_level = xp % 100
        xp_to_next = 100 - xp_in_level

        # Additional robust metrics
        detour_km = float(max(0.0, best_total_km - base_km))
        detour_ratio = float(detour_km / base_km) if base_km > 1e-9 else 0.0
        reliability = "High" if 0.40 <= alpha <= 0.70 else "Medium"

        # Badges
        badges = []
        badges.append(("NET POSITIVE", "badge")) if benefit >= 0 else badges.append(("NET NEGATIVE", "badge bad"))
        if offset_pct >= 50:
            badges.append(("COST HALF COVERED", "badge cool"))
        if detour_ratio <= 0.20:
            badges.append(("LOW DETOUR", "badge"))
        if len(selected_profiles) >= 3:
            badges.append(("TEAM RIDER", "badge"))
        if fairness_score >= 0.8:
            badges.append(("FAIR DEAL", "badge"))

        badge_html = " ".join([f"<span class=\"{cls}\">{txt}</span>" for txt, cls in badges])

        st.markdown(
            f"""
<div class="card">
  <h3>🏆 Commute Scoreboard</h3>
  <div class="muted">Tip: pick closer passengers (lower detours) and keep α near 0.5 for higher acceptance.</div>
  <hr/>
  {badge_html}
  <hr/>
  <div class="metricRow">
    <div class="metricBox"><div class="metricLabel">Level</div><div class="metricValue">{level}</div></div>
    <div class="metricBox"><div class="metricLabel">XP</div><div class="metricValue">{xp}</div></div>
    <div class="metricBox"><div class="metricLabel">Coins</div><div class="metricValue">€{coins:.2f}</div></div>
    <div class="metricBox"><div class="metricLabel">Fairness</div><div class="metricValue">{fairness_score:.2f}</div></div>
    <div class="metricBox"><div class="metricLabel">Offset</div><div class="metricValue">{offset_pct:.0f}%</div></div>
  </div>
  <hr/>
  <div class="muted">
    <b>Key numbers:</b><br/>
    Solo: {base_km:.2f} km (€{solo_cost:.2f})<br/>
    Shared: {best_total_km:.2f} km (€{shared_cost:.2f}) | Detour: +{detour_km:.2f} km<br/>
    Revenue: €{revenue:.2f} | Extra pickup cost: €{incremental_cost:.2f}<br/>
    <b>Benefit vs solo:</b> {'+' if benefit>=0 else '-'}€{abs(benefit):.2f} | Reliability: {reliability}
  </div>
</div>
""",
            unsafe_allow_html=True,
        )

        # Progress bars: offset and XP-to-next-level
        st.caption(f"🎯 Level {level} | Next level in {xp_to_next} XP")
        st.progress(xp_in_level / 100.0)
        st.caption("💸 Revenue offset vs solo commute")
        st.progress(min(1.0, offset_pct / 100.0))

        st.markdown("#### 🧾 Passenger price breakdown")
        breakdown = econ["passenger_breakdown"]
        rows = []
        for pid, b in breakdown.items():
            rows.append({
                "Passenger": pid,
                "Solo to office (km)": round(b["passenger_solo_km"], 2),
                "Assigned detour (km)": round(b["assigned_detour_km"], 2),
                "(1-α)*solo avoided (€)": round((1.0 - alpha) * b["solo_avoided_cost"], 2),
                "Detour cost (€)": round(b["detour_cost"], 2),
                "Payment (€)": round(b["payment"], 2),
            })
        st.dataframe(pd.DataFrame(rows).sort_values("Payment (€)", ascending=False), use_container_width=True)

        st.markdown("---")
        confirm_disabled = len(selected_profiles) == 0
        if st.button("✅ Confirm & Send Requests", use_container_width=True, disabled=confirm_disabled):
            remove_ids = set(selected_profiles.keys())
            st.session_state.last_sent_email_previews = []
            st.session_state.passenger_breakdown = breakdown  # Store breakdown for preview section

            for pid in remove_ids:
                pr = selected_profiles[pid]
                # Get cost breakdown for this passenger
                cost_breakdown = breakdown.get(pid, {})
                subj, body = compose_passenger_email(pr, start_time_str, office_label, alpha, pickup_radius_km, driver_benefit_hint=benefit, cost_breakdown=cost_breakdown)
                st.session_state.last_sent_email_previews.append((pid, subj, body))

                write_outbox_email("to_passenger_REQUEST", {
                    "driver_id": st.session_state.driver_id,
                    "passenger_id": pid,
                    "office_label": office_label,
                    "driver_start_time": start_time_str,
                    "payment_eur": float(breakdown.get(pid, {}).get("payment", 0.0) or 0.0),
                    "coins_val": 0.0,
                    "co2_saved_kg": float(max(0.0, breakdown.get(pid, {}).get("passenger_solo_km", 0.0) or 0.0) * CO2_PER_KM_KG),
                    "alpha": alpha,
                    "subject": subj,
                    "body": body,
                    "time": now_local().isoformat(),
                })

                delay = random.randint(10, 45)
                _passenger_solo_km = float(breakdown.get(pid, {}).get("passenger_solo_km", 0.0) or 0.0)
                _co2_saved = float(_passenger_solo_km * CO2_PER_KM_KG)
                _payment = float(breakdown.get(pid, {}).get("payment", 0.0) or 0.0)
                
                st.session_state.pending_requests.append({
                    "driver_id": st.session_state.driver_id,
                    "passenger_id": pid,
                    "alpha": alpha,
                    "sent_at": now_local().isoformat(),
                    "resolve_at_epoch": time.time() + delay,
                    "status": "pending",
                    "passenger_profile": pr,
                    "office_label": office_label,
                    "driver_start_time": start_time_str,
                    "payment_eur": _payment,
                    "coins_val": 0.0,
                    "co2_saved_kg": _co2_saved,
                })
            
            # Register requests sent (earnings counted only on acceptance)
            _send_stats = load_platform_stats()
            register_requests_sent(_send_stats, st.session_state.driver_id, len(remove_ids))
            save_platform_stats(_send_stats)
            st.session_state.platform_stats = _send_stats

            st.session_state.passenger_pool = pool[~pool["passenger_id"].isin(remove_ids)].copy()
            st.session_state.selected_passengers = set()
            st.session_state.selected_profiles = {}

            st.success("Requests sent. Waiting for passenger confirmations...")
            st.rerun()
    
    # ============================================================
    # GAMIFIED PLATFORM IMPACT — Cumulative Stats (All Drivers)
    # ============================================================
    st.markdown("---")
    st.markdown("#### 🏆 Platform Impact — Cumulative Stats (All Drivers)")

    _viz_stats = load_platform_stats()
    st.session_state.platform_stats = _viz_stats
    _totals = _viz_stats.get("totals", {})
    _leaders = _viz_stats.get("leaders", {})

    # Gamified platform stats card
    _platform_html = f"""
<div class="card">
  <h3>🌍 Global Community Impact</h3>
  <div class="metricRow">
    <div class="metricBox">
      <div class="metricLabel">👥 Drivers Active</div>
      <div class="metricValue">{int(_totals.get("drivers_sent_requests", 0) or 0)}</div>
    </div>
    <div class="metricBox">
      <div class="metricLabel">✅ Success Rate</div>
      <div class="metricValue">{int(_totals.get("drivers_confirmed", 0) or 0)}</div>
    </div>
  </div>
  <hr/>
  <div class="metricRow">
    <div class="metricBox">
      <div class="metricLabel">📨 Requests Sent</div>
      <div class="metricValue">{int(_totals.get("requests_sent", 0) or 0)}</div>
    </div>
    <div class="metricBox">
      <div class="metricLabel">🎉 Confirmed Rides</div>
      <div class="metricValue">{int(_totals.get("requests_confirmed", 0) or 0)}</div>
    </div>
  </div>
  <hr/>
  <div class="metricRow">
    <div class="metricBox">
      <div class="metricLabel">💰 Total Revenue</div>
      <div class="metricValue">€{float(_totals.get('revenue_eur', 0.0) or 0.0):.2f}</div>
    </div>
    <div class="metricBox">
      <div class="metricLabel">🌱 CO₂ Saved</div>
      <div class="metricValue">{float(_totals.get('co2_saved_kg', 0.0) or 0.0):.1f} kg</div>
    </div>
  </div>
  <hr/>
  <div class="muted">
    <b>🌟 Leaders:</b><br/>
    💵 Top earner: {_leaders.get("top_earner_driver_id", "N/A")} ({_leaders.get("top_earner_revenue_eur", 0.0):.2f}€)<br/>
    🌍 CO₂ hero: {_leaders.get("co2_hero_driver_id", "N/A")} ({_leaders.get("co2_hero_saved_kg", 0.0):.1f}kg)
  </div>
</div>
"""
    st.markdown(_platform_html, unsafe_allow_html=True)
    
    # Gamified your driver impact card
    st.markdown("---")
    st.markdown("#### 👤 Your Driver Impact Card")
    
    _my = _viz_stats.get("drivers", {}).get(st.session_state.driver_id, {
        "requests_sent": 0, "requests_confirmed": 0, "revenue_eur": 0.0, "coins": 0.0, "co2_saved_kg": 0.0
    })
    _my_badges = format_badges(_my)

    _my_rev = float(_my.get("revenue_eur", 0.0) or 0.0)
    _my_conf = int(_my.get("requests_confirmed", 0) or 0)
    _my_sent = int(_my.get("requests_sent", 0) or 0)
    _my_co2 = float(_my.get("co2_saved_kg", 0.0) or 0.0)
    _my_success_rate = ((_my_conf / _my_sent * 100) if _my_sent > 0 else 0.0)

    _driver_html = f"""
<div class="card">
  <h3>🎖️ Your Session Stats</h3>
  <div class="metricRow">
    <div class="metricBox">
      <div class="metricLabel">💰 Revenue Earned</div>
      <div class="metricValue">€{_my_rev:.2f}</div>
    </div>
    <div class="metricBox">
      <div class="metricLabel">🎉 Confirmed</div>
      <div class="metricValue">{_my_conf}</div>
    </div>
  </div>
  <hr/>
  <div class="metricRow">
    <div class="metricBox">
      <div class="metricLabel">📨 Requests Sent</div>
      <div class="metricValue">{_my_sent}</div>
    </div>
    <div class="metricBox">
      <div class="metricLabel">✨ Success Rate</div>
      <div class="metricValue">{_my_success_rate:.0f}%</div>
    </div>
  </div>
  <hr/>
  <div class="metricRow">
    <div class="metricBox">
      <div class="metricLabel">🌱 CO₂ Saved</div>
      <div class="metricValue">{_my_co2:.1f} kg</div>
    </div>
    <div class="metricBox">
      <div class="metricLabel">🌍 Avg CO₂/Ride</div>
      <div class="metricValue">{(_my_co2 / _my_conf if _my_conf > 0 else 0.0):.2f} kg</div>
    </div>
  </div>
  <hr/>
  <div class="muted">
    <b>Achievements:</b> {', '.join(_my_badges) if _my_badges else 'Keep driving to unlock badges!'}
  </div>
</div>
"""
    st.markdown(_driver_html, unsafe_allow_html=True)

    # Top performers leaderboard
    _all_drivers = list(_viz_stats.get("drivers", {}).values())
    if _all_drivers:
        st.markdown("---")
        st.markdown("#### 🏅 Top Performers (All Time)")
        _top5 = sorted(_all_drivers, key=lambda x: float(x.get("revenue_eur", 0.0) or 0.0), reverse=True)[:5]
        _leaderboard_html = '<div style="background:#ffffff10;border:1px solid #ffffff22;border-radius:8px;padding:12px;">'
        for _ri, _row in enumerate(_top5, 1):
            _badge = "🥇" if _ri == 1 else ("🥈" if _ri == 2 else ("🥉" if _ri == 3 else f"#{_ri}"))
            _leaderboard_html += f'<div style="padding:8px;border-bottom:1px solid #ffffff11;"><b>{_badge} {_row.get("driver_id")}</b> — €{float(_row.get("revenue_eur", 0.0) or 0.0):.2f} | {int(_row.get("requests_confirmed", 0) or 0)} rides | {float(_row.get("co2_saved_kg", 0.0) or 0.0):.1f}kg CO₂</div>'
        _leaderboard_html += '</div>'
        st.markdown(_leaderboard_html, unsafe_allow_html=True)

    _impact_card = {
        "driver_id": st.session_state.driver_id,
        "your_revenue_eur": float(_my.get("revenue_eur", 0.0) or 0.0),
        "your_requests_sent": int(_my.get("requests_sent", 0) or 0),
        "your_confirmed": int(_my.get("requests_confirmed", 0) or 0),
        "your_co2_saved_kg": float(_my.get("co2_saved_kg", 0.0) or 0.0),
        "your_badges": _my_badges,
        "platform_totals": _totals,
        "leaders": _leaders,
        "generated_at": now_local().isoformat(),
    }
    st.download_button("📥 Download your impact card (JSON)", data=json.dumps(_impact_card, indent=2),
                       file_name=f"impact_card_{st.session_state.driver_id}.json", mime="application/json")
    st.download_button("📥 Download platform rollup (JSON)", data=json.dumps(_viz_stats, indent=2),
                       file_name="platform_stats.json", mime="application/json")


# ------------------------------------------------------------
# Bottom: Psychological emails
# ------------------------------------------------------------

st.markdown("---")
st.markdown("## ✉️ Simulated Psychological Emails")

sel_live = st.session_state.selected_profiles
if len(sel_live) > 0:
    st.subheader("📨 Email previews (if you confirm now)")
    for pid, pr in sel_live.items():
        # Get cost breakdown for preview if available
        cost_breakdown = st.session_state.passenger_breakdown.get(pid, {}) if st.session_state.passenger_breakdown else {}
        subj, body = compose_passenger_email(pr, start_time_str, office_label, float(st.session_state.alpha), pickup_radius_km, cost_breakdown=cost_breakdown)
        with st.expander(f"Preview to {pid}: {subj}", expanded=False):
            st.text_area("Subject", subj, height=40, key=f"subj_live_{pid}")
            st.text_area("Body", body, height=220, key=f"body_live_{pid}")
else:
    st.info("Select passengers to preview the psychological emails.")

if st.session_state.last_sent_email_previews:
    st.subheader("✅ Emails sent on last Confirm")
    for pid, subj, body in st.session_state.last_sent_email_previews[:10]:
        with st.expander(f"Sent to {pid}: {subj}", expanded=False):
            st.text_area("Subject", subj, height=40, key=f"subj_sent_{pid}")
            st.text_area("Body", body, height=220, key=f"body_sent_{pid}")


with st.expander("📤 Outbox & Debug", expanded=False):
    st.write(f"Driver ID: **{st.session_state.driver_id}**")
    st.write("Pending requests:", sum(1 for r in st.session_state.pending_requests if r["status"] == "pending"))
    st.write(f"Accepted passengers: {len(st.session_state.accepted_passengers)}")
    st.write(f"Declined passengers: {len(st.session_state.declined_passengers)}")
    files = sorted([f for f in os.listdir(OUTBOX_DIR) if f.endswith(".json")], reverse=True)[:15]
    st.write("Recent outbox files:")
    for f in files:
        st.code(os.path.join(OUTBOX_DIR, f))

st.markdown("---")
st.markdown("## 👤 Switch Driver (Simulate Another User)")

col1, col2 = st.columns([2, 1])
with col1:
    st.write("Click to simulate selecting passengers for another driver. Previously selected passengers will not appear in the new driver's pool.")
with col2:
    if st.button("🚗 Next Driver", use_container_width=True):
        # Save current driver's session stats
        current_driver_confirmed = len([r for r in st.session_state.pending_requests if r["driver_id"] == st.session_state.driver_id and r["status"] == "accepted"])
        if current_driver_confirmed > 0:
            st.session_state.driver_sessions.append({
                "driver_id": st.session_state.driver_id,
                "confirmed_passengers": current_driver_confirmed,
            })
        
        # Collect only THIS driver's passengers that have been interacted with
        this_driver_used_passengers = set()
        for req in st.session_state.pending_requests:
            if req["driver_id"] == st.session_state.driver_id:
                this_driver_used_passengers.add(req["passenger_id"])
        # Also add currently selected passengers
        this_driver_used_passengers.update(st.session_state.selected_passengers)
        
        # Remove only this driver's used passengers from pool
        if st.session_state.passenger_pool is not None and len(this_driver_used_passengers) > 0:
            st.session_state.passenger_pool = st.session_state.passenger_pool[
                ~st.session_state.passenger_pool["passenger_id"].isin(this_driver_used_passengers)
            ].copy()
        
        # Reset per-driver state only
        st.session_state.selected_passengers = set()
        st.session_state.selected_profiles = {}
        st.session_state.passenger_breakdown = {}
        
        # Generate new driver ID
        new_driver_id = f"D{random.randint(1000, 9999)}"
        st.session_state.driver_id = new_driver_id
        
        st.success(f"✅ New driver {new_driver_id} ready! {len(this_driver_used_passengers)} passengers removed from pool.")
        st.rerun()
