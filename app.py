"""
Photo Search — Google-style image search (top 5 relevant images)
Backend: Python standard library + Pillow (optional, for near-duplicate filter)
Engine : DuckDuckGo i.js (Bing/Google sourced, relevant results, no API key)
Similarity: (1) text relevance re-ranking, (2) perceptual-hash near-duplicate removal

Run:  python3 app.py
Open: http://localhost:8000
"""
import os

PORT = int(os.environ.get("PORT", 8000))

import concurrent.futures as _fut
import hashlib as _hashlib
import io as _io
import json
import re
import urllib.parse
import urllib.request
from difflib import SequenceMatcher as _Seq
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

# ---------------- Image fetching (DuckDuckGo, Google/Bing sourced) ----------------

def _http_get(url, params=None, headers=None, timeout=15):
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        # Render/proxy kabhi gzip bhej deta hai — usko handle karo
        if r.headers.get("Content-Encoding", "") == "gzip":
            import gzip as _gz
            try:
                raw = _gz.decompress(raw)
            except Exception:
                pass
        return raw.decode("utf-8", "ignore")

def _fetch_bing_direct(query, count):
    """Google Images-style multi-image approach: parses the Bing Images page +
    async endpoint directly. Also works for company / obscure queries."""
    import html as _H
    headers = {
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.bing.com/",
    }
    out, seen = [], set()
    pages = [
        ("https://www.bing.com/images/async",
         {"q": query, "first": "1", "count": str(max(count * 4, 35)),
          "cw": "1177", "ch": "705", "relp": "35", "datsrc": "I", "layout": "RowBased"}),
        ("https://www.bing.com/images/search",
         {"q": query, "form": "HDRSC2"}),
    ]
    for url, params in pages:
        try:
            raw = _http_get(url, params, headers, timeout=20)
        except Exception:
            continue
        t = _H.unescape(raw.replace("&quot;", '"').replace("&amp;", "&"))
        murls = re.findall(r'"murl":"(.*?)"', t)
        turls = re.findall(r'"turl":"(.*?)"', t)
        purls = re.findall(r'"purl":"(.*?)"', t)
        titles = re.findall(r'"t":"(.*?)"', t)
        for i, mu in enumerate(murls):
            mu = mu.replace("\\/", "/").strip()
            if not mu or mu in seen or len(mu) < 12:
                continue
            seen.add(mu)
            tu = (turls[i].replace("\\/", "/") if i < len(turls) else mu)
            pu = (purls[i].replace("\\/", "/") if i < len(purls) else "")
            ti = (titles[i] if i < len(titles) else query)
            out.append({"title": ti or query, "image": mu,
                        "thumbnail": tu or mu, "page": pu,
                        "source": "Bing", "width": 0, "height": 0})
            if len(out) >= count:
                return out
        if len(out) >= count:
            break
    return out


def _fetch_ddg(query, count):
    """Fallback: DuckDuckGo i.js (Bing/Google sourced JSON)."""
    base_headers = {
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://duckduckgo.com/",
    }
    html = _http_get("https://duckduckgo.com/",
                     {"q": query, "iar": "images", "iax": "images", "ia": "images"},
                     base_headers)
    m = re.search(r"vqd=([\d-]+)", html)
    if not m:
        return []
    data = _http_get("https://duckduckgo.com/i.js",
                     {"l": "us-en", "o": "json", "q": query, "vqd": m.group(1),
                      "f": ",,,", "p": "1"},
                     base_headers)
    j = json.loads(data)
    out = []
    for it in j.get("results", [])[:count]:
        out.append({
            "title": it.get("title", query),
            "image": it.get("image", ""),
            "thumbnail": it.get("thumbnail", it.get("image", "")),
            "page": it.get("url", ""),
            "source": it.get("source", "Bing"),
            "width": it.get("width", 0),
            "height": it.get("height", 0),
        })
    return out


def _fetch_openverse(query, count):
    """Openverse (WordPress) image search — proper search engine, API key
    nahi chahiye, aur datacenter IP (Render) par block nahi hota.
    Multi-word query ('golden temple') ko server-side phrase ki tarah
    samajhta hai, isliye 'sirf pehla word' wali problem nahi aati."""
    try:
        data = _http_get("https://api.openverse.org/v1/images/",
                         {"q": query, "page_size": str(min(count, 20)),
                          "filter_dead": "true"},
                         {"User-Agent": UA,
                          "Accept-Language": "en-US,en;q=0.9"})
        j = json.loads(data)
        out = []
        for it in j.get("results", [])[:count]:
            full = it.get("url", "") or ""
            thumb = it.get("thumbnail", "") or full
            title = it.get("title", "") or query
            page = it.get("foreign_landing_url", "") or ""
            if full:
                out.append({"title": title or query, "image": full,
                            "thumbnail": thumb or full, "page": page,
                            "source": "Openverse", "width": 0, "height": 0})
        return out
    except Exception:
        return []


def _fetch_wiki(query, count):
    """Fallback jo Render jaise datacenter IP par bhi chalta hai:
    Wikimedia Commons API — exact phrase search, bilkul relevant.
    Isme 'golden temple' search par sirf golden-temple wali
    images aati hain, sirf 'golden' wali nahi."""
    try:
        data = _http_get("https://commons.wikimedia.org/w/api.php",
                         {"action": "query", "format": "json",
                          "generator": "search",
                          "gsrsearch": 'filetype:bitmap "%s"' % query,
                          "gsrlimit": str(min(count, 20)),
                          "gsrnamespace": "6",
                          "prop": "imageinfo",
                          "iiprop": "url|extmetadata",
                          "iiurlwidth": "640"},
                         {"User-Agent": UA,
                          "Accept-Language": "en-US,en;q=0.9",
                          "Referer": "https://commons.wikimedia.org/"})
        j = json.loads(data)
        pages = (j.get("query") or {}).get("pages", {})
        out = []
        for pid, pg in pages.items():
            infos = pg.get("imageinfo") or []
            if not infos:
                continue
            ii = infos[0]
            full = ii.get("url", "")
            thumb = ii.get("thumburl", "") or full
            meta = ii.get("extmetadata") or {}
            title = pg.get("title", query)
            for k in ("ImageDescription", "ObjectName"):
                v = (meta.get(k) or {}).get("value", "")
                if v:
                    title = re.sub(r"<[^>]+>", "", v).strip()[:120] or title
                    break
            if full:
                out.append({"title": title or query, "image": full,
                            "thumbnail": thumb or full,
                            "page": ii.get("descriptionurl", ""),
                            "source": "Wikimedia", "width": 0, "height": 0})
            if len(out) >= count:
                break
        return out
    except Exception:
        return []


def fetch_images(query, count=5):
    """Google Images style: Openverse + Bing direct + DDG + Wikimedia.
    Openverse ko sabse pehle rakha hai kyunki wo Render jaise
    datacenter IP par bhi multi-word query ko phrase ki tarah
    samajhta hai (Bing datacenter par kabhi sirf pehla word
    samajhta hai — 'golden temple' -> 'golden').
    Phir (1) text relevance re-ranking (full-phrase bonus ke saath)
    aur (2) perceptual-hash near-duplicate removal. Top `count`."""
    query = query.strip()
    if not query:
        return []
    pool, seen_url = [], set()
    need = max(count * 6, 30)  # over-fetch so filters still leave enough
    for fetcher in (_fetch_openverse, _fetch_bing_direct,
                    _fetch_ddg, _fetch_wiki):
        try:
            for im in fetcher(query, need):
                u = im.get("image", "")
                if u and u not in seen_url:
                    seen_url.add(u)
                    pool.append(im)
                    if len(pool) >= need:
                        break
        except Exception:
            continue
        if len(pool) >= need:
            break
    if not pool:
        raise RuntimeError("No images found. Please check the spelling and try again.")
    ranked = _rerank_by_text(query, pool)
    unique = _drop_near_duplicates(ranked)
    return unique[:count]


# ---------------- Similarity (1) text relevance re-ranking ----------------

_STOP = {"the", "a", "an", "of", "in", "on", "at", "for", "and", "or",
        "to", "near", "road", "photo", "photos", "image", "images",
        "mein", "me", "ka", "ki", "ke", "hai", "par"}

def _words(s):
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower())
            if w not in _STOP and len(w) > 1]

def _text_score(query, item, base_rank):
    """0..1 score: word overlap (50%) + FULL-PHRASE bonus (30%) +
    fuzzy title match (10%) + engine rank (10%).
    Full-phrase bonus isliye taaki 'golden temple' wala title
    sirf 'golden' wale ko hamesha hara de — Render/local dono par."""
    qw = _words(query)
    title = (item.get("title") or "") + " " + (item.get("page") or "")
    tw = set(_words(title))
    overlap = (len(set(qw) & tw) / max(len(set(qw)), 1)) if qw else 0.0
    # full phrase bonus: poora query title me aaye to bada boost
    qlow = (query or "").lower().strip()
    tlow = ((item.get("title") or "") + " " + (item.get("page") or "")).lower()
    phrase = 1.0 if (qlow and qlow in tlow) else 0.0
    fuzzy = _Seq(None, qlow, (item.get("title") or "").lower()).ratio()
    rank_bonus = 1.0 / (1.0 + base_rank * 0.15)  # earlier engine hit = small bonus
    return round(0.50 * overlap + 0.30 * phrase + 0.10 * fuzzy + 0.10 * rank_bonus, 4)

def _rerank_by_text(query, items):
    scored = []
    for rank, im in enumerate(items):
        s = _text_score(query, im, rank)
        scored.append((s, rank, im))
    scored.sort(key=lambda t: (-t[0], t[1]))
    out = []
    for s, _, im in scored:
        d = dict(im)
        d["relevance"] = s
        out.append(d)
    return out


# ---------------- Similarity (2) perceptual-hash near-duplicate filter ----------------

def _download_bytes(url, timeout=6):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Referer": "https://www.bing.com/"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(1_500_000)  # cap 1.5MB per thumbnail
    return data

def _dhash(data):
    """64-bit difference-hash of image bytes. Falls back to md5 prefix if
    Pillow is unavailable, so the module never crashes without it."""
    try:
        from PIL import Image as _Image
    except Exception:
        return "md5:" + _hashlib.md5(data).hexdigest()[:16]
    try:
        img = _Image.open(_io.BytesIO(data)).convert("L").resize((9, 8))
        px = list(img.getdata())
        bits = 0
        for r in range(8):
            for c in range(8):
                bits = (bits << 1) | (1 if px[r * 9 + c] > px[r * 9 + c + 1] else 0)
        return "ph:%016x" % bits
    except Exception:
        return "md5:" + _hashlib.md5(data).hexdigest()[:16]

def _hamdist(a, b):
    try:
        x = int(a[3:], 16) ^ int(b[3:], 16)
        return bin(x).count("1")
    except Exception:
        return 999  # different schemes (md5 vs ph) never match

def _drop_near_duplicates(items, threshold=5):
    """Keep text order, but drop images whose dHash is within `threshold`
    bits of an already-kept image (i.e. visually near-identical)."""
    thumbs = [it.get("thumbnail") or it.get("image", "") for it in items]
    hashes = [None] * len(items)
    with _fut.ThreadPoolExecutor(max_workers=8) as ex:
        fut2idx = {ex.submit(_download_bytes, u): i
                   for i, u in enumerate(thumbs) if u}
        for fut in _fut.as_completed(fut2idx, timeout=25):
            i = fut2idx[fut]
            try:
                hashes[i] = _dhash(fut.result())
            except Exception:
                hashes[i] = None
    kept, kept_hashes = [], []
    for im, h in zip(items, hashes):
        if h is None:  # download failed -> keep (don't punish relevant result)
            kept.append(im)
            continue
        dup = False
        for kh in kept_hashes:
            if h[:2] == kh[:2] and _hamdist(h, kh) <= (threshold if h.startswith("ph:") else 0):
                dup = True
                break
        if not dup:
            kept.append(im)
            kept_hashes.append(h)
    return kept

# ---------------- HTTP server ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "PhotoSearch/1.0"

    def _send(self, code, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        # FIX (Render first-word bug): Render ka proxy kabhi %20 ko
        # request-line me wapas literal space bana deta hai, jaise:
        #   GET /api/search?q=golden temple&n=5 HTTP/1.1
        # HTTP me space se request-line toot jati hai, isliye
        # BaseHTTPRequestHandler self.path me sirf pehla hissa rakhta
        # hai ("/api/search?q=golden") — baki ("temple&n=5") kat jata
        # hai. Isliye poori requestline se query recover karo.
        rawline = (getattr(self, "requestline", "") or "")
        m = re.match(r"^\S+\s+(.+?)\s+HTTP/\S*\s*$", rawline)
        if m:
            full_target = m.group(1)
        else:
            full_target = self.path
        if "?" in full_target:
            path, _, qstr = full_target.partition("?")
            path = path.split(" ", 1)[0]
        elif "?" in self.path:
            path, _, qstr = self.path.partition("?")
        else:
            path, qstr = self.path.split(" ", 1)[0], ""
        # parse_qsl '+' ko space banata hai aur %20 ko bhi — dono cover
        try:
            pairs = urllib.parse.parse_qsl(qstr, keep_blank_values=True)
        except Exception:
            pairs = []
        qs = {}
        for k, v in pairs:
            qs.setdefault(k, []).append(v)
        # fallback: purana tareeka
        if not qs and qstr:
            try:
                qs = urllib.parse.parse_qs(
                    urllib.parse.urlparse(self.path).query,
                    keep_blank_values=True)
            except Exception:
                qs = {}

        if path == "/api/health":
            self._send(200, json.dumps({"ok": True, "service": "photo-search",
                                        "version": "1.2.0",
                                        "build": "openverse-first"}).encode(),
                       "application/json")
            return

        if path == "/api/debug":
            # Render par check karne ke liye: server ko query kya mili?
            # Kholo: /api/debug?q=golden%20temple  -> {"q": "golden temple"...}
            self._send(200, json.dumps({"ok": True,
                                        "q": (qs.get("q", [""])[0] or ""),
                                        "n": (qs.get("n", [""])[0] or ""),
                                        "requestline": getattr(
                                            self, "requestline", ""),
                                        "path": self.path}).encode("utf-8"),
                       "application/json")
            return

        if path == "/api/docs":
            docs = {
                "name": "Photo Search API",
                "version": "1.1.0",
                "base_url": "http://localhost:8000",
                "endpoints": [
                    {"method": "GET", "path": "/api/search",
                     "params": {"q": "search text (required)",
                                "n": "1-10, default 5 (optional)"},
                     "example": "/api/search?q=taj+mahal&n=5"},
                    {"method": "GET", "path": "/api/health",
                     "example": "/api/health"},
                    {"method": "GET", "path": "/api/docs",
                     "example": "/api/docs"},
                ],
                "response_ok": {"ok": True, "query": "taj mahal", "count": 5,
                                "engine": "Google-style (DDG/Bing) + similarity 1+2",
                                "images": [{"title": "...", "image": "https://...",
                                            "thumbnail": "https://...",
                                            "page": "https://...",
                                            "source": "Bing",
                                            "width": 0, "height": 0}]},
                "response_error": {"ok": False, "error": "message"},
            }
            self._send(200, json.dumps(docs, ensure_ascii=False).encode("utf-8"),
                       "application/json")
            return

        if path == "/api/search":
            q = (qs.get("q", [""])[0] or "").strip()
            try:
                n = int((qs.get("n", ["5"])[0] or "5"))
            except ValueError:
                self._send(400, json.dumps({"ok": False, "error": "Parameter 'n' must be a number (1-10)"}).encode(),
                           "application/json")
                return
            n = max(1, min(n, 10))
            if not q:
                self._send(400, json.dumps({"ok": False, "error": "Please type something in the search box first"}).encode(),
                           "application/json")
                return
            try:
                images = fetch_images(q, n)
                self._send(200, json.dumps({"ok": True, "query": q, "count": len(images),
                                            "engine": "Openverse+Bing/DDG/Wiki + similarity 1+2", "images": images},
                                           ensure_ascii=False).encode("utf-8"), "application/json")
            except Exception as e:
                self._send(502, json.dumps({"ok": False, "error": str(e)}).encode("utf-8"), "application/json")
            return

        if path in ("/", "/index.html"):
            try:
                import os as _os
                _base = _os.path.dirname(_os.path.abspath(__file__))
                with open(_os.path.join(_base, "index.html"), "rb") as f:
                    self._send(200, f.read())
            except FileNotFoundError:
                self._send(500, b"index.html missing")
            return

        self.send_error(404, "Not found")

    def log_message(self, *a):
        pass  # quiet logs

if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"Photo Search running on port {PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped")