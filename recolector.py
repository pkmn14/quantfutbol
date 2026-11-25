import os
import time
import json
import csv
import io
import difflib
from datetime import datetime, timezone, timedelta
import requests

# ==========================================
# CONFIGURACIÓN PRINCIPAL
# ==========================================
APIFOOTBALL_KEY = os.environ.get("APIFOOTBALL_KEY", "")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

MAX_PARTIDOS_DIA = 15

LIGAS_OBJETIVO = {
    # --- SUDAMÉRICA ---
    128: {"nombre": "Liga Profesional Argentina", "odds_key": "soccer_argentina_primera_division", "avg_h": 1.28, "avg_a": 0.94},
    13:  {"nombre": "Copa Libertadores",          "odds_key": "soccer_conmebol_copa_libertadores", "avg_h": 1.50, "avg_a": 0.95},
    11:  {"nombre": "Copa Sudamericana",          "odds_key": "soccer_conmebol_copa_sudamericana", "avg_h": 1.44, "avg_a": 0.96},
    71:  {"nombre": "Brasileirão Serie A",        "odds_key": "soccer_brazil_campeonato",          "avg_h": 1.48, "avg_a": 1.02},
    # --- EUROPA ---
    39:  {"nombre": "Premier League",             "odds_key": "soccer_epl",                        "avg_h": 1.62, "avg_a": 1.32},
    140: {"nombre": "LaLiga",                     "odds_key": "soccer_spain_la_liga",              "avg_h": 1.48, "avg_a": 1.12},
    135: {"nombre": "Serie A",                    "odds_key": "soccer_italy_serie_a",              "avg_h": 1.45, "avg_a": 1.15},
    78:  {"nombre": "Bundesliga",                 "odds_key": "soccer_germany_bundesliga",         "avg_h": 1.75, "avg_a": 1.38},
    61:  {"nombre": "Ligue 1",                    "odds_key": "soccer_france_ligue_one",           "avg_h": 1.52, "avg_a": 1.18},
    2:   {"nombre": "Champions League",           "odds_key": "soccer_uefa_champs_league",         "avg_h": 1.68, "avg_a": 1.30},
}

HEADERS_FOOTBALL = {"x-apisports-key": APIFOOTBALL_KEY}
CACHE_CIUDADES = {}


# ==========================================
# HELPER API-FOOTBALL (RESPETA 10 REQ/MIN)
# ==========================================
def peticion_football(endpoint, params):
    url = f"https://v3.football.api-sports.io/{endpoint}"
    for _ in range(2):
        try:
            r = requests.get(url, headers=HEADERS_FOOTBALL, params=params, timeout=20)
            data = r.json()
            errores = data.get("errors", {})

            if errores and "rateLimit" in str(errores):
                print("  [Pausa] Límite de 10 req/min alcanzado. Esperando 62s...")
                time.sleep(62)
                continue

            if errores:
                print(f"  [Error API-Football {endpoint}] {errores}")
                return []

            restantes_min = r.headers.get("x-ratelimit-requests-remaining")
            if restantes_min is not None and int(restantes_min) <= 1:
                time.sleep(15)

            return data.get("response", [])
        except Exception as e:
            print(f"  [Aviso] Fallo en {endpoint}: {e}")
    return []


# ==========================================
# 1. OPEN-METEO (FACTOR MATEMÁTICO SILENCIOSO)
# ==========================================
def obtener_factor_clima(ciudad, fecha_iso):
    if not ciudad or not fecha_iso:
        return 1.0
    ciudad_limpia = ciudad.split(",")[0].strip()

    if ciudad_limpia not in CACHE_CIUDADES:
        try:
            r = requests.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": ciudad_limpia, "count": 1},
                timeout=10,
            )
            res = r.json().get("results", [])
            CACHE_CIUDADES[ciudad_limpia] = (res[0]["latitude"], res[0]["longitude"]) if res else None
        except Exception:
            CACHE_CIUDADES[ciudad_limpia] = None

    coords = CACHE_CIUDADES[ciudad_limpia]
    if not coords:
        return 1.0

    try:
        dt_utc = datetime.fromisoformat(fecha_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
        hora_obj = dt_utc.strftime("%Y-%m-%dT%H:00")
        dia = dt_utc.strftime("%Y-%m-%d")

        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": coords[0],
                "longitude": coords[1],
                "hourly": "temperature_2m,precipitation,wind_speed_10m",
                "timezone": "UTC",
                "start_date": dia,
                "end_date": dia,
            },
            timeout=10,
        )
        hourly = r.json().get("hourly", {})
        tiempos = hourly.get("time", [])
        if hora_obj in tiempos:
            idx = tiempos.index(hora_obj)
            temp = hourly["temperature_2m"][idx]
            lluvia = hourly["precipitation"][idx]
            viento = hourly["wind_speed_10m"][idx]

            factor = 1.0
            if lluvia >= 4.0:
                factor -= 0.08
            elif lluvia >= 1.0:
                factor -= 0.04
            if viento >= 35.0:
                factor -= 0.06
            elif viento >= 22.0:
                factor -= 0.03
            if temp >= 33.0 or temp <= 2.0:
                factor -= 0.03
            return round(max(0.84, factor), 2)
    except Exception:
        pass
    return 1.0


# ==========================================
# 2. CLUB ELO (EUROPA)
# ==========================================
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


# ==========================================
# 3. THE ODDS API (CUOTAS EN VIVO)
# ==========================================
def obtener_cuotas_liga(odds_key):
    url = f"https://api.the-odds-api.com/v4/sports/{odds_key}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "eu,uk",
        "markets": "h2h,totals",
        "oddsFormat": "decimal",
    }
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
                        if es_pin:
                            cuotas["pinnacle_home"] = price
                        cuotas["home"] = max(cuotas["home"], price)
                    elif name == evento["away_team"]:
                        if es_pin:
                            cuotas["pinnacle_away"] = price
                        cuotas["away"] = max(cuotas["away"], price)
                    elif name.lower() == "draw":
                        if es_pin:
                            cuotas["pinnacle_draw"] = price
                        cuotas["draw"] = max(cuotas["draw"], price)
            elif mkey == "totals":
                for out in market.get("outcomes", []):
                    if out.get("point") == 2.5:
                        price = out.get("price", 0)
                        if out.get("name") == "Over":
                            cuotas["over25"] = max(cuotas["over25"], price)
                        elif out.get("name") == "Under":
                            cuotas["under25"] = max(cuotas["under25"], price)
    return cuotas


# ==========================================
# 4. TABLA DE POSICIONES (1 CALL POR LIGA)
# ==========================================
def obtener_tabla_liga(league_id, season):
    """Extrae goles local/visita, racha real y Pseudo-Elo de todos los equipos de la liga."""
    resp = peticion_football("standings", {"league": league_id, "season": season})
    tabla = {}
    if not resp:
        return tabla

    grupos = resp[0].get("league", {}).get("standings", [])
    for grupo in grupos:
        for fila in grupo:
            tid = fila.get("team", {}).get("id")
            if not tid:
                continue

            pj_all = fila.get("all", {}).get("played") or 0
            pts = fila.get("points") or 0
            gd = fila.get("goalsDiff") or 0
            form_raw = (fila.get("form") or "")[-5:]

            h_data = fila.get("home", {})
            a_data = fila.get("away", {})

            tabla[tid] = {
                "h_pj": h_data.get("played") or 0,
                "h_gf": h_data.get("goals", {}).get("for") or 0,
                "h_ga": h_data.get("goals", {}).get("against") or 0,
                "a_pj": a_data.get("played") or 0,
                "a_gf": a_data.get("goals", {}).get("for") or 0,
                "a_ga": a_data.get("goals", {}).get("against") or 0,
                "form": form_raw,
                "ppg": pts / max(1, pj_all),
                "gd_pg": gd / max(1, pj_all),
            }
    print(f"  [Standings] Liga {league_id} (Temp {season}): {len(tabla)} equipos cargados.")
    return tabla


def extraer_metricas_equipo(team_id, es_local, tabla_liga, avg_h, avg_a):
    base_gf = avg_h if es_local else avg_a
    base_ga = avg_a if es_local else avg_h
    info = tabla_liga.get(team_id)

    if not info:
        return {
            "gf": base_gf, "ga": base_ga,
            "form_str": "DDDDD", "form_score": 0.5,
            "pseudo_elo": 1550,
        }

    pj = info["h_pj"] if es_local else info["a_pj"]
    gf_tot = info["h_gf"] if es_local else info["a_gf"]
    ga_tot = info["h_ga"] if es_local else info["a_ga"]

    # Suavizado bayesiano (evita extremos si llevan pocos partidos)
    gf_final = round((gf_tot + 2.0 * base_gf) / (pj + 2.0), 2)
    ga_final = round((ga_tot + 2.0 * base_ga) / (pj + 2.0), 2)

    form_str = info["form"] or "DDDDD"
    pts_5 = sum(3 if c == "W" else 1 if c == "D" else 0 for c in form_str)
    form_score = round(pts_5 / (len(form_str) * 3), 2)

    # Pseudo-Elo basado en rendimiento real de temporada (aprox. 1400 - 1720)
    pseudo_elo = round(1380 + (info["ppg"] * 115) + (info["gd_pg"] * 45))

    return {
        "gf": gf_final,
        "ga": ga_final,
        "form_str": form_str,
        "form_score": form_score,
        "pseudo_elo": pseudo_elo,
    }


def obtener_dias_descanso(team_id, season, fecha_partido_iso):
    """Consulta partidos recientes por rango de fechas (sin usar parámetro pago 'last')."""
    try:
        dt_partido = datetime.fromisoformat(fecha_partido_iso.replace("Z", "+00:00"))
        desde = (dt_partido - timedelta(days=21)).strftime("%Y-%m-%d")
        hasta = (dt_partido - timedelta(days=1)).strftime("%Y-%m-%d")

        recientes = peticion_football("fixtures", {
            "team": team_id,
            "season": season,
            "from": desde,
            "to": hasta,
        })
        terminados = [
            p for p in recientes
            if p.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN")
        ]
        if terminados:
            terminados.sort(key=lambda x: x["fixture"]["date"], reverse=True)
            dt_ult = datetime.fromisoformat(terminados[0]["fixture"]["date"].replace("Z", "+00:00"))
            return max(1, min(14, (dt_partido - dt_ult).days))
    except Exception:
        pass
    return 5


def obtener_impacto_bajas(fixture_id, home_id, away_id):
    items = peticion_football("injuries", {"fixture": fixture_id})
    bajas_home, bajas_away = [], []
    for item in items:
        t_id = item.get("team", {}).get("id")
        jugador = item.get("player", {}).get("name", "Desconocido")
        motivo = item.get("player", {}).get("reason", "Lesión")
        if t_id == home_id:
            bajas_home.append(f"{jugador} ({motivo})")
        elif t_id == away_id:
            bajas_away.append(f"{jugador} ({motivo})")

    return min(len(bajas_home) * 2.5, 15.0), min(len(bajas_away) * 2.5, 15.0), bajas_home, bajas_away


# ==========================================
# 5. FLUJO PRINCIPAL
# ==========================================
def main():
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"Iniciando recolección para: {hoy}")

    elo_map = obtener_tabla_elo(hoy)
    fixtures = peticion_football("fixtures", {"date": hoy})

    filtrados = [
        f for f in fixtures
        if f["league"]["id"] in LIGAS_OBJETIVO
        and f["fixture"]["status"]["short"] in ("NS", "TBD")
    ][:MAX_PARTIDOS_DIA]

    print(f"Total partidos hoy: {len(fixtures)} | En ligas objetivo: {len(filtrados)}")

    # Cachear cuotas y tablas de posiciones por (league_id, season)
    cuotas_por_liga = {}
    tablas_por_liga = {}
    for f in filtrados:
        lid = f["league"]["id"]
        season = f["league"]["season"]
        if lid not in cuotas_por_liga:
            cuotas_por_liga[lid] = obtener_cuotas_liga(LIGAS_OBJETIVO[lid]["odds_key"])
        if (lid, season) not in tablas_por_liga:
            tablas_por_liga[(lid, season)] = obtener_tabla_liga(lid, season)

    partidos_salida = []
    for f in filtrados:
        fid = f["fixture"]["id"]
        lid = f["league"]["id"]
        season = f["league"]["season"]
        cfg = LIGAS_OBJETIVO[lid]
        home = f["teams"]["home"]
        away = f["teams"]["away"]
        ciudad = f["fixture"].get("venue", {}).get("city", "")
        fecha_iso = f["fixture"]["date"]

        print(f"Procesando: {home['name']} vs {away['name']} ({cfg['nombre']} {season})...")

        tabla = tablas_por_liga.get((lid, season), {})
        stats_h = extraer_metricas_equipo(home["id"], True, tabla, cfg["avg_h"], cfg["avg_a"])
        stats_a = extraer_metricas_equipo(away["id"], False, tabla, cfg["avg_h"], cfg["avg_a"])

        rest_h = obtener_dias_descanso(home["id"], season, fecha_iso)
        rest_a = obtener_dias_descanso(away["id"], season, fecha_iso)
        pen_h, pen_a, bajas_h, bajas_a = obtener_impacto_bajas(fid, home["id"], away["id"])
        cuotas = extraer_cuotas_partido(home["name"], away["name"], cuotas_por_liga.get(lid, []))
        weather_factor = obtener_factor_clima(ciudad, fecha_iso)

        elo_h = buscar_elo(home["name"], elo_map)
        elo_a = buscar_elo(away["name"], elo_map)
        if elo_h is None or elo_a is None:
            elo_h, elo_a = stats_h["pseudo_elo"], stats_a["pseudo_elo"]

        partidos_salida.append({
            "id": str(fid),
            "league": cfg["nombre"],
            "date": fecha_iso,
            "homeTeam": home["name"],
            "awayTeam": away["name"],
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
                "homeRestDays": rest_h,
                "awayRestDays": rest_a,
                "homeInjuryImpact": pen_h,
                "awayInjuryImpact": pen_a,
                "homeInjuriesList": bajas_h,
                "awayInjuriesList": bajas_a,
                "weatherFactor": weather_factor,
            },
            "odds": cuotas,
        })

    salida = {
        "updatedAt": datetime.now(timezone.utc).isoformat(),
        "date": hoy,
        "matches": partidos_salida,
    }

    with open("partidos_hoy.json", "w", encoding="utf-8") as fp:
        json.dump(salida, fp, ensure_ascii=False, indent=2)

    print(f"Listo. Guardados {len(partidos_salida)} partidos en partidos_hoy.json")


if __name__ == "__main__":
    main()
