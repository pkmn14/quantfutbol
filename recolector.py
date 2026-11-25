import os
import json
import csv
import io
import difflib
from datetime import datetime, timezone
import requests

# ==========================================
# CONFIGURACIÓN PRINCIPAL
# ==========================================
APIFOOTBALL_KEY = os.environ.get("APIFOOTBALL_KEY", "")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

SEASON = 2026          # Año de la temporada actual
MAX_PARTIDOS_DIA = 15  # Mantiene el consumo seguro (< 60 req/día en plan gratis)

# Promedios históricos reales (avg_home / avg_away) por competición
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

HEADERS_FOOTBALL = {
    "x-apisports-key": APIFOOTBALL_KEY,
}

# Caché en memoria para no repetir consultas de coordenadas por ciudad
CACHE_CIUDADES = {}


# ==========================================
# 1. OPEN-METEO API (CLIMA EXACTO AL PITAZO)
# ==========================================
def obtener_coordenadas_ciudad(ciudad):
    """Convierte el nombre de la ciudad del estadio en latitud/longitud gratis."""
    if not ciudad:
        return None
    # Limpia formatos como "Avellaneda, Provincia de Buenos Aires" -> "Avellaneda"
    ciudad_limpia = ciudad.split(",")[0].strip()
    if ciudad_limpia in CACHE_CIUDADES:
        return CACHE_CIUDADES[ciudad_limpia]

    url = "https://geocoding-api.open-meteo.com/v1/search"
    try:
        r = requests.get(url, params={"name": ciudad_limpia, "count": 1, "language": "es"}, timeout=10)
        results = r.json().get("results", [])
        if results:
            coords = (results[0]["latitude"], results[0]["longitude"])
            CACHE_CIUDADES[ciudad_limpia] = coords
            return coords
    except Exception as e:
        print(f"  [Aviso Clima] No se geocodificó '{ciudad_limpia}': {e}")

    CACHE_CIUDADES[ciudad_limpia] = None
    return None


def obtener_clima_partido(ciudad, fecha_iso):
    """Consulta lluvia, viento y temperatura a la hora exacta del partido."""
    default = (1.0, "Sin datos de clima")
    coords = obtener_coordenadas_ciudad(ciudad)
    if not coords or not fecha_iso:
        return default

    try:
        dt_utc = datetime.fromisoformat(fecha_iso.replace("Z", "+00:00")).astimezone(timezone.utc)
        hora_objetivo = dt_utc.strftime("%Y-%m-%dT%H:00")
        fecha_dia = dt_utc.strftime("%Y-%m-%d")

        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            "latitude": coords[0],
            "longitude": coords[1],
            "hourly": "temperature_2m,precipitation,wind_speed_10m",
            "timezone": "UTC",
            "start_date": fecha_dia,
            "end_date": fecha_dia,
        }
        r = requests.get(url, params=params, timeout=10)
        hourly = r.json().get("hourly", {})
        tiempos = hourly.get("time", [])

        if hora_objetivo in tiempos:
            idx = tiempos.index(hora_objetivo)
            temp = hourly["temperature_2m"][idx]
            lluvia = hourly["precipitation"][idx]
            viento = hourly["wind_speed_10m"][idx]

            # Cálculo automático del multiplicador de goles (weatherFactor)
            factor = 1.0
            if lluvia >= 4.0:
                factor -= 0.08  # Lluvia fuerte frena el balón y baja goles esperados
            elif lluvia >= 1.0:
                factor -= 0.04  # Lluvia moderada

            if viento >= 35.0:
                factor -= 0.06  # Viento fuerte complica centros y tiros largos
            elif viento >= 22.0:
                factor -= 0.03

            if temp >= 33.0 or temp <= 2.0:
                factor -= 0.03  # Calor extremo o frío intenso baja el ritmo físico

            factor = round(max(0.84, factor), 2)
            resumen = f"{temp}°C | Lluvia {lluvia}mm | Viento {viento} km/h"
            return factor, resumen
    except Exception as e:
        print(f"  [Aviso Clima] Error Open-Meteo en '{ciudad}': {e}")

    return default


# ==========================================
# 2. CLUB ELO (EUROPA - 100% GRATIS)
# ==========================================
def obtener_tabla_elo(fecha_str):
    url = f"http://api.clubelo.com/{fecha_str}"
    elo_map = {}
    try:
        r = requests.get(url, timeout=15)
        if r.status_code == 200:
            reader = csv.DictReader(io.StringIO(r.text))
            for row in reader:
                if row.get("Club") and row.get("Elo"):
                    elo_map[row["Club"].strip()] = round(float(row["Elo"]))
    except Exception as e:
        print(f"[Aviso] No se pudo obtener ClubElo: {e}")
    return elo_map


def buscar_elo(nombre_equipo, elo_map):
    if not elo_map:
        return 1550
    coincidencias = difflib.get_close_matches(nombre_equipo, elo_map.keys(), n=1, cutoff=0.6)
    return elo_map[coincidencias[0]] if coincidencias else 1550


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
            restantes = r.headers.get("x-requests-remaining", "?")
            print(f"  [Odds API] {odds_key} OK (Créditos restantes: {restantes})")
            return r.json()
        print(f"  [Aviso Odds API] {odds_key} -> Status {r.status_code}")
    except Exception as e:
        print(f"  [Aviso Odds API] Error en {odds_key}: {e}")
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

    nombres_odds = [ev.get("home_team", "") for ev in eventos_odds]
    match = difflib.get_close_matches(home_name, nombres_odds, n=1, cutoff=0.50)
    if not match:
        return cuotas

    evento = next((ev for ev in eventos_odds if ev.get("home_team") == match[0]), None)
    if not evento:
        return cuotas

    for bookie in evento.get("bookmakers", []):
        es_pinnacle = (bookie.get("key") == "pinnacle")
        for market in bookie.get("markets", []):
            mkey = market.get("key")
            if mkey == "h2h":
                for out in market.get("outcomes", []):
                    name, price = out.get("name"), out.get("price", 0)
                    if name == evento["home_team"]:
                        if es_pinnacle:
                            cuotas["pinnacle_home"] = price
                        cuotas["home"] = max(cuotas["home"], price)
                    elif name == evento["away_team"]:
                        if es_pinnacle:
                            cuotas["pinnacle_away"] = price
                        cuotas["away"] = max(cuotas["away"], price)
                    elif name.lower() == "draw":
                        if es_pinnacle:
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
# 4. API-SPORTS (ESTADÍSTICAS Y BAJAS)
# ==========================================
def obtener_estadisticas_equipo(league_id, team_id, avg_h, avg_a):
    url = "https://v3.football.api-sports.io/teams/statistics"
    params = {"league": league_id, "season": SEASON, "team": team_id}
    try:
        r = requests.get(url, headers=HEADERS_FOOTBALL, params=params, timeout=15)
        data = r.json().get("response", {})
        goals_for = data.get("goals", {}).get("for", {}).get("average", {})
        goals_ag = data.get("goals", {}).get("against", {}).get("average", {})
        form_str = (data.get("form") or "")[-5:]

        puntos = sum(3 if c == "W" else 1 if c == "D" else 0 for c in form_str)
        forma_pct = round(puntos / (len(form_str) * 3), 2) if form_str else 0.5

        return {
            "gf_home": float(goals_for.get("home") or avg_h),
            "ga_home": float(goals_ag.get("home") or avg_a),
            "gf_away": float(goals_for.get("away") or avg_a),
            "ga_away": float(goals_ag.get("away") or avg_h),
            "form_str": form_str or "DDDDD",
            "form_score": forma_pct,
        }
    except Exception as e:
        print(f"[Aviso] Error estadísticas equipo {team_id}: {e}")
        return {
            "gf_home": avg_h, "ga_home": avg_a,
            "gf_away": avg_a, "ga_away": avg_h,
            "form_str": "DDDDD", "form_score": 0.5,
        }


def obtener_impacto_bajas(fixture_id, home_id, away_id):
    url = "https://v3.football.api-sports.io/injuries"
    params = {"fixture": fixture_id}
    bajas_home, bajas_away = [], []
    try:
        r = requests.get(url, headers=HEADERS_FOOTBALL, params=params, timeout=15)
        for item in r.json().get("response", []):
            t_id = item.get("team", {}).get("id")
            jugador = item.get("player", {}).get("name", "Desconocido")
            motivo = item.get("player", {}).get("reason", "Lesión")
            if t_id == home_id:
                bajas_home.append(f"{jugador} ({motivo})")
            elif t_id == away_id:
                bajas_away.append(f"{jugador} ({motivo})")
    except Exception as e:
        print(f"[Aviso] Error bajas fixture {fixture_id}: {e}")

    pen_home = min(len(bajas_home) * 2.5, 15.0)
    pen_away = min(len(bajas_away) * 2.5, 15.0)
    return pen_home, pen_away, bajas_home, bajas_away


# ==========================================
# 5. FLUJO PRINCIPAL
# ==========================================
def main():
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"Iniciando recolección para: {hoy}")

    elo_map = obtener_tabla_elo(hoy)

    url_fix = "https://v3.football.api-sports.io/fixtures"
    r_fix = requests.get(url_fix, headers=HEADERS_FOOTBALL, params={"date": hoy}, timeout=20)
    resp_json = r_fix.json()
    if resp_json.get("errors"):
        print(f"[Error API-Football] {resp_json['errors']}")

    fixtures = resp_json.get("response", [])
    filtrados = [
        f for f in fixtures
        if f["league"]["id"] in LIGAS_OBJETIVO
        and f["fixture"]["status"]["short"] in ("NS", "TBD")
    ][:MAX_PARTIDOS_DIA]

    print(f"Total partidos hoy: {len(fixtures)} | En ligas objetivo: {len(filtrados)}")

    cuotas_por_liga = {}
    for lid in set(f["league"]["id"] for f in filtrados):
        cuotas_por_liga[lid] = obtener_cuotas_liga(LIGAS_OBJETIVO[lid]["odds_key"])

    partidos_salida = []
    for f in filtrados:
        fid = f["fixture"]["id"]
        lid = f["league"]["id"]
        cfg = LIGAS_OBJETIVO[lid]
        home = f["teams"]["home"]
        away = f["teams"]["away"]
        ciudad = f["fixture"].get("venue", {}).get("city", "")
        fecha_iso = f["fixture"]["date"]

        print(f"Procesando: {home['name']} vs {away['name']} ({cfg['nombre']} - {ciudad})...")

        stats_h = obtener_estadisticas_equipo(lid, home["id"], cfg["avg_h"], cfg["avg_a"])
        stats_a = obtener_estadisticas_equipo(lid, away["id"], cfg["avg_h"], cfg["avg_a"])
        pen_h, pen_a, bajas_h, bajas_a = obtener_impacto_bajas(fid, home["id"], away["id"])
        cuotas = extraer_cuotas_partido(home["name"], away["name"], cuotas_por_liga.get(lid, []))
        weather_factor, weather_summary = obtener_clima_partido(ciudad, fecha_iso)

        partidos_salida.append({
            "id": str(fid),
            "league": cfg["nombre"],
            "date": fecha_iso,
            "venueCity": ciudad,
            "homeTeam": home["name"],
            "awayTeam": away["name"],
            "stats": {
                "homeGoalsFor": stats_h["gf_home"],
                "homeGoalsAgainst": stats_h["ga_home"],
                "homeXG": stats_h["gf_home"],
                "homeXGA": stats_h["ga_home"],
                "awayGoalsFor": stats_a["gf_away"],
                "awayGoalsAgainst": stats_a["ga_away"],
                "awayXG": stats_a["gf_away"],
                "awayXGA": stats_a["ga_away"],
                "leagueAvgHome": cfg["avg_h"],
                "leagueAvgAway": cfg["avg_a"],
            },
            "context": {
                "homeElo": buscar_elo(home["name"], elo_map),
                "awayElo": buscar_elo(away["name"], elo_map),
                "homeForm": stats_h["form_str"],
                "awayForm": stats_a["form_str"],
                "homeFormScore": stats_h["form_score"],
                "awayFormScore": stats_a["form_score"],
                "homeRestDays": 5,
                "awayRestDays": 5,
                "homeInjuryImpact": pen_h,
                "awayInjuryImpact": pen_a,
                "homeInjuriesList": bajas_h,
                "awayInjuriesList": bajas_a,
                "weatherFactor": weather_factor,
                "weatherSummary": weather_summary,
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
