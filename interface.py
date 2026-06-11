"""
YouTube Channel Finder — applicazione web Flask basata sulla YouTube Data API v3.

Permette di cercare canali YouTube a partire da una keyword, filtrando per
finestra temporale, views minime, durata dei video e numero massimo di iscritti
del canale (utile per scovare canali piccoli/emergenti).

Flusso dati:
  1. Search API   -> trova i video per keyword (filtri data + durata).
  2. Videos API   -> views, durata reale, titolo di ogni video.
  3. Channels API -> iscritti reali e data di creazione del canale.
  4. Outlier score = views del video top / iscritti del canale.
  5. (best-effort) scraping di socialblade.com per la crescita a 30 giorni.

Avvio:
    python interface.py
Poi apri http://127.0.0.1:5000 nel browser.
"""

import datetime as _dt
import json
import os
import re
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from flask import Flask, render_template_string, request

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Configurazione / costanti
# ---------------------------------------------------------------------------

# API key: preferibilmente da variabile d'ambiente YOUTUBE_API_KEY.
# NOTA DI SICUREZZA: evita di lasciare la chiave hardcoded in produzione e
# restringila (per referer/API) dalla Google Cloud Console.
API_KEY = os.environ.get("YOUTUBE_API_KEY") or "AIzaSyBievBwnEXPM0ApRWUX-eWTt8FYhHNZGwQ"
API_BASE = "https://www.googleapis.com/youtube/v3"

# Soglie YouTube Partner Program per la monetizzazione.
MONETIZATION_MIN_SUBS = 1000
MONETIZATION_MIN_WATCH_HOURS = 4000

# Quanti risultati di ricerca esaminare (la Search API ne restituisce max 50 per pagina).
SEARCH_MAX_RESULTS = 50

# User-Agent "browser-like" per lo scraping best-effort di SocialBlade.
SCRAPE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

# Durate (in secondi) per i filtri lato applicazione.
DURATION_FILTERS = {
    "all": (None, None),
    "short": (None, 60),          # Shorts: < 60s
    "medium": (60, 8 * 60),       # 1 - 8 minuti
    "long": (8 * 60, None),       # > 8 minuti
}

# Mappatura del filtro durata al parametro videoDuration della Search API.
# L'API conosce solo short(<4min)/medium(4-20min)/long(>20min): usiamola solo
# dove restringe correttamente, poi filtriamo con precisione lato client.
SEARCH_DURATION_PARAM = {
    "all": "any",
    "short": "short",   # <4min copre i nostri <60s, poi rifiniamo
    "medium": "any",    # 1-8min sta a cavallo: meglio "any" + filtro client
    "long": "any",      # i nostri >8min includono 8-20min: "long" API li perderebbe
}

# Finestre temporali disponibili (in mesi).
TIME_WINDOWS = {"3": 3, "6": 6, "12": 12}

# Regex per la durata ISO 8601 (es. PT1H2M3S) restituita dalla Videos API.
_ISO_DUR_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")


# ---------------------------------------------------------------------------
# Helper generici
# ---------------------------------------------------------------------------

def _months_ago(months: int) -> _dt.date:
    """Ritorna la data di 'months' mesi fa (approssimata a 30 giorni/mese)."""
    return _dt.date.today() - _dt.timedelta(days=months * 30)


def _parse_rfc3339_date(raw) -> _dt.date | None:
    """Converte una data RFC3339 (es. 2025-12-05T00:00:00Z) in date."""
    if not raw:
        return None
    try:
        return _dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None


def _parse_iso_duration(raw) -> int | None:
    """Converte una durata ISO 8601 (PT#H#M#S) in secondi."""
    if not raw:
        return None
    m = _ISO_DUR_RE.fullmatch(str(raw))
    if not m:
        return None
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + se


def _duration_ok(duration, filter_key: str) -> bool:
    """Verifica se la durata (secondi) rientra nel filtro selezionato."""
    lo, hi = DURATION_FILTERS.get(filter_key, (None, None))
    if duration is None:
        return filter_key == "all"
    if lo is not None and duration < lo:
        return False
    if hi is not None and duration >= hi:
        return False
    return True


# ---------------------------------------------------------------------------
# Chiamate alla YouTube Data API v3
# ---------------------------------------------------------------------------

def _api_get(endpoint: str, params: dict) -> dict:
    """Esegue una GET sull'API YouTube e ritorna il JSON, alzando errori parlanti."""
    params = {**params, "key": API_KEY}
    resp = requests.get(f"{API_BASE}/{endpoint}", params=params, timeout=20)
    if resp.status_code != 200:
        try:
            msg = resp.json()["error"]["message"]
        except Exception:  # noqa: BLE001
            msg = resp.text[:200]
        raise RuntimeError(f"YouTube API '{endpoint}' HTTP {resp.status_code}: {msg}")
    return resp.json()


def _search_video_ids(keyword: str, cutoff: _dt.date, duration_key: str) -> list[str]:
    """1) Search API: trova gli ID dei video per keyword, data e durata."""
    published_after = _dt.datetime.combine(cutoff, _dt.time.min).isoformat() + "Z"
    params = {
        "part": "snippet",
        "q": keyword,
        "type": "video",
        "order": "viewCount",          # i piu' visti emergono prima (utile per outlier)
        "publishedAfter": published_after,
        "videoDuration": SEARCH_DURATION_PARAM.get(duration_key, "any"),
        "maxResults": min(SEARCH_MAX_RESULTS, 50),
    }
    print(f"[DEBUG] search: q={keyword!r} publishedAfter={published_after} "
          f"videoDuration={params['videoDuration']}")
    data = _api_get("search", params)
    ids = [
        it["id"]["videoId"]
        for it in data.get("items", [])
        if it.get("id", {}).get("videoId")
    ]
    print(f"[DEBUG] search: {len(ids)} video ID trovati")
    return ids


def _fetch_videos(video_ids: list[str]) -> list[dict]:
    """2) Videos API: dettagli (views, durata, titolo) a batch da 50."""
    items: list[dict] = []
    for i in range(0, len(video_ids), 50):
        batch = video_ids[i:i + 50]
        data = _api_get(
            "videos",
            {"part": "snippet,contentDetails,statistics", "id": ",".join(batch)},
        )
        items.extend(data.get("items", []))
    print(f"[DEBUG] videos: dettagli recuperati per {len(items)} video")
    return items


def _fetch_channels(channel_ids: list[str]) -> dict[str, dict]:
    """3) Channels API: iscritti reali e data creazione, a batch da 50."""
    result: dict[str, dict] = {}
    ids = list(channel_ids)
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        data = _api_get(
            "channels",
            {"part": "snippet,statistics", "id": ",".join(batch)},
        )
        for it in data.get("items", []):
            result[it["id"]] = it
    print(f"[DEBUG] channels: metadati recuperati per {len(result)} canali")
    return result


# ---------------------------------------------------------------------------
# Scraping best-effort di SocialBlade (crescita ultimi 30 giorni)
# ---------------------------------------------------------------------------

def _scrape_socialblade(channel_id: str) -> dict | None:
    """
    Recupera la crescita recente dalla pagina pubblica di SocialBlade.

    BEST-EFFORT: SocialBlade e' dietro Cloudflare e il suo ToS vieta lo scraping.
    I numeri sono renderizzati via React e incapsulati nel blob JSON __NEXT_DATA__:
    ne estraiamo la serie temporale giornaliera (date/subscribers/views) e calcoliamo
    la crescita come differenza tra l'ultimo e il primo punto disponibile.
    Se la pagina e' protetta o cambia struttura, ritorniamo None senza bloccare la
    ricerca. Per dati affidabili/completi usare l'API ufficiale (a pagamento).
    """
    url = f"https://socialblade.com/youtube/channel/{channel_id}/monthly"
    try:
        resp = requests.get(url, headers={"User-Agent": SCRAPE_UA}, timeout=12)
    except requests.RequestException as exc:
        print(f"[DEBUG] socialblade: rete KO per {channel_id}: {exc}")
        return None

    if resp.status_code != 200:
        print(f"[DEBUG] socialblade: HTTP {resp.status_code} per {channel_id}")
        return None

    html = resp.text
    # Rileva la challenge di Cloudflare / pagina che richiede JavaScript.
    if "Just a moment" in html or "cf-browser-verification" in html:
        print(f"[DEBUG] socialblade: bloccato da Cloudflare per {channel_id}")
        return {"blocked": True, "subs_30d": None, "views_30d": None, "days": None}

    series = _extract_socialblade_series(html)
    if not series or len(series) < 2:
        print(f"[DEBUG] socialblade: serie temporale non trovata per {channel_id}")
        return None

    first, last = series[0], series[-1]
    subs_delta = _safe_int(last.get("subscribers")) - _safe_int(first.get("subscribers"))
    views_delta = _safe_int(last.get("views")) - _safe_int(first.get("views"))
    days = _series_days(first.get("date"), last.get("date"))
    print(f"[DEBUG] socialblade: {channel_id} +{subs_delta} iscr / +{views_delta} "
          f"views su ~{days} gg")
    return {
        "blocked": False,
        "subs_30d": _fmt_delta(subs_delta),
        "views_30d": _fmt_delta(views_delta),
        "days": days,
    }


def _extract_socialblade_series(html: str) -> list[dict] | None:
    """Estrae dal blob __NEXT_DATA__ la serie giornaliera con date/subscribers/views."""
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.S,
    )
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None

    # La serie e' una delle 'queries' tRPC: cerchiamo la lista di dict che contiene
    # contemporaneamente 'date', 'subscribers' e 'views'.
    best: list[dict] | None = None
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            if (
                node
                and isinstance(node[0], dict)
                and {"date", "subscribers", "views"} <= set(node[0].keys())
            ):
                if best is None or len(node) > len(best):
                    best = node
            else:
                stack.extend(node)
    if not best:
        return None
    # Ordina cronologicamente per sicurezza.
    return sorted(best, key=lambda p: str(p.get("date")))


def _safe_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _series_days(first_date, last_date) -> int | None:
    d0 = _parse_rfc3339_date(first_date)
    d1 = _parse_rfc3339_date(last_date)
    return (d1 - d0).days if d0 and d1 else None


def _fmt_delta(n: int) -> str:
    """Formatta un delta con segno e separatore di migliaia (stile italiano)."""
    sign = "+" if n >= 0 else "-"
    return f"{sign}{'{:,}'.format(abs(n)).replace(',', '.')}"


# ---------------------------------------------------------------------------
# Monetizzazione (stima)
# ---------------------------------------------------------------------------

def _estimate_monetization(subs, total_views: int) -> dict:
    """
    Stima lo stato di monetizzazione di un canale.

    Le ore di watch time non sono esposte dall'API pubblica, quindi le stimiamo
    dalle views aggregate ipotizzando ~3 minuti di visione media per view.
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


# ---------------------------------------------------------------------------
# Pipeline di ricerca
# ---------------------------------------------------------------------------

def search_channels(
    keyword: str,
    months: int,
    min_views: int,
    duration_key: str,
    max_subs: int | None,
    enable_socialblade: bool = True,
) -> list[dict]:
    """Cerca canali YouTube secondo i criteri indicati e ritorna i dati pronti."""
    print("\n" + "=" * 70)
    print(f"[DEBUG] search_channels: keyword={keyword!r} months={months} "
          f"min_views={min_views} duration={duration_key!r} max_subs={max_subs}")
    cutoff = _months_ago(months)
    print(f"[DEBUG]   cutoff = {cutoff}")

    # 1) Search API -> ID video.
    video_ids = _search_video_ids(keyword, cutoff, duration_key)
    if not video_ids:
        print("[DEBUG] search_channels: nessun video dalla Search API")
        return []

    # 2) Videos API -> dettagli.
    videos = _fetch_videos(video_ids)

    # Filtri a livello di video + aggregazione per canale.
    skip_views = skip_duration = kept = 0
    channels: dict[str, dict] = {}
    for v in videos:
        stats = v.get("statistics", {})
        snippet = v.get("snippet", {})
        content = v.get("contentDetails", {})

        views = int(stats.get("viewCount", 0) or 0)
        duration = _parse_iso_duration(content.get("duration"))

        if views < min_views:
            skip_views += 1
            continue
        if not _duration_ok(duration, duration_key):
            skip_duration += 1
            continue
        kept += 1

        ch_id = snippet.get("channelId")
        if not ch_id:
            continue

        ch = channels.setdefault(
            ch_id,
            {
                "channel_id": ch_id,
                "name": snippet.get("channelTitle") or "(sconosciuto)",
                "url": f"https://www.youtube.com/channel/{ch_id}",
                "top_video": None,
                "top_views": -1,
                "total_views": 0,
                "match_count": 0,
            },
        )

        if views > ch["top_views"]:
            ch["top_views"] = views
            ch["top_video"] = {
                "title": snippet.get("title") or "(senza titolo)",
                "url": f"https://www.youtube.com/watch?v={v.get('id')}",
                "views": views,
            }
        ch["total_views"] += views
        ch["match_count"] += 1

    print(f"[DEBUG] filtri video -> tenuti={kept} "
          f"scartati[views<{min_views}={skip_views}, durata={skip_duration}]")
    print(f"[DEBUG] canali unici aggregati = {len(channels)}")
    if not channels:
        return []

    # 3) Channels API -> iscritti reali + data creazione.
    ch_meta = _fetch_channels(list(channels.keys()))
    for ch_id, ch in channels.items():
        meta = ch_meta.get(ch_id, {})
        c_stats = meta.get("statistics", {})
        c_snip = meta.get("snippet", {})
        subs = c_stats.get("subscriberCount")
        ch["subs"] = int(subs) if subs is not None else None
        ch["subs_hidden"] = c_stats.get("hiddenSubscriberCount", False)
        ch["created_date"] = _parse_rfc3339_date(c_snip.get("publishedAt"))

        # 4) Outlier score = views top / iscritti.
        if ch["subs"] and ch["subs"] > 0:
            ch["outlier_score"] = round(ch["top_views"] / ch["subs"], 1)
        else:
            ch["outlier_score"] = None

    # Filtro sul numero massimo di iscritti + monetizzazione.
    skip_subs = 0
    results: list[dict] = []
    for ch in channels.values():
        if max_subs is not None and ch["subs"] is not None and ch["subs"] > max_subs:
            skip_subs += 1
            continue
        ch["monetization"] = _estimate_monetization(ch["subs"], ch["total_views"])
        results.append(ch)
    print(f"[DEBUG] scartati per iscritti>{max_subs}: {skip_subs} | "
          f"canali finali = {len(results)}")

    # 5) SocialBlade (best-effort, in parallelo).
    if enable_socialblade and results:
        print(f"[DEBUG] socialblade: scraping per {len(results)} canali...")
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {
                pool.submit(_scrape_socialblade, ch["channel_id"]): ch
                for ch in results
            }
            for fut in as_completed(futures):
                futures[fut]["socialblade"] = fut.result()
    else:
        for ch in results:
            ch["socialblade"] = None

    # Ordina per outlier score (poi per views top come tie-break).
    results.sort(
        key=lambda c: (c["outlier_score"] or 0, c["top_views"]),
        reverse=True,
    )
    print("[DEBUG] search_channels: FATTO")
    print("=" * 70 + "\n")
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
      --green: #2ecc71; --orange: #f39c12; --red: #e74c3c; --blue: #4ea1ff;
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
    .wrap { max-width: 980px; margin: 0 auto; padding: 16px 20px 60px; }
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
    .card h3 { margin: 0 0 4px; font-size: 1.15rem; display: inline-block; }
    .card h3 a { color: var(--text); text-decoration: none; }
    .card h3 a:hover { color: var(--accent); }
    .row { display: flex; flex-wrap: wrap; gap: 18px 28px; margin-top: 10px; }
    .stat { display: flex; flex-direction: column; }
    .stat .k { font-size: .72rem; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
    .stat .v { font-size: .98rem; }
    .stat .v a { color: var(--blue); text-decoration: none; }
    .stat .v a:hover { text-decoration: underline; }
    .badge {
      display: inline-block; padding: 4px 10px; border-radius: 999px;
      font-size: .78rem; font-weight: 600; margin-left: 8px;
    }
    .badge.yes { background: rgba(46,204,113,.15); color: var(--green); }
    .badge.partial { background: rgba(243,156,18,.15); color: var(--orange); }
    .badge.no { background: rgba(231,76,60,.15); color: var(--red); }
    .outlier { font-weight: 700; }
    .outlier.hot { color: var(--green); }
    .outlier.mid { color: var(--orange); }
    .empty { color: var(--muted); text-align: center; padding: 30px; }
    .error { background: rgba(231,76,60,.1); border: 1px solid var(--red);
             color: #ffb3ab; border-radius: 10px; padding: 14px; }
    footer { text-align: center; color: var(--muted); font-size: .78rem; padding: 20px; }
  </style>
</head>
<body>
  <header>
    <h1><span>▶</span> YouTube Channel Finder</h1>
    <p>Trova canali piccoli ed emergenti tramite la YouTube Data API v3</p>
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
        “{{ f.keyword }}”. Ordinati per outlier score (views top / iscritti).</div>
      {% if results %}
        {% for c in results %}
          <div class="card">
            <h3><a href="{{ c.url }}" target="_blank" rel="noopener">{{ c.name }}</a></h3>
            <span class="badge {{ c.monetization.status }}">{{ c.monetization.label }}</span>
            <div class="row">
              <div class="stat">
                <span class="k">Outlier score</span>
                <span class="v outlier {{ 'hot' if c.outlier_score and c.outlier_score >= 10 else ('mid' if c.outlier_score and c.outlier_score >= 3 else '') }}">
                  {{ ('%.1fx'|format(c.outlier_score)) if c.outlier_score is not none else 'n/d' }}
                </span>
              </div>
              <div class="stat">
                <span class="k">Iscritti</span>
                <span class="v">{{ '{:,}'.format(c.subs).replace(',', '.') if c.subs is not none else ('nascosti' if c.subs_hidden else 'n/d') }}</span>
              </div>
              <div class="stat">
                <span class="k">Canale creato</span>
                <span class="v">{{ c.created_date.strftime('%d/%m/%Y') if c.created_date else 'n/d' }}</span>
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
                <span class="k">SocialBlade (~{{ c.socialblade.days if c.socialblade and c.socialblade.days else 30 }} gg)</span>
                <span class="v">
                  {% if c.socialblade and c.socialblade.blocked %}
                    bloccato (Cloudflare)
                  {% elif c.socialblade and (c.socialblade.subs_30d or c.socialblade.views_30d) %}
                    {{ c.socialblade.subs_30d or '?' }} iscr. / {{ c.socialblade.views_30d or '?' }} views
                  {% else %}n/d{% endif %}
                </span>
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
    Dati da YouTube Data API v3. Outlier score = views del video piu' visto /
    iscritti del canale. Crescita 30 giorni da socialblade.com (best-effort:
    puo' essere bloccata da Cloudflare).
  </footer>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET", "POST"])
def index():
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
            print("[DEBUG] index: ECCEZIONE durante la ricerca:")
            traceback.print_exc()

    return render_template_string(
        PAGE,
        f=form,
        results=results,
        error=error,
        searched=searched,
    )


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000)
