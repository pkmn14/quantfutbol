import os
import time
import json
import csv
import io
import difflib
import unicodedata
from datetime import datetime, timezone, timedelta
import requests

# ==========================================
# CONFIGURACIÓN PRINCIPAL
# ==========================================
APIFOOTBALL_KEY = os.environ.get("APIFOOTBALL_KEY", "")
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

MAX_PARTIDOS_DIA = 15

# Mapeo completo: API-Football ID -> Odds API -> ESPN Slug -> Promedios de liga
LIGAS_OBJETIVO = {
    # --- SUDAMÉRICA ---
    128: {"nombre": "Liga Profesional Argentina", "odds_key": "soccer_argentina_primera_division", "espn": "arg.1",                 "avg_h": 1.28, "avg_a": 0.94},
    13:  {"nombre": "Copa Libertadores",          "odds_key": "soccer_conmebol_copa_libertadores", "espn": "conmebol.libertadores", "avg_h": 1.50, "avg_a": 0.95},
    11:  {"nombre": "Copa Sudamericana",          "odds_key": "soccer_conmebol_copa_sudamericana", "espn": "conmebol.sudamericana", "avg_h": 1.44, "avg_a": 0.96},
    71:  {"nombre": "Brasileirão Serie A",        "odds_key": "soccer_brazil_campeonato",          "espn": "bra.1",                 "avg_h": 1.48, "avg_a": 1.02},
    # --- EUROPA ---
    39:  {"nombre": "Premier League",             "odds_key": "soccer_epl",                        "espn": "eng.1",                 "avg_h": 1.62, "avg_a": 1.32},
    140: {"nombre": "LaLiga",                     "odds_key": "soccer_spain_la_liga",              "espn": "esp.1",                 "avg_h": 1.48, "avg_a": 1.12},
    135: {"nombre": "Serie A",                    "odds_key": "soccer_italy_serie_a",              "espn": "ita.1",                 "avg_h": 1.45, "avg_a": 1.15},
    78:  {"nombre": "Bundesliga",                 "odds_key": "soccer_germany_bundesliga",         "espn": "ger.1",                 "avg_h": 1.75, "avg_a": 1.38},
    61:  {"nombre": "Ligue 1",                    "odds_key": "soccer_france_ligue_one",           "espn": "fra.1",                 "avg_h": 1.52, "avg_a": 1.18},
    2:   {"nombre": "Champions League",           "odds_key": "soccer_uefa_champs_league",         "espn": "uefa.champions",        "avg_h": 1.68, "avg_a": 1.30},
}

HEADERS_FOOTBALL = {"x-apisports-key": APIFOOTBALL_KEY}
CACHE_CIUDADES = {}


def normalizar(texto):
    """Elimina acentos, guiones y mayúsculas para emparejar nombres entre APIs."""
    t = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode("ascii")
    return t.lower().replace("-", " ").replace("fc", "").replace("cf", "").strip()


def buscar_equipo_en_diccionario(nombre_objetivo, diccionario):
    if not diccionario:
        return None
    objetivo_norm = normalizar(nombre_objetivo)
    mapa_norm = {normalizar(k): k for k in diccionario.keys()}

    if objetivo_norm in mapa_norm:
        return diccionario[mapa_norm[objetivo_norm]]

    # Coincidencia parcial (ej. "atletico mg" vs "atletico mineiro")
    for k_norm, k_orig in mapa_norm.items():
        if objetivo_norm in k_norm or k_norm in objetivo_norm:
            return diccionario[k_orig]

    match = difflib.get_close_matches(objetivo_norm, mapa_norm.keys(), n=1, cutoff=0.50)
    return diccionario[mapa_norm[match[0]]] if match else None


# ==========================================
# 1. ESPN PUBLIC API (SIN API KEY - TEMPORADA ACTUAL)
# ==========================================
def obtener_standings_espn(espn_slug):
    """Extrae tabla real 2026 (PJ, GF, GA, Puntos, DG) desde ESPN."""
    url = f"https://site.api.espn.com/apis/v2/sports/soccer/{espn_slug}/standings"
    equipos = {}
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return equipos
        data = r.json()

        # ESPN puede devolver grupos dentro de 'children' o directo en 'standings'
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

        print(f"  [ESPN Standings] {espn_slug}: {len(bloques)} equipos cargados.")
    except Exception as e:
        print(f"  [Aviso ESPN Standings] {espn_slug}: {e}")
    return equipos


def obtener_recientes_espn(espn_slug, fecha_hoy_dt):
    """Descarga partidos terminados de los últimos 25 días para sacar racha y descanso real."""
    desde = (fecha_hoy_dt - timedelta(days=25)).strftime("%Y%m%d")
    hasta = (fecha_hoy_dt - timedelta(days=1)).strftime("%Y%m%d")
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

                historial.setdefault(t_name, []).append({
                    "date": fecha_ev,
                    "res": res,
                    "gf": g_f,
                    "ga": g_c,
                })

        # Ordenar del más reciente al más antiguo
        for t in historial:
            historial[t].sort(key=lambda x: x["date"], reverse=True)

        print(f"  [ESPN Recientes] {espn_slug}: historial cargado para {len(historial)} equipos.")
    except Exception as e:
        print(f"  [Aviso ESPN Recientes] {espn_slug}: {e}")
    return historial


def construir_metricas_espn(nombre_equipo, es_local, tabla_espn, hist_espn, fecha_partido_iso, avg_h, avg_a):
    base_gf = avg_h if es_local else avg_a
    base_ga = avg_a if es_local else avg_h
    ratio_sede = (avg_h / avg_a) if es_local else (avg_a / avg_h)
    ajuste_sede_gf = ratio_sede ** 0.35
    ajuste_sede_ga = (1.0 / ratio_sede) ** 0.35

    info = buscar_equipo_en_diccionario(nombre_equipo, tabla_espn)
    partidos = buscar_equipo_en_diccionario(nombre_equipo, hist_espn) or []

    # 1. Goles ajustados por sede + suavizado bayesiano
    if info and info["pj"] > 0:
        gf_raw = info["gf_pg"] * ajuste_sede_gf
        ga_raw = info["ga_pg"] * ajuste_sede_ga
        gf_final = round((gf_raw * info["pj"] + 2.5 * base_gf) / (info["pj"] + 2.5), 2)
        ga_final = round((ga_raw * info["pj"] + 2.5 * base_ga) / (info["pj"] + 2.5), 2)
        pseudo_elo = round(1380 + (info["ppg"] * 115) + (info["gd_pg"] * 45))
    else:
        gf_final, ga_final, pseudo_elo = base_gf, base_ga, 1550

    # 2. Forma real (últimos 5) y días reales de descanso
    rest_days = 5
    if partidos:
        try:
            dt_hoy = datetime.fromisoformat(fecha_partido_iso.replace("Z", "+00:00"))
            dt_ult = datetime.fromisoformat(partidos[0]["date"].replace("Z", "+00:00"))
            rest_days = max(1, min(14, (dt_hoy - dt_ult).days))
        except Exception:
            pass
        ult5 = [p["res"] for p in partidos[:5]]
        form_str = "".join(reversed(ult5))
        pts_5 = sum(3 if c == "W" else 1 if c == "D" else 0 for c in form_str)
        form_score = round(pts_5 / (len(form_str) * 3), 2)
    else:
        form_str = "DDDDD"
        form_score = 0.5

    return {
        "gf": gf_final,
        "ga": ga_final,
        "form_str": form_str,
        "form_score": form_score,
        "rest_days": rest_days,
        "pseudo_elo": pseudo_elo,
    }


# ==========================================
# 2. API-FOOTBALL (PARTIDOS DEL DÍA Y BAJAS)
# ==========================================
def peticion_football(endpoint, params):
    url = f"https://v3.football.api-sports.io/{endpoint}"
    for _ in range(2):
        try:
            r = requests.get(url, headers=HEADERS_FOOTBALL, params=params, timeout=20)
            data = r.json()
            errores = data.get("errors", {})
            if errores and "rateLimit" in str(errores):
                time.sleep(62)
                continue
            if errores:
                print(f"  [Error API-Football {endpoint}] {errores}")
                return []
            return data.get("response", [])
        except Exception as e:
            print(f"  [Aviso] {endpoint}: {e}")
    return []


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
# 3. OPEN-METEO (FACTOR CLIMA SILENCIOSO)
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
                "latitude": coords[0], "longitude": coords[1],
                "hourly": "temperature_2m,precipitation,wind_speed_10m",
                "timezone": "UTC", "start_date": dia, "end_date": dia,
            },
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


# ==========================================
# 4. CLUB ELO + THE ODDS API
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
# 5. FLUJO PRINCIPAL
# ==========================================
def main():
    ahora_utc = datetime.now(timezone.utc)
    hoy = ahora_utc.strftime("%Y-%m-%d")
    print(f"Iniciando recolección híbrida para: {hoy}")

    elo_map = obtener_tabla_elo(hoy)
    fixtures = peticion_football("fixtures", {"date": hoy})

    filtrados = [
        f for f in fixtures
        if f["league"]["id"] in LIGAS_OBJETIVO
        and f["fixture"]["status"]["short"] in ("NS", "TBD")
    ][:MAX_PARTIDOS_DIA]

    print(f"Total partidos hoy: {len(fixtures)} | En ligas objetivo: {len(filtrados)}")

    cuotas_por_liga, espn_tablas, espn_recientes = {}, {}, {}
    for lid in set(f["league"]["id"] for f in filtrados):
        cfg = LIGAS_OBJETIVO[lid]
        cuotas_por_liga[lid] = obtener_cuotas_liga(cfg["odds_key"])
        espn_tablas[lid] = obtener_standings_espn(cfg["espn"])
        espn_recientes[lid] = obtener_recientes_espn(cfg["espn"], ahora_utc)

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

        stats_h = construir_metricas_espn(home["name"], True, espn_tablas[lid], espn_recientes[lid], fecha_iso, cfg["avg_h"], cfg["avg_a"])
        stats_a = construir_metricas_espn(away["name"], False, espn_tablas[lid], espn_recientes[lid], fecha_iso, cfg["avg_h"], cfg["avg_a"])
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
        "updatedAt": ahora_utc.isoformat(),
        "date": hoy,
        "matches": partidos_salida,
    }

    with open("partidos_hoy.json", "w", encoding="utf-8") as fp:
        json.dump(salida, fp, ensure_ascii=False, indent=2)

    print(f"Listo. Guardados {len(partidos_salida)} partidos en partidos_hoy.json")


if __name__ == "__main__":
    main()
