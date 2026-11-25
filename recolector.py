import os
import json
import csv
import io
import difflib
import unicodedata
from datetime import datetime, timezone, timedelta
import requests

# ==========================================
# CONFIGURACIÓN PRINCIPAL (SIN API-FOOTBALL)
# ==========================================
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")
FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_KEY", "")

MAX_PARTIDOS_DIA = 20

LIGAS_OBJETIVO = [
    # --- SUDAMÉRICA ---
    {"nombre": "Liga Profesional Argentina", "odds_key": "soccer_argentina_primera_division", "fd_code": None,  "espn": "arg.1",                 "avg_h": 1.28, "avg_a": 0.94},
    {"nombre": "Copa Libertadores",          "odds_key": "soccer_conmebol_copa_libertadores", "fd_code": None,  "espn": "conmebol.libertadores", "avg_h": 1.50, "avg_a": 0.95},
    {"nombre": "Copa Sudamericana",          "odds_key": "soccer_conmebol_copa_sudamericana", "fd_code": None,  "espn": "conmebol.sudamericana", "avg_h": 1.44, "avg_a": 0.96},
    {"nombre": "Brasileirão Serie A",        "odds_key": "soccer_brazil_campeonato",          "fd_code": "BSA", "espn": "bra.1",                 "avg_h": 1.48, "avg_a": 1.02},
    # --- EUROPA ---
    {"nombre": "Premier League",             "odds_key": "soccer_epl",                        "fd_code": "PL",  "espn": "eng.1",                 "avg_h": 1.62, "avg_a": 1.32},
    {"nombre": "LaLiga",                     "odds_key": "soccer_spain_la_liga",              "fd_code": "PD",  "espn": "esp.1",                 "avg_h": 1.48, "avg_a": 1.12},
    {"nombre": "Serie A",                    "odds_key": "soccer_italy_serie_a",              "fd_code": "SA",  "espn": "ita.1",                 "avg_h": 1.45, "avg_a": 1.15},
    {"nombre": "Bundesliga",                 "odds_key": "soccer_germany_bundesliga",         "fd_code": "BL1", "espn": "ger.1",                 "avg_h": 1.75, "avg_a": 1.38},
    {"nombre": "Ligue 1",                    "odds_key": "soccer_france_ligue_one",           "fd_code": "FL1", "espn": "fra.1",                 "avg_h": 1.52, "avg_a": 1.18},
    {"nombre": "Champions League",           "odds_key": "soccer_uefa_champs_league",         "fd_code": "CL",  "espn": "uefa.champions",        "avg_h": 1.68, "avg_a": 1.30},
]

HEADERS_FD = {"X-Auth-Token": FOOTBALL_DATA_KEY}
CACHE_CIUDADES = {}


def normalizar(texto):
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    t = t.lower().replace("-", " ").replace(".", " ")
    for palabra in ("fc", "cf", "sc", "ec", "ac", "ca", "cd", "club", "atletico", "atlético"):
        t = t.replace(f" {palabra} ", " ")
    return " ".join(t.split())


def buscar_equipo(nombre_objetivo, diccionario):
    if not diccionario:
        return None
    objetivo_norm = normalizar(nombre_objetivo)
    mapa_norm = {normalizar(k): k for k in diccionario.keys()}

    if objetivo_norm in mapa_norm:
        return diccionario[mapa_norm[objetivo_norm]]

    for k_norm, k_orig in mapa_norm.items():
        if objetivo_norm and (objetivo_norm in k_norm or k_norm in objetivo_norm):
            return diccionario[k_orig]

    match = difflib.get_close_matches(objetivo_norm, mapa_norm.keys(), n=1, cutoff=0.52)
    return diccionario[mapa_norm[match[0]]] if match else None


# ==========================================
# 1. CARTELERA 48H + ESTADÍSTICAS ESPN (GRATIS)
# ==========================================
def obtener_partidos_proximos_espn(espn_slug, ahora_utc):
    """Trae partidos programados de hoy y mañana (próximas 48h)."""
    desde = ahora_utc.strftime("%Y%m%d")
    hasta = (ahora_utc + timedelta(days=1)).strftime("%Y%m%d")
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_slug}/scoreboard"
    partidos = []
    try:
        r = requests.get(url, params={"dates": f"{desde}-{hasta}", "limit": 50}, timeout=15)
        if r.status_code != 200:
            return partidos

        for ev in r.json().get("events", []):
            state = ev.get("status", {}).get("type", {}).get("state", "")
            if state != "pre":  # Solo partidos que aún no empiezan
                continue

            comp = (ev.get("competitions") or [{}])[0]
            competitors = comp.get("competitors", [])
            home_c = next((c for c in competitors if c.get("homeAway") == "home"), None)
            away_c = next((c for c in competitors if c.get("homeAway") == "away"), None)
            if not home_c or not away_c:
                continue

            ciudad = comp.get("venue", {}).get("address", {}).get("city", "")
            partidos.append({
                "id": str(ev.get("id", "")),
                "date": ev.get("date", ""),
                "homeTeam": home_c.get("team", {}).get("displayName", ""),
                "awayTeam": away_c.get("team", {}).get("displayName", ""),
                "venueCity": ciudad,
            })
    except Exception as e:
        print(f"  [Aviso ESPN Cartelera] {espn_slug}: {e}")
    return partidos


def obtener_standings_espn(espn_slug):
    url = f"https://site.api.espn.com/apis/v2/sports/soccer/{espn_slug}/standings"
    equipos = {}
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return equipos
        data = r.json()
        bloques = []
        if "children" in data:
            for ch in data["children"]:
                bloques.extend(ch.get("standings", {}).get("entries", []))
        elif "standings" in data:
            bloques.extend(data["standings"].get("entries", []))

        for entry in bloques:
            team = entry.get("team", {})
            nombre = team.get("displayName") or team.get("name") or ""
            if not nombre:
                continue
            stats_map = {s.get("name"): s.get("value", 0) for s in entry.get("stats", [])}
            pj = float(stats_map.get("gamesPlayed", 0) or 0)
            gf = float(stats_map.get("pointsFor", 0) or 0)
            ga = float(stats_map.get("pointsAgainst", 0) or 0)
            pts = float(stats_map.get("points", 0) or 0)
            gd = float(stats_map.get("pointDifferential", gf - ga) or 0)

            info = {
                "pj": pj,
                "gf_pg": gf / max(1.0, pj),
                "ga_pg": ga / max(1.0, pj),
                "ppg": pts / max(1.0, pj),
                "gd_pg": gd / max(1.0, pj),
            }
            equipos[nombre] = info
            if team.get("shortDisplayName"):
                equipos[team["shortDisplayName"]] = info
    except Exception as e:
        print(f"  [Aviso ESPN Standings] {espn_slug}: {e}")
    return equipos


def obtener_recientes_espn(espn_slug, ahora_utc):
    desde = (ahora_utc - timedelta(days=25)).strftime("%Y%m%d")
    hasta = (ahora_utc - timedelta(days=1)).strftime("%Y%m%d")
    url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{espn_slug}/scoreboard"
    historial = {}
    try:
        r = requests.get(url, params={"dates": f"{desde}-{hasta}", "limit": 200}, timeout=15)
        if r.status_code != 200:
            return historial
        for ev in r.json().get("events", []):
            if not ev.get("status", {}).get("type", {}).get("completed"):
                continue
            fecha_ev = ev.get("date", "")
            comp = (ev.get("competitions") or [{}])[0]
            competitors = comp.get("competitors", [])
            if len(competitors) != 2:
                continue
            for idx, c in enumerate(competitors):
                rival = competitors[1 - idx]
                t_name = c.get("team", {}).get("displayName", "")
                if not t_name:
                    continue
                g_f = int(float(c.get("score", 0) or 0))
                g_c = int(float(rival.get("score", 0) or 0))
                res = "W" if g_f > g_c else ("L" if g_f < g_c else "D")
                historial.setdefault(t_name, []).append({"date": fecha_ev, "res": res})
        for t in historial:
            historial[t].sort(key=lambda x: x["date"], reverse=True)
    except Exception:
        pass
    return historial


def construir_metricas_espn(nombre_equipo, es_local, tabla_espn, hist_espn, fecha_partido_iso, avg_h, avg_a):
    base_gf = avg_h if es_local else avg_a
    base_ga = avg_a if es_local else avg_h
    ratio_sede = (avg_h / avg_a) if es_local else (avg_a / avg_h)

    info = buscar_equipo(nombre_equipo, tabla_espn)
    partidos = buscar_equipo(nombre_equipo, hist_espn) or []

    if info and info["pj"] > 0:
        gf_raw = info["gf_pg"] * (ratio_sede ** 0.35)
        ga_raw = info["ga_pg"] * ((1.0 / ratio_sede) ** 0.35)
        gf_final = round((gf_raw * info["pj"] + 2.5 * base_gf) / (info["pj"] + 2.5), 2)
        ga_final = round((ga_raw * info["pj"] + 2.5 * base_ga) / (info["pj"] + 2.5), 2)
        pseudo_elo = round(1380 + (info["ppg"] * 115) + (info["gd_pg"] * 45))
    else:
        gf_final, ga_final, pseudo_elo = base_gf, base_ga, 1550

    rest_days = 5
    if partidos:
        try:
            dt_hoy = datetime.fromisoformat(fecha_partido_iso.replace("Z", "+00:00"))
            dt_ult = datetime.fromisoformat(partidos[0]["date"].replace("Z", "+00:00"))
            rest_days = max(1, min(14, (dt_hoy - dt_ult).days))
        except Exception:
            pass
        form_str = "".join(reversed([p["res"] for p in partidos[:5]]))
        pts_5 = sum(3 if c == "W" else 1 if c == "D" else 0 for c in form_str)
        form_score = round(pts_5 / (len(form_str) * 3), 2)
    else:
        form_str, form_score = "DDDDD", 0.5

    return {
        "gf": gf_final, "ga": ga_final,
        "form_str": form_str, "form_score": form_score,
        "rest_days": rest_days, "pseudo_elo": pseudo_elo,
    }


# ==========================================
# 2. FOOTBALL-DATA.ORG (PRIORIDAD 1 - EXACTO)
# ==========================================
def obtener_historial_football_data(fd_code):
    if not fd_code or not FOOTBALL_DATA_KEY:
        return {}
    url = f"https://api.football-data.org/v4/competitions/{fd_code}/matches"
    historial = {}
    try:
        r = requests.get(url, headers=HEADERS_FD, params={"status": "FINISHED"}, timeout=20)
        if r.status_code != 200:
            print(f"  [Aviso Football-Data] {fd_code} -> Status {r.status_code}")
            return {}
        partidos = r.json().get("matches", [])
        for m in partidos:
            ft = m.get("score", {}).get("fullTime", {})
            gh, ga = ft.get("home"), ft.get("away")
            if gh is None or ga is None:
                continue
            fecha = m.get("utcDate", "")
            h_team, a_team = m.get("homeTeam", {}), m.get("awayTeam", {})
            reg_h = {"date": fecha, "es_local": True,  "gf": gh, "ga": ga}
            reg_a = {"date": fecha, "es_local": False, "gf": ga, "ga": gh}
            for clave in (h_team.get("name"), h_team.get("shortName")):
                if clave: historial.setdefault(clave, []).append(reg_h)
            for clave in (a_team.get("name"), a_team.get("shortName")):
                if clave: historial.setdefault(clave, []).append(reg_a)
        for k in historial:
            historial[k].sort(key=lambda x: x["date"], reverse=True)
        print(f"  [Football-Data] {fd_code}: {len(partidos)} partidos terminados cargados.")
    except Exception as e:
        print(f"  [Aviso Football-Data] {fd_code}: {e}")
    return historial


def calcular_desde_historial(partidos, es_local_hoy, fecha_partido_iso, avg_h, avg_a):
    base_gf = avg_h if es_local_hoy else avg_a
    base_ga = avg_a if es_local_hoy else avg_h

    rest_days = 5
    try:
        dt_hoy = datetime.fromisoformat(fecha_partido_iso.replace("Z", "+00:00"))
        dt_ult = datetime.fromisoformat(partidos[0]["date"].replace("Z", "+00:00"))
        rest_days = max(1, min(14, (dt_hoy - dt_ult).days))
    except Exception:
        pass

    gf_gen_s, ga_gen_s, w_gen = 0.0, 0.0, 0.0
    gf_sed_s, ga_sed_s, w_sed = 0.0, 0.0, 0.0
    ult5_res, pts_tot, dg_tot = [], 0, 0
    n_tot = len(partidos)

    for i, p in enumerate(partidos):
        gf, ga = p["gf"], p["ga"]
        dg_tot += (gf - ga)
        if gf > ga: pts_tot += 3; res = "W"
        elif gf == ga: pts_tot += 1; res = "D"
        else: res = "L"
        if i < 5: ult5_res.append(res)
        if i < 15:
            w = 0.92 ** i
            gf_gen_s += gf * w; ga_gen_s += ga * w; w_gen += w
            if p["es_local"] == es_local_hoy:
                gf_sed_s += gf * w; ga_sed_s += ga * w; w_sed += w

    gf_gen = (gf_gen_s + 2.0 * base_gf) / (w_gen + 2.0)
    ga_gen = (ga_gen_s + 2.0 * base_ga) / (w_gen + 2.0)
    if w_sed >= 1.8:
        gf_sed = (gf_sed_s + 2.0 * base_gf) / (w_sed + 2.0)
        ga_sed = (ga_sed_s + 2.0 * base_ga) / (w_sed + 2.0)
        gf_final = round(0.65 * gf_sed + 0.35 * gf_gen, 2)
        ga_final = round(0.65 * ga_sed + 0.35 * ga_gen, 2)
    else:
        gf_final, ga_final = round(gf_gen, 2), round(ga_gen, 2)

    form_str = "".join(reversed(ult5_res)) or "DDDDD"
    pts_5 = sum(3 if c == "W" else 1 if c == "D" else 0 for c in form_str)
    form_score = round(pts_5 / (len(form_str) * 3), 2)
    pseudo_elo = round(1380 + ((pts_tot / max(1, n_tot)) * 115) + ((dg_tot / max(1, n_tot)) * 45))

    return {
        "gf": gf_final, "ga": ga_final,
        "form_str": form_str, "form_score": form_score,
        "rest_days": rest_days, "pseudo_elo": pseudo_elo,
    }


# ==========================================
# 3. OPEN-METEO + CLUB ELO + THE ODDS API
# ==========================================
def obtener_factor_clima(ciudad, fecha_iso):
    if not ciudad or not fecha_iso:
        return 1.0
    ciudad_limpia = ciudad.split(",")[0].strip()
    if ciudad_limpia not in CACHE_CIUDADES:
        try:
            r = requests.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": ciudad_limpia, "count": 1}, timeout=10)
            res = r.json().get("results", [])
            CACHE_CIUDADES[ciudad_limpia] = (res[0]["latitude"], res[0]["longitude"]) if res else None
        except Exception:
            CACHE_CIUDADES[ciudad_limpia] = None

    coords = CACHE_CIUDADES[ciudad_limpia]
    if not coords:
        return 1.0
    try:
        dt_utc = datetime.fromisoformat(fecha_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
        hora_obj, dia = dt_utc.strftime("%Y-%m-%dT%H:00"), dt_utc.strftime("%Y-%m-%d")
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={"latitude": coords[0], "longitude": coords[1], "hourly": "temperature_2m,precipitation,wind_speed_10m", "timezone": "UTC", "start_date": dia, "end_date": dia},
            timeout=10,
        )
        hourly = r.json().get("hourly", {})
        tiempos = hourly.get("time", [])
        if hora_obj in tiempos:
            idx = tiempos.index(hora_obj)
            temp, lluvia, viento = hourly["temperature_2m"][idx], hourly["precipitation"][idx], hourly["wind_speed_10m"][idx]
            factor = 1.0
            if lluvia >= 4.0: factor -= 0.08
            elif lluvia >= 1.0: factor -= 0.04
            if viento >= 35.0: factor -= 0.06
            elif viento >= 22.0: factor -= 0.03
            if temp >= 33.0 or temp <= 2.0: factor -= 0.03
            return round(max(0.84, factor), 2)
    except Exception:
        pass
    return 1.0


def obtener_tabla_elo(fecha_str):
    elo_map = {}
    try:
        r = requests.get(f"http://api.clubelo.com/{fecha_str}", timeout=15)
        if r.status_code == 200:
            for row in csv.DictReader(io.StringIO(r.text)):
                if row.get("Club") and row.get("Elo"):
                    elo_map[row["Club"].strip()] = round(float(row["Elo"]))
    except Exception:
        pass
    return elo_map


def buscar_elo(nombre_equipo, elo_map):
    if not elo_map:
        return None
    match = difflib.get_close_matches(nombre_equipo, elo_map.keys(), n=1, cutoff=0.6)
    return elo_map[match[0]] if match else None


def obtener_cuotas_liga(odds_key):
    url = f"https://api.the-odds-api.com/v4/sports/{odds_key}/odds"
    params = {"apiKey": ODDS_API_KEY, "regions": "eu,uk", "markets": "h2h,totals", "oddsFormat": "decimal"}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            print(f"  [Odds API] {odds_key} OK (Restantes: {r.headers.get('x-requests-remaining', '?')})")
            return r.json()
    except Exception as e:
        print(f"  [Aviso Odds API] {odds_key}: {e}")
    return []


def extraer_cuotas_partido(home_name, away_name, eventos_odds):
    cuotas = {
        "home": 0, "draw": 0, "away": 0,
        "over25": 0, "under25": 0,
        "btts_yes": 0, "btts_no": 0,
        "pinnacle_home": 0, "pinnacle_draw": 0, "pinnacle_away": 0,
    }
    if not eventos_odds:
        return cuotas
    nombres = [ev.get("home_team", "") for ev in eventos_odds]
    match = difflib.get_close_matches(home_name, nombres, n=1, cutoff=0.50)
    if not match:
        return cuotas
    evento = next((ev for ev in eventos_odds if ev.get("home_team") == match[0]), None)
    if not evento:
        return cuotas

    for bookie in evento.get("bookmakers", []):
        es_pin = (bookie.get("key") == "pinnacle")
        for market in bookie.get("markets", []):
            mkey = market.get("key")
            if mkey == "h2h":
                for out in market.get("outcomes", []):
                    name, price = out.get("name"), out.get("price", 0)
                    if name == evento["home_team"]:
                        if es_pin: cuotas["pinnacle_home"] = price
                        cuotas["home"] = max(cuotas["home"], price)
                    elif name == evento["away_team"]:
                        if es_pin: cuotas["pinnacle_away"] = price
                        cuotas["away"] = max(cuotas["away"], price)
                    elif name.lower() == "draw":
                        if es_pin: cuotas["pinnacle_draw"] = price
                        cuotas["draw"] = max(cuotas["draw"], price)
            elif mkey == "totals":
                for out in market.get("outcomes", []):
                    if out.get("point") == 2.5:
                        price = out.get("price", 0)
                        if out.get("name") == "Over": cuotas["over25"] = max(cuotas["over25"], price)
                        elif out.get("name") == "Under": cuotas["under25"] = max(cuotas["under25"], price)
    return cuotas


# ==========================================
# 4. FLUJO PRINCIPAL
# ==========================================
def main():
    ahora_utc = datetime.now(timezone.utc)
    hoy = ahora_utc.strftime("%Y-%m-%d")
    print(f"Iniciando recolección (ESPN + Football-Data + Odds + Open-Meteo) para 48h desde {hoy}")

    elo_map = obtener_tabla_elo(hoy)
    partidos_salida = []

    for cfg in LIGAS_OBJETIVO:
        if len(partidos_salida) >= MAX_PARTIDOS_DIA:
            break

        proximos = obtener_partidos_proximos_espn(cfg["espn"], ahora_utc)
        if not proximos:
            continue

        print(f"\n[{cfg['nombre']}] {len(proximos)} partidos encontrados en próximas 48h.")
        cuotas_liga = obtener_cuotas_liga(cfg["odds_key"])
        fd_hist = obtener_historial_football_data(cfg["fd_code"])
        espn_tablas = obtener_standings_espn(cfg["espn"])
        espn_recientes = obtener_recientes_espn(cfg["espn"], ahora_utc)

        for p in proximos:
            if len(partidos_salida) >= MAX_PARTIDOS_DIA:
                break

            home_name, away_name = p["homeTeam"], p["awayTeam"]
            fecha_iso, ciudad = p["date"], p["venueCity"]
            print(f"  Procesando: {home_name} vs {away_name}...")

            hist_h = buscar_equipo(home_name, fd_hist)
            hist_a = buscar_equipo(away_name, fd_hist)

            if hist_h:
                stats_h = calcular_desde_historial(hist_h, True, fecha_iso, cfg["avg_h"], cfg["avg_a"])
            else:
                stats_h = construir_metricas_espn(home_name, True, espn_tablas, espn_recientes, fecha_iso, cfg["avg_h"], cfg["avg_a"])

            if hist_a:
                stats_a = calcular_desde_historial(hist_a, False, fecha_iso, cfg["avg_h"], cfg["avg_a"])
            else:
                stats_a = construir_metricas_espn(away_name, False, espn_tablas, espn_recientes, fecha_iso, cfg["avg_h"], cfg["avg_a"])

            cuotas = extraer_cuotas_partido(home_name, away_name, cuotas_liga)
            weather_factor = obtener_factor_clima(ciudad, fecha_iso)

            elo_h = buscar_elo(home_name, elo_map)
            elo_a = buscar_elo(away_name, elo_map)
            if elo_h is None or elo_a is None:
                elo_h, elo_a = stats_h["pseudo_elo"], stats_a["pseudo_elo"]

            partidos_salida.append({
                "id": p["id"],
                "league": cfg["nombre"],
                "date": fecha_iso,
                "homeTeam": home_name,
                "awayTeam": away_name,
                "stats": {
                    "homeGoalsFor": stats_h["gf"],
                    "homeGoalsAgainst": stats_h["ga"],
                    "homeXG": stats_h["gf"],
                    "homeXGA": stats_h["ga"],
                    "awayGoalsFor": stats_a["gf"],
                    "awayGoalsAgainst": stats_a["ga"],
                    "awayXG": stats_a["gf"],
                    "awayXGA": stats_a["ga"],
                    "leagueAvgHome": cfg["avg_h"],
                    "leagueAvgAway": cfg["avg_a"],
                },
                "context": {
                    "homeElo": elo_h,
                    "awayElo": elo_a,
                    "homeForm": stats_h["form_str"],
                    "awayForm": stats_a["form_str"],
                    "homeFormScore": stats_h["form_score"],
                    "awayFormScore": stats_a["form_score"],
                    "homeRestDays": stats_h["rest_days"],
                    "awayRestDays": stats_a["rest_days"],
                    "homeInjuryImpact": 0.0,
                    "awayInjuryImpact": 0.0,
                    "homeInjuriesList": [],
                    "awayInjuriesList": [],
                    "weatherFactor": weather_factor,
                },
                "odds": cuotas,
            })

    salida = {
        "updatedAt": ahora_utc.isoformat(),
        "date": hoy,
        "matches": partidos_salida,
    }

    with open("partidos_hoy.json", "w", encoding="utf-8") as fp:
        json.dump(salida, fp, ensure_ascii=False, indent=2)

    print(f"\nListo. Guardados {len(partidos_salida)} partidos en partidos_hoy.json")


if __name__ == "__main__":
    main()
