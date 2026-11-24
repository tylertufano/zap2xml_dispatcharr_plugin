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
PROGRAMME_STYLES = {
    "landscape_iconic": ("i_h9",),
    "landscape_banner": ("b_h8",),
    "portrait_banner": ("b_v8", "b_v12", "b_v9", "b_v13"),
    "portrait_iconic": ("i_v8", "i_v12", "i_v9", "i_v13"),
    "square_iconic": ("i_s4",),
}
CDN_HOST = "zpmc.tmsimg.com"
HOSTS = (CDN_HOST, "dshm.tmsimg.com", "zap2it.tmsimg.com")
PROVIDER_URL = re.compile(r"https?://(?P<host>[a-z0-9.]+)(?P<path>/[a-zA-Z0-9_./-]+)")
STATION_PATH = re.compile(
    r"(?P<prefix>/(?:[A-Za-z0-9_-]{1,64}/){0,6})"
    r"(?P<asset>s[0-9]{1,16})_l[dwgl]_(?P<size>[hv][0-9]{1,3})_(?P<suffix>[a-z]{2})\.png"
)
PROGRAMME_PATH = re.compile(
    r"/assets/(?:assets/)?(?P<asset>p[0-9]{1,16})_[a-z]{1,3}_[hvs][0-9]{1,3}_(?P<suffix>[a-z]{2})\.jpg"
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
        stems = (f"https://{url['host']}{asset['prefix']}{asset['asset']}_{style}_{asset['size']}",)
        extension = "png"
    else:
        asset = PROGRAMME_PATH.fullmatch(url["path"])
        if not asset:
            return ()
        variants = (style,) if isinstance(style, str) else style
        stems = (f"https://{CDN_HOST}/assets/{asset['asset']}_{variant}" for variant in variants)
        extension = "jpg"

    suffixes = dict.fromkeys((asset["suffix"], "aa"))
    return tuple(f"{stem}_{suffix}.{extension}" for stem in stems for suffix in suffixes)


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
def _temporary_output(path):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        try:
            yield output
        finally:
            Path(output.name).unlink(missing_ok=True)


def _publish_output(output, path, mode=None):
    if mode is not None:
        os.fchmod(output.fileno(), mode)
    output.close()
    Path(output.name).replace(path)


@contextmanager
def _xmltv_records(path):
    """Yield the root attributes and a stream of detached top-level elements."""
    with Path(path).open("rb") as source:
        events = ET.iterparse(source, events=("start", "end"))
        _, root = next(events)
        if root.tag != "tv":
            raise ET.ParseError("expected an XMLTV tv root")

        def records():
            depth = 0
            for event, element in events:
                if event == "start":
                    depth += 1
                elif depth:
                    depth -= 1
                    if depth == 0:
                        root.remove(element)
                        yield element

        yield root.attrib, records()


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
        # Only memoize assets that have used a request, so this stays bounded
        # even if every programme in a guide has a distinct original icon.
        self.probed_assets = {}
        self.requests = 0
        self.lookup_seconds = 0.0
        self.station_requests = 0
        self.station_seconds = 0.0
        self.reserve_programme = False
        self.rate_limited = False
        self.timed_out_types = set()
        self.selected = 0
        self.fallback = 0
        self.cache_hits = 0
        self.budget_skipped = 0

    @property
    def enabled(self):
        return bool(self.station_style or self.programme_style or not self.include_programme)

    @property
    def uses_mappings(self):
        return bool(self.station_style or self.programme_style)

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
            with _temporary_output(self.cache_path) as output:
                output.write(json.dumps(self.cache, separators=(",", ":")).encode())
                _publish_output(output, self.cache_path)
        except OSError:
            self.cache_writable = False
        self.cache_dirty = False

    def _request(self, url, content_type):
        if self.rate_limited:
            return "temporary"
        station = content_type == "image/png"
        remaining = MAX_LOOKUP_SECONDS - self.lookup_seconds
        if station and self.reserve_programme:
            reserved_requests = max(1, MAX_REQUESTS // 4)
            if self.station_requests >= MAX_REQUESTS - reserved_requests:
                return "budget"
            remaining = min(remaining, MAX_LOOKUP_SECONDS * 2 / 3 - self.station_seconds)
        if self.requests >= MAX_REQUESTS or remaining <= 0 or content_type in self.timed_out_types:
            return "budget"

        limit = min(REQUEST_SECONDS, remaining)
        started = time.monotonic()
        deadline = started + limit
        result = []

        # Socket timeouts bound inactivity. A daemon thread also bounds the
        # caller's wait across DNS, TLS, headers and a slow response body.
        def probe():
            if self.rate_limited:
                return
            status = _probe(url, content_type, deadline)
            if status == "rate_limited":
                self.rate_limited = True
            result.append(status)

        worker = threading.Thread(target=contextvars.copy_context().run, args=(probe,), daemon=True)
        self.requests += 1
        worker.start()
        worker.join(max(0, deadline - time.monotonic()))
        elapsed = time.monotonic() - started
        self.lookup_seconds += elapsed
        if station:
            self.station_requests += 1
            self.station_seconds += elapsed
        if worker.is_alive():
            # Bound overdue workers to one per type while the other type can
            # still use its share of the refresh budget.
            self.timed_out_types.add(content_type)
            return "temporary"
        return result[0] if result else "temporary"

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
        # The first variant identifies the style and keeps existing landscape cache keys valid.
        cache_style = style[0] if isinstance(style, tuple) else style
        for url in variants:
            key = hashlib.sha256(f"{cache_style}\0{original}\0{url}".encode()).hexdigest()
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

    def _resolve(self, icon, style, *, station, report=True):
        original = icon.get("src")
        if not original:
            return None
        key = (original, style)
        result = self.probed_assets.get(key)
        if result is None:
            requests_before = self.requests
            result = self._select(original, style, station)
            if self.requests > requests_before:
                self.probed_assets[key] = result
        variant, cache_hit, budget_skipped = result
        if not self.cache_writable:
            variant = None
        if report:
            self.selected += variant is not None
            self.fallback += variant is None
            self.cache_hits += cache_hit
            self.budget_skipped += budget_skipped
        return variant

    def prepare_mapped_logos(self, path, mapped_station_ids):
        if not self.station_style or not mapped_station_ids:
            return
        # Both writers emit channels before programmes. Only scan this header
        # to prioritize mapped logos across every source before rewriting any.
        with _xmltv_records(path) as (_, records):
            for element in records:
                if element.tag == "programme":
                    break
                if element.tag == "channel" and element.get("id") in mapped_station_ids:
                    for icon in element.iterfind("icon"):
                        self._resolve(icon, self.station_style, station=True, report=False)

    def _write_guide(self, path, output, mapped_station_ids, *, select_variants=True):
        changed = False
        with _xmltv_records(path) as (attributes, records):
            root_xml = ET.tostring(
                ET.Element("tv", attributes), encoding="utf-8", short_empty_elements=False,
            )
            output.write(b"<?xml version='1.0' encoding='utf-8'?>\n")
            output.write(root_xml.removesuffix(b"</tv>"))
            for element in records:
                if element.tag == "programme" and not self.include_programme:
                    for icon in element.findall("icon"):
                        element.remove(icon)
                        changed = True
                elif select_variants:
                    station = element.tag == "channel"
                    style = self.station_style if station else None
                    if element.tag == "programme" and element.get("channel") in mapped_station_ids:
                        style = self.programme_style
                    if style:
                        for icon in element.iterfind("icon"):
                            variant = self._resolve(icon, style, station=station)
                            if variant and variant != icon.get("src"):
                                icon.set("src", variant)
                                changed = True
                element.tail = None
                output.write(b"\n  ")
                output.write(ET.tostring(element, encoding="utf-8"))
            output.write(b"\n</tv>")
        return changed

    def apply(self, path, mapped_station_ids):
        if not (self.station_style or (self.programme_style and mapped_station_ids) or not self.include_programme):
            return
        path = Path(path)
        with _temporary_output(path) as output:
            cache_was_writable = self.cache_writable
            changed = self._write_guide(path, output, mapped_station_ids)
            self._save_cache()
            if not self.cache_writable and self.include_programme:
                return
            if cache_was_writable and not self.cache_writable and self.station_style:
                # Keep the independent programme-artwork toggle even if a
                # cache failure requires falling back to all original logos.
                output.seek(0)
                output.truncate()
                changed = self._write_guide(path, output, mapped_station_ids, select_variants=False)
            if changed:
                _publish_output(output, path, stat.S_IMODE(path.stat().st_mode))

    def summary(self):
        return (
            f"artwork selected={self.selected} fallback={self.fallback} "
            f"cache-hit={self.cache_hits} budget-skipped={self.budget_skipped} "
            f"requests={self.requests} rate-limited={self.rate_limited} "
            f"cache-writable={self.cache_writable}"
        )
