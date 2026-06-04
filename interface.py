"""
YouTube Channel Finder — applicazione web Flask basata su yt-dlp.

Permette di cercare canali YouTube a partire da una keyword, filtrando per
finestra temporale, views minime, durata dei video e numero massimo di iscritti
del canale (utile per scovare canali piccoli/emergenti).

Per ogni canale trovato mostra: nome, link, data del primo video, video piu'
visto con relative views e una stima sullo stato di monetizzazione (basata sulle
soglie YouTube Partner Program: 1000 iscritti + 4000 ore di watch time annuo).

Avvio:
    python interface.py
Poi apri http://127.0.0.1:5000 nel browser.
"""

import datetime as _dt
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, render_template_string, request

try:
    from yt_dlp import YoutubeDL
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "yt-dlp non e' installato. Esegui:  pip install yt-dlp"
    ) from exc

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configurazione / costanti
# ---------------------------------------------------------------------------

# Soglie YouTube Partner Program per la monetizzazione.
MONETIZATION_MIN_SUBS = 1000
MONETIZATION_MIN_WATCH_HOURS = 4000

# Quanti risultati di ricerca esaminare al massimo (piu' alto = piu' lento).
SEARCH_POOL = 60

# Durate (in secondi) per i filtri.
DURATION_FILTERS = {
    "all": (None, None),
    "short": (None, 60),          # Shorts: < 60s
    "medium": (60, 8 * 60),       # 1 - 8 minuti
    "long": (8 * 60, None),       # > 8 minuti
}

# Finestre temporali disponibili (in mesi).
TIME_WINDOWS = {"3": 3, "6": 6, "12": 12}

# --- Autenticazione cookie (per superare "Sign in to confirm you're not a bot") ---
#
# YouTube blocca yt-dlp se non riceve i cookie di una sessione reale. Due modi:
#
#  A) File cookies.txt (CONSIGLIATO su Windows): esporta i cookie con un'estensione
#     del browser (es. "Get cookies.txt LOCALLY") e salva il file accanto a questo
#     script. Funziona anche con il browser aperto.
#  B) Estrazione diretta dal browser: richiede che il browser sia COMPLETAMENTE
#     chiuso, altrimenti il database dei cookie e' bloccato (yt-dlp issue #7271).
#
# Il file cookies.txt, se presente, ha la precedenza sul browser.
import os as _os

COOKIES_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "cookies.txt")

# Browser da cui estrarre i cookie se cookies.txt non esiste. None per disabilitare.
# Valori validi: "brave", "chrome", "edge", "firefox", "chromium", "opera", ...
COOKIES_BROWSER = "brave"


def _base_ydl_opts() -> dict:
    """Opzioni comuni a tutte le istanze YoutubeDL (inclusa l'autenticazione cookie)."""
    opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    if _os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    elif COOKIES_BROWSER:
        # yt-dlp si aspetta una tupla: (nome_browser, profilo, keyring, container).
        opts["cookiesfrombrowser"] = (COOKIES_BROWSER, None, None, None)
    return opts


# ---------------------------------------------------------------------------
# Logica di ricerca
# ---------------------------------------------------------------------------

def _months_ago(months: int) -> _dt.date:
    """Ritorna la data di 'months' mesi fa (approssimata a 30 giorni/mese)."""
    return _dt.date.today() - _dt.timedelta(days=months * 30)


def _parse_upload_date(raw) -> _dt.date | None:
    """Converte una data yt-dlp (YYYYMMDD o timestamp) in date."""
    if raw is None:
        return None
    try:
        return _dt.datetime.strptime(str(raw), "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def _duration_ok(duration, filter_key: str) -> bool:
    """Verifica se la durata (secondi) rientra nel filtro selezionato."""
    lo, hi = DURATION_FILTERS.get(filter_key, (None, None))
    if duration is None:
        # Senza informazioni sulla durata accettiamo solo il filtro "all".
        return filter_key == "all"
    if lo is not None and duration < lo:
        return False
    if hi is not None and duration >= hi:
        return False
    return True


def _search_videos(keyword: str, pool: int) -> list[dict]:
    """Esegue una ricerca YouTube e ritorna i metadati 'flat' dei video."""
    opts = _base_ydl_opts()
    opts["extract_flat"] = True
    query = f"ytsearch{pool}:{keyword}"
    print(f"[DEBUG] _search_videos: PRIMA della chiamata yt-dlp, query={query!r}")
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query, download=False)
    entries = [e for e in (info or {}).get("entries", []) if e]
    print(f"[DEBUG] _search_videos: DOPO la chiamata yt-dlp, {len(entries)} video grezzi")
    if entries:
        print(f"[DEBUG] _search_videos: primo entry id={entries[0].get('id')!r} "
              f"chiavi={list(entries[0].keys())}")
    return entries


def _fetch_video_details(video_id: str) -> dict | None:
    """Recupera i dettagli completi di un singolo video."""
    opts = _base_ydl_opts()
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except Exception as exc:  # noqa: BLE001 — un video puo' fallire senza bloccare tutto
        # Prima questo errore veniva ingoiato silenziosamente: ora lo stampiamo.
        print(f"[DEBUG] _fetch_video_details: FALLITO video {video_id!r}: "
              f"{type(exc).__name__}: {exc}")
        return None


def _estimate_monetization(subs, total_views: int) -> dict:
    """
    Stima lo stato di monetizzazione di un canale.

    Le ore di watch time non sono esposte da yt-dlp, quindi le stimiamo dalle
    views aggregate ipotizzando una durata media di visione di ~3 minuti.
    """
    est_watch_hours = round((total_views * 3) / 60) if total_views else 0
    subs_ok = subs is not None and subs >= MONETIZATION_MIN_SUBS
    hours_ok = est_watch_hours >= MONETIZATION_MIN_WATCH_HOURS

    if subs_ok and hours_ok:
        status, label = "yes", "Probabilmente monetizzabile"
    elif subs_ok or hours_ok:
        status, label = "partial", "Vicino alle soglie"
    else:
        status, label = "no", "Sotto le soglie"

    return {
        "status": status,
        "label": label,
        "est_watch_hours": est_watch_hours,
        "subs_ok": subs_ok,
        "hours_ok": hours_ok,
    }


def search_channels(
    keyword: str,
    months: int,
    min_views: int,
    duration_key: str,
    max_subs: int | None,
) -> list[dict]:
    """
    Cerca canali YouTube secondo i criteri indicati.

    Ritorna una lista di dizionari, uno per canale, gia' pronti per il template.
    """
    print("\n" + "=" * 70)
    print(f"[DEBUG] search_channels: AVVIO ricerca")
    print(f"[DEBUG]   keyword={keyword!r} months={months} min_views={min_views} "
          f"duration_key={duration_key!r} max_subs={max_subs}")
    cutoff = _months_ago(months)
    print(f"[DEBUG]   cutoff (data minima primo video) = {cutoff}")

    raw_videos = _search_videos(keyword, SEARCH_POOL)
    if not raw_videos:
        print("[DEBUG] search_channels: yt-dlp non ha restituito NESSUN video. "
              "Possibile blocco di rete, captcha o keyword senza risultati.")

    # Raccogliamo i dettagli dei video in parallelo (rete-bound).
    print(f"[DEBUG] search_channels: recupero dettagli di {len(raw_videos)} video...")
    detailed: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(_fetch_video_details, v.get("id")): v
            for v in raw_videos
            if v.get("id")
        }
        for fut in as_completed(futures):
            info = fut.result()
            if info:
                detailed.append(info)
    print(f"[DEBUG] search_channels: dettagli recuperati con successo: "
          f"{len(detailed)}/{len(raw_videos)}")

    # Contatori per capire dove vengono scartati i video.
    skip_no_chid = skip_views = skip_duration = skip_date = kept = 0

    # Aggrega per canale.
    channels: dict[str, dict] = {}
    for v in detailed:
        ch_id = v.get("channel_id") or v.get("uploader_id")
        if not ch_id:
            skip_no_chid += 1
            continue

        views = v.get("view_count") or 0
        duration = v.get("duration")
        upload_date = _parse_upload_date(v.get("upload_date"))

        # Filtri a livello di video.
        if views < min_views:
            skip_views += 1
            continue
        if not _duration_ok(duration, duration_key):
            skip_duration += 1
            continue
        if upload_date is not None and upload_date < cutoff:
            skip_date += 1
            continue
        kept += 1

        ch = channels.setdefault(
            ch_id,
            {
                "channel_id": ch_id,
                "name": v.get("channel") or v.get("uploader") or "(sconosciuto)",
                "url": v.get("channel_url")
                or v.get("uploader_url")
                or f"https://www.youtube.com/channel/{ch_id}",
                "subs": v.get("channel_follower_count"),
                "first_video_date": upload_date,
                "top_video": None,
                "top_views": -1,
                "total_views": 0,
                "match_count": 0,
            },
        )

        # Aggiorna iscritti se non ancora noti.
        if ch["subs"] is None and v.get("channel_follower_count") is not None:
            ch["subs"] = v.get("channel_follower_count")

        # Primo video (data piu' antica vista).
        if upload_date is not None:
            if ch["first_video_date"] is None or upload_date < ch["first_video_date"]:
                ch["first_video_date"] = upload_date

        # Video piu' visto.
        if views > ch["top_views"]:
            ch["top_views"] = views
            ch["top_video"] = {
                "title": v.get("title") or "(senza titolo)",
                "url": v.get("webpage_url")
                or f"https://www.youtube.com/watch?v={v.get('id')}",
                "views": views,
            }

        ch["total_views"] += views
        ch["match_count"] += 1

    print(f"[DEBUG] search_channels: filtri video -> tenuti={kept} "
          f"scartati[no_channel_id={skip_no_chid}, views<{min_views}={skip_views}, "
          f"durata={skip_duration}, troppo_vecchi={skip_date}]")
    print(f"[DEBUG] search_channels: canali unici aggregati = {len(channels)}")

    # Filtro sul numero massimo di iscritti + calcolo monetizzazione.
    skip_subs = 0
    results: list[dict] = []
    for ch in channels.values():
        if max_subs is not None and ch["subs"] is not None and ch["subs"] > max_subs:
            skip_subs += 1
            continue
        ch["monetization"] = _estimate_monetization(ch["subs"], ch["total_views"])
        results.append(ch)

    print(f"[DEBUG] search_channels: scartati per iscritti>{max_subs}: {skip_subs}")
    print(f"[DEBUG] search_channels: RISULTATI FINALI = {len(results)}")
    print("=" * 70 + "\n")

    # Ordina per views del video piu' visto (decrescente).
    results.sort(key=lambda c: c["top_views"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Template HTML
# ---------------------------------------------------------------------------

PAGE = """
<!doctype html>
<html lang="it">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>YouTube Channel Finder</title>
  <style>
    :root {
      --bg: #0f0f0f; --card: #1c1c1c; --line: #303030;
      --text: #f1f1f1; --muted: #aaaaaa; --accent: #ff0033;
      --green: #2ecc71; --orange: #f39c12; --red: #e74c3c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; background: var(--bg); color: var(--text);
      font-family: "Segoe UI", Roboto, Arial, sans-serif; line-height: 1.5;
    }
    header { padding: 28px 20px 12px; text-align: center; }
    header h1 { margin: 0; font-size: 1.7rem; }
    header h1 span { color: var(--accent); }
    header p { color: var(--muted); margin: 6px 0 0; font-size: .9rem; }
    .wrap { max-width: 960px; margin: 0 auto; padding: 16px 20px 60px; }
    form {
      background: var(--card); border: 1px solid var(--line);
      border-radius: 14px; padding: 20px; display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px;
    }
    .field { display: flex; flex-direction: column; gap: 6px; }
    .field.full { grid-column: 1 / -1; }
    label { font-size: .82rem; color: var(--muted); }
    input, select {
      background: #111; color: var(--text); border: 1px solid var(--line);
      border-radius: 8px; padding: 10px 12px; font-size: .95rem;
    }
    input:focus, select:focus { outline: none; border-color: var(--accent); }
    button {
      grid-column: 1 / -1; background: var(--accent); color: #fff; border: 0;
      border-radius: 8px; padding: 13px; font-size: 1rem; font-weight: 600;
      cursor: pointer; transition: filter .15s;
    }
    button:hover { filter: brightness(1.1); }
    .meta { color: var(--muted); font-size: .85rem; margin: 22px 2px 10px; }
    .card {
      background: var(--card); border: 1px solid var(--line); border-radius: 14px;
      padding: 18px 20px; margin-bottom: 14px;
    }
    .card h3 { margin: 0 0 4px; font-size: 1.15rem; }
    .card h3 a { color: var(--text); text-decoration: none; }
    .card h3 a:hover { color: var(--accent); }
    .row { display: flex; flex-wrap: wrap; gap: 18px 28px; margin-top: 10px; }
    .stat { display: flex; flex-direction: column; }
    .stat .k { font-size: .72rem; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
    .stat .v { font-size: .98rem; }
    .stat .v a { color: #4ea1ff; text-decoration: none; }
    .stat .v a:hover { text-decoration: underline; }
    .badge {
      display: inline-block; padding: 4px 10px; border-radius: 999px;
      font-size: .78rem; font-weight: 600;
    }
    .badge.yes { background: rgba(46,204,113,.15); color: var(--green); }
    .badge.partial { background: rgba(243,156,18,.15); color: var(--orange); }
    .badge.no { background: rgba(231,76,60,.15); color: var(--red); }
    .empty { color: var(--muted); text-align: center; padding: 30px; }
    .error { background: rgba(231,76,60,.1); border: 1px solid var(--red);
             color: #ffb3ab; border-radius: 10px; padding: 14px; }
    footer { text-align: center; color: var(--muted); font-size: .78rem; padding: 20px; }
  </style>
</head>
<body>
  <header>
    <h1><span>▶</span> YouTube Channel Finder</h1>
    <p>Trova canali piccoli ed emergenti tramite yt-dlp</p>
  </header>
  <div class="wrap">
    <form method="post">
      <div class="field full">
        <label for="keyword">Keyword di ricerca</label>
        <input id="keyword" name="keyword" required
               placeholder="es. recensioni gadget" value="{{ f.keyword }}">
      </div>

      <div class="field">
        <label for="window">Finestra temporale</label>
        <select id="window" name="window">
          <option value="3"  {{ 'selected' if f.window == '3'  }}>Ultimi 3 mesi</option>
          <option value="6"  {{ 'selected' if f.window == '6'  }}>Ultimi 6 mesi</option>
          <option value="12" {{ 'selected' if f.window == '12' }}>Ultimi 12 mesi</option>
        </select>
      </div>

      <div class="field">
        <label for="min_views">Views minime per video</label>
        <input id="min_views" name="min_views" type="number" min="0"
               value="{{ f.min_views }}">
      </div>

      <div class="field">
        <label for="duration">Durata video</label>
        <select id="duration" name="duration">
          <option value="all"    {{ 'selected' if f.duration == 'all'    }}>Tutti</option>
          <option value="short"  {{ 'selected' if f.duration == 'short'  }}>Shorts (&lt; 60s)</option>
          <option value="medium" {{ 'selected' if f.duration == 'medium' }}>Medium (1–8 min)</option>
          <option value="long"   {{ 'selected' if f.duration == 'long'   }}>Long-form (&gt; 8 min)</option>
        </select>
      </div>

      <div class="field">
        <label for="max_subs">Massimo iscritti canale (0 = nessun limite)</label>
        <input id="max_subs" name="max_subs" type="number" min="0"
               value="{{ f.max_subs }}">
      </div>

      <button type="submit">Cerca canali</button>
    </form>

    {% if error %}
      <div class="meta"></div>
      <div class="error">{{ error }}</div>
    {% elif searched %}
      <div class="meta">{{ results|length }} canale/i trovato/i per
        “{{ f.keyword }}”.</div>
      {% if results %}
        {% for c in results %}
          <div class="card">
            <h3><a href="{{ c.url }}" target="_blank" rel="noopener">{{ c.name }}</a></h3>
            <span class="badge {{ c.monetization.status }}">{{ c.monetization.label }}</span>
            <div class="row">
              <div class="stat">
                <span class="k">Iscritti</span>
                <span class="v">{{ '{:,}'.format(c.subs).replace(',', '.') if c.subs is not none else 'n/d' }}</span>
              </div>
              <div class="stat">
                <span class="k">Primo video</span>
                <span class="v">{{ c.first_video_date.strftime('%d/%m/%Y') if c.first_video_date else 'n/d' }}</span>
              </div>
              <div class="stat">
                <span class="k">Video piu' visto</span>
                <span class="v">
                  {% if c.top_video %}
                    <a href="{{ c.top_video.url }}" target="_blank" rel="noopener">{{ c.top_video.title }}</a>
                  {% else %}n/d{% endif %}
                </span>
              </div>
              <div class="stat">
                <span class="k">Views (top)</span>
                <span class="v">{{ '{:,}'.format(c.top_views).replace(',', '.') if c.top_views >= 0 else 'n/d' }}</span>
              </div>
              <div class="stat">
                <span class="k">Watch time stimato</span>
                <span class="v">~{{ '{:,}'.format(c.monetization.est_watch_hours).replace(',', '.') }} h</span>
              </div>
            </div>
          </div>
        {% endfor %}
      {% else %}
        <div class="empty">Nessun canale corrisponde ai criteri. Prova ad
          allargare la finestra temporale o ad abbassare le views minime.</div>
      {% endif %}
    {% endif %}
  </div>
  <footer>
    Stima monetizzazione basata sulle soglie YouTube Partner Program:
    {{ min_subs }} iscritti + {{ min_hours }} h di watch time/anno.
    Le ore sono stimate dalle views (yt-dlp non le espone).
  </footer>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
    # Valori di default del form.
    form = {
        "keyword": "",
        "window": "6",
        "min_views": "1000",
        "duration": "all",
        "max_subs": "10000",
    }
    results: list[dict] = []
    error = None
    searched = False

    if request.method == "POST":
        searched = True
        form["keyword"] = (request.form.get("keyword") or "").strip()
        form["window"] = request.form.get("window", "6")
        form["min_views"] = request.form.get("min_views", "0")
        form["duration"] = request.form.get("duration", "all")
        form["max_subs"] = request.form.get("max_subs", "0")

        try:
            if not form["keyword"]:
                raise ValueError("Inserisci una keyword di ricerca.")

            months = TIME_WINDOWS.get(form["window"], 6)
            min_views = max(0, int(form["min_views"] or 0))
            max_subs_raw = int(form["max_subs"] or 0)
            max_subs = max_subs_raw if max_subs_raw > 0 else None
            duration_key = form["duration"] if form["duration"] in DURATION_FILTERS else "all"

            results = search_channels(
                keyword=form["keyword"],
                months=months,
                min_views=min_views,
                duration_key=duration_key,
                max_subs=max_subs,
            )
        except ValueError as exc:
            error = str(exc)
            print(f"[DEBUG] index: ValueError: {exc}")
        except Exception as exc:  # noqa: BLE001 — mostriamo l'errore all'utente
            error = f"Errore durante la ricerca: {exc}"
            # Traceback completo nel terminale per il debug.
            print("[DEBUG] index: ECCEZIONE durante la ricerca:")
            traceback.print_exc()

    return render_template_string(
        PAGE,
        f=form,
        results=results,
        error=error,
        searched=searched,
        min_subs=MONETIZATION_MIN_SUBS,
        min_hours=MONETIZATION_MIN_WATCH_HOURS,
    )


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
