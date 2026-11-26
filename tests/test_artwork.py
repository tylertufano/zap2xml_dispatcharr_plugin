import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from email.message import Message
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

from src.zap2xml import artwork, plugin, zap2xml


ESPN = "https://zpmc.tmsimg.com/h3/NowShowing/32645/s32645_ll_h15_ab.png"
WBBM = "https://zpmc.tmsimg.com/h3/NowShowing/20454/s28711_ll_h15_ab.png"
POSTER = "https://zpmc.tmsimg.com/assets/p21040818_p_v9_ab.jpg"
LEGACY_POSTER = "https://dshm.tmsimg.com/assets/p21040818_p_v9_ab.jpg"
ICONIC = "https://zpmc.tmsimg.com/assets/p21040818_i_h9_aa.jpg"
BANNER = "https://zpmc.tmsimg.com/assets/p21040818_b_h8_aa.jpg"
LINEUPS = ("USA-IL54437-X", "USA-OTA53963")


def sd(station_id, style):
    """A Schedules Direct station-logo URL for the given Gracenote id and style."""
    return f"https://logos.json.schedulesdirect.org/stationLogos/s{station_id}_{style}_360w_270h.png"


def grid_channel(station_id, name, logo, *, asset_id=21040818):
    return {
        "stationId": station_id,
        "channelNo": "12",
        "callSign": name,
        "thumbnail": logo + "?keep-default-normalization=true",
        "events": [{
            "startTime": "2026-11-24T16:00:00Z",
            "endTime": "2026-11-24T17:00:00Z",
            "program": {
                "title": "Fixture",
                "genres": ["sports"],
                "preferredImage": {"uri": f"//zap2it.tmsimg.com/assets/p{asset_id}_p_v9_ab.jpg"},
            },
        }],
    }


def icon_urls(path, element):
    root = ET.parse(path)
    return {
        row.get("id" if element == "channel" else "channel"): row.find("icon").get("src")
        for row in root.iterfind(element)
        if row.find("icon") is not None
    }


class ProbeTests(unittest.TestCase):
    def test_png_and_jpeg_signature_type_status_and_range(self):
        for content_type, signature in (
            ("image/png", b"\x89PNG\r\n\x1a\n"),
            ("image/jpeg", b"\xff\xd8\xff"),
        ):
            for status, header, data, expected in (
                (206, content_type, signature, "valid"),
                (200, content_type, signature, "valid"),
                (200, "text/html", signature, "invalid"),
                (200, content_type, b"bad data", "invalid"),
                (302, content_type, signature, "temporary"),
                (403, content_type, signature, "temporary"),
                (404, content_type, signature, "missing"),
                (410, content_type, signature, "missing"),
                (429, content_type, signature, "rate_limited"),
                (503, content_type, signature, "temporary"),
            ):
                with self.subTest(content_type=content_type, status=status, expected=expected):
                    response = MagicMock(status=status, headers=Message())
                    response.headers["Content-Type"] = header
                    response.headers["Location"] = "https://unrelated.example/image.png"
                    response.read.return_value = data
                    response.__enter__.return_value = response
                    with patch.object(artwork.http.client, "HTTPSConnection") as factory:
                        connection = factory.return_value
                        connection.getresponse.return_value = response
                        result = artwork._probe(ESPN, content_type, artwork.time.monotonic() + 2)
                    self.assertEqual(result, expected)
                    factory.assert_called_once()
                    self.assertEqual(factory.call_args.args, ("zpmc.tmsimg.com",))
                    connection.request.assert_called_once_with(
                        "GET", "/h3/NowShowing/32645/s32645_ll_h15_ab.png",
                        headers={"Range": f"bytes=0-{len(signature) - 1}"},
                    )
                    connection.close.assert_called_once()
                    if status in (200, 206) and header == content_type:
                        response.read.assert_called_once_with(len(signature))
                    else:
                        response.read.assert_not_called()

    def test_forbidden_is_missing_only_when_requested(self):
        # The SD logo server returns 403 application/xml for a missing id, so the
        # station path asks for 403 to be treated as a confirmed miss; posters
        # keep treating 403 as a transient error.
        response = MagicMock(status=403, headers=Message())
        response.headers["Content-Type"] = "application/xml"
        response.__enter__.return_value = response
        with patch.object(artwork.http.client, "HTTPSConnection") as factory:
            factory.return_value.getresponse.return_value = response
            self.assertEqual(artwork._probe(ESPN, "image/png", artwork.time.monotonic() + 2), "temporary")
            self.assertEqual(
                artwork._probe(ESPN, "image/png", artwork.time.monotonic() + 2, forbidden_missing=True),
                "missing",
            )

    def test_connection_errors_and_timeouts_are_temporary(self):
        for error in (ConnectionError, TimeoutError):
            with self.subTest(error=error), patch.object(artwork.http.client, "HTTPSConnection") as factory:
                connection = factory.return_value
                connection.connect.side_effect = error
                status = artwork._probe(ESPN, "image/png", artwork.time.monotonic() + 2)
                self.assertEqual(status, "temporary")
                connection.request.assert_not_called()
                connection.close.assert_called_once()


class ArtworkTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cache = self.root / ".artwork-cache.json"
        self.path = self.root / "guide.xml"
        self.enterContext(patch.object(zap2xml, "_rewrite_first_display_name_with_station_name"))
        self.probe = self.enterContext(patch.object(artwork, "_probe", return_value="valid"))
        self.channels = [
            grid_channel("32645", "ESPN", ESPN),
            grid_channel("20454", "WBBM", WBBM, asset_id=28711),
        ]
        self.write_guide()

    def write_guide(self):
        zap2xml.write_xmltv(self.channels, self.path)

    def refresh(self, **settings):
        return artwork.ArtworkRefresh(settings, self.cache)

    def test_default_and_unknown_settings_preserve_fixture_bytes(self):
        original = self.path.read_bytes()
        root = ET.parse(self.path)
        event = root.find("./programme")
        self.assertEqual(event.attrib, {
            "start": "20261124160000 +0000", "stop": "20261124170000 +0000", "channel": "32645",
        })
        self.assertEqual(event.findtext("category"), "Sports")
        self.assertEqual(root.findtext("./channel/display-name[2]"), "12")
        self.assertEqual(icon_urls(self.path, "channel")["32645"], ESPN)
        self.assertEqual(icon_urls(self.path, "programme")["32645"], POSTER)
        for settings in ({}, {"station_logo_style": "unknown", "programme_artwork_style": [], "include_programme_artwork": "unknown"}):
            with self.subTest(settings=settings):
                refresh = self.refresh(**settings)
                self.assertFalse(refresh.enabled)
                refresh.apply(self.path, set())
                self.assertEqual(self.path.read_bytes(), original)
        self.probe.assert_not_called()
        self.assertFalse(self.cache.exists())

    def test_station_logo_urls_try_station_id_then_logo_id(self):
        # Order by callSign: ESPN (32645, logo id == station id) before WBBM;
        # WBBM is station 20454 whose logo file id is 28711, so the SD server is
        # tried with the station id first and the logo id second.
        self.probe.side_effect = ["valid", "missing", "valid"]
        refresh = self.refresh(station_logo_style="dark")
        refresh.apply(self.path, set())
        self.assertEqual(icon_urls(self.path, "channel"), {
            "32645": sd("32645", "dark"),
            "20454": sd("28711", "dark"),
        })
        self.assertEqual([args[0] for args, _ in self.probe.call_args_list], [
            sd("32645", "dark"), sd("20454", "dark"), sd("28711", "dark"),
        ])
        self.assertIn("station-logos selected=2 fallback=0 cache-hit=0 budget-skipped=0 requests=3", refresh.summary())

    def test_station_logo_falls_back_to_default_when_nothing_matches(self):
        self.probe.return_value = "missing"
        refresh = self.refresh(station_logo_style="dark")
        refresh.apply(self.path, set())
        self.assertEqual(icon_urls(self.path, "channel"), {"32645": ESPN, "20454": WBBM})
        self.assertIn("station-logos selected=0 fallback=2 cache-hit=0 budget-skipped=0 requests=3", refresh.summary())

    def test_station_style_mapping_uses_sd_style_names(self):
        for style in ("dark", "white", "gray", "light"):
            with self.subTest(style=style):
                self.write_guide()
                self.probe.reset_mock()
                self.probe.return_value = "valid"
                refresh = self.refresh(station_logo_style=style)
                refresh.apply(self.path, set())
                self.assertEqual(icon_urls(self.path, "channel")["32645"], sd("32645", style))
                self.assertEqual(self.probe.call_args_list[0].args[0], sd("32645", style))
                self.assertTrue(self.probe.call_args_list[0].kwargs["forbidden_missing"])

    def test_station_logo_results_and_negatives_persist_across_refreshes(self):
        self.probe.side_effect = ["valid", "missing", "missing"]
        refresh = self.refresh(station_logo_style="dark")
        refresh.apply(self.path, set())
        self.assertEqual(icon_urls(self.path, "channel"), {"32645": sd("32645", "dark"), "20454": WBBM})
        self.assertEqual(refresh.station_requests, 3)

        # A repeat refresh must reuse the cache: two cache hits, no probes.
        self.write_guide()
        self.probe.reset_mock()
        refresh = self.refresh(station_logo_style="dark")
        refresh.apply(self.path, set())
        self.assertEqual(icon_urls(self.path, "channel"), {"32645": sd("32645", "dark"), "20454": WBBM})
        self.assertEqual(refresh.station_cache_hits, 2)
        self.assertEqual(refresh.station_requests, 0)
        self.probe.assert_not_called()

    def test_station_logo_non_image_response_falls_through_to_logo_id(self):
        self.probe.side_effect = ["valid", "invalid", "valid"]
        refresh = self.refresh(station_logo_style="dark")
        refresh.apply(self.path, set())
        self.assertEqual(icon_urls(self.path, "channel"), {
            "32645": sd("32645", "dark"),
            "20454": sd("28711", "dark"),
        })

    def test_station_logo_budget_is_separate_from_poster_budget(self):
        refresh = self.refresh(
            station_logo_style="dark", programme_artwork_style="landscape_iconic",
            station_logo_request_limit=1, artwork_request_limit=128,
        )
        self.assertEqual(refresh.station_limit, 1)
        self.assertEqual(refresh.request_limit, 128)
        refresh.apply(self.path, {"32645"})
        # Exactly one station probe ran; the second station hit the station cap
        # while posters keep their own (untouched) 128 budget.
        self.assertEqual(refresh.station_requests, 1)
        self.assertEqual(refresh.station_budget_skipped, 1)
        self.assertEqual(refresh.requests, 1)

    def test_station_logo_budget_default_and_validation(self):
        self.assertEqual(self.refresh().station_limit, 2500)
        self.assertEqual(self.refresh().request_limit, 128)
        self.assertEqual(self.refresh(station_logo_request_limit="300").station_limit, 300)
        self.assertEqual(self.refresh(station_logo_request_limit=0).station_limit, 0)
        self.assertEqual(self.refresh(station_logo_request_limit=-1).station_limit, 2500)
        self.assertIn("request-limit=128", self.refresh().summary())
        self.assertIn("request-limit=2500", self.refresh(station_logo_style="dark").summary())

    def test_programme_styles_check_only_mapped_stations_and_deduplicate_events(self):
        self.channels[0]["events"] *= 2
        for style, expected in (("landscape_iconic", ICONIC), ("landscape_banner", BANNER)):
            with self.subTest(style=style):
                self.write_guide()
                self.probe.reset_mock()
                self.probe.side_effect = ["missing", "valid"]
                refresh = self.refresh(programme_artwork_style=style)
                refresh.apply(self.path, set())
                self.probe.assert_not_called()
                refresh.apply(self.path, {"32645"})
                self.assertEqual(icon_urls(self.path, "programme"), {
                    "32645": expected, "20454": POSTER.replace("21040818", "28711"),
                })
                self.assertEqual([args[:2] for args, _ in self.probe.call_args_list], [
                    (expected.replace("_aa.jpg", "_ab.jpg"), "image/jpeg"),
                    (expected, "image/jpeg"),
                ])
                self.assertEqual(refresh.selected, 2)
                # Posters never ask for forbidden-as-missing.
                for probe_call in self.probe.call_args_list:
                    self.assertNotIn("forbidden_missing", probe_call.kwargs)

    def test_portrait_sizes_and_suffixes_are_tried_in_order(self):
        for style, asset, sizes, selected in (
            ("portrait_banner", 21040818, ("b_v8", "b_v12"), "p21040818_b_v12_aa.jpg"),
            ("portrait_iconic", 18535690, ("i_v8", "i_v12", "i_v9", "i_v13"), "p18535690_i_v13_aa.jpg"),
        ):
            with self.subTest(style=style):
                self.channels[0] = grid_channel("32645", "ESPN", ESPN, asset_id=asset)
                self.write_guide()
                original = ET.parse(self.path).getroot()
                expected = f"https://zpmc.tmsimg.com/assets/{selected}"
                self.probe.reset_mock()
                self.probe.side_effect = lambda url, *_: "valid" if url == expected else "missing"
                self.refresh(programme_artwork_style=style).apply(self.path, {"32645"})
                self.assertEqual([args[:2] for args, _ in self.probe.call_args_list], [
                    (f"https://zpmc.tmsimg.com/assets/p{asset}_{size}_{suffix}.jpg", "image/jpeg")
                    for size in sizes for suffix in ("ab", "aa")
                ])
                original.find('./programme[@channel="32645"]/icon').set("src", expected)
                self.assertEqual(ET.tostring(ET.parse(self.path).getroot()), ET.tostring(original))

    def test_square_style_and_square_input_keep_the_asset_id(self):
        self.channels[0]["events"][0]["program"]["preferredImage"] = {
            "uri": "//zap2it.tmsimg.com/assets/p18535690_i_s4_ab.jpg",
        }
        self.write_guide()
        self.refresh(programme_artwork_style="square_iconic").apply(self.path, {"32645"})
        square = "https://zpmc.tmsimg.com/assets/p18535690_i_s4_ab.jpg"
        self.assertEqual(icon_urls(self.path, "programme")["32645"], square)
        self.probe.assert_called_once()
        self.assertEqual(self.probe.call_args.args[:2], (square, "image/jpeg"))
        source = square.replace("zpmc", "dshm").replace("_ab.jpg", "_aa.jpg")
        candidates = artwork.candidates(source, artwork.PROGRAMME_STYLES["portrait_iconic"])
        self.assertEqual(candidates, tuple(
            f"https://zpmc.tmsimg.com/assets/p18535690_i_{size}_aa.jpg" for size in ("v8", "v12", "v9", "v13")
        ))
        self.assertEqual(artwork.candidates(source + "?extra", ("i_s4",)), ())

    def test_portrait_misses_share_the_budget_and_persist(self):
        original = self.path.read_bytes()
        other = self.root / "second.xml"
        other.write_bytes(original.replace(b"p21040818", b"p8130405"))
        second_original = other.read_bytes()
        self.probe.side_effect = lambda *_: "missing"
        with patch.object(artwork, "MAX_REQUESTS", 5):
            refresh = self.refresh(programme_artwork_style="portrait_banner")
            refresh.apply(self.path, {"32645"})
            refresh.apply(other, {"32645"})
        self.assertEqual(self.probe.call_count, 5)
        self.assertEqual(refresh.budget_skipped, 2)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(other.read_bytes(), second_original)

        self.probe.reset_mock()
        self.refresh(programme_artwork_style="portrait_banner").apply(self.path, {"32645"})
        self.assertEqual(self.probe.call_count, 3)
        self.probe.reset_mock()
        self.refresh(programme_artwork_style="portrait_banner").apply(self.path, {"32645"})
        self.probe.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)

    def test_saved_landscape_cache_keys_are_still_used(self):
        expires = int(artwork.time.time()) + 60
        first = ICONIC.replace("_aa.jpg", "_ab.jpg")
        self.cache.write_text(json.dumps({
            hashlib.sha256(f"i_h9\0{LEGACY_POSTER}\0{first}".encode()).hexdigest(): [expires, False],
            hashlib.sha256(f"i_h9\0{LEGACY_POSTER}\0{ICONIC}".encode()).hexdigest(): [expires, True],
        }))
        self.refresh(programme_artwork_style="landscape_iconic").apply(self.path, {"32645"})
        self.assertEqual(icon_urls(self.path, "programme")["32645"], ICONIC)
        self.probe.assert_not_called()
        stored = json.loads(self.cache.read_text())
        self.assertIn(
            hashlib.sha256(f"i_h9\0{POSTER}\0{ICONIC}".encode()).hexdigest(), stored,
        )

    def test_artwork_off_omits_programme_icons_and_still_selects_station_style(self):
        refresh = self.refresh(
            station_logo_style="gray", programme_artwork_style="landscape_banner", include_programme_artwork=False,
        )
        refresh.apply(self.path, {"32645", "20454"})
        # programme icons are omitted; station logos still become SD gray URLs.
        self.assertEqual(icon_urls(self.path, "channel"), {"32645": sd("32645", "gray"), "20454": sd("20454", "gray")})
        self.assertEqual(icon_urls(self.path, "programme"), {})
        self.assertEqual([args[0] for args, _ in self.probe.call_args_list], [sd("32645", "gray"), sd("20454", "gray")])

    def test_unrecognized_station_urls_have_no_sd_candidates(self):
        for original in (
            ESPN.replace("zpmc.tmsimg.com", "zpmc.tmsimg.com.example.com"),
            "not a URL",
            "https://example.com/assets/p1_b_v9_ab.jpg",
        ):
            with self.subTest(original=original):
                self.assertEqual(artwork._station_logo_id(original), None)
        self.assertEqual(artwork._station_logo_id(ESPN), "32645")
        self.assertEqual(artwork._station_logo_id(WBBM), "28711")
        self.assertEqual(artwork._station_logo_candidates("32645", "32645", "dark"), (sd("32645", "dark"),))
        self.assertEqual(
            artwork._station_logo_candidates("20454", "28711", "dark"),
            (sd("20454", "dark"), sd("28711", "dark")),
        )
        self.assertEqual(artwork._station_logo_candidates("", "28711", "dark"), (sd("28711", "dark"),))
        self.assertEqual(artwork._station_logo_candidates("abc", "", "dark"), ())

    def test_unrecognized_poster_urls_and_identical_suffix(self):
        for url in (
            POSTER.replace("zpmc.tmsimg.com", "zpmc.tmsimg.com.example.com"),
            POSTER.replace("zpmc.tmsimg.com", "zpmc.tmsimg.com@localhost"),
            POSTER.replace("zpmc.tmsimg.com", "zpmc.tmsimg.com:443"),
            POSTER.replace("/assets/", "/../assets/"),
            POSTER.replace("p21040818", "arbitrary"),
            POSTER + "#fragment", "not a URL",
        ):
            with self.subTest(url=url):
                self.assertEqual(artwork.candidates(url, "i_h9"), ())
        suffix_aa = POSTER.replace("_ab.jpg", "_aa.jpg")
        self.assertEqual(artwork.candidates(suffix_aa, "i_h9"), (ICONIC,))
        self.assertEqual(artwork.candidates(POSTER.replace("/assets/", "/unknown/"), "i_h9"), ())

    def test_429_stops_new_requests_and_transient_failure_is_retried_next_refresh(self):
        self.probe.side_effect = ["temporary", "rate_limited"]
        refresh = self.refresh(station_logo_style="dark", programme_artwork_style="landscape_iconic")
        refresh.apply(self.path, {"32645"})
        self.assertEqual(icon_urls(self.path, "channel"), {"32645": ESPN, "20454": WBBM})
        self.assertTrue(refresh.rate_limited)
        self.assertEqual(self.probe.call_count, 2)
        self.assertEqual(json.loads(self.cache.read_text()), {})
        self.probe.side_effect = None
        self.probe.reset_mock()
        refresh = self.refresh(station_logo_style="dark")
        refresh.apply(self.path, set())
        self.assertEqual(icon_urls(self.path, "channel")["32645"], sd("32645", "dark"))
        self.assertEqual(self.probe.call_count, 2)

    def test_corrupt_cache_recovers_and_unwritable_cache_keeps_originals(self):
        self.cache.write_bytes(b"{invalid json")
        refresh = self.refresh(station_logo_style="white")
        refresh.apply(self.path, set())
        self.assertEqual(len(json.loads(self.cache.read_text())), 2)
        self.assertEqual(self.probe.call_count, 2)

        self.write_guide()
        self.probe.reset_mock()
        with patch.object(artwork, "_publish_output", side_effect=PermissionError):
            refresh = self.refresh(station_logo_style="white")
            refresh.apply(self.path, set())
        self.assertFalse(refresh.cache_writable)
        self.assertEqual(icon_urls(self.path, "channel"), {"32645": ESPN, "20454": WBBM})
        self.probe.assert_not_called()

    def test_cache_failure_after_probing_keeps_original_logos_and_removes_programme_icons(self):
        publish = artwork._publish_output
        cache_writes = 0

        def publish_output(output, path, mode=None):
            nonlocal cache_writes
            if path == self.cache:
                cache_writes += 1
                if cache_writes == 2:
                    raise PermissionError("fixture cache error")
            publish(output, path, mode)

        refresh = self.refresh(station_logo_style="dark", include_programme_artwork=False)
        with patch.object(artwork, "_publish_output", side_effect=publish_output):
            refresh.apply(self.path, set())
        self.assertEqual(self.probe.call_count, 2)
        self.assertFalse(refresh.cache_writable)
        self.assertEqual(icon_urls(self.path, "channel"), {"32645": ESPN, "20454": WBBM})
        self.assertEqual(icon_urls(self.path, "programme"), {})
        self.assertEqual(set(self.root.iterdir()), {self.path, self.cache})

    def test_late_parse_error_preserves_original_guide_and_removes_staging_file(self):
        original = self.path.read_bytes().replace(b"</tv>", b"<unclosed>")
        self.path.write_bytes(original)
        with self.assertRaises(ET.ParseError):
            self.refresh(include_programme_artwork=False).apply(self.path, set())
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_expired_cache_entries_are_probed_again(self):
        self.probe.return_value = "missing"
        self.refresh(station_logo_style="white").apply(self.path, set())
        self.probe.reset_mock()
        now = artwork.time.time()
        with patch.object(artwork.time, "time", return_value=now + artwork.MISS_AGE + 1):
            self.refresh(station_logo_style="white").apply(self.path, set())
        self.assertEqual(self.probe.call_count, 3)

    def test_negatives_use_a_seven_day_ttl(self):
        self.assertEqual(artwork.MISS_AGE, 7 * 24 * 60 * 60)

    def test_elapsed_lookup_limit_stops_waiting_and_prevents_further_requests(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.probe.side_effect = lambda *args, **kwargs: release.wait(5)
        with patch.object(artwork, "MAX_LOOKUP_SECONDS", 0.02):
            refresh = self.refresh(station_logo_style="white")
            refresh.apply(self.path, set())
        self.assertEqual(refresh.station_requests, 1)
        self.assertEqual(refresh.station_budget_skipped, 1)
        self.assertEqual(refresh.station_fallback, 2)
        self.assertEqual(icon_urls(self.path, "channel"), {"32645": ESPN, "20454": WBBM})

    def test_station_elapsed_share_leaves_time_for_programme_requests(self):
        refresh = self.refresh(station_logo_style="white", programme_artwork_style="landscape_iconic")
        refresh.reserve_programme = True
        refresh.lookup_seconds = refresh.station_seconds = 17
        refresh.apply(self.path, {"32645"})
        self.assertEqual(icon_urls(self.path, "channel"), {"32645": ESPN, "20454": WBBM})
        self.assertEqual(icon_urls(self.path, "programme")["32645"], ICONIC.replace("_aa.jpg", "_ab.jpg"))
        self.probe.assert_called_once()
        self.assertEqual(self.probe.call_args.args[1], "image/jpeg")
        self.assertLess(refresh.lookup_seconds, artwork.MAX_LOOKUP_SECONDS)

    def test_hard_station_timeout_leaves_programme_style_available(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def probe(_url, content_type, _deadline, forbidden_missing=False):
            return release.wait(5) if content_type == "image/png" else "valid"

        self.probe.side_effect = probe
        refresh = self.refresh(station_logo_style="white", programme_artwork_style="landscape_iconic")
        with patch.object(artwork, "REQUEST_SECONDS", 0.1):
            refresh.apply(self.path, {"32645"})
        self.assertEqual(refresh.station_requests, 1)
        self.assertEqual(refresh.requests, 1)
        self.assertEqual(refresh.timed_out_types, {"image/png"})
        self.assertEqual(icon_urls(self.path, "programme")["32645"], ICONIC.replace("_aa.jpg", "_ab.jpg"))

    def test_skipped_distinct_programmes_do_not_grow_probe_memo(self):
        with self.path.open("wb") as fixture:
            fixture.write(b"<tv>")
            for asset in range(500):
                url = POSTER.replace("21040818", str(asset))
                fixture.write(f'<programme channel="32645"><icon src="{url}" /></programme>'.encode())
            fixture.write(b"</tv>")
        with patch.object(artwork, "MAX_REQUESTS", 1):
            refresh = self.refresh(programme_artwork_style="landscape_iconic")
            refresh.apply(self.path, {"32645"})
        self.assertEqual(refresh.budget_skipped, 499)
        self.assertEqual(len(refresh.probed_assets), 1)

    def test_large_xml_rewrite_has_bounded_peak_memory(self):
        rewrite = dedent("""
            import resource
            import sys
            from pathlib import Path
            from src.zap2xml.artwork import ArtworkRefresh

            guide = Path(sys.argv[1])
            refresh = ArtworkRefresh({"include_programme_artwork": False}, guide.with_suffix(".json"))
            refresh.apply(guide, set())
            peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            print(peak_rss // (1024 if sys.platform == "darwin" else 1))
            with guide.open("rb") as output:
                assert output.readline().startswith(b"<?xml")
                assert output.readline() == b'<tv generator-info-name="fixture">\\n'
                assert output.readline().strip() == b'<channel id="1"><icon src="station.png" /></channel>'
                count = 0
                for line in output:
                    if line.startswith(b"  <programme"):
                        assert b"<icon" not in line
                        count += 1
                assert count == int(sys.argv[2])
        """)
        programme = (
            b'<programme channel="1" start="20261124160000 +0000" stop="20261124170000 +0000">'
            b'<title>Fixture</title><desc>' + b"Programme description " * 34 +
            b'</desc><category>Sports</category><icon src="programme.jpg" /></programme>'
        )
        measurements = []
        for count in (8_000, 164_000):
            with self.path.open("wb") as fixture:
                fixture.write(b'<tv generator-info-name="fixture"><channel id="1"><icon src="station.png" /></channel>')
                for _ in range(count):
                    fixture.write(programme)
                fixture.write(b"</tv>")
            input_size = self.path.stat().st_size
            result = subprocess.check_output(
                [sys.executable, "-B", "-c", rewrite, str(self.path), str(count)],
                text=True, timeout=60,
            )
            measurements.append(int(result))
            print(
                f"XMLTV rewrite for {count:,} programmes ({input_size:,} bytes) peaked at {int(result) / 1024:.1f} MiB",
                file=sys.stderr,
            )
        self.assertLess(measurements[1] - measurements[0], 24 * 1024)

    def test_guide_rewrites_only_eligible_programme_icons(self):
        self.channels[0]["thumbnail"] = "https://dshm.tmsimg.com/h3/NowShowing/1/s1_ll_h15_ab.png"
        self.channels[0]["events"][0]["program"]["preferredImage"]["uri"] = (
            "https://dshm.tmsimg.com/assets/p21040818_p_v9_ab.jpg"
        )
        self.channels[1]["events"][0]["program"]["preferredImage"]["uri"] = (
            "https://example.com/assets/p1_b_v9_ab.jpg"
        )
        self.write_guide()
        self.assertEqual(icon_urls(self.path, "programme"), {
            "32645": "https://zpmc.tmsimg.com/assets/p21040818_p_v9_ab.jpg",
            "20454": "https://example.com/assets/p1_b_v9_ab.jpg",
        })
        self.assertEqual(
            icon_urls(self.path, "channel")["32645"],
            "https://dshm.tmsimg.com/h3/NowShowing/1/s1_ll_h15_ab.png",
        )

    def test_request_limit_above_default(self):
        refresh = self.refresh(artwork_request_limit=200)
        self.assertEqual(refresh.request_limit, 200)
        for _ in range(129):
            self.assertEqual(refresh._request(LEGACY_POSTER, "image/jpeg"), "valid")
        self.assertEqual(refresh.requests, 129)

    def test_zero_request_limit_reuses_cached_variants(self):
        expires = int(artwork.time.time()) + 60
        self.cache.write_text(json.dumps({
            hashlib.sha256(f"i_h9\0{POSTER}\0{ICONIC}".encode()).hexdigest(): [expires, True],
        }))
        refresh = self.refresh(programme_artwork_style="landscape_iconic", artwork_request_limit=0)
        refresh.apply(self.path, {"32645"})
        self.assertEqual(icon_urls(self.path, "programme")["32645"], ICONIC)
        self.probe.assert_not_called()
        self.assertEqual(refresh.requests, 0)

    def test_zero_station_limit_reuses_cached_variants(self):
        expires = int(artwork.time.time()) + 60
        self.cache.write_text(json.dumps({
            hashlib.sha256(f"dark\0{32645}\0{sd('32645', 'dark')}".encode()).hexdigest(): [expires, True],
        }))
        refresh = self.refresh(station_logo_style="dark", station_logo_request_limit=0)
        refresh.apply(self.path, set())
        self.assertEqual(icon_urls(self.path, "channel")["32645"], sd("32645", "dark"))
        self.probe.assert_not_called()
        self.assertEqual(refresh.station_requests, 0)

    def test_invalid_request_settings_use_defaults(self):
        for value in (True, False, 12.5, -1, "abc", "12.5", "\u00b2", "\u0661\u0662", "", "  ", None, [], {}):
            with self.subTest(value=value):
                refresh = self.refresh(artwork_request_limit=value, station_logo_request_limit=value)
                self.assertEqual(refresh.request_limit, 128)
                self.assertEqual(refresh.station_limit, 2500)
        self.assertEqual(self.refresh(artwork_request_limit="200").request_limit, 200)
        self.assertEqual(self.refresh(station_logo_request_limit="300").station_limit, 300)

    def test_whole_setting_rejects_invalid_persisted_strings(self):
        for value in ("", " ", "abc", "12.5", "-1", "+1", "1 2", "\u00b2", "\u0661\u0662", "1\u00b2"):
            with self.subTest(value=value):
                self.assertEqual(artwork._whole_setting(value, 128, 0), 128)
                self.assertEqual(artwork._whole_setting(value, 25, 0, 100), 25)
        self.assertEqual(artwork._whole_setting(" 0 ", 128, 0), 0)
        self.assertEqual(artwork._whole_setting(" 200 ", 128, 0), 200)

    def test_one_station_budget_is_shared_across_outputs(self):
        refresh = self.refresh(station_logo_style="white", station_logo_request_limit=1)
        refresh.apply(self.path, set())
        self.assertEqual(refresh.station_requests, 1)
        other = self.root / "other.xml"
        channels = [
            grid_channel("11111", "Other", "https://zpmc.tmsimg.com/h3/NowShowing/11111/s11111_ll_h15_ab.png"),
            grid_channel("22222", "Other2", "https://zpmc.tmsimg.com/h3/NowShowing/22222/s22222_ll_h15_ab.png", asset_id=28711),
        ]
        zap2xml.write_xmltv(channels, other)
        refresh.apply(other, set())
        self.assertLessEqual(refresh.station_requests, 1)
        self.assertGreater(refresh.station_budget_skipped, 0)


class ProgrammeIconTests(unittest.TestCase):
    def icon(self, program=None, event=None):
        return zap2xml._program_icon(program or {}, event or {})

    def test_legacy_hosts_and_bare_names_use_the_public_cdn(self):
        cases = (
            ({"preferredImage": {"uri": "https://dshm.tmsimg.com/assets/p21040818_p_v9_ab.jpg"}},
             "https://zpmc.tmsimg.com/assets/p21040818_p_v9_ab.jpg"),
            ({"image": "http://zap2it.tmsimg.com/assets/GNLZZGG003FKQNX.jpg"},
             "https://zpmc.tmsimg.com/assets/GNLZZGG003FKQNX.jpg"),
            ({"image": "//zap2it.tmsimg.com/assets/p1_b_v9_ab.jpg"},
             "https://zpmc.tmsimg.com/assets/p1_b_v9_ab.jpg"),
            ({"image": "GNLZZGG003FKQNX.jpg"},
             "https://zpmc.tmsimg.com/assets/GNLZZGG003FKQNX.jpg"),
            ({"image": "p21040818_p_v9_ab"},
             "https://zpmc.tmsimg.com/assets/p21040818_p_v9_ab.jpg"),
            ({"image": "https://zpmc.tmsimg.com/assets/p1_b_v9_ab.jpg"},
             "https://zpmc.tmsimg.com/assets/p1_b_v9_ab.jpg"),
            ({"image": "https://example.com/assets/p1_b_v9_ab.jpg"},
             "https://example.com/assets/p1_b_v9_ab.jpg"),
            ({"image": "https://dshm.tmsimg.com/h3/NowShowing/1/s1_ll_h15_ab.png"},
             "https://dshm.tmsimg.com/h3/NowShowing/1/s1_ll_h15_ab.png"),
        )
        for program, expected in cases:
            with self.subTest(program=program):
                self.assertEqual(self.icon(program), expected)

    def test_event_thumbnail_is_corrected_and_query_is_stripped(self):
        self.assertEqual(
            self.icon({}, {"thumbnail": "https://dshm.tmsimg.com/assets/p1_b_v9_ab.jpg"}),
            "https://zpmc.tmsimg.com/assets/p1_b_v9_ab.jpg",
        )
        self.assertEqual(
            self.icon({"image": "https://dshm.tmsimg.com/assets/p1_b_v9_ab.jpg?keep=1"}),
            "https://zpmc.tmsimg.com/assets/p1_b_v9_ab.jpg",
        )


class PluginArtworkTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.settings = {
            "lineup_ids": ",".join(LINEUPS),
            "programme_artwork_style": "landscape_iconic",
            "merge_lineups": False,
            "user_agent": "Fixture",
        }
        self.logger = Mock(spec=logging.Logger)
        self.enterContext(patch.dict(os.environ, {"DISPATCHARR_DATA_DIR": str(self.root)}))
        self.enterContext(patch.object(plugin, "PLUGIN_DIR", self.root))
        self.enterContext(patch.object(plugin, "_ensure_scheduler_started"))
        self.enterContext(patch.object(zap2xml, "_rewrite_first_display_name_with_station_name"))
        self.instance = plugin.Plugin()
        self.enterContext(patch.object(self.instance, "_run_zap2it_script", side_effect=self.run_lineup))
        self.enterContext(patch.object(self.instance, "_cleanup_plugin_workspace"))
        self.sources = {}
        self.enterContext(patch.object(self.instance, "_create_or_update_epg_source", side_effect=self.update_source))
        self.mapped = self.enterContext(patch.object(self.instance, "_mapped_station_ids", return_value={"20454"}))
        self.probe = self.enterContext(patch.object(artwork, "_probe", return_value="valid"))

    def run_lineup(self, *, lineup_id, **_kwargs):
        path = self.root / f"{lineup_id}.xml"
        if lineup_id == LINEUPS[0]:
            fixture = grid_channel("32645", "ESPN", ESPN)
        else:
            fixture = grid_channel("20454", "WBBM", WBBM, asset_id=28711)
        zap2xml.write_xmltv([fixture], path)
        return "ok", str(path), "ok", "fixture command"

    def update_source(self, *, epg_name, xml_path, **_kwargs):
        self.sources[epg_name] = xml_path
        return {"status": "ok", "epg_name": epg_name, "file": xml_path}

    def test_separate_lineups_use_sd_station_logos_and_a_shared_station_budget(self):
        self.settings["station_logo_style"] = "white"
        self.mapped.side_effect = [set(), {"20454"}]
        result = self.instance.run("download_epg", {}, {"settings": self.settings, "logger": self.logger})
        self.assertEqual(result["status"], "queued")
        self.assertEqual(self.mapped.call_args_list, [call(f"Zap2XML ({lu})") for lu in LINEUPS])
        requests = list(self.probe.call_args_list)
        # Mapped station 20454 is resolved first (prepare_mapped_logos), then the
        # stations in output order, using the SD logo-server URLs.
        self.assertEqual(requests[0].args[0], sd("20454", "white"))
        self.assertTrue(requests[0].kwargs["forbidden_missing"])
        unmapped_guide = self.sources[f"Zap2XML ({LINEUPS[0]})"]
        self.assertEqual(icon_urls(unmapped_guide, "channel"), {"32645": sd("32645", "white")})
        mapped_guide = self.sources[f"Zap2XML ({LINEUPS[1]})"]
        self.assertEqual(icon_urls(mapped_guide, "channel"), {"20454": sd("20454", "white")})
        self.assertEqual(icon_urls(mapped_guide, "programme"), {
            "20454": "https://zpmc.tmsimg.com/assets/p28711_i_h9_ab.jpg",
        })
        self.logger.info.assert_called_once()
        summary = self.logger.info.call_args.args[0]
        self.assertIn("station-logos selected=2 fallback=0 cache-hit=0 budget-skipped=0 requests=2", summary)

    def test_two_consecutive_downloads_reuse_the_artwork_cache(self):
        self.settings["station_logo_style"] = "dark"
        self.settings["programme_artwork_style"] = "provider_default"
        self.mapped.return_value = set()

        # The real workspace cleanup removes transient xmltv files and the
        # cache/ directory but must leave .artwork-cache.json untouched.
        def real_cleanup(logger=None):
            for name in os.listdir(self.root):
                if name.startswith("xmltv_") and name.endswith(".xml"):
                    (self.root / name).unlink(missing_ok=True)
            cache_dir = self.root / "cache"
            if cache_dir.is_dir():
                shutil.rmtree(cache_dir)

        self.instance._cleanup_plugin_workspace.side_effect = real_cleanup

        first = self.instance.run("download_epg", {}, {"settings": self.settings, "logger": self.logger})
        self.assertEqual(first["status"], "queued")
        self.assertIn("station-logos selected=2", self.logger.info.call_args.args[0])

        self.probe.reset_mock()
        self.logger.reset_mock()
        second = self.instance.run("download_epg", {}, {"settings": self.settings, "logger": self.logger})
        self.assertEqual(second["status"], "queued")
        self.probe.assert_not_called()
        summary = self.logger.info.call_args.args[0]
        self.assertIn("station-logos selected=2 fallback=0 cache-hit=2", summary)
        self.assertIn("station-logos selected=2 fallback=0 cache-hit=2 budget-skipped=0 requests=0", summary)

    def test_scheduled_merged_download_uses_merged_source_mapping(self):
        self.settings["merge_lineups"] = True
        published = self.root / "epgs/zap2xml_merged.xml"
        published.parent.mkdir()
        published.write_text("previous guide")

        def probe(*args, **kwargs):
            return "valid" if published.read_text() == "previous guide" else "temporary"

        self.probe.side_effect = probe
        config = SimpleNamespace(objects=Mock())
        config.objects.filter.return_value.first.return_value = SimpleNamespace(enabled=True, settings=self.settings)
        with (
            patch.dict("sys.modules", {"apps.plugins.models": SimpleNamespace(PluginConfig=config)}),
            patch.object(plugin, "Plugin", return_value=self.instance),
        ):
            plugin._scheduler_tick(self.logger)
        self.mapped.assert_called_once_with("Zap2XML (merged)")
        self.assertEqual(set(self.sources), {"Zap2XML (merged)"})
        self.assertEqual(icon_urls(self.sources["Zap2XML (merged)"], "programme"), {
            "32645": POSTER, "20454": "https://zpmc.tmsimg.com/assets/p28711_i_h9_ab.jpg",
        })
        self.assertEqual(self.probe.call_count, 1)
        self.assertEqual(list(published.parent.iterdir()), [published])
        self.assertIn("last_generated", json.loads((self.root / ".download-state.json").read_text()))

    def test_mapping_query_uses_only_attached_ids_from_the_matching_source(self):
        source = SimpleNamespace(id=21)
        source_model = SimpleNamespace(objects=Mock())
        source_model.objects.filter.return_value.first.return_value = source
        epg_data = SimpleNamespace(objects=Mock())
        epg_data.objects.filter.return_value.values_list.return_value = ["20454", None]
        attached = Mock(return_value={123})
        with patch.dict("sys.modules", {
            "apps.channels.managers": SimpleNamespace(epg_ids_mapped_to_channels=attached),
            "apps.epg.models": SimpleNamespace(EPGSource=source_model, EPGData=epg_data),
        }):
            ids = plugin.Plugin._mapped_station_ids("Zap2XML (USA-OTA53963)")
            self.assertEqual(ids, {"20454"})
            source_model.objects.filter.assert_called_once_with(name="Zap2XML (USA-OTA53963)", source_type="xmltv")
            attached.assert_called_once_with(epg_source=source)
            epg_data.objects.filter.assert_called_once_with(epg_source=source, pk__in={123})
            source_model.objects.filter.return_value.first.return_value = None
            self.assertEqual(plugin.Plugin._mapped_station_ids("new source"), set())
            self.assertEqual(attached.call_count, 1)

    def test_mapping_error_does_not_fail_refresh(self):
        self.mapped.side_effect = RuntimeError("fixture database error")
        result = self.instance.run("download_epg", {}, {"settings": self.settings, "logger": self.logger})
        self.assertEqual(result["status"], "queued")
        self.probe.assert_not_called()
        self.assertEqual(len(self.sources), 2)
        self.assertIn("last_generated", json.loads((self.root / ".download-state.json").read_text()))

    def test_manifest_and_plugin_fields_agree(self):
        manifest = json.loads((Path(plugin.__file__).parent / "plugin.json").read_text())
        self.assertEqual(manifest["fields"], plugin.Plugin.fields)


if __name__ == "__main__":
    unittest.main()