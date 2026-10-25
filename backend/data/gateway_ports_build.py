"""Builds data/gateway_ports.json: the world's container gateway ports, for routing any city pair via the nearest
container ports in the quoting engine.

Sources
- Curated port list below (PORTS): which ports have a scheduled container service, their trade region, tier,
  aliases and notes, from liner-network knowledge (Lloyd's List One Hundred Ports; MSC, Maersk, CMA CGM, COSCO,
  Hapag-Lloyd, ONE, Evergreen service networks and their regional feeder partners).
  Tier 1 = global mainline / transshipment hub, 2 = deep-sea gateway with direct mainline calls,
  3 = regional / feeder-only port.
- NGA World Port Index, Pub. 150 (public domain): harbour coordinates by UN/LOCODE and the container facility /
  container crane flags used for the coverage check.
  https://msi.nga.mil/api/publications/download?key=16920959/SFH00000/UpdatedPub150.csv&type=download
- data/locations_unlocode.json (UN/LOCODE + OurAirports, see locations_build.py): every LOCODE must exist here or in
  the WPI; codes found in neither are reported and dropped.
- data/locations_curated.json: terminal-level coordinates, preferred over everything else.

Coordinates: curated record > WPI harbour position for the LOCODE > dataset LOCODE row (> dataset airport row that
carries the LOCODE, reported). Ports where WPI and the dataset differ by more than 30 km are reported; WPI is kept
(it is the harbour position, UN/LOCODE coordinates are the town).
Russia, Belarus, North Korea, Iran and Syria are left out (EU sanctions; domain spec RULE K-2 default-deny) and listed
under "excluded".

Run: ~/work/venv/bin/python /home/user/work/huolitsija/backend/data/gateway_ports_build.py [cache_dir]
     (reads cache_dir/wpi.csv or /tmp/wpi.csv; downloads the WPI CSV into cache_dir when neither exists)
"""
import csv
import io
import json
import math
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
OUT = HERE / "gateway_ports.json"
WPI_URL = "https://msi.nga.mil/api/publications/download?key=16920959/SFH00000/UpdatedPub150.csv&type=download"

REGIONS = {
    "FEA": "Far East", "SEA": "Southeast Asia", "ISC": "Indian Subcontinent", "MEG": "Middle East Gulf",
    "RSE": "Red Sea", "MED": "Mediterranean & Black Sea", "NEU": "North Europe", "WAF": "West & Central Africa",
    "SAF": "Southern Africa", "EAF": "East Africa & Indian Ocean islands", "NAW": "North America West Coast",
    "NAE": "North America East Coast", "USG": "US Gulf & Mexico Gulf", "CAR": "Caribbean & Central America",
    "SAE": "South America East Coast", "SAW": "South America West Coast", "OCE": "Oceania",
}

# Countries allowed per region (consistency check: catches a port filed under the wrong region).
REGION_COUNTRIES = {
    "FEA": "CN HK MO TW KR JP",
    "SEA": "VN TH MY SG ID PH KH MM BN TL",
    "ISC": "IN PK BD LK MV",
    "MEG": "AE QA BH KW IQ OM SA",
    "RSE": "SA EG SD DJ ER YE JO",
    "MED": "ES FR IT MT SI HR ME AL GR TR CY IL LB EG LY TN DZ MA RO BG GE UA",
    "NEU": "GB IE FR BE NL DE DK NO SE PL LT LV EE FI IS FO GL SJ ES PT",
    "WAF": "MA ES MR SN GM GW GN SL LR CI GH TG BJ NG CM GQ GA ST CG CD AO CV",
    "SAF": "ZA NA MZ SH",
    "EAF": "KE TZ MZ MG MU RE YT SC SO KM",
    "NAW": "US CA MX",
    "NAE": "US CA PM",
    "USG": "US MX",
    "CAR": "PA CO VE GY SR GF GT HN SV NI CR BZ JM DO HT CU BS TT BB LC VC GD DM AG KN PR CW AW BQ SX MQ GP VI VG KY TC BM",
    "SAE": "BR UY AR FK",
    "SAW": "CL PE EC CO",
    "OCE": "AU NZ FJ PG SB VU NC PF WS TO KI TV NR MH FM PW GU MP AS CK WF NU",
}
REGION_COUNTRIES = {r: set(v.split()) for r, v in REGION_COUNTRIES.items()}

# Every UN member state with a sea coast (Caspian-only states are in LANDLOCKED: no ocean container service).
COASTAL_STATES = set("""
DZ AO BJ CV CM KM CG CD CI DJ EG GQ ER GA GM GH GN GW KE LR LY MG MR MU MA MZ NA NG ST SN SC SL SO ZA SD TZ TG TN
AE BH BD BN KH CN CY GE IN ID IR IQ IL JP JO KP KR KW LB MY MV MM OM PK PH QA SA SG LK SY TH TL TR VN YE
AL BE BA BG HR DK EE FI FR DE GR IS IE IT LV LT MT MC ME NL NO PL PT RO RU SI ES SE UA GB
AG AR BS BB BZ BR CA CL CO CR CU DM DO EC SV GD GT GY HT HN JM MX NI PA PE KN LC VC SR TT US UY VE
AU FJ KI MH FM NR NZ PW PG WS SB TO TV VU
""".split())
LANDLOCKED = set("""
AF AD AM AT AZ BY BO BW BF BI CF TD CZ SZ ET HU KZ XK KG LA LS LI LU MW ML MD MN NP NE MK PY RW SM RS SK SS CH TJ
TM UG UZ VA ZM ZW
""".split())

EXCLUDED = [
    {"country": "RU", "name": "Russia",
     "reason": "EU sanctions (Council Regulation (EU) No 833/2014); domain spec RULE K-2 default-deny",
     "ports_not_listed": ["St Petersburg", "Ust-Luga", "Kaliningrad", "Novorossiysk", "Vladivostok", "Vostochny"]},
    {"country": "BY", "name": "Belarus",
     "reason": "EU sanctions (Council Regulation (EC) No 765/2006); domain spec RULE K-2 default-deny. Landlocked.",
     "ports_not_listed": []},
    {"country": "KP", "name": "North Korea",
     "reason": "UN/EU sanctions (Council Regulation (EU) 2017/1509); domain spec RULE K-2 default-deny",
     "ports_not_listed": ["Nampo", "Chongjin", "Rajin"]},
    {"country": "IR", "name": "Iran",
     "reason": "EU sanctions (Council Regulation (EU) No 267/2012); domain spec RULE K-2 default-deny",
     "ports_not_listed": ["Bandar Abbas (Shahid Rajaee)", "Bushehr", "Chabahar"]},
    {"country": "SY", "name": "Syria",
     "reason": "EU sanctions (Council Regulation (EU) No 36/2012; most economic measures lifted May 2025); "
               "domain spec RULE K-2 default-deny still applies",
     "ports_not_listed": ["Latakia", "Tartus"]},
]
EXCLUDED_CC = {e["country"] for e in EXCLUDED}

# Coastal countries/territories that deliberately get no gateway (printed with the coverage check).
NO_GATEWAY = {
    "BA": "Neum is Bosnia and Herzegovina's only coast and has no cargo port; BiH cargo moves via Ploče (HRPLE)",
    "MC": "Monaco has no cargo port; served via Genoa / Fos",
    "BL": "Saint-Barthélemy: lo-lo feeder from Sint Maarten; quote via Philipsburg (SXPHI)",
}

# (UN/LOCODE, name, region, tier, aliases "a|b|c", notes)
PORTS = [
    # ---- FEA: Far East ----
    ("CNSHA", "Shanghai (Yangshan)", "FEA", 1, "Shanghai|Yangshan|Waigaoqiao", "World's busiest container port; Yangshan deep-water and Waigaoqiao terminals"),
    ("CNNGB", "Ningbo-Zhoushan", "FEA", 1, "Ningbo|Beilun|Zhoushan|Meishan", "Beilun and Meishan terminals; Zhejiang mainline hub"),
    ("CNYTN", "Shenzhen (Yantian)", "FEA", 1, "Yantian|Shenzhen|Shenzhen East", "East Shenzhen deep-water mainline terminal"),
    ("CNSHK", "Shenzhen (Shekou)", "FEA", 1, "Shekou|Chiwan|Mawan|Shenzhen|Shenzhen West", "West Shenzhen terminals (Shekou, Chiwan, Mawan)"),
    ("CNNSA", "Guangzhou (Nansha)", "FEA", 1, "Nansha|Guangzhou|Canton", "Pearl River Delta mainline terminal"),
    ("CNTAO", "Qingdao", "FEA", 1, "Qingdao|Tsingtao|Qianwan|Dongjiakou", "North China mainline hub (Qianwan)"),
    ("CNTSN", "Tianjin (Xingang)", "FEA", 1, "Tianjin|Xingang|Tianjin Xingang", "Gateway for Beijing; carriers also file it as CNTXG"),
    ("CNDLC", "Dalian", "FEA", 2, "Dalian|Dayaowan", "Northeast China gateway"),
    ("CNXMN", "Xiamen", "FEA", 2, "Xiamen|Amoy|Haicang", "Fujian mainline gateway"),
    ("CNLYG", "Lianyungang", "FEA", 2, "Lianyungang", "Jiangsu gateway; China–Central Asia rail bridgehead"),
    ("CNYIK", "Yingkou", "FEA", 3, "Yingkou|Bayuquan", "Liaoning; intra-Asia services, rail to Mongolia/Europe"),
    ("CNQZH", "Qinzhou (Beibu Gulf)", "FEA", 3, "Qinzhou|Beibu Gulf|Guangxi", "Western China land–sea corridor (rail-sea to Chongqing/Chengdu)"),
    ("HKHKG", "Hong Kong", "FEA", 1, "Hong Kong|Kwai Tsing|Kwai Chung", "Kwai Tsing terminals; transshipment hub"),
    ("MOMFM", "Macau", "FEA", 3, "Macau|Macao|Ka Ho", "River/feeder service via Hong Kong"),
    ("TWKHH", "Kaohsiung", "FEA", 1, "Kaohsiung|Gaoxiong", "Taiwan's mainline and transshipment hub"),
    ("TWTXG", "Taichung", "FEA", 2, "Taichung", "Central Taiwan"),
    ("TWTPE", "Taipei Port", "FEA", 2, "Taipei Port|Tamsui", "Taipei Port (Bali, New Taipei); mainline calls"),
    ("TWKEL", "Keelung", "FEA", 3, "Keelung|Jilong|Chilung", "Northern Taiwan; intra-Asia services"),
    ("KRPUS", "Busan", "FEA", 1, "Busan|Pusan|Busan New Port", "Korea's mainline port and Northeast Asia transshipment hub"),
    ("KRINC", "Incheon", "FEA", 2, "Incheon|Inchon", "Seoul gateway; China/SEA and some transpacific services"),
    ("KRKAN", "Gwangyang", "FEA", 2, "Gwangyang|Kwangyang", "South Korea's second container port"),
    ("KRPTK", "Pyeongtaek", "FEA", 3, "Pyeongtaek|Pyeongtaek-Dangjin", "China/intra-Asia services"),
    ("JPTYO", "Tokyo", "FEA", 2, "Tokyo|Ohi|Aomi", "Keihin ports; mainline calls"),
    ("JPYOK", "Yokohama", "FEA", 2, "Yokohama|Honmoku|Minami Honmoku", "Keihin ports; mainline calls"),
    ("JPNGO", "Nagoya", "FEA", 2, "Nagoya|Tobishima", "Chukyo industrial region gateway"),
    ("JPUKB", "Kobe", "FEA", 2, "Kobe|Port Island|Rokko Island", "Hanshin ports; mainline calls"),
    ("JPOSA", "Osaka", "FEA", 2, "Osaka|Yumeshima|Nanko", "Hanshin ports"),
    ("JPHKT", "Hakata (Fukuoka)", "FEA", 3, "Hakata|Fukuoka", "Kyushu; Korea/China/SEA services"),
    ("JPNAH", "Naha", "FEA", 3, "Naha|Okinawa", "Okinawa; domestic and Taiwan/Asia feeders"),
    # ---- SEA: Southeast Asia ----
    ("SGSIN", "Singapore", "SEA", 1, "Singapore|Pasir Panjang|Tuas|Keppel", "World's largest transshipment hub (PSA Pasir Panjang, Tuas)"),
    ("MYPKG", "Port Klang", "SEA", 1, "Port Klang|Westports|Northport|Kuala Lumpur", "Transshipment hub (Westports, Northport)"),
    ("MYTPP", "Tanjung Pelepas", "SEA", 1, "Tanjung Pelepas|PTP", "Maersk/MSC transshipment hub"),
    ("MYPEN", "Penang", "SEA", 3, "Penang|Pulau Pinang|Butterworth|North Butterworth", "Northern Peninsular Malaysia"),
    ("MYJHB", "Pasir Gudang (Johor Port)", "SEA", 3, "Pasir Gudang|Johor Port", "Johor Port; regional services"),
    ("MYBKI", "Kota Kinabalu (Sepanggar)", "SEA", 3, "Kota Kinabalu|Sepanggar", "Sabah; feeder via Port Klang/Singapore"),
    ("MYKCH", "Kuching", "SEA", 3, "Kuching|Senari", "Sarawak; feeder via Singapore/Port Klang"),
    ("VNCMT", "Cai Mep", "SEA", 2, "Cai Mep|Cai Mep-Thi Vai|Gemalink", "Deep-water mainline terminals for southern Vietnam"),
    ("VNSGN", "Ho Chi Minh City (Cat Lai)", "SEA", 3, "Ho Chi Minh City|Saigon|Cat Lai|HCMC", "River terminal; intra-Asia services (mainline at Cai Mep)"),
    ("VNHPH", "Hai Phong", "SEA", 2, "Hai Phong|Haiphong|Lach Huyen", "Northern Vietnam; Lach Huyen deep-water terminal"),
    ("VNDAD", "Da Nang", "SEA", 3, "Da Nang|Tien Sa", "Central Vietnam; regional services"),
    ("THLCH", "Laem Chabang", "SEA", 2, "Laem Chabang|Bangkok", "Thailand's deep-sea gateway"),
    ("THBKK", "Bangkok (PAT)", "SEA", 3, "Bangkok|Khlong Toei|Klong Toey|PAT", "River port; intra-Asia services"),
    ("IDJKT", "Jakarta (Tanjung Priok)", "SEA", 2, "Jakarta|Tanjung Priok|NPCT1|JICT", "Indonesia's main gateway; some direct mainline calls"),
    ("IDSUB", "Surabaya (Tanjung Perak)", "SEA", 3, "Surabaya|Tanjung Perak|Teluk Lamong", "East Java; intra-Asia and feeder"),
    ("IDSRG", "Semarang", "SEA", 3, "Semarang|Tanjung Emas", "Central Java; feeder via Singapore"),
    ("IDBLW", "Belawan (Medan)", "SEA", 3, "Belawan|Medan", "North Sumatra; feeder via Port Klang/Singapore"),
    ("IDMAK", "Makassar", "SEA", 3, "Makassar|Ujung Pandang", "Sulawesi; domestic and feeder"),
    ("IDDJJ", "Jayapura", "SEA", 3, "Jayapura", "Papua; domestic feeder via Surabaya/Jakarta"),
    ("PHMNL", "Manila", "SEA", 2, "Manila|MICT|Manila North Harbor|Manila South Harbor", "Philippines' main gateway"),
    ("PHBTG", "Batangas", "SEA", 3, "Batangas", "South Luzon alternative to Manila"),
    ("PHCEB", "Cebu", "SEA", 3, "Cebu", "Visayas; feeder"),
    ("PHDVO", "Davao", "SEA", 3, "Davao|Sasa", "Mindanao; feeder and banana reefers"),
    ("KHKOS", "Sihanoukville", "SEA", 3, "Sihanoukville|Kampong Saom|Kompong Som", "Cambodia's deep-water port; feeder via Singapore"),
    ("MMRGN", "Yangon (Thilawa)", "SEA", 3, "Yangon|Rangoon|Thilawa", "Myanmar; feeder via Singapore/Port Klang"),
    ("BNMUA", "Muara", "SEA", 3, "Muara|Bandar Seri Begawan|Brunei", "Brunei; feeder via Singapore/Tanjung Pelepas"),
    ("TLDIL", "Dili", "SEA", 3, "Dili|Tibar", "Timor-Leste; feeder via Singapore/Surabaya"),
    # ---- ISC: Indian Subcontinent ----
    ("LKCMB", "Colombo", "ISC", 1, "Colombo|JCT|CICT|SAGT|ECT", "South Asia transshipment hub"),
    ("INNSA", "Nhava Sheva (JNPT)", "ISC", 1, "Nhava Sheva|JNPT|Jawaharlal Nehru Port|Mumbai|Navi Mumbai", "India's main mainline gateway (Mumbai)"),
    ("INMUN", "Mundra", "ISC", 1, "Mundra|Adani Mundra", "India's largest container port by volume"),
    ("INPAV", "Pipavav", "ISC", 2, "Pipavav|Port Pipavav", "APM Terminals Pipavav, Gujarat"),
    ("INHZA", "Hazira", "ISC", 2, "Hazira|Surat", "Adani Hazira, Gujarat"),
    ("INMAA", "Chennai", "ISC", 2, "Chennai|Madras", "South-east India gateway"),
    ("INKAT", "Kattupalli", "ISC", 2, "Kattupalli", "Adani Kattupalli, Chennai region"),
    ("INVTZ", "Visakhapatnam", "ISC", 3, "Visakhapatnam|Vizag", "East coast; regional and Colombo/Singapore feeders"),
    ("INCCU", "Kolkata", "ISC", 3, "Kolkata|Calcutta|Haldia|Syama Prasad Mookerjee Port", "River port with Haldia dock; feeder via Colombo/Singapore"),
    ("INCOK", "Kochi (Vallarpadam)", "ISC", 3, "Kochi|Cochin|Vallarpadam|ICTT", "DP World ICTT; Colombo competitor feeder hub"),
    ("INTUT", "Tuticorin (Thoothukudi)", "ISC", 3, "Tuticorin|Thoothukudi|VOC Port", "South India; Colombo feeders and some direct services"),
    ("PKKHI", "Karachi", "ISC", 2, "Karachi|KICT|SAPT|Keamari", "Pakistan's main gateway"),
    ("PKBQM", "Port Qasim", "ISC", 2, "Port Qasim|Muhammad Bin Qasim|QICT", "Karachi region's second port"),
    ("BDCGP", "Chittagong", "ISC", 3, "Chittagong|Chattogram", "Bangladesh; feeder via Colombo/Singapore/Port Klang"),
    ("BDMGL", "Mongla", "ISC", 3, "Mongla|Chalna", "Bangladesh's second port; feeder"),
    ("MVMLE", "Malé", "ISC", 3, "Male|Malé|Hulhumale|Thilafushi", "Maldives; feeder via Colombo"),
    # ---- MEG: Middle East Gulf / Arabian Sea ----
    ("AEJEA", "Jebel Ali", "MEG", 1, "Jebel Ali|Dubai|DP World Jebel Ali", "Middle East's main hub"),
    ("AEKHL", "Abu Dhabi (Khalifa Port)", "MEG", 2, "Khalifa Port|Abu Dhabi|KPCT|Kizad", "Abu Dhabi mainline gateway"),
    ("AEKLF", "Khor Fakkan", "MEG", 2, "Khor Fakkan|Khawr Fakkan", "Transshipment hub outside the Strait of Hormuz"),
    ("AESHJ", "Sharjah (Port Khalid)", "MEG", 3, "Sharjah|Port Khalid|Mina Khalid", "Regional/feeder"),
    ("QAHMD", "Hamad (Doha)", "MEG", 2, "Hamad Port|Doha|Qatar", "Qatar's gateway"),
    ("BHKBS", "Khalifa Bin Salman (Bahrain)", "MEG", 3, "Khalifa Bin Salman|Bahrain|Hidd|Manama", "Bahrain; APM Terminals; mostly feeder"),
    ("KWSWK", "Shuwaikh (Kuwait)", "MEG", 3, "Shuwaikh|Kuwait|Kuwait City", "Kuwait; regional services"),
    ("IQUQR", "Umm Qasr", "MEG", 3, "Umm Qasr|Iraq", "Iraq's main container port; feeder via Jebel Ali and some direct calls"),
    ("OMSLL", "Salalah", "MEG", 1, "Salalah|Raysut", "Transshipment hub (APM Terminals)"),
    ("OMSOH", "Sohar", "MEG", 3, "Sohar|Suhar", "Northern Oman; regional services"),
    ("OMDQM", "Duqm", "MEG", 3, "Duqm", "Central Oman; limited services"),
    ("SADMM", "Dammam (King Abdulaziz Port)", "MEG", 2, "Dammam|King Abdulaziz Port", "Saudi Gulf gateway; rail to Riyadh"),
    ("SAJUB", "Jubail", "MEG", 3, "Jubail|Al Jubail", "Industrial port; feeder"),
    # ---- RSE: Red Sea ----
    ("SAJED", "Jeddah", "RSE", 1, "Jeddah|Jiddah|Jeddah Islamic Port", "Red Sea hub; Saudi west coast gateway"),
    ("SAKAC", "King Abdullah Port", "RSE", 2, "King Abdullah Port|King Abdullah Economic City|KAEC", "Red Sea mainline port"),
    ("EGSOK", "Ain Sokhna", "RSE", 2, "Sokhna|Ain Sokhna|Ain Sukhna", "Gulf of Suez; DP World Sokhna; serves Cairo"),
    ("SDPZU", "Port Sudan", "RSE", 3, "Port Sudan|Bur Sudan", "Sudan; feeder via Jeddah/Djibouti; civil-war disruption"),
    ("DJJIB", "Djibouti (Doraleh)", "RSE", 2, "Djibouti|Doraleh", "Gateway for Ethiopia; Doraleh container terminal"),
    ("ERMSW", "Massawa", "RSE", 3, "Massawa|Mitsiwa", "Eritrea; occasional feeder via Jeddah/Djibouti"),
    ("YEADE", "Aden", "RSE", 3, "Aden|Aden Container Terminal", "Yemen; feeder via Jebel Ali/Djibouti/Salalah; war-risk"),
    ("YEHOD", "Hodeidah", "RSE", 3, "Hodeidah|Hudaydah|Al Hudaydah", "Yemen Red Sea port; war-risk, UN inspection regime"),
    ("JOAQJ", "Aqaba", "RSE", 2, "Aqaba|Al Aqabah|Aqaba Container Terminal", "Jordan's only port"),
    # ---- MED: Mediterranean & Black Sea ----
    ("ESALG", "Algeciras", "MED", 1, "Algeciras", "Strait of Gibraltar transshipment hub"),
    ("ESVLC", "Valencia", "MED", 1, "Valencia|València", "Spain's largest container port; Madrid hinterland"),
    ("ESBCN", "Barcelona", "MED", 2, "Barcelona", "Catalonia gateway"),
    ("FRFOS", "Marseille-Fos", "MED", 2, "Fos-sur-Mer|Fos|Marseille", "French Med gateway (Fos 2XL, Seayard)"),
    ("ITGIT", "Gioia Tauro", "MED", 1, "Gioia Tauro", "MSC transshipment hub"),
    ("ITGOA", "Genoa", "MED", 2, "Genoa|Genova|Pra'|Voltri", "Italy's main gateway (PSA Pra', Spinelli)"),
    ("ITSPE", "La Spezia", "MED", 2, "La Spezia|LSCT", "Ligurian gateway"),
    ("ITVDL", "Vado Ligure", "MED", 2, "Vado Ligure|Vado Gateway|Savona", "APM Terminals Vado Gateway"),
    ("ITLIV", "Livorno", "MED", 2, "Livorno|Leghorn", "Tuscany gateway"),
    ("ITSAL", "Salerno", "MED", 3, "Salerno", "Southern Italy; Med services"),
    ("ITTRS", "Trieste", "MED", 2, "Trieste|Triest", "Adriatic mainline gateway; rail to Central Europe"),
    ("ITVCE", "Venice", "MED", 3, "Venice|Venezia|Porto Marghera", "Northern Adriatic; feeder/Med services"),
    ("MTMAR", "Marsaxlokk (Malta Freeport)", "MED", 1, "Marsaxlokk|Malta Freeport|Malta|Birzebbuga", "CMA CGM transshipment hub"),
    ("SIKOP", "Koper", "MED", 2, "Koper|Capodistria|Luka Koper", "Adriatic mainline gateway for Central Europe"),
    ("HRRJK", "Rijeka", "MED", 2, "Rijeka|Fiume|Rijeka Gateway", "Croatia's main container port"),
    ("HRPLE", "Ploče", "MED", 3, "Ploce|Ploče", "Southern Croatia; serves Bosnia and Herzegovina"),
    ("MEBAR", "Bar", "MED", 3, "Bar|Montenegro", "Montenegro; feeder"),
    ("ALDRZ", "Durrës", "MED", 3, "Durres|Durrës|Durazzo", "Albania; feeder via Piraeus/Gioia Tauro"),
    ("GRPIR", "Piraeus", "MED", 1, "Piraeus|Pireas|Athens|PCT", "COSCO East Med hub"),
    ("GRSKG", "Thessaloniki", "MED", 3, "Thessaloniki|Salonica", "Northern Greece; serves North Macedonia/Balkans"),
    ("TRAMR", "Ambarlı (Istanbul)", "MED", 2, "Ambarli|Ambarlı|Istanbul|Kumport|Marport", "Istanbul's container gateway"),
    ("TRMER", "Mersin", "MED", 2, "Mersin|MIP", "Turkey's largest single container port"),
    ("TRALI", "Aliağa (Izmir)", "MED", 2, "Aliaga|Aliağa|Nemrut|Izmir|TCEEGE", "Izmir region deep-water terminals"),
    ("TRGEM", "Gemlik", "MED", 2, "Gemlik|Borusan|Gemport", "Bursa region"),
    ("TRIZT", "Izmit (Kocaeli)", "MED", 2, "Izmit|Kocaeli|Evyap|Yarimca|DP World Yarimca|Derince", "Gulf of Izmit terminals (Evyap, DP World Yarımca, Belde)"),
    ("TRTEK", "Tekirdağ (Asyaport)", "MED", 2, "Tekirdag|Tekirdağ|Asyaport", "Asyaport deep-water terminal"),
    ("CYLMS", "Limassol", "MED", 3, "Limassol|Lemesos|Cyprus", "Cyprus; feeder and Med services"),
    ("ILHFA", "Haifa", "MED", 2, "Haifa|Haifa Bayport", "Northern Israel; Bayport terminal"),
    ("ILASH", "Ashdod", "MED", 2, "Ashdod|Southport", "Southern Israel"),
    ("LBBEY", "Beirut", "MED", 2, "Beirut|Bayrut", "Lebanon; CMA CGM terminal"),
    ("LBKYE", "Tripoli (Lebanon)", "MED", 3, "Tripoli|Tarabulus", "Northern Lebanon; feeder"),
    ("EGPSD", "Port Said", "MED", 1, "Port Said|East Port Said|SCCT|Suez Canal Container Terminal", "Suez Canal transshipment hub"),
    ("EGALY", "Alexandria", "MED", 2, "Alexandria|El Dekheila|Iskandariya", "Egypt's Med gateway (Alexandria and El Dekheila)"),
    ("EGDAM", "Damietta", "MED", 2, "Damietta|Dumyat", "East Nile Delta"),
    ("LYMIS", "Misrata", "MED", 3, "Misrata|Misurata|Misratah", "Libya's main container port; feeder"),
    ("LYTIP", "Tripoli (Libya)", "MED", 3, "Tripoli|Tarabulus", "Libya; feeder"),
    ("TNRDS", "Radès (Tunis)", "MED", 3, "Rades|Radès|Tunis", "Tunisia's main container port; feeder"),
    ("TNSFA", "Sfax", "MED", 3, "Sfax", "Southern Tunisia; feeder"),
    ("DZALG", "Algiers", "MED", 3, "Algiers|Alger|El Djazair", "Algeria; feeder and Med services"),
    ("DZORN", "Oran", "MED", 3, "Oran|Wahran", "Western Algeria; feeder"),
    ("MAPTM", "Tanger Med", "MED", 1, "Tanger Med|Tangier|Tanger", "Strait of Gibraltar transshipment hub"),
    ("ROCND", "Constanța", "MED", 2, "Constanta|Constanța|Constantza", "Black Sea gateway; Danube connection"),
    ("BGVAR", "Varna", "MED", 3, "Varna", "Bulgaria; Black Sea feeder"),
    ("GEPTI", "Poti", "MED", 3, "Poti", "Georgia; Black Sea feeder; Middle Corridor to the Caspian"),
    ("UAODS", "Odesa", "MED", 3, "Odesa|Odessa", "War-risk; services resumed via the Black Sea corridor"),
    ("UAILK", "Chornomorsk", "MED", 3, "Chornomorsk|Illichivsk|Ilyichevsk", "War-risk; services resumed via the Black Sea corridor"),
    # ---- NEU: North Europe ----
    ("NLRTM", "Rotterdam", "NEU", 1, "Rotterdam|Maasvlakte|Maasvlakte II", "Europe's largest container port"),
    ("BEANR", "Antwerp-Bruges", "NEU", 1, "Antwerp|Antwerpen|Anvers|Antwerp-Bruges|Deurganck", "Europe's second-largest container port"),
    ("DEHAM", "Hamburg", "NEU", 1, "Hamburg|Waltershof|Altenwerder", "North Range hub; Baltic feeder hub"),
    ("DEBRV", "Bremerhaven", "NEU", 1, "Bremerhaven|Bremen", "North Range hub"),
    ("DEWVN", "Wilhelmshaven", "NEU", 2, "Wilhelmshaven|JadeWeserPort|CTW", "Deep-water JadeWeserPort"),
    ("BEZEE", "Zeebrugge", "NEU", 2, "Zeebrugge|Bruges|Brugge", "Port of Antwerp-Bruges coastal terminals"),
    ("NLAMS", "Amsterdam", "NEU", 3, "Amsterdam", "Shortsea/feeder"),
    ("FRLEH", "Le Havre", "NEU", 2, "Le Havre|Port 2000", "France's main container port"),
    ("FRDKK", "Dunkirk", "NEU", 2, "Dunkirk|Dunkerque", "Northern France; mainline calls"),
    ("FRMTX", "Nantes Saint-Nazaire (Montoir)", "NEU", 3, "Montoir|Montoir-de-Bretagne|Saint-Nazaire|Nantes", "Atlantic France; feeder and some direct services"),
    ("GBFXT", "Felixstowe", "NEU", 2, "Felixstowe", "UK's largest container port"),
    ("GBSOU", "Southampton", "NEU", 2, "Southampton", "UK mainline gateway"),
    ("GBLGP", "London Gateway", "NEU", 2, "London Gateway|London", "DP World London Gateway"),
    ("GBLIV", "Liverpool", "NEU", 2, "Liverpool|Seaforth|Liverpool2", "Liverpool2; transatlantic services and feeder"),
    ("GBTIL", "Tilbury", "NEU", 3, "Tilbury|Port of Tilbury", "Thames; shortsea/feeder"),
    ("GBTEE", "Teesport", "NEU", 3, "Teesport|Middlesbrough|Teesside", "North East England; feeder/shortsea"),
    ("GBGRG", "Grangemouth", "NEU", 3, "Grangemouth", "Scotland's main container port; feeder"),
    ("GBBEL", "Belfast", "NEU", 3, "Belfast", "Northern Ireland; feeder"),
    ("IEDUB", "Dublin", "NEU", 3, "Dublin|Baile Átha Cliath", "Ireland's main port; feeder/shortsea"),
    ("IEORK", "Cork", "NEU", 3, "Cork|Ringaskiddy", "Southern Ireland; feeder"),
    ("DKAAR", "Aarhus", "NEU", 2, "Aarhus|Århus", "Denmark's main container port"),
    ("DKCPH", "Copenhagen", "NEU", 3, "Copenhagen|København|Kobenhavn", "Feeder via Hamburg/Bremerhaven/Rotterdam"),
    ("NOOSL", "Oslo", "NEU", 3, "Oslo|Sjursøya", "Norway's main container port; feeder"),
    ("NOBGO", "Bergen", "NEU", 3, "Bergen", "Western Norway; feeder"),
    ("NOSVG", "Stavanger", "NEU", 3, "Stavanger|Risavika", "Western Norway; feeder"),
    ("NOTRD", "Trondheim", "NEU", 3, "Trondheim", "Mid-Norway; feeder"),
    ("SEGOT", "Gothenburg", "NEU", 2, "Gothenburg|Göteborg|Goteborg|Skandia", "Scandinavia's largest port; direct mainline calls"),
    ("SEHEL", "Helsingborg", "NEU", 3, "Helsingborg|Hälsingborg", "Southern Sweden; feeder"),
    ("SENYN", "Stockholm Norvik", "NEU", 3, "Stockholm|Norvik|Nynäshamn", "Stockholm's container terminal; feeder"),
    ("SEGVX", "Gävle", "NEU", 3, "Gävle|Gavle", "Central Sweden; feeder"),
    ("PLGDN", "Gdańsk (Baltic Hub)", "NEU", 2, "Gdansk|Gdańsk|Danzig|Baltic Hub|DCT", "Baltic's deep-sea mainline port"),
    ("PLGDY", "Gdynia", "NEU", 3, "Gdynia|BCT|GCT", "Feeder/shortsea"),
    ("PLSZZ", "Szczecin", "NEU", 3, "Szczecin|Stettin|Świnoujście", "Feeder"),
    ("LTKLJ", "Klaipėda", "NEU", 3, "Klaipeda|Klaipėda|Memel", "Lithuania; feeder and some direct services"),
    ("LVRIX", "Riga", "NEU", 3, "Riga|Rīga", "Latvia; feeder"),
    ("EEMUG", "Tallinn (Muuga)", "NEU", 3, "Muuga|Tallinn", "Estonia; feeder"),
    ("FIHEL", "Helsinki (Vuosaari)", "NEU", 3, "Helsinki|Vuosaari|Helsingfors", "Feeder via Hamburg/Rotterdam/Antwerp/Bremerhaven/Gdańsk"),
    ("FIKTK", "Kotka (Mussalo)", "NEU", 3, "Kotka|Mussalo|HaminaKotka", "Port of HaminaKotka; Finland's largest container port"),
    ("FIHMN", "Hamina", "NEU", 3, "Hamina|Fredrikshamn|HaminaKotka", "Port of HaminaKotka"),
    ("FIRAU", "Rauma", "NEU", 3, "Rauma|Raumo", "West coast; forest-product exports"),
    ("FIOUL", "Oulu", "NEU", 3, "Oulu|Uleåborg|Oritkari", "Northern Finland; feeder"),
    ("FIKOK", "Kokkola", "NEU", 3, "Kokkola|Karleby", "Feeder"),
    ("FIKEM", "Kemi", "NEU", 3, "Kemi|Ajos", "Feeder"),
    ("ISREY", "Reykjavík", "NEU", 3, "Reykjavik|Reykjavík|Sundahöfn", "Iceland; Eimskip/Samskip from Northern Europe"),
    ("FOTHO", "Tórshavn", "NEU", 3, "Torshavn|Tórshavn|Thorshavn|Faroe Islands", "Faroe Islands; shortsea from Northern Europe"),
    ("GLGOH", "Nuuk", "NEU", 3, "Nuuk|Godthåb|Greenland", "Greenland; Royal Arctic Line from Aalborg/Reykjavík"),
    ("SJLYR", "Longyearbyen", "NEU", 3, "Longyearbyen|Svalbard", "Svalbard; supply services from Tromsø"),
    ("ESBIO", "Bilbao", "NEU", 3, "Bilbao", "Spanish Atlantic; feeder and some direct services"),
    ("ESVGO", "Vigo", "NEU", 3, "Vigo", "Galicia; feeder/shortsea"),
    ("PTSIE", "Sines", "NEU", 2, "Sines|Terminal XXI", "Portugal's deep-sea mainline port"),
    ("PTLEI", "Leixões (Porto)", "NEU", 3, "Leixoes|Leixões|Porto|Oporto", "Northern Portugal; feeder/shortsea"),
    ("PTLIS", "Lisbon", "NEU", 3, "Lisbon|Lisboa", "Feeder/shortsea; Atlantic islands services"),
    ("PTCNL", "Caniçal (Madeira)", "NEU", 3, "Canical|Caniçal|Madeira|Funchal", "Madeira; shortsea from Lisbon/Leixões"),
    ("PTPDL", "Ponta Delgada (Azores)", "NEU", 3, "Ponta Delgada|Azores|Açores", "Azores; shortsea from Lisbon/Leixões"),
    # ---- WAF: West & Central Africa ----
    ("TGLFW", "Lomé", "WAF", 1, "Lomé|Lome|LCT", "West Africa transshipment hub (Lomé Container Terminal)"),
    ("MACAS", "Casablanca", "WAF", 3, "Casablanca|Dar el Beida", "Morocco Atlantic; feeder via Tanger Med/Algeciras"),
    ("MAAGA", "Agadir", "WAF", 3, "Agadir", "Southern Morocco; reefer/feeder"),
    ("ESLPA", "Las Palmas", "WAF", 2, "Las Palmas|Gran Canaria|Canary Islands|La Luz", "Canary Islands (Spain); West Africa transshipment hub"),
    ("MRNKC", "Nouakchott", "WAF", 3, "Nouakchott", "Mauritania; feeder via Las Palmas/Tanger Med/Dakar"),
    ("SNDKR", "Dakar", "WAF", 2, "Dakar", "Senegal; serves Mali"),
    ("GMBJL", "Banjul", "WAF", 3, "Banjul", "The Gambia; feeder"),
    ("GWOXB", "Bissau", "WAF", 3, "Bissau", "Guinea-Bissau; feeder"),
    ("GNCKY", "Conakry", "WAF", 3, "Conakry", "Guinea; feeder and some direct services"),
    ("SLFNA", "Freetown", "WAF", 3, "Freetown", "Sierra Leone; feeder"),
    ("LRMLW", "Monrovia", "WAF", 3, "Monrovia", "Liberia; feeder"),
    ("CIABJ", "Abidjan", "WAF", 2, "Abidjan|Vridi|TC2", "Côte d'Ivoire; serves Burkina Faso and Mali"),
    ("CISPY", "San-Pédro", "WAF", 3, "San Pedro|San-Pédro", "Western Côte d'Ivoire; cocoa"),
    ("GHTEM", "Tema", "WAF", 2, "Tema|Accra|MPS", "Ghana; Meridian Port Services; serves Burkina Faso"),
    ("GHTKD", "Takoradi", "WAF", 3, "Takoradi|Sekondi-Takoradi", "Western Ghana"),
    ("BJCOO", "Cotonou", "WAF", 2, "Cotonou", "Benin; serves Niger"),
    ("NGLOS", "Lagos (Apapa)", "WAF", 2, "Lagos|Apapa|APMT Apapa", "Lagos port complex; carriers file Apapa as NGAPP, which is in neither the dataset nor the WPI"),
    ("NGTIN", "Lagos (Tin Can Island)", "WAF", 2, "Tin Can|Tin Can Island", "Lagos port complex"),
    ("NGLKK", "Lekki", "WAF", 2, "Lekki|Lekki Deep Sea Port", "Deep-water port east of Lagos"),
    ("NGONN", "Onne", "WAF", 2, "Onne", "Eastern Nigeria; oil and gas hub"),
    ("CMDLA", "Douala", "WAF", 2, "Douala|Duala", "Cameroon; serves Chad and Central African Republic"),
    ("CMKBI", "Kribi", "WAF", 2, "Kribi|Kribi Deep Sea Port", "Cameroon deep-water port"),
    ("GQSSG", "Malabo", "WAF", 3, "Malabo", "Equatorial Guinea; feeder"),
    ("GAOWE", "Libreville (Owendo)", "WAF", 3, "Owendo|Libreville", "Gabon; feeder"),
    ("STTMS", "São Tomé", "WAF", 3, "Sao Tome|São Tomé", "São Tomé and Príncipe; feeder"),
    ("CGPNR", "Pointe-Noire", "WAF", 2, "Pointe-Noire|Pointe Noire", "Congo; transshipment hub; serves DR Congo"),
    ("CDMAT", "Matadi", "WAF", 3, "Matadi", "DR Congo; Congo River port"),
    ("AOLAD", "Luanda", "WAF", 2, "Luanda", "Angola's main gateway"),
    ("AOLOB", "Lobito", "WAF", 3, "Lobito", "Angola; Lobito Corridor to DR Congo/Zambia"),
    ("CVRAI", "Praia", "WAF", 3, "Praia|Cabo Verde|Cape Verde", "Cape Verde; feeder"),
    # ---- SAF: Southern Africa ----
    ("ZADUR", "Durban", "SAF", 1, "Durban", "Sub-Saharan Africa's busiest container port; serves Gauteng"),
    ("ZACPT", "Cape Town", "SAF", 2, "Cape Town|Kaapstad", "Western Cape; reefer"),
    ("ZAZBA", "Ngqura (Coega)", "SAF", 2, "Ngqura|Coega", "Deep-water transshipment terminal"),
    ("ZAPLZ", "Gqeberha (Port Elizabeth)", "SAF", 3, "Port Elizabeth|Gqeberha", "Eastern Cape"),
    ("NAWVB", "Walvis Bay", "SAF", 2, "Walvis Bay", "Namibia; corridors to Zambia/Botswana"),
    ("MZMPM", "Maputo", "SAF", 3, "Maputo|Lourenço Marques", "Mozambique; serves Gauteng and Eswatini"),
    ("SHSHN", "Jamestown (St Helena)", "SAF", 3, "Jamestown|St Helena|Saint Helena", "Monthly cargo ship from Cape Town"),
    # ---- EAF: East Africa & Indian Ocean islands ----
    ("KEMBA", "Mombasa", "EAF", 2, "Mombasa|Kilindini", "Kenya; serves Uganda/Rwanda/South Sudan"),
    ("TZDAR", "Dar es Salaam", "EAF", 2, "Dar es Salaam|DSM", "Tanzania; serves Zambia/Malawi/DR Congo"),
    ("TZZNZ", "Zanzibar", "EAF", 3, "Zanzibar", "Zanzibar; feeder"),
    ("MZBEW", "Beira", "EAF", 3, "Beira", "Central Mozambique; Beira Corridor to Zimbabwe/Malawi"),
    ("MZMNC", "Nacala", "EAF", 3, "Nacala", "Northern Mozambique; Nacala Corridor to Malawi"),
    ("MGTOA", "Toamasina", "EAF", 3, "Toamasina|Tamatave", "Madagascar's main port; feeder via Port Louis/Réunion"),
    ("MUPLU", "Port Louis", "EAF", 2, "Port Louis|Mauritius", "Mauritius; Indian Ocean transshipment hub"),
    ("RELPT", "Port Réunion (Le Port)", "EAF", 3, "Le Port|Port Réunion|Pointe des Galets|Réunion", "Réunion (France); CMA CGM regional hub"),
    ("YTLON", "Longoni (Mayotte)", "EAF", 3, "Longoni|Mayotte", "Mayotte (France); feeder"),
    ("SCPOV", "Port Victoria", "EAF", 3, "Victoria|Mahé|Port Victoria|Seychelles", "Seychelles; feeder"),
    ("SOMGQ", "Mogadishu", "EAF", 3, "Mogadishu|Muqdisho", "Somalia; feeder via Salalah/Jebel Ali"),
    ("SOBBO", "Berbera", "EAF", 3, "Berbera|Somaliland", "Somaliland; DP World Berbera; Ethiopia corridor"),
    ("KMMUT", "Mutsamudu", "EAF", 3, "Mutsamudu|Anjouan|Comoros", "Comoros; feeder"),
    # ---- NAW: North America West Coast ----
    ("USLAX", "Los Angeles", "NAW", 1, "Los Angeles|San Pedro|LA", "US's busiest container port (San Pedro Bay)"),
    ("USLGB", "Long Beach", "NAW", 1, "Long Beach", "San Pedro Bay complex"),
    ("USOAK", "Oakland", "NAW", 2, "Oakland|San Francisco Bay", "Northern California gateway"),
    ("USSEA", "Seattle", "NAW", 2, "Seattle|Northwest Seaport Alliance", "Northwest Seaport Alliance"),
    ("USTIW", "Tacoma", "NAW", 2, "Tacoma|Northwest Seaport Alliance", "Northwest Seaport Alliance"),
    ("USHNL", "Honolulu", "NAW", 3, "Honolulu|Hawaii", "Hawaii; Jones Act services from the US West Coast"),
    ("USANC", "Anchorage", "NAW", 3, "Anchorage|Alaska", "Alaska; Jones Act services from Tacoma"),
    ("CAVAN", "Vancouver", "NAW", 2, "Vancouver|Deltaport|Centerm", "Canada's largest port"),
    ("CAPRR", "Prince Rupert", "NAW", 2, "Prince Rupert|Fairview", "Rail gateway to Chicago/Midwest"),
    ("MXZLO", "Manzanillo", "NAW", 2, "Manzanillo", "Mexico's largest container port"),
    ("MXLZC", "Lázaro Cárdenas", "NAW", 2, "Lazaro Cardenas|Lázaro Cárdenas", "Rail gateway to central Mexico"),
    ("MXESE", "Ensenada", "NAW", 3, "Ensenada", "Baja California"),
    # ---- NAE: North America East Coast ----
    ("USNYC", "New York / New Jersey", "NAE", 1, "New York|Newark|Port Elizabeth|NY/NJ|Port Newark", "US East Coast's largest port"),
    ("USSAV", "Savannah", "NAE", 1, "Savannah|Garden City", "US East Coast hub"),
    ("USCHS", "Charleston", "NAE", 2, "Charleston|Wando Welch|Leatherman", "South Carolina"),
    ("USORF", "Norfolk (Virginia)", "NAE", 2, "Norfolk|Virginia|Portsmouth|Hampton Roads", "Port of Virginia"),
    ("USBAL", "Baltimore", "NAE", 2, "Baltimore|Seagirt", "Mid-Atlantic"),
    ("USPHL", "Philadelphia", "NAE", 3, "Philadelphia|Packer Avenue", "Delaware River; reefer and selected services"),
    ("USJAX", "Jacksonville", "NAE", 2, "Jacksonville|JAXPORT|Blount Island|Dames Point", "North Florida; Asia services and Puerto Rico trade"),
    ("USBOS", "Boston", "NAE", 3, "Boston|Conley Terminal", "New England"),
    ("USMIA", "Miami", "NAE", 2, "Miami|PortMiami", "South Florida; Latin America/Caribbean"),
    ("USPEF", "Port Everglades", "NAE", 2, "Port Everglades|Fort Lauderdale", "South Florida; Caribbean/Central America"),
    ("CAMTR", "Montréal", "NAE", 2, "Montreal|Montréal", "St Lawrence; transatlantic services; rail to Toronto/Chicago"),
    ("CAHAL", "Halifax", "NAE", 2, "Halifax", "Deep-water Atlantic gateway"),
    ("CASJF", "St. John's (Newfoundland)", "NAE", 3, "St. John's|St Johns|Newfoundland", "Newfoundland; feeder/coastal"),
    ("PMFSP", "Saint-Pierre", "NAE", 3, "Saint-Pierre|Saint-Pierre-et-Miquelon", "Saint-Pierre and Miquelon (France); feeder from Halifax"),
    # ---- USG: US Gulf & Mexico Gulf ----
    ("USHOU", "Houston", "USG", 1, "Houston|Barbours Cut|Bayport", "US Gulf's largest container port"),
    ("USMSY", "New Orleans", "USG", 2, "New Orleans|Napoleon Avenue|NOLA", "Mississippi River gateway"),
    ("USMOB", "Mobile", "USG", 2, "Mobile", "APM Terminals Mobile"),
    ("USFPO", "Freeport (Texas)", "USG", 3, "Port Freeport|Freeport TX", "Texas; Central America services"),
    ("USGPT", "Gulfport", "USG", 3, "Gulfport", "Central America reefer services"),
    ("USTPA", "Tampa", "USG", 3, "Tampa|Port Tampa Bay", "West Florida"),
    ("MXVER", "Veracruz", "USG", 2, "Veracruz", "Mexico Gulf gateway"),
    ("MXATM", "Altamira", "USG", 2, "Altamira", "Mexico Gulf; serves Monterrey"),
    ("MXPGO", "Progreso", "USG", 3, "Progreso|Puerto Progreso", "Yucatán"),
    # ---- CAR: Caribbean & Central America ----
    ("PAONX", "Colón (MIT / CCT)", "CAR", 1, "Colon|Colón|Manzanillo International Terminal|MIT|Colon Container Terminal|CCT", "Atlantic-side Panama transshipment hub; carriers file MIT as PAMIT, which is in neither the dataset nor the WPI"),
    ("PACTB", "Cristóbal", "CAR", 2, "Cristobal|Cristóbal|Panama Ports Cristobal", "Atlantic side, Panama Canal"),
    ("PABLB", "Balboa", "CAR", 1, "Balboa|Panama City|Rodman|PSA Panama", "Pacific-side Panama transshipment hub"),
    ("COCTG", "Cartagena", "CAR", 1, "Cartagena|Contecar|SPRC", "Caribbean transshipment hub"),
    ("COBAQ", "Barranquilla", "CAR", 3, "Barranquilla", "Colombia Caribbean; feeder"),
    ("VEPBL", "Puerto Cabello", "CAR", 3, "Puerto Cabello", "Venezuela's main container port; feeder"),
    ("GYGEO", "Georgetown", "CAR", 3, "Georgetown|Guyana|Demerara", "Guyana; feeder via Port of Spain/Kingston"),
    ("SRPBM", "Paramaribo", "CAR", 3, "Paramaribo|Suriname", "Suriname; feeder"),
    ("GFDDC", "Dégrad des Cannes", "CAR", 3, "Degrad des Cannes|Cayenne|French Guiana", "French Guiana; feeder"),
    ("GTPRQ", "Puerto Quetzal", "CAR", 2, "Puerto Quetzal|Quetzal", "Guatemala Pacific coast"),
    ("GTSTC", "Santo Tomás de Castilla", "CAR", 3, "Santo Tomas de Castilla|Santo Tomás", "Guatemala Caribbean coast"),
    ("HNPCR", "Puerto Cortés", "CAR", 2, "Puerto Cortes|Puerto Cortés", "Honduras; Central America gateway"),
    ("SVAQJ", "Acajutla", "CAR", 3, "Acajutla", "El Salvador Pacific"),
    ("NICIO", "Corinto", "CAR", 3, "Corinto", "Nicaragua Pacific"),
    ("CRMOB", "Moín", "CAR", 2, "Moin|Moín|Limón|Puerto Limón|APM Terminals Moín", "Costa Rica Caribbean; APM Terminals Moín"),
    ("CRCAL", "Caldera", "CAR", 3, "Caldera|Puerto Caldera", "Costa Rica Pacific"),
    ("BZBZE", "Belize City", "CAR", 3, "Belize City|Belize", "Belize; feeder"),
    ("JMKIN", "Kingston", "CAR", 1, "Kingston|KFTL|Kingston Freeport", "Caribbean transshipment hub (CMA CGM)"),
    ("DOCAU", "Caucedo", "CAR", 2, "Caucedo|Punta Caucedo|DP World Caucedo", "Dominican Republic; Caribbean transshipment hub"),
    ("DOINA", "Rio Haina", "CAR", 3, "Rio Haina|Haina|Bajos de Haina", "Dominican Republic; feeder"),
    ("HTPAP", "Port-au-Prince", "CAR", 3, "Port-au-Prince|Port au Prince|Haiti", "Haiti; feeder; security disruption"),
    ("CUMAR", "Mariel", "CAR", 3, "Mariel|Havana|La Habana", "Cuba; feeder via Kingston/Caucedo; US embargo"),
    ("BSFPO", "Freeport (Grand Bahama)", "CAR", 2, "Freeport|Grand Bahama|Freeport Container Port", "MSC transshipment hub"),
    ("BSNAS", "Nassau", "CAR", 3, "Nassau|Arawak Cay", "Bahamas; feeder from Florida"),
    ("TTPOS", "Port of Spain", "CAR", 3, "Port of Spain|Trinidad", "Trinidad and Tobago; southern Caribbean feeder hub"),
    ("BBBGI", "Bridgetown", "CAR", 3, "Bridgetown|Barbados", "Barbados; feeder"),
    ("LCCAS", "Castries", "CAR", 3, "Castries|Saint Lucia", "Saint Lucia; feeder"),
    ("VCKTN", "Kingstown", "CAR", 3, "Kingstown|Saint Vincent", "St Vincent and the Grenadines; feeder"),
    ("GDSTG", "St. George's", "CAR", 3, "St. George's|St Georges|Grenada", "Grenada; feeder"),
    ("DMRSU", "Roseau", "CAR", 3, "Roseau|Woodbridge Bay|Dominica", "Dominica; feeder"),
    ("AGSJO", "St. John's (Antigua)", "CAR", 3, "St. John's|St Johns|Antigua", "Antigua and Barbuda; feeder"),
    ("KNBAS", "Basseterre", "CAR", 3, "Basseterre|Saint Kitts|Nevis", "St Kitts and Nevis; feeder"),
    ("PRSJU", "San Juan", "CAR", 3, "San Juan|Puerto Rico", "Puerto Rico; Jones Act services from Jacksonville; feeder"),
    ("CWWIL", "Willemstad", "CAR", 3, "Willemstad|Curaçao|Curacao", "Curaçao; feeder"),
    ("AWBAR", "Barcadera (Aruba)", "CAR", 3, "Barcadera|Oranjestad|Aruba", "Aruba's container port; feeder"),
    ("BQKRA", "Kralendijk", "CAR", 3, "Kralendijk|Bonaire", "Bonaire (Caribbean Netherlands); feeder"),
    ("SXPHI", "Philipsburg", "CAR", 3, "Philipsburg|Sint Maarten|St Maarten", "Sint Maarten; north-eastern Caribbean feeder hub"),
    ("MQFDF", "Fort-de-France", "CAR", 3, "Fort-de-France|Martinique", "Martinique (France); direct services from Le Havre"),
    ("GPPTP", "Pointe-à-Pitre (Jarry)", "CAR", 3, "Pointe-à-Pitre|Pointe a Pitre|Jarry|Guadeloupe", "Guadeloupe (France)"),
    ("VISTT", "St. Thomas", "CAR", 3, "Saint Thomas|Charlotte Amalie|US Virgin Islands", "US Virgin Islands; feeder"),
    ("VGRAD", "Road Town", "CAR", 3, "Road Town|Tortola|British Virgin Islands", "BVI; feeder"),
    ("KYGEC", "George Town (Grand Cayman)", "CAR", 3, "George Town|Grand Cayman|Cayman Islands", "Cayman Islands; feeder from Florida"),
    ("TCPLS", "Providenciales", "CAR", 3, "Providenciales|Turks and Caicos", "Turks and Caicos; feeder from Florida"),
    ("BMBDA", "Hamilton (Bermuda)", "CAR", 3, "Hamilton|Bermuda", "Bermuda; weekly service from New York/New Jersey"),
    # ---- SAE: South America East Coast ----
    ("BRSSZ", "Santos", "SAE", 1, "Santos|São Paulo|Sao Paulo|BTP|Santos Brasil", "South America's largest container port"),
    ("BRPNG", "Paranaguá", "SAE", 2, "Paranagua|Paranaguá|TCP", "Paraná; also serves Paraguay"),
    ("BRIOA", "Itapoá", "SAE", 2, "Itapoa|Itapoá", "Santa Catarina"),
    ("BRITJ", "Itajaí", "SAE", 2, "Itajai|Itajaí", "Santa Catarina"),
    ("BRNVT", "Navegantes", "SAE", 2, "Navegantes|Portonave", "Itajaí River, Santa Catarina"),
    ("BRRIG", "Rio Grande", "SAE", 2, "Rio Grande|Tecon Rio Grande", "Southern Brazil"),
    ("BRRIO", "Rio de Janeiro", "SAE", 2, "Rio de Janeiro|Rio|MultiRio|ICTSI Rio", "Rio de Janeiro terminals"),
    ("BRSPB", "Itaguaí (Sepetiba)", "SAE", 2, "Itaguai|Itaguaí|Sepetiba|Sepetiba Tecon", "Rio de Janeiro state deep-water terminal"),
    ("BRVIX", "Vitória", "SAE", 3, "Vitoria|Vitória|TVV", "Espírito Santo"),
    ("BRSSA", "Salvador", "SAE", 2, "Salvador|Tecon Salvador", "Bahia"),
    ("BRSUA", "Suape", "SAE", 2, "Suape", "North-east Brazil"),
    ("BRPEC", "Pecém", "SAE", 2, "Pecem|Pecém", "North-east Brazil"),
    ("BRMAO", "Manaus", "SAE", 3, "Manaus|Chibatão", "Amazon River port; cabotage and some direct services"),
    ("UYMVD", "Montevideo", "SAE", 2, "Montevideo|TCP Montevideo", "Uruguay; River Plate transshipment, serves Paraguay"),
    ("ARBUE", "Buenos Aires", "SAE", 2, "Buenos Aires|Exolgan|Dock Sud|Terminal 4|TRP", "Argentina's main gateway"),
    ("ARZAE", "Zárate", "SAE", 3, "Zarate|Zárate", "Paraná River"),
    ("FKPSY", "Stanley", "SAE", 3, "Stanley|Port Stanley|Falkland Islands", "Falkland Islands; monthly service from the UK"),
    # ---- SAW: South America West Coast ----
    ("CLSAI", "San Antonio", "SAW", 2, "San Antonio|Santiago", "Chile's largest container port"),
    ("CLVAP", "Valparaíso", "SAW", 2, "Valparaiso|Valparaíso", "Central Chile"),
    ("CLSVE", "San Vicente", "SAW", 3, "San Vicente|Talcahuano", "Biobío region"),
    ("CLCNL", "Coronel", "SAW", 3, "Coronel", "Biobío region"),
    ("CLIQQ", "Iquique", "SAW", 3, "Iquique", "Northern Chile; free zone"),
    ("CLARI", "Arica", "SAW", 3, "Arica", "Northern Chile; Bolivian transit cargo"),
    ("CLANF", "Antofagasta", "SAW", 3, "Antofagasta", "Northern Chile; mining"),
    ("CLPMC", "Puerto Montt", "SAW", 3, "Puerto Montt", "Southern Chile; salmon"),
    ("PECLL", "Callao", "SAW", 2, "Callao|Lima|DP World Callao|APM Callao", "Peru's main gateway"),
    ("PECHY", "Chancay", "SAW", 2, "Chancay", "COSCO deep-water hub north of Lima (opened 2024)"),
    ("PEPAI", "Paita", "SAW", 3, "Paita", "Northern Peru"),
    ("PEMRI", "Matarani", "SAW", 3, "Matarani", "Southern Peru"),
    ("ECGYE", "Guayaquil", "SAW", 2, "Guayaquil|Contecon|Naportec", "Ecuador; river terminals"),
    ("ECPSJ", "Posorja", "SAW", 2, "Posorja|DP World Posorja", "Ecuador deep-water port"),
    ("ECMEC", "Manta", "SAW", 3, "Manta", "Ecuador"),
    ("ECPBO", "Puerto Bolívar", "SAW", 3, "Puerto Bolivar|Machala", "Ecuador; banana reefer port"),
    ("COBUN", "Buenaventura", "SAW", 2, "Buenaventura", "Colombia's Pacific gateway"),
    # ---- OCE: Oceania ----
    ("AUMEL", "Melbourne", "OCE", 2, "Melbourne|Swanson Dock|Webb Dock", "Australia's largest container port"),
    ("AUSYD", "Sydney (Port Botany)", "OCE", 2, "Sydney|Port Botany|Botany", "New South Wales"),
    ("AUBNE", "Brisbane", "OCE", 2, "Brisbane|Fisherman Islands", "Queensland"),
    ("AUFRE", "Fremantle", "OCE", 2, "Fremantle|Perth", "Western Australia"),
    ("AUADL", "Adelaide", "OCE", 3, "Adelaide|Outer Harbor|Flinders", "South Australia"),
    ("AUBWT", "Burnie", "OCE", 3, "Burnie|Tasmania", "Tasmania; Bass Strait services to Melbourne"),
    ("AUTSV", "Townsville", "OCE", 3, "Townsville", "North Queensland"),
    ("AUDRW", "Darwin", "OCE", 3, "Darwin", "Northern Australia; feeder from Singapore"),
    ("NZAKL", "Auckland", "OCE", 2, "Auckland", "New Zealand North Island"),
    ("NZTRG", "Tauranga", "OCE", 2, "Tauranga|Mount Maunganui", "New Zealand's largest container port"),
    ("NZWLG", "Wellington", "OCE", 3, "Wellington|CentrePort", "Lower North Island"),
    ("NZNPE", "Napier", "OCE", 3, "Napier|Hawke's Bay", "East coast North Island"),
    ("NZLYT", "Lyttelton", "OCE", 3, "Lyttelton|Christchurch", "South Island"),
    ("NZPOE", "Port Chalmers (Otago)", "OCE", 3, "Port Chalmers|Dunedin|Otago", "South Island"),
    ("FJSUV", "Suva", "OCE", 3, "Suva", "Fiji; regional Pacific hub"),
    ("FJLTK", "Lautoka", "OCE", 3, "Lautoka", "Western Viti Levu"),
    ("PGLAE", "Lae", "OCE", 3, "Lae", "PNG's main container port"),
    ("PGPOM", "Port Moresby", "OCE", 3, "Port Moresby|Motukea", "PNG capital; Motukea terminal"),
    ("SBHIR", "Honiara", "OCE", 3, "Honiara|Solomon Islands", "Solomon Islands; feeder"),
    ("VUVLI", "Port Vila", "OCE", 3, "Port Vila|Vanuatu", "Vanuatu; feeder"),
    ("NCNOU", "Nouméa", "OCE", 3, "Noumea|Nouméa|New Caledonia", "New Caledonia (France)"),
    ("PFPPT", "Papeete", "OCE", 3, "Papeete|Tahiti|French Polynesia", "French Polynesia (France)"),
    ("WSAPW", "Apia", "OCE", 3, "Apia|Samoa", "Samoa; feeder"),
    ("TOTBU", "Nuku'alofa", "OCE", 3, "Nukualofa|Nuku'alofa|Tonga", "Tonga; feeder"),
    ("KITRW", "Tarawa (Betio)", "OCE", 3, "Tarawa|Betio|Kiribati", "Kiribati; feeder"),
    ("TVFUN", "Funafuti", "OCE", 3, "Funafuti|Tuvalu", "Tuvalu; feeder"),
    ("NRINU", "Nauru (Aiwo)", "OCE", 3, "Nauru|Aiwo", "Nauru; feeder"),
    ("MHMAJ", "Majuro", "OCE", 3, "Majuro|Marshall Islands", "Marshall Islands; feeder"),
    ("FMPNI", "Pohnpei", "OCE", 3, "Pohnpei|Kolonia|Micronesia", "Federated States of Micronesia; feeder"),
    ("PWROR", "Koror (Malakal)", "OCE", 3, "Koror|Malakal|Palau", "Palau; feeder via Guam/Asia"),
    ("GUAPR", "Guam (Apra Harbor)", "OCE", 3, "Guam|Apra Harbor|Apra", "Guam (US); Micronesia hub"),
    ("MPSPN", "Saipan", "OCE", 3, "Saipan|Northern Mariana Islands", "Northern Mariana Islands (US); feeder via Guam"),
    ("ASPPG", "Pago Pago", "OCE", 3, "Pago Pago|American Samoa", "American Samoa (US)"),
    ("CKRAR", "Rarotonga (Avatiu)", "OCE", 3, "Rarotonga|Avatiu|Cook Islands", "Cook Islands; feeder from Auckland"),
    ("WFMAU", "Mata-Utu", "OCE", 3, "Mata-Utu|Wallis|Wallis and Futuna", "Wallis and Futuna (France); feeder"),
    ("NUALO", "Alofi", "OCE", 3, "Alofi|Niue", "Niue; feeder from Auckland"),
]

# Sanity check cities: (name, lat, lon).
SANITY = [("Ouagadougou", 12.37, -1.52), ("Lagos", 6.45, 3.39), ("Lima", -12.05, -77.04), ("Nairobi", -1.29, 36.82),
          ("Belo Horizonte", -19.92, -43.94), ("Chengdu", 30.66, 104.06), ("Kuopio", 62.89, 27.68),
          ("Denver", 39.74, -104.99), ("Hobart", -42.88, 147.33), ("Ulaanbaatar", 47.92, 106.92)]

SIZE_RANK = {"Large": 4, "Medium": 3, "Small": 2, "Very Small": 1}


def fold(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)).lower().strip()


def km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def load_wpi(cache: Path | None) -> list[dict]:
    for p in ([cache / "wpi.csv"] if cache else []) + [Path("/tmp/wpi.csv")]:
        if p.exists():
            return list(csv.DictReader(io.StringIO(p.read_text(encoding="utf-8-sig"))))
    r = httpx.get(WPI_URL, timeout=60, follow_redirects=True)
    r.raise_for_status()
    text = r.content.decode("utf-8-sig")
    if cache:
        (cache / "wpi.csv").write_text(text, encoding="utf-8")
    return list(csv.DictReader(io.StringIO(text)))


def wpi_pick(rows: list[dict], names: list[str]) -> dict:
    """Several WPI rows can share a LOCODE (e.g. UAILK is Illichivsk and a Crimean anchorage): prefer the row whose
    name matches ours, then the larger harbour."""
    want = {fold(n) for n in names}

    def score(w):
        cand = [fold(w["Main Port Name"])] + [fold(x) for x in (w["Alternate Port Name"] or "").split(";")]
        cand = [c for c in cand if c]
        exact = any(c in want for c in cand)
        part = any(len(c) >= 4 and len(n) >= 4 and (c in n or n in c) for c in cand for n in want)
        return (2 if exact else 1 if part else 0, SIZE_RANK.get(w["Harbor Size"].strip(), 0))

    return max(rows, key=score)


def main():
    cache = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)

    ds = json.loads((HERE / "locations_unlocode.json").read_text(encoding="utf-8"))
    cols = ds["columns"]
    ds_rows = [dict(zip(cols, r)) for r in ds["rows"]]
    ds_port = {r["code"]: r for r in ds_rows if r["code"] == r["unlocode"]}  # the LOCODE's own row
    ds_any: dict[str, dict] = {}
    for r in ds_rows:
        if r["unlocode"]:
            ds_any.setdefault(r["unlocode"], r)  # e.g. an airport row that carries the city's LOCODE
    curated = {c["code"]: c for c in json.loads((HERE / "locations_curated.json").read_text(encoding="utf-8"))["locations"]}

    wpi_rows = load_wpi(cache)
    wpi: dict[str, list[dict]] = {}
    for w in wpi_rows:
        lc = (w["UN/LOCODE"] or "").replace(" ", "").upper()
        if len(lc) == 5:
            wpi.setdefault(lc, []).append(w)

    problems = []
    codes = [p[0] for p in PORTS]
    dupes = sorted({c for c in codes if codes.count(c) > 1})
    if dupes:
        problems.append(f"duplicate LOCODEs: {dupes}")

    ports, missing, far, src_count, airport_coord = [], [], [], {}, []
    for code, name, region, tier, aliases, notes in PORTS:
        cc = code[:2]
        if region not in REGIONS:
            problems.append(f"{code}: unknown region {region}")
        elif cc not in REGION_COUNTRIES[region]:
            problems.append(f"{code}: country {cc} not allowed in region {region}")
        if tier not in (1, 2, 3):
            problems.append(f"{code}: bad tier {tier}")
        if cc in EXCLUDED_CC or cc in LANDLOCKED:
            problems.append(f"{code}: country {cc} is excluded/landlocked")
        if code not in ds_any and code not in wpi:
            missing.append(f"{code} {name}")
            continue
        alias_list = [a for a in aliases.split("|") if a and a != name]
        names = [name, name.split(" (")[0]] + alias_list
        w = wpi_pick(wpi[code], names) if code in wpi else None
        w_ll = (round(float(w["Latitude"]), 4), round(float(w["Longitude"]), 4)) if w else None
        d_row = ds_port.get(code)
        d_ll = (d_row["lat"], d_row["lon"]) if d_row else None
        if code in curated:
            ll, src = (curated[code]["lat"], curated[code]["lon"]), "curated"
        elif w_ll:
            ll, src = w_ll, "wpi"
        elif d_ll:
            ll, src = d_ll, "unlocode"
        else:
            r = ds_any[code]
            ll, src = (r["lat"], r["lon"]), "dataset-airport"
            airport_coord.append(f"{code} {name} (from {r['code']} {r['name']})")
        src_count[src] = src_count.get(src, 0) + 1
        if w_ll and d_ll:
            dist = km(*w_ll, *d_ll)
            if dist > 30:
                far.append((code, name, w["Main Port Name"], d_row["source"], round(dist), src))
        ports.append({"unlocode": code, "name": name, "country": cc, "region": region, "tier": tier,
                      "lat": round(ll[0], 4), "lon": round(ll[1], 4), "aliases": alias_list, "notes": notes})

    order = list(REGIONS)
    ports.sort(key=lambda p: (order.index(p["region"]), p["tier"], p["unlocode"]))
    covered = {p["country"] for p in ports}

    # Coverage: UN coastal states + every country with WPI container evidence + every country named in a region.
    wpi_evidence: dict[str, list[str]] = {}
    for lc, rows in wpi.items():
        for w in rows:
            if w["Facilities - Container"] == "Yes" or w["Cranes Container"] == "Yes" or w["Harbor Size"] == "Large":
                wpi_evidence.setdefault(lc[:2], []).append(w["Main Port Name"])
    region_cc = set().union(*REGION_COUNTRIES.values())
    should = (COASTAL_STATES | set(wpi_evidence) | region_cc) - LANDLOCKED - EXCLUDED_CC
    uncovered = sorted(should - covered - set(NO_GATEWAY))

    # ---- report ----
    print(f"WPI rows: {len(wpi_rows)}  dataset rows: {len(ds_rows)}  curated: {len(curated)}")
    print(f"\nTotal ports: {len(ports)} in {len(covered)} countries/territories")
    print("Ports per region:")
    for r in order:
        print(f"  {r} {REGIONS[r]:<36} {sum(p['region'] == r for p in ports):>3}")
    print("Ports per tier:", {t: sum(p["tier"] == t for p in ports) for t in (1, 2, 3)})
    print("Coordinate sources:", src_count)
    print(f"\nCoastal countries with a container service that have NO port: {uncovered or 'none'}")
    for cc, why in NO_GATEWAY.items():
        print(f"  no gateway by design: {cc} - {why}")
    print("  excluded (sanctions):", ", ".join(f"{e['country']}" for e in EXCLUDED),
          "| WPI container evidence in excluded:", sorted(set(wpi_evidence) & EXCLUDED_CC))
    print(f"\nLOCODEs not found in dataset or WPI (dropped): {missing or 'none'}")
    if airport_coord:
        print("Coordinates taken from an airport row carrying the LOCODE (no port row, no WPI):")
        for a in airport_coord:
            print("  ", a)
    print(f"\nWPI vs dataset > 30 km ({len(far)}):")
    for code, name, wname, dsrc, dist, used in sorted(far, key=lambda x: -x[4]):
        print(f"  {code} {name:<32} WPI '{wname}' vs dataset ({dsrc}) {dist:>5} km -> using {used}")
    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print("  ", p)

    print("\nNearest 3 ports (great-circle):")
    for city, lat, lon in SANITY:
        near = sorted(ports, key=lambda p: km(lat, lon, p["lat"], p["lon"]))[:3]
        print(f"  {city:<15}", "; ".join(f"{p['unlocode']} {p['name']} {km(lat, lon, p['lat'], p['lon']):.0f} km" for p in near))

    now = datetime.now(timezone.utc)
    out = {
        "version": f"{now:%Y%m%d}-{len(ports)}",
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": [
            "Curated liner-network list in data/gateway_ports_build.py (Lloyd's List One Hundred Ports; carrier service networks)",
            WPI_URL + " (NGA World Port Index, Pub. 150, public domain: harbour coordinates by UN/LOCODE)",
            "data/locations_unlocode.json (UN/LOCODE existence, fallback coordinates)",
            "data/locations_curated.json (terminal-level coordinates, preferred)",
        ],
        "regions": REGIONS,
        "ports": ports,
        "excluded": EXCLUDED,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\nwrote", OUT, len(ports), "ports")
    if problems or uncovered:
        sys.exit(1)


if __name__ == "__main__":
    main()
