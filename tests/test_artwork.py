import json
import logging
import os
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, call, patch

from src.zap2xml import artwork, plugin, zap2xml


ESPN = "https://zpmc.tmsimg.com/h3/NowShowing/32645/s32645_ll_h15_ab.png"
WBBM = "https://zpmc.tmsimg.com/h3/NowShowing/20454/s28711_ll_h15_ab.png"
POSTER = "https://dshm.tmsimg.com/assets/p21040818_p_v9_ab.jpg"
ICONIC = "https://zpmc.tmsimg.com/assets/p21040818_i_h9_aa.jpg"
BANNER = "https://zpmc.tmsimg.com/assets/p21040818_b_h8_aa.jpg"
LINEUPS = ("USA-IL54437-X", "USA-OTA53963")


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

    def test_station_styles_keep_path_and_asset_and_persist_hits_and_misses(self):
        self.probe.side_effect = ["missing", "valid", "missing", "missing"]
        refresh = self.refresh(station_logo_style="white")
        refresh.apply(self.path, set())
        espn_white = ESPN.replace("_ll_", "_lw_").replace("_ab.png", "_aa.png")
        self.assertEqual(icon_urls(self.path, "channel"), {"32645": espn_white, "20454": WBBM})
        self.assertEqual([args[0] for args, _ in self.probe.call_args_list], [
            ESPN.replace("_ll_", "_lw_"), espn_white,
            WBBM.replace("_ll_", "_lw_"), WBBM.replace("_ll_", "_lw_").replace("_ab.png", "_aa.png"),
        ])
        self.assertIn("selected=1 fallback=1 cache-hit=0 budget-skipped=0 requests=4", refresh.summary())
        self.assertEqual(sorted(entry[1] for entry in json.loads(self.cache.read_text()).values()), [
            False, False, False, True,
        ])

        self.write_guide()
        self.probe.reset_mock()
        refresh = self.refresh(station_logo_style="white")
        refresh.apply(self.path, set())
        self.assertEqual(icon_urls(self.path, "channel"), {"32645": espn_white, "20454": WBBM})
        self.assertEqual(refresh.cache_hits, 2)
        self.probe.assert_not_called()

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
                self.assertEqual(refresh.selected, 1)

    def test_artwork_off_omits_programme_icons_and_still_selects_station_style(self):
        baseline = ET.parse(self.path)
        for event in baseline.iterfind("./programme"):
            event.remove(event.find("icon"))
        for icon in baseline.iterfind("./channel/icon"):
            icon.set("src", icon.get("src").replace("_ll_", "_lg_"))
        refresh = self.refresh(
            station_logo_style="gray", programme_artwork_style="landscape_banner", include_programme_artwork=False,
        )
        refresh.apply(self.path, {"32645", "20454"})
        self.assertEqual(ET.tostring(ET.parse(self.path).getroot()), ET.tostring(baseline.getroot()))
        self.assertEqual([args[:2] for args, _ in self.probe.call_args_list], [
            (ESPN.replace("_ll_", "_lg_"), "image/png"), (WBBM.replace("_ll_", "_lg_"), "image/png"),
        ])

    def test_unrecognized_urls_and_identical_suffix(self):
        for url in (
            ESPN.replace("zpmc.tmsimg.com", "zpmc.tmsimg.com.example.com"),
            ESPN.replace("zpmc.tmsimg.com", "zpmc.tmsimg.com@localhost"),
            ESPN.replace("zpmc.tmsimg.com", "zpmc.tmsimg.com:443"),
            ESPN.replace("/h3/", "/../"), ESPN.replace("_ll_", "_zz_"),
            ESPN.replace("s32645", "arbitrary"), ESPN + "#fragment", "not a URL",
        ):
            with self.subTest(url=url):
                self.assertEqual(artwork.candidates(url, "lg", station=True), ())
        suffix_aa = POSTER.replace("_ab.jpg", "_aa.jpg")
        self.assertEqual(artwork.candidates(suffix_aa, "i_h9", station=False), (ICONIC,))
        self.assertEqual(artwork.candidates(POSTER.replace("/assets/", "/unknown/"), "i_h9", station=False), ())

    def test_429_stops_new_requests_and_transient_failure_is_retried_next_refresh(self):
        self.probe.side_effect = ["temporary", "rate_limited"]
        refresh = self.refresh(station_logo_style="dark", programme_artwork_style="landscape_iconic")
        refresh.apply(self.path, {"32645"})
        self.assertEqual(icon_urls(self.path, "channel"), {"32645": ESPN, "20454": WBBM})
        self.assertTrue(refresh.rate_limited)
        self.assertEqual(self.probe.call_count, 2)
        self.assertEqual(json.loads(self.cache.read_text()), {})
        self.probe.side_effect = None
        refresh = self.refresh(station_logo_style="dark")
        refresh.apply(self.path, set())
        self.assertEqual(icon_urls(self.path, "channel")["32645"], ESPN.replace("_ll_", "_ld_"))
        self.assertEqual(self.probe.call_count, 4)

    def test_corrupt_cache_recovers_and_unwritable_cache_keeps_originals(self):
        self.cache.write_bytes(b"{invalid json")
        refresh = self.refresh(station_logo_style="white")
        refresh.apply(self.path, set())
        self.assertEqual(len(json.loads(self.cache.read_text())), 2)
        self.assertEqual(self.probe.call_count, 2)

        self.write_guide()
        self.probe.reset_mock()
        with patch.object(artwork, "_atomic_output", side_effect=PermissionError):
            refresh = self.refresh(station_logo_style="white")
            refresh.apply(self.path, set())
        self.assertFalse(refresh.cache_writable)
        self.assertEqual(icon_urls(self.path, "channel"), {"32645": ESPN, "20454": WBBM})
        self.probe.assert_not_called()

    def test_expired_cache_entries_are_probed_again(self):
        self.probe.return_value = "missing"
        self.refresh(station_logo_style="white").apply(self.path, set())
        self.probe.reset_mock()
        now = artwork.time.time()
        with patch.object(artwork.time, "time", return_value=now + artwork.MISS_AGE + 1):
            self.refresh(station_logo_style="white").apply(self.path, set())
        self.assertEqual(self.probe.call_count, 4)

    def test_elapsed_lookup_limit_stops_waiting_and_prevents_further_requests(self):
        release = threading.Event()
        self.addCleanup(release.set)
        self.probe.side_effect = lambda *_: release.wait(5)
        with patch.object(artwork, "MAX_LOOKUP_SECONDS", 0.02):
            refresh = self.refresh(station_logo_style="white")
            refresh.apply(self.path, set())
        self.assertEqual(refresh.requests, 1)
        self.assertEqual(refresh.budget_skipped, 1)
        self.assertEqual(icon_urls(self.path, "channel"), {"32645": ESPN, "20454": WBBM})


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

    def test_manual_separate_lineups_share_one_cap_and_prioritize_station_logos(self):
        self.settings["station_logo_style"] = "white"
        self.mapped.side_effect = [{"32645"}, {"20454"}]
        with patch.object(artwork, "MAX_REQUESTS", 2):
            result = self.instance.run("download_epg", {}, {"settings": self.settings, "logger": self.logger})
        self.assertEqual(result["status"], "queued")
        self.assertEqual(self.mapped.call_args_list, [call(f"Zap2XML ({lu})") for lu in LINEUPS])
        self.assertEqual([args[:2] for args, _ in self.probe.call_args_list], [
            (ESPN.replace("_ll_", "_lw_"), "image/png"), (WBBM.replace("_ll_", "_lw_"), "image/png"),
        ])
        self.assertEqual(icon_urls(self.sources[f"Zap2XML ({LINEUPS[0]})"], "programme"), {"32645": POSTER})
        self.assertEqual(icon_urls(self.sources[f"Zap2XML ({LINEUPS[1]})"], "channel"), {
            "20454": WBBM.replace("_ll_", "_lw_"),
        })
        self.logger.info.assert_called_once()
        self.assertIn("selected=2 fallback=2 cache-hit=0 budget-skipped=2 requests=2", self.logger.info.call_args.args[0])

    def test_scheduled_merged_download_uses_merged_source_mapping(self):
        self.settings["merge_lineups"] = True
        published = self.root / "epgs/zap2xml_merged.xml"
        published.parent.mkdir()
        published.write_text("previous guide")

        def probe(*_):
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
