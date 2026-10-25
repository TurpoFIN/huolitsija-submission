"""Builds data/rail_corridors.json: real rail lines as node polylines for services/geo.py.

Finnish lines come from Fintraffic Digitraffic (rata.digitraffic.fi): the station sequence of a real
timetabled train between two stations (timetable rows list every timing point the train passes) and the
station coordinates from the station metadata. International corridors are curated waypoint lists of
the real lines (station/junction coordinates), checked against the operators' published route maps.
Nothing routes through Russia or Belarus (Finland–Russia rail border closed; EU sanctions).

Run:  ~/work/venv/bin/python /home/user/work/huolitsija/backend/data/rail_corridors_build.py
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

OUT = Path(__file__).resolve().parent / "rail_corridors.json"
H = {"Digitraffic-User": "Huolitsija/1.0", "Accept-Encoding": "gzip"}
RATA = "https://rata.digitraffic.fi/api/v1"

# (id, name, from, to). The station sequence between from/to is read from a real train's timetable.
FI_LINES = [
    ("fi-hki-ol", "Helsinki–Riihimäki–Tampere–Seinäjoki–Oulu main line", "HKI", "OL"),
    ("fi-ke-kv", "Kerava–Lahti–Kouvola line", "KE", "KV"),
    ("fi-lh-ri", "Lahti–Riihimäki line", "LH", "RI"),
    ("fi-kv-mss", "Kouvola–Kotka Mussalo port line", "KV", "MSS"),
    ("fi-kv-hma", "Kouvola–Hamina port line", "KV", "HMA"),
    ("fi-kv-lr", "Kouvola–Lappeenranta line", "KV", "LR"),
    ("fi-lr-imr", "Lappeenranta–Imatra line", "LR", "IMR"),
    ("fi-tpe-tku", "Tampere–Toijala–Turku line", "TPE", "TKU"),
    ("fi-hki-tku", "Helsinki–Karjaa–Turku coastal line", "HKI", "TKU"),
    ("fi-sk-vs", "Seinäjoki–Vaasa line", "SK", "VS"),
    ("fi-ol-roi", "Oulu–Kemi–Rovaniemi line", "OL", "ROI"),
    ("fi-kem-tor", "Kemi–Tornio line", "KEM", "TOR"),
    ("fi-tpe-rma", "Tampere–Kokemäki–Rauma line", "TPE", "RMA"),
    ("fi-kv-kuo", "Kouvola–Mikkeli–Pieksämäki–Kuopio line", "KV", "KUO"),
    ("fi-kuo-ol", "Kuopio–Iisalmi–Kontiomäki–Oulu line", "KUO", "OL"),
]

# Short Finnish links that have no through passenger train; station codes in track order.
FI_MANUAL = [
    ("fi-sav-vsa", "Savio–Vuosaari harbour railway", ["SAV", "VSA"]),
    ("fi-tor-hpa", "Tornio–Haparanda border line (gauge-change yard)", ["TOR", "TRR", "HPA"]),
    ("fi-kvla-kv", "Kouvola marshalling yard", ["KV", "KVLA"]),
]

# Curated international corridors: [lon, lat, node]. Nodes shared between corridors join the graph.
INTL = [
    {"id": "se-hpa-lla", "name": "Haparanda–Kalix–Boden–Luleå (Haparandabanan, Malmbanan)", "gauge_mm": 1435,
     "points": [[24.130219, 65.828226, "FI:HPA"], [23.1569, 65.8550, "SE:Kalix"], [22.3622, 65.9283, "SE:Morjärv"],
                [21.6886, 65.8253, "SE:Boden"], [22.1547, 65.5848, "SE:Luleå"]]},
    {"id": "de-dui-ham", "name": "Duisburg–Münster–Osnabrück–Bremen–Hamburg (Rollbahn)", "gauge_mm": 1435,
     "points": [[6.7590, 51.4300, "DE:Duisburg"], [6.8516, 51.4744, "DE:Oberhausen"], [7.0989, 51.5047, "DE:Gelsenkirchen"],
                [7.1979, 51.6140, "DE:Recklinghausen"], [7.6346, 51.9566, "DE:Münster"], [8.0596, 52.2730, "DE:Osnabrück"],
                [8.3709, 52.6077, "DE:Diepholz"], [8.8135, 53.0835, "DE:Bremen"], [9.3982, 53.1157, "DE:Rotenburg"],
                [9.8790, 53.3247, "DE:Buchholz"], [10.0570, 53.4039, "DE:Maschen"], [9.9900, 53.4562, "DE:Hamburg-Harburg"],
                [9.9300, 53.5300, "DE:Hamburg-Waltershof"]]},
    {"id": "nl-de-betuwe", "name": "Rotterdam Maasvlakte–Betuweroute–Emmerich–Duisburg", "gauge_mm": 1435,
     "points": [[4.0500, 51.9500, "NL:Maasvlakte"], [4.3000, 51.8950, "NL:Botlek"], [4.5330, 51.8600, "NL:Barendrecht"],
                [5.0000, 51.8350, "NL:Gorinchem"], [5.2900, 51.8800, "NL:Geldermalsen"], [5.8400, 51.9200, "NL:Elst"],
                [6.0700, 51.9300, "NL:Zevenaar"], [6.2500, 51.8300, "DE:Emmerich"], [6.4500, 51.6600, "DE:Wesel"],
                [6.6800, 51.5300, "DE:Oberhausen-West"], [6.7590, 51.4300, "DE:Duisburg"]]},
    {"id": "de-pl-dui-waw", "name": "Duisburg–Hannover–Berlin–Frankfurt (Oder)–Poznań–Warsaw (E20)", "gauge_mm": 1435,
     "points": [[6.7590, 51.4300, "DE:Duisburg"], [7.4652, 51.5136, "DE:Dortmund"], [7.8140, 51.6730, "DE:Hamm"],
                [8.5325, 52.0302, "DE:Bielefeld"], [9.7390, 52.3760, "DE:Hannover"], [10.7865, 52.4227, "DE:Wolfsburg"],
                [11.8600, 52.6060, "DE:Stendal"], [13.1980, 52.5350, "DE:Berlin-Spandau"], [13.3690, 52.5250, "DE:Berlin"],
                [14.5400, 52.3470, "DE:Frankfurt (Oder)"], [15.5060, 52.2310, "PL:Rzepin"], [16.9290, 52.4060, "PL:Poznań"],
                [18.2560, 52.2190, "PL:Konin"], [19.4560, 52.2200, "PL:Kutno"], [21.0030, 52.2290, "PL:Warszawa"]]},
    {"id": "pl-gdn-waw", "name": "Gdańsk–Tczew–Iława–Warsaw (E65)", "gauge_mm": 1435,
     "points": [[18.7000, 54.4000, "PL:Gdańsk DCT"], [18.6460, 54.3550, "PL:Gdańsk"], [18.7930, 54.0920, "PL:Tczew"],
                [19.5650, 53.5970, "PL:Iława"], [20.3830, 53.1490, "PL:Działdowo"], [20.9730, 52.4240, "PL:Nasielsk"],
                [21.0030, 52.2290, "PL:Warszawa"]]},
    {"id": "pl-lt-rail-baltica", "name": "Rail Baltica standard gauge: Warsaw–Białystok–Ełk–Trakiszki–Kaunas", "gauge_mm": 1435,
     "points": [[21.0030, 52.2290, "PL:Warszawa"], [21.6110, 52.4990, "PL:Tłuszcz"], [22.0570, 52.8130, "PL:Czyżew"],
                [23.1550, 53.1330, "PL:Białystok"], [22.4040, 53.6370, "PL:Grajewo"], [22.3620, 53.8280, "PL:Ełk"],
                [22.9330, 54.1020, "PL:Suwałki"], [23.0740, 54.2380, "PL:Trakiszki"], [23.2280, 54.3200, "LT:Mockava"],
                [23.4480, 54.3630, "LT:Šeštokai"], [23.4900, 54.7600, "LT:Kazlų Rūda"], [23.8870, 54.8890, "LT:Kaunas"]]},
    {"id": "lt-lv-ee-1520", "name": "Kaunas–Šiauliai–Jelgava–Riga–Valga–Tartu–Tapa–Tallinn (1520 mm network)", "gauge_mm": 1520,
     "points": [[23.8870, 54.8890, "LT:Kaunas"], [23.6600, 55.2700, "LT:Kėdainiai"], [23.3150, 55.9330, "LT:Šiauliai"],
                [23.7270, 56.6510, "LV:Jelgava"], [24.1210, 56.9470, "LV:Rīga"], [24.6500, 57.1600, "LV:Sigulda"],
                [25.2300, 57.3100, "LV:Cēsis"], [26.0470, 57.7770, "LV:Valga"], [26.0300, 58.0750, "EE:Elva"],
                [26.7090, 58.3740, "EE:Tartu"], [26.3500, 58.7600, "EE:Jõgeva"], [25.9580, 59.2600, "EE:Tapa"],
                [25.2400, 59.3500, "EE:Aegviidu"], [24.9650, 59.4900, "EE:Muuga"], [24.7380, 59.4400, "EE:Tallinn"]]},
    {"id": "lt-lv-ee-klaipeda", "name": "Klaipėda port–Šiauliai", "gauge_mm": 1520,
     "points": [[21.1350, 55.7000, "LT:Klaipėda"], [21.8500, 55.9200, "LT:Kretinga-Plungė"], [22.5000, 55.9000, "LT:Telšiai"],
                [23.3150, 55.9330, "LT:Šiauliai"]]},
    {"id": "asia-eu-middle-corridor",
     "name": "Trans-Caspian International Transport Route (Middle Corridor): Xi'an–Khorgos–Almaty–Aktau ⛴ Alat–Tbilisi–Kars–Istanbul–Sofia–Belgrade–Budapest–Vienna–Nuremberg–Duisburg",
     "gauge_mm": None, "note": "Caspian Sea crossing Aktau–Alat is a rail ferry; gauge changes at Khorgos (1435/1520) and Akhalkalaki (1520/1435).",
     "points": [[109.0300, 34.4200, "CN:Xi'an ITL"], [107.1500, 34.3600, "CN:Baoji"], [105.7200, 34.5800, "CN:Tianshui"],
                [103.8340, 36.0610, "CN:Lanzhou"], [102.6380, 37.9280, "CN:Wuwei"], [100.4500, 38.9330, "CN:Zhangye"],
                [98.2900, 39.7730, "CN:Jiayuguan"], [95.7800, 40.5200, "CN:Liuyuan"], [93.5150, 42.8180, "CN:Hami"],
                [89.1900, 42.9500, "CN:Turpan"], [87.6200, 43.8260, "CN:Ürümqi"], [84.8700, 44.4300, "CN:Kuytun"],
                [82.9000, 44.6000, "CN:Jinghe"], [80.4200, 44.2100, "CN:Khorgos"], [80.2700, 44.2400, "KZ:Altynkol"],
                [78.4000, 43.6000, "KZ:Shelek"], [76.9460, 43.2380, "KZ:Almaty"], [73.7600, 43.6000, "KZ:Shu"],
                [71.3700, 42.9000, "KZ:Taraz"], [69.5900, 42.3200, "KZ:Shymkent"], [68.8000, 42.4300, "KZ:Arys"],
                [66.9000, 43.7500, "KZ:Zhosaly"], [65.5100, 44.8500, "KZ:Kyzylorda"], [61.6700, 46.8000, "KZ:Aralsk"],
                [58.1000, 47.1000, "KZ:Shalkar"], [55.2000, 45.3200, "KZ:Beineu"], [52.2000, 43.9000, "KZ:Mangystau"],
                [51.1700, 43.6500, "KZ:Aktau port"], [50.3000, 41.8000, "Caspian Sea (ferry)"],
                [49.4100, 39.9500, "AZ:Alat port"], [48.3800, 40.4100, "AZ:Kurdamir"], [46.3600, 40.6800, "AZ:Ganja"],
                [45.3400, 41.1700, "AZ:Boyuk Kesik"], [44.7900, 41.7200, "GE:Tbilisi"], [43.4800, 41.4100, "GE:Akhalkalaki"],
                [43.1000, 40.6000, "TR:Kars"], [41.2700, 39.9000, "TR:Erzurum"], [39.4900, 39.7500, "TR:Erzincan"],
                [37.0200, 39.7500, "TR:Sivas"], [35.4800, 38.7300, "TR:Kayseri"], [33.5100, 39.8400, "TR:Kırıkkale"],
                [32.8600, 39.9400, "TR:Ankara"], [30.5200, 39.7800, "TR:Eskişehir"], [29.9200, 40.7700, "TR:İzmit"],
                [29.2300, 40.8800, "TR:Pendik"],
                # Marmaray: the Bosporus rail tunnel between Üsküdar and Sirkeci (2013)
                [29.1900, 40.8970, "TR:Kartal"], [29.0950, 40.9630, "TR:Bostancı"], [29.0330, 41.0020, "TR:Ayrılık Çeşmesi"], [29.0150, 41.0260, "TR:Üsküdar (Marmaray)"], [28.9770, 41.0140, "TR:Sirkeci (Marmaray)"], [28.9500, 41.0040, "TR:Yenikapı"], [28.9200, 40.9930, "TR:Kazlıçeşme"], [28.8700, 40.9800, "TR:Bakırköy"],
                [28.7700, 41.0200, "TR:Halkalı"], [28.0000, 41.2900, "TR:Çerkezköy"],
                [26.5600, 41.6800, "TR:Edirne"], [26.3600, 41.7200, "TR:Kapıkule"], [26.2000, 41.7700, "BG:Svilengrad"],
                [25.5600, 42.0500, "BG:Dimitrovgrad"], [24.7500, 42.1400, "BG:Plovdiv"], [23.3200, 42.7000, "BG:Sofia"],
                [22.7800, 43.0100, "RS:Dimitrovgrad"], [21.9000, 43.3200, "RS:Niš"], [21.1000, 44.0000, "RS:Lapovo"],
                [20.4600, 44.8100, "RS:Beograd"], [19.8400, 45.2500, "RS:Novi Sad"], [19.6700, 46.1000, "RS:Subotica"],
                [19.6200, 46.1900, "HU:Kelebia"], [19.4000, 46.6500, "HU:Kiskunhalas-Kunszentmiklós"], [19.0400, 47.5000, "HU:Budapest"],
                [17.6300, 47.6900, "HU:Győr"], [17.1600, 47.9100, "HU:Hegyeshalom"], [16.3700, 48.1900, "AT:Wien"],
                [15.6300, 48.2000, "AT:St. Pölten"], [14.2900, 48.3100, "AT:Linz"], [13.4300, 48.5700, "DE:Passau"],
                [12.1000, 49.0100, "DE:Regensburg"], [11.0800, 49.4500, "DE:Nürnberg"], [9.9300, 49.7900, "DE:Würzburg"],
                [8.6800, 50.1100, "DE:Frankfurt am Main"], [7.5900, 50.3600, "DE:Koblenz"], [6.9600, 50.9400, "DE:Köln"],
                [6.7800, 51.2300, "DE:Düsseldorf"], [6.7590, 51.4300, "DE:Duisburg"]]},
]


def fetch_stations() -> dict:
    r = httpx.get(f"{RATA}/metadata/stations", headers=H, timeout=60)
    r.raise_for_status()
    return {s["stationShortCode"]: s for s in r.json()}


def station_sequence(a: str, b: str) -> list[str] | None:
    r = httpx.get(f"{RATA}/live-trains/station/{a}/{b}", headers=H, timeout=60)
    if r.status_code != 200:
        return None
    for t in r.json():
        codes: list[str] = []
        for row in t["timeTableRows"]:
            c = row["stationShortCode"]
            if not codes or codes[-1] != c:
                codes.append(c)
        if a in codes and b in codes and codes.index(a) < codes.index(b):
            return codes[codes.index(a): codes.index(b) + 1]
    return None


def main():
    stations = fetch_stations()
    corridors = []
    for cid, name, a, b in FI_LINES:
        seq = station_sequence(a, b)
        if not seq:
            print("no train found for", a, b)
            continue
        pts = [[round(stations[c]["longitude"], 6), round(stations[c]["latitude"], 6), f"FI:{c}"] for c in seq if c in stations]
        corridors.append({"id": cid, "name": name, "country": "FI", "gauge_mm": 1524, "operator": "VR Transpoint / Fenniarail / Operail",
                          "source": f"rata.digitraffic.fi timetable {a}->{b}", "points": pts})
        print(cid, len(pts))
    for cid, name, seq in FI_MANUAL:
        pts = [[round(stations[c]["longitude"], 6), round(stations[c]["latitude"], 6), f"FI:{c}"] for c in seq]
        corridors.append({"id": cid, "name": name, "country": "FI", "gauge_mm": 1524, "source": "rata.digitraffic.fi station metadata",
                          "points": pts})
    for c in INTL:
        corridors.append({**c, "source": "curated waypoints (station/junction coordinates)"})
    # Rail terminals: location code -> graph node (the yard/station that serves it).
    terminals = {
        "FIHEL": "FI:VSA", "FIVSS": "FI:VSA", "FIKTK": "FI:MSS", "FIHMN": "FI:HMA", "FIKVO": "FI:KVLA", "FIOUL": "FI:OL",
        "FITMP": "FI:TPE", "FITKU": "FI:TKU", "FIRAU": "FI:RMA", "FIVAA": "FI:VS", "FIKOK": "FI:KOK", "FIRVN": "FI:ROI",
        "FIKEM": "FI:KEM", "FITOR": "FI:TOR", "FILHI": "FI:LH", "FILPP": "FI:LR", "FIKUO": "FI:KUO", "FISJY": "FI:SK",
        "SEHAA": "FI:HPA", "SELLA": "SE:Luleå", "DEDUI": "DE:Duisburg", "DEHAM": "DE:Hamburg-Waltershof", "NLRTM": "NL:Maasvlakte",
        "PLGDN": "PL:Gdańsk DCT", "PLWAW": "PL:Warszawa", "LTKUN": "LT:Kaunas", "LTKLJ": "LT:Klaipėda", "LVRIX": "LV:Rīga",
        "EETLL": "EE:Tallinn", "EEMUG": "EE:Muuga", "CNXIA": "CN:Xi'an ITL", "KZALA": "KZ:Almaty", "KZAAU": "KZ:Aktau port",
        "GETBS": "GE:Tbilisi", "TRIST": "TR:Halkalı", "HUBUD": "HU:Budapest", "ATVIE": "AT:Wien", "DENUE": "DE:Nürnberg",
    }
    out = {"version": 1, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "sources": ["https://rata.digitraffic.fi/api/v1/metadata/stations", "https://rata.digitraffic.fi/api/v1/live-trains/station/{from}/{to}",
                       "curated international waypoints"],
           "note": "Finnish Kouvola–Vainikkala–Russia and other Russia/Belarus links are intentionally absent.",
           "terminals": terminals, "corridors": corridors}
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
    print("wrote", OUT, sum(len(c["points"]) for c in corridors), "points")


if __name__ == "__main__":
    main()
