# Ce s-a schimbat

Botul a fost rescris de la zero, mult mai simplu (din ~1585 linii → ~400),
păstrând DOAR fluxul de pontaj cu locație. S-au eliminat: checklist-urile
(deschidere/control calitate/închidere), verificarea cu programul (Schedule)
din Sheets, remindere automate către admin, primire poze, Mini App/WebApp —
toate astea erau surse de bug-uri și mesaje în plus.

## Ce face acum botul

- **Meniu persistent** de lângă bara de text (`✅ Чек-ин` / `🔴 Чек-аут`),
  activat o singură dată cu `/start` (sau `/meniu`) — nu mai e nevoie de
  nicio comandă zi de zi, tastatura rămâne mereu vizibilă (`is_persistent`).
- Userul apasă un buton → mesajul lui e șters instant → apare
  `📍 Trimite locația`.
- Locație greșită → se șterge mesajul cu locația, apare `❌ Locație
  greșită`, se poate retrimite oricâte ori.
- Locație corectă → se șterg TOATE mesajele intermediare, rămâne un singur
  mesaj: `✅ Nume — Checked In — ora`.
- La check-out se repetă pasul, iar la final se șterg toate mesajele
  intermediare + mesajul de "Checked In", rămânând un singur mesaj final cu
  orele lucrate: `🏁 Nume — 09:00:00 – 17:30:00 (8.5 ore lucrate)`.
- **Orice user poate ponta**, indiferent dacă apare sau nu într-o listă —
  nu mai există verificare de program/orar/early-late.
- Dacă apare vreo eroare (ex. Sheets nu răspunde), mesajele intermediare se
  șterg automat și userul poate reîncerca — nu rămâne "gunoi" în chat.
- Comanda `/ore` arată orele lucrate în luna curentă (opțional, poți s-o
  ignori dacă nu ai nevoie).

## Ce am scos din README-ul vechi (nu mai e nevoie)

- Pasul cu "privacy mode" / Group Privacy nu mai e critic, dar tot e bine să-l
  ai dezactivat, ca botul să vadă mesajele din grup fără probleme.
- `/panel` nu mai există — folosește `/start` o singură dată.
- Fișa `Schedule`/`Angajați` din Sheets nu mai e citită de bot (poate rămâne
  în tabel, doar nu mai are efect asupra pontajului).

## Configurare (.env)

Vezi `.env.example` — aceleași câmpuri ca înainte (`BOT_TOKEN`,
`SPREADSHEET_ID`, `SHEET_NAME`, `GOOGLE_CREDENTIALS_FILE`, `ALLOWED_CHAT_ID`)
plus câteva noi, opționale:

- `ALLOWED_TOPIC_ID` — dacă grupul are topicuri (forum), ca botul să scrie
  doar în topicul potrivit.
- `LOCATION_LAT` / `LOCATION_LON` / `LOCATION_RADIUS_METERS` — coordonatele
  localului și raza permisă (implicit 150m).
- `CLEANUP_DELAY_SECONDS` — cât rămân vizibile avertizările (implicit 5s).
- `PENDING_TIMEOUT_MINUTES` — după cât timp se anulează o cerere de locație
  neterminată (implicit 10 min).

## Instalare

La fel ca înainte:
```
pip install -r requirements.txt
```
copiază `.env.example` în `.env` și completează-l, apoi:
```
python bot.py
```
scrie `/start` o dată în grup ca să apară meniul persistent, și gata.
