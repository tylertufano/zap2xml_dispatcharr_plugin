"""Best-effort discovery of Gracenote artwork for a plugin refresh."""

import contextvars
import hashlib
import http.client
import json
import os
import re
import stat
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit


STATION_STYLES = {"dark": "ld", "white": "lw", "gray": "lg", "light": "ll"}
PROGRAMME_STYLES = {"landscape_iconic": "i_h9", "landscape_banner": "b_h8"}
CDN_HOST = "zpmc.tmsimg.com"
HOSTS = (CDN_HOST, "dshm.tmsimg.com", "zap2it.tmsimg.com")
PROVIDER_URL = re.compile(r"https?://(?P<host>[a-z0-9.]+)(?P<path>/[a-zA-Z0-9_./-]+)")
STATION_PATH = re.compile(
    r"(?P<prefix>/(?:[A-Za-z0-9_-]{1,64}/){0,6})"
    r"(?P<asset>s[0-9]{1,16})_l[dwgl]_(?P<size>[hv][0-9]{1,3})_(?P<suffix>[a-z]{2})\.png"
)
PROGRAMME_PATH = re.compile(
    r"/assets/(?:assets/)?(?P<asset>p[0-9]{1,16})_[a-z]{1,3}_[hv][0-9]{1,3}_(?P<suffix>[a-z]{2})\.jpg"
)
IMAGE_SIGNATURES = {"image/png": b"\x89PNG\r\n\x1a\n", "image/jpeg": b"\xff\xd8\xff"}
MAX_REQUESTS = 128
MAX_LOOKUP_SECONDS = 25
REQUEST_SECONDS = 2
CACHE_ENTRIES = 8192
CACHE_READ_BYTES = 1024 * 1024
HIT_AGE = 30 * 24 * 60 * 60
MISS_AGE = 2 * 24 * 60 * 60


def candidates(original, style, *, station):
    url = PROVIDER_URL.fullmatch(original)
    if not url or url["host"] not in HOSTS:
        return ()

    if station:
        asset = STATION_PATH.fullmatch(url["path"])
        if not asset:
            return ()
        stem = f"https://{url['host']}{asset['prefix']}{asset['asset']}_{style}_{asset['size']}"
        extension = "png"
    else:
        asset = PROGRAMME_PATH.fullmatch(url["path"])
        if not asset:
            return ()
        stem = f"https://{CDN_HOST}/assets/{asset['asset']}_{style}"
        extension = "jpg"

    suffixes = dict.fromkeys((asset["suffix"], "aa"))
    return tuple(f"{stem}_{suffix}.{extension}" for suffix in suffixes)


def _probe(url, content_type, deadline):
    """Read only enough bytes to check the expected image signature."""
    parsed = urlsplit(url)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return "temporary"
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=remaining)
    try:
        connection.connect()
        if time.monotonic() >= deadline:
            return "temporary"
        signature = IMAGE_SIGNATURES[content_type]
        connection.request("GET", parsed.path, headers={"Range": f"bytes=0-{len(signature) - 1}"})
        with connection.getresponse() as response:
            if response.status == 429:
                return "rate_limited"
            if response.status in (404, 410):
                return "missing"
            if response.status not in (200, 206):
                return "temporary"
            if response.headers.get_content_type() != content_type:
                return "invalid"
            return "valid" if response.read(len(signature)) == signature else "invalid"
    except (OSError, http.client.HTTPException):
        return "temporary"
    finally:
        connection.close()


@contextmanager
def _atomic_output(path, mode=None):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary_path = Path(output.name)
        try:
            yield output
            if mode is not None:
                os.fchmod(output.fileno(), mode)
            output.close()
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)


class ArtworkRefresh:
    """Share preferences, a request budget and cached results across all lineup outputs.

    The plugin download lock covers the cache and the entire lifetime of this object.
    """

    def __init__(self, settings, cache_path):
        station = settings.get("station_logo_style")
        programme = settings.get("programme_artwork_style")
        self.station_style = STATION_STYLES.get(station) if isinstance(station, str) else None
        self.programme_style = PROGRAMME_STYLES.get(programme) if isinstance(programme, str) else None
        self.include_programme = settings.get("include_programme_artwork") is not False
        if not self.include_programme:
            self.programme_style = None

        self.cache_path = cache_path
        self.cache = {}
        self.cache_loaded = False
        self.cache_writable = True
        self.cache_dirty = False
        self.resolved = {}
        self.requests = 0
        self.lookup_seconds = 0.0
        self.rate_limited = False
        self.request_timed_out = False
        self.selected = 0
        self.fallback = 0
        self.cache_hits = 0
        self.budget_skipped = 0

    @property
    def enabled(self):
        return bool(self.station_style or self.programme_style or not self.include_programme)

    def _load_cache(self):
        self.cache_loaded = True
        try:
            with self.cache_path.open("rb") as source:
                data = source.read(CACHE_READ_BYTES + 1)
            if len(data) <= CACHE_READ_BYTES:
                entries = json.loads(data)
                if isinstance(entries, dict):
                    now = time.time()
                    self.cache = {
                        key: entry
                        for key, entry in entries.items()
                        if re.fullmatch(r"[a-f0-9]{64}", key)
                        and isinstance(entry, list) and len(entry) == 2
                        and type(entry[1]) is bool
                        and type(entry[0]) is int
                        and now < entry[0] <= now + HIT_AGE
                    }
        except (FileNotFoundError, ValueError, RecursionError):
            pass
        except OSError:
            self.cache_writable = False
            return

        # Check that this refresh can persist results before making any probes.
        self.cache_dirty = True
        self._save_cache()

    def _save_cache(self):
        if not self.cache_dirty or not self.cache_writable:
            return
        if len(self.cache) > CACHE_ENTRIES:
            newest = sorted(self.cache, key=lambda key: self.cache[key][0], reverse=True)
            self.cache = {key: self.cache[key] for key in newest[:CACHE_ENTRIES]}
        try:
            with _atomic_output(self.cache_path) as output:
                output.write(json.dumps(self.cache, separators=(",", ":")).encode())
        except OSError:
            self.cache_writable = False
        self.cache_dirty = False

    def _request(self, url, content_type):
        if self.rate_limited:
            return "temporary"
        remaining = MAX_LOOKUP_SECONDS - self.lookup_seconds
        if self.requests >= MAX_REQUESTS or remaining <= 0 or self.request_timed_out:
            return "budget"

        limit = min(REQUEST_SECONDS, remaining)
        started = time.monotonic()
        deadline = started + limit
        result = []

        # Socket timeouts bound inactivity. A daemon thread also bounds the
        # caller's wait across DNS, TLS, headers and a slow response body.
        def probe():
            result.append(_probe(url, content_type, deadline))

        worker = threading.Thread(target=contextvars.copy_context().run, args=(probe,), daemon=True)
        self.requests += 1
        worker.start()
        worker.join(max(0, deadline - time.monotonic()))
        self.lookup_seconds += time.monotonic() - started
        if worker.is_alive():
            self.request_timed_out = True
            return "temporary"
        status = result[0] if result else "temporary"
        self.rate_limited = status == "rate_limited"
        return status

    def _select(self, original, style, station):
        variants = candidates(original, style, station=station)
        if not variants:
            return None, False, False
        if not self.cache_loaded:
            self._load_cache()
        if not self.cache_writable:
            return None, False, False

        content_type = "image/png" if station else "image/jpeg"
        cache_hit = False
        for url in variants:
            key = hashlib.sha256(f"{style}\0{original}\0{url}".encode()).hexdigest()
            cached = self.cache.get(key)
            if cached and cached[0] > time.time():
                cache_hit = True
                if cached[1]:
                    return url, cache_hit, False
                continue

            status = self._request(url, content_type)
            if status in ("valid", "missing"):
                age = HIT_AGE if status == "valid" else MISS_AGE
                self.cache[key] = [int(time.time()) + age, status == "valid"]
                self.cache_dirty = True
            if status == "valid":
                return url, cache_hit, False
            if status not in ("missing", "invalid"):
                return None, cache_hit, status == "budget"
        return None, cache_hit, False

    def _resolve(self, icon, style, *, station):
        original = icon.get("src")
        if not original:
            return None
        key = (original, style)
        if key not in self.resolved:
            variant, cache_hit, budget_skipped = self._select(original, style, station)
            self.resolved[key] = variant
            self.selected += variant is not None
            self.fallback += variant is None
            self.cache_hits += cache_hit
            self.budget_skipped += budget_skipped
        return self.resolved[key] if self.cache_writable else None

    def prepare_station_logos(self, path):
        # XMLTV writers put channels first. Finish station discovery across
        # separate lineups before programme artwork can consume the budget.
        with open(path, "rb") as source:
            for event, element in ET.iterparse(source, events=("start", "end")):
                if event == "start" and element.tag == "programme":
                    break
                if event == "end" and element.tag == "channel":
                    for icon in element.iterfind("icon"):
                        self._resolve(icon, self.station_style, station=True)
                    element.clear()

    def apply(self, path, mapped_station_ids):
        if not (self.station_style or mapped_station_ids or not self.include_programme):
            return
        path = Path(path)
        tree = ET.parse(path)
        replacements = []
        removed_programme_icons = False

        if self.station_style:
            for icon in tree.iterfind("./channel/icon"):
                variant = self._resolve(icon, self.station_style, station=True)
                if variant and variant != icon.get("src"):
                    replacements.append((icon, variant))

        if not self.include_programme:
            for programme in tree.iterfind("./programme"):
                for icon in programme.findall("icon"):
                    programme.remove(icon)
                    removed_programme_icons = True
        elif self.programme_style and mapped_station_ids:
            for programme in tree.iterfind("./programme"):
                if programme.get("channel") in mapped_station_ids:
                    for icon in programme.iterfind("icon"):
                        variant = self._resolve(icon, self.programme_style, station=False)
                        if variant and variant != icon.get("src"):
                            replacements.append((icon, variant))

        self._save_cache()
        if self.cache_writable:
            for icon, variant in replacements:
                icon.set("src", variant)
        if removed_programme_icons or (self.cache_writable and replacements):
            with _atomic_output(path, stat.S_IMODE(path.stat().st_mode)) as output:
                tree.write(output, encoding="utf-8", xml_declaration=True)

    def summary(self):
        return (
            f"artwork selected={self.selected} fallback={self.fallback} "
            f"cache-hit={self.cache_hits} budget-skipped={self.budget_skipped} "
            f"requests={self.requests} rate-limited={self.rate_limited} "
            f"cache-writable={self.cache_writable}"
        )
