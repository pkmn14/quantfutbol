import os
import time
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

MAX_PARTIDOS_DIA = 15  # Consumo seguro en plan gratis (< 60 req/día)

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
    for intento in range(2):
        try:
            r = requests.get(url, headers=HEADERS_FOOTBALL, params=params, timeout=20)
            data = r.json()
            errores = data.get("errors", {})

            # Si golpea el límite por minuto, espera 62s y reintenta una vez
            if errores and "rateLimit" in str(errores):
                print("  [Pausa] Límite de 10 req/min alcanzado. Esperando 62s...")
                time.sleep(62)
                continue

            if errores:
                print(f"  [Error API-Football {endpoint}] {errores}")
                return []

            # Pausa preventiva si queda 1 sola petición en el minuto actual
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
# 2. CLUB ELO (CON FALLBACK PARA SUDAMÉRICA)
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
# 4. ESTADÍSTICAS REALES DESDE ÚLTIMOS 10 PARTIDOS
# ==========================================
def analizar_ultimos_partidos(team_id, es_local_hoy, fecha_hoy_iso, avg_h, avg_a):
    """Calcula goles ponderados (Time-Decay), forma real, días de descanso y Pseudo-Elo."""
    partidos = peticion_football("fixtures", {"team": team_id, "last": 10})
    terminados = [
        p for p in partidos
        if p.get("fixture", {}).get("status", {}).get("short") in ("FT", "AET", "PEN")
        and p.get("goals", {}).get("home") is not None
    ]

    # Ordenar del más reciente al más antiguo
    terminados.sort(key=lambda x: x["fixture"]["date"], reverse=True)

    base_gf = avg_h if es_local_hoy else avg_a
    base_ga = avg_a if es_local_hoy else avg_h

    if not terminados:
        return {
            "gf": base_gf, "ga": base_ga,
            "form_str": "DDDDD", "form_score": 0.5,
            "rest_days": 5, "pseudo_elo": 1550,
        }

    # 1. Días reales de descanso
    try:
        dt_hoy = datetime.fromisoformat(fecha_hoy_iso.replace("Z", "+00:00"))
        dt_ult = datetime.fromisoformat(terminados[0]["fixture"]["date"].replace("Z", "+00:00"))
        rest_days = max(1, min(14, (dt_hoy - dt_ult).days))
    except Exception:
        rest_days = 5

    # 2. Forma real (últimos 5) + Goles ponderados por tiempo + Pseudo-Elo
    gf_pond, ga_pond, peso_total = 0.0, 0.0, 0.0
    gf_sede, ga_sede, peso_sede = 0.0, 0.0, 0.0
    letras_forma = []
    puntos_10 = 0
    dif_gol_10 = 0

    for i, p in enumerate(terminados):
        peso = 0.90 ** i  # Decaimiento temporal: el último partido pesa 1.0, el 10° pesa 0.39
        fue_local = (p["teams"]["home"]["id"] == team_id)
        g_favor = p["goals"]["home"] if fue_local else p["goals"]["away"]
        g_contra = p["goals"]["away"] if fue_local else p["goals"]["home"]

        gf_pond += g_favor * peso
        ga_pond += g_contra * peso
        peso_total += peso

        # Separa rendimiento específico en la misma condición (local o visitante)
        if fue_local == es_local_hoy:
            gf_sede += g_favor * peso
            ga_sede += g_contra * peso
            peso_sede += peso

        dif_gol_10 += (g_favor - g_contra)
        if g_favor > g_contra:
            puntos_10 += 3
            if i < 5:
                letras_forma.append("W")
        elif g_favor == g_contra:
            puntos_10 += 1
            if i < 5:
                letras_forma.append("D")
        else:
            if i < 5:
                letras_forma.append("L")

    # Regresión ligera hacia el promedio de la liga (evita extremos por pocos partidos)
    gf_gen = (gf_pond + 1.5 * base_gf) / (peso_total + 1.5)
    ga_gen = (ga_pond + 1.5 * base_ga) / (peso_total + 1.5)

    if peso_sede >= 1.5:
        gf_esp = (gf_sede + 1.5 * base_gf) / (peso_sede + 1.5)
        ga_esp = (ga_sede + 1.5 * base_ga) / (peso_sede + 1.5)
        gf_final = round(0.60 * gf_esp + 0.40 * gf_gen, 2)
        ga_final = round(0.60 * ga_esp + 0.40 * ga_gen, 2)
    else:
        gf_final = round(gf_gen, 2)
        ga_final = round(ga_gen, 2)

    # Forma (más reciente a la derecha, formato estándar ej. "WLDWW")
    form_str = "".join(reversed(letras_forma)) or "DDDDD"
    pts_5 = sum(3 if c == "W" else 1 if c == "D" else 0 for c in form_str)
    form_score = round(pts_5 / (len(form_str) * 3), 2)

    # Pseudo-Elo para ligas sin ClubElo (rango aprox. 1380 - 1720)
    ppg = puntos_10 / len(terminados)
    pseudo_elo = round(1400 + (ppg * 95) + (dif_gol_10 * 4))

    return {
        "gf": gf_final,
        "ga": ga_final,
        "form_str": form_str,
        "form_score": form_score,
        "rest_days": rest_days,
        "pseudo_elo": pseudo_elo,
    }


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

        print(f"Procesando: {home['name']} vs {away['name']} ({cfg['nombre']})...")

        stats_h = analizar_ultimos_partidos(home["id"], True, fecha_iso, cfg["avg_h"], cfg["avg_a"])
        stats_a = analizar_ultimos_partidos(away["id"], False, fecha_iso, cfg["avg_h"], cfg["avg_a"])
        pen_h, pen_a, bajas_h, bajas_a = obtener_impacto_bajas(fid, home["id"], away["id"])
        cuotas = extraer_cuotas_partido(home["name"], away["name"], cuotas_por_liga.get(lid, []))
        weather_factor = obtener_factor_clima(ciudad, fecha_iso)

        # Usa ClubElo si existe para ambos; si no (Sudamérica), usa Pseudo-Elo
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
                "homeRestDays": stats_h["rest_days"],
                "awayRestDays": stats_a["rest_days"],
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
