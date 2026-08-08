from __future__ import annotations

import json
import unittest

from foldarium_pipeline.cameo import (
    CameoError,
    af3_availability,
    af3_import_manifest,
    af3_prediction_urls,
    parse_sitemap_targets,
    parse_target_page,
    validate_coordinate_url,
)


def payload(target_id: str = "2026-06-20_00000082") -> dict:
    return {
        "target": {"id": target_id, "week_id": target_id[:10]},
        "entities": [],
        "biounits": [{"assembly_id": 1}, {"assembly_id": 2}],
        "predictions": [
            {
                "server_id": "993_3D",
                "model": 1,
                "complex_mdl_filename": "some/path/model-1.cif",
                "complex_assembly_id": 2,
            }
        ],
    }


class SitemapTests(unittest.TestCase):
    def test_filters_and_sorts_targets_for_one_week(self) -> None:
        xml = """<?xml version="1.0"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://cameo3d.org/target/2026-08-01_00000009</loc></url>
          <url><loc>https://cameo3d.org/target/2026-07-25_00000002</loc></url>
          <url><loc>https://cameo3d.org/target/2026-08-01_00000001</loc></url>
        </urlset>"""
        self.assertEqual(
            parse_sitemap_targets(xml, "2026-08-01"),
            ["2026-08-01_00000001", "2026-08-01_00000009"],
        )

    def test_invalid_xml_is_rejected(self) -> None:
        with self.assertRaises(CameoError):
            parse_sitemap_targets("not xml")


class TargetPageTests(unittest.TestCase):
    def test_decodes_nextjs_flight_payload(self) -> None:
        inner = "f:" + json.dumps(["$", "$L1", None, payload()], separators=(",", ":"))
        script_argument = json.dumps([1, inner], separators=(",", ":"))
        page = f"<html><script>self.__next_f.push({script_argument})</script></html>"
        self.assertEqual(parse_target_page(page), payload())

    def test_page_without_payload_fails_closed(self) -> None:
        with self.assertRaises(CameoError):
            parse_target_page("<html></html>")


class PredictionTests(unittest.TestCase):
    def test_builds_all_five_public_af3_urls(self) -> None:
        urls = af3_prediction_urls("2026-06-20_00000082")
        self.assertEqual(len(urls), 5)
        self.assertTrue(urls[0].endswith("/993/1/model-1.cif"))
        self.assertTrue(urls[-1].endswith("/993/5/model-5.cif"))

    def test_availability_distinguishes_advertised_from_probe_urls(self) -> None:
        report = af3_availability(payload())
        self.assertEqual(report["advertised_models"], [1])
        self.assertEqual(len(report["coordinate_urls"]), 5)

    def test_import_manifest_pins_models_reference_and_license(self) -> None:
        manifest = af3_import_manifest(payload())
        self.assertEqual(manifest["method"], "alphafold3")
        self.assertEqual(manifest["provider_server_id"], "993")
        self.assertEqual(manifest["preferred_reference_assembly"], 2)
        self.assertEqual(len(manifest["models"]), 5)
        self.assertEqual(len(manifest["references"]), 2)
        self.assertEqual(manifest["license"], "CC-BY-SA-4.0")

    def test_only_generated_coordinate_urls_are_accepted(self) -> None:
        prediction_url = af3_prediction_urls("2026-06-20_00000082")[0]
        self.assertEqual(validate_coordinate_url(prediction_url)["model_index"], 1)
        reference_url = af3_import_manifest(payload())["references"][0]["url"]
        self.assertEqual(validate_coordinate_url(reference_url)["assembly_id"], 1)
        with self.assertRaises(CameoError):
            validate_coordinate_url("https://cameo3d.org/api/coords/../../secret")


if __name__ == "__main__":
    unittest.main()
