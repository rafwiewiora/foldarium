from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from foldarium_pipeline.foldseek import (
    FOLDSEEK_API,
    FoldseekError,
    fetch_result,
    parse_pdbid,
    release_dates,
    submit,
)


class FakeResponse:
    def __init__(self, value: object) -> None:
        self.body = json.dumps(value).encode()
        self.status = 200

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]

    def close(self) -> None:
        pass


class RecordingOpener:
    def __init__(self, values: list[object]) -> None:
        self.values = values
        self.requests: list[object] = []

    def __call__(self, request: object, *, timeout: float) -> FakeResponse:
        self.requests.append(request)
        return FakeResponse(self.values.pop(0))


class FoldseekClientTests(unittest.TestCase):
    def test_submits_only_pdb100_structure_search(self) -> None:
        opener = RecordingOpener([{"id": "ticket_12345678", "status": "PENDING"}])
        with tempfile.TemporaryDirectory() as temporary:
            query = Path(temporary) / "query.pdb"
            query.write_text("ATOM      1  CA  ALA A   1       0.0 0.0 0.0\nEND\n")
            self.assertEqual(
                submit(query, opener=opener), ("ticket_12345678", "PENDING")
            )
        request = opener.requests[0]
        self.assertEqual(request.full_url, FOLDSEEK_API + "/ticket")
        self.assertIn(b'name="database[]"', request.data)
        self.assertIn(b"pdb100", request.data)
        self.assertNotIn(b"afdb50", request.data)

    def test_fetch_result_validates_ticket_and_shape(self) -> None:
        opener = RecordingOpener([{"results": []}])
        self.assertEqual(fetch_result("ticket_12345678", opener=opener), {"results": []})
        with self.assertRaises(FoldseekError):
            fetch_result("../ticket", opener=opener)

    def test_parses_only_pdb_targets(self) -> None:
        self.assertEqual(parse_pdbid("9abc_A description"), "9ABC")
        self.assertEqual(parse_pdbid("pdb|1XYZ|B"), "1XYZ")
        self.assertIsNone(parse_pdbid("AF-Q5VSL9-F1-model_v4"))

    def test_batches_release_dates(self) -> None:
        opener = RecordingOpener(
            [
                {
                    "data": {
                        "entries": [
                            {
                                "rcsb_id": "1XYZ",
                                "rcsb_accession_info": {
                                    "initial_release_date": "2020-01-02T00:00:00Z"
                                },
                            }
                        ]
                    }
                }
            ]
        )
        self.assertEqual(
            release_dates(["1xyz", "9abc"], opener=opener),
            {"1XYZ": "2020-01-02T00:00:00Z", "9ABC": None},
        )
        payload = json.loads(opener.requests[0].data)
        self.assertEqual(payload["variables"]["ids"], ["1XYZ", "9ABC"])


if __name__ == "__main__":
    unittest.main()
