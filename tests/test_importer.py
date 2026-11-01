"""Synthetic, offline evidence for the importer's supported semantic subset."""

import base64
import hashlib
import json
import unittest
from typing import Any
from unittest.mock import patch

from har2load.domain import Har2LoadError
from har2load.importer import HarImporter


def entry() -> dict[str, Any]:
    return {
        "startedDateTime": "2026-11-01T12:00:00+00:00",
        "time": 125,
        "request": {"method": "GET", "url": "https://example.invalid/"},
        "response": {"status": 200, "content": {"mimeType": "text/plain", "text": "ok"}},
    }


def encode(entries: list[Any]) -> bytes:
    return json.dumps({"log": {"entries": entries}}, ensure_ascii=False).encode()


def parse(item: Any) -> Any:
    return HarImporter().parse(encode([item]))


def codes(capture: Any) -> set[str]:
    return {diagnostic.code for diagnostic in capture.diagnostics}


class ImporterTests(unittest.TestCase):
    def test_bytes_bom_unknown_fields_and_stable_identity(self) -> None:
        item = entry()
        item["_vendor"] = {"unknown": [None, "雪", {"secret": "retained"}]}
        raw = b"\xef\xbb\xbf" + encode([item, item]) + b"\n"
        capture = HarImporter().parse(raw)
        digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual(capture.raw_bytes, raw)
        self.assertEqual((capture.sha256, capture.id), (digest, f"cap_{digest}"))
        for index, exchange in enumerate(capture.exchanges):
            self.assertEqual(exchange.id, f"ex_{digest}_{index:06d}")
            self.assertEqual((exchange.index, exchange.raw), (index, item))

    def test_rejects_unsafe_or_ambiguous_json_with_safe_errors(self) -> None:
        inputs = [b"\xffSECRET", b"SECRET", b"[]", b"{}", b'{"log":[]}',
                  b'{"log":{"entries":{}}}', b'{"SECRET":1,"SECRET":2}',
                  b'{"log":{"entries":[NaN]}}', b'{"log":{"entries":[Infinity]}}',
                  b"[" * 2000 + b"]" * 2000]
        for raw in inputs:
            with self.subTest(raw=raw[:30]), self.assertRaises(Har2LoadError) as caught:
                HarImporter().parse(raw)
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertTrue(caught.exception.code.startswith("HAR_"))
            self.assertTrue(caught.exception.__suppress_context__)

    def test_malformed_entries_are_preserved_and_diagnostics_aggregated_once(self) -> None:
        capture = HarImporter().parse(encode([None, [], "SECRET", {"request": False}]))
        self.assertEqual([ex.raw for ex in capture.exchanges], [None, [], "SECRET",
                                                             {"request": False}])
        flattened = tuple(d for ex in capture.exchanges for d in
                          (*ex.diagnostics, *ex.request_body.diagnostics,
                           *ex.response_body.diagnostics))
        self.assertEqual(capture.diagnostics, flattened)
        self.assertEqual(len(flattened), len(set(flattened)))
        self.assertNotIn("SECRET", repr(flattened))

    def test_absent_response_never_invents_status(self) -> None:
        item = entry()
        del item["response"]
        capture = parse(item)
        self.assertNotIn("response", capture.exchanges[0].raw)
        self.assertEqual(capture.exchanges[0].response_body.availability, "unavailable")
        self.assertIn("HAR_INVALID_FIELD", codes(capture))

    def test_text_is_not_heuristically_decoded(self) -> None:
        for text in ["SGVsbG8=", "%7B%22value%22%3A1%7D", "雪😀", ""]:
            item = entry()
            item["response"]["content"]["text"] = text
            body = parse(item).exchanges[0].response_body
            self.assertEqual(body.text, text)
            self.assertEqual(body.availability, "present" if text else "empty")

    def test_base64_is_strict_and_only_declared(self) -> None:
        item = entry()
        content = item["response"]["content"]
        content.update(encoding="base64", text=base64.b64encode("雪".encode()).decode())
        self.assertEqual(parse(item).exchanges[0].response_body.text, "雪")
        for text in ["@@SECRET", "YQ==\n", "é", "a"]:
            content["text"] = text
            body = parse(item).exchanges[0].response_body
            self.assertEqual(body.availability, "invalid")
            self.assertIsNone(body.text)

    def test_binary_and_unknown_encoding_are_not_invalid_or_guessed(self) -> None:
        cases = [{"mimeType": "image/png", "text": "plain"},
                 {"mimeType": "text/plain", "encoding": "base64", "text": "/w=="},
                 {"mimeType": "text/plain", "encoding": "gzip", "text": "SECRET"}]
        for content in cases:
            item = entry()
            item["response"]["content"] = content
            body = parse(item).exchanges[0].response_body
            self.assertEqual(body.availability, "present")
            self.assertIn(body.view_status, {"nontext", "unsupported"})
            self.assertIsNone(body.text)
            self.assertTrue(body.diagnostics)

    def test_zero_size_does_not_prove_empty_body(self) -> None:
        for content in [{}, {"size": 0}, {"size": 12}, {"text": None}]:
            item = entry()
            item["response"]["content"] = content
            body = parse(item).exchanges[0].response_body
            self.assertEqual(body.availability, "invalid" if "text" in content
                             else "unavailable")
        item["response"]["content"] = {"encoding": "base64", "text": ""}
        self.assertEqual(parse(item).exchanges[0].response_body.availability, "empty")

    def test_params_reconstruction_keeps_query_separate_and_duplicates(self) -> None:
        item = entry()
        query = [{"name": f"q{i}", "value": str(i)} for i in range(5)]
        params = [{"name": "same", "value": value} for value in ["", "a b", "雪", "3", "4", "5"]]
        item["request"].update(url="https://example.invalid/?q0=0&q1=1&q2=2&q3=3&q4=4",
                               queryString=query, headers=[{"name": "X", "value": "1"}] * 2,
                               postData={"mimeType": "application/x-www-form-urlencoded",
                                         "params": params})
        capture = parse(item)
        body = capture.exchanges[0].request_body
        self.assertEqual(body.text, "same=&same=a+b&same=%E9%9B%AA&same=3&same=4&same=5")
        self.assertEqual((body.representation, body.availability), ("params", "present"))
        self.assertEqual(capture.exchanges[0].raw, item)
        self.assertNotIn("HAR_REPRESENTATION_CONFLICT", codes(capture))

    def test_empty_params_are_empty_form_and_multipart_is_unsupported(self) -> None:
        item = entry()
        post = {"mimeType": "application/x-www-form-urlencoded", "params": []}
        item["request"]["postData"] = post
        body = parse(item).exchanges[0].request_body
        self.assertEqual((body.text, body.availability, body.representation),
                         ("", "empty", "params"))
        for post in [{"mimeType": "multipart/form-data", "text": "SECRET"},
                     {"mimeType": "application/x-www-form-urlencoded",
                      "params": [{"name": "file", "value": "", "fileName": "SECRET"}]}]:
            item["request"]["postData"] = post
            body = parse(item).exchanges[0].request_body
            self.assertIsNone(body.text)
            self.assertEqual(body.availability, "present" if "text" in post else "unavailable")

    def test_timing_requires_timezone_and_valid_duration(self) -> None:
        item = entry()
        exchange = parse(item).exchanges[0]
        self.assertEqual(exchange.end_time - exchange.start_time, 0.125)
        for value in [None, True, -1, "125", 10**400]:
            item["time"] = value
            exchange = parse(item).exchanges[0]
            self.assertIsNotNone(exchange.start_time)
            self.assertIsNone(exchange.end_time)
        item["time"] = 0
        for value in [None, True, "2026-11-01T12:00:00", "bad", "2026-11-01"]:
            item["startedDateTime"] = value
            self.assertIsNone(parse(item).exchanges[0].start_time)

    def test_malformed_optional_fields_do_not_crash_or_mutate(self) -> None:
        cases = [("request", "headers", {}), ("request", "queryString", [None]),
                 ("request", "cookies", [{"name": "x", "value": False}]),
                 ("request", "method", 7), ("request", "url", False),
                 ("response", "status", True), ("response", "httpVersion", []),
                 ("response", "content", []), ("request", "postData", None)]
        for side, key, value in cases:
            item = entry()
            item[side][key] = value
            capture = parse(item)
            self.assertEqual(capture.exchanges[0].raw, item)
            self.assertIn("HAR_INVALID_FIELD", codes(capture))

    def test_url_query_conflicts_and_provable_equivalence(self) -> None:
        item = entry()
        item["request"].update(url="https://example.invalid/?x=a%20b&x=",
                               queryString=[{"name": "x", "value": "a b"},
                                            {"name": "x", "value": ""}])
        self.assertNotIn("HAR_REPRESENTATION_CONFLICT", codes(parse(item)))
        item["request"]["queryString"].reverse()
        self.assertIn("HAR_REPRESENTATION_CONFLICT", codes(parse(item)))
        item["request"].update(url="https://example.invalid/?x=%FF", queryString=[])
        self.assertIn("HAR_REPRESENTATION_CONFLICT", codes(parse(item)))

    def test_post_text_params_conflicts_and_equivalence(self) -> None:
        item = entry()
        post = {"mimeType": "application/x-www-form-urlencoded", "text": "x=a%20b&x=",
                "params": [{"name": "x", "value": "a b"}, {"name": "x", "value": ""}]}
        item["request"]["postData"] = post
        self.assertNotIn("HAR_REPRESENTATION_CONFLICT", codes(parse(item)))
        post["text"] = "x=different"
        capture = parse(item)
        self.assertIn("HAR_REPRESENTATION_CONFLICT", codes(capture))
        self.assertEqual(capture.exchanges[0].request_body.text, "x=different")
        post["mimeType"] = "application/json"
        self.assertIn("HAR_REPRESENTATION_CONFLICT", codes(parse(item)))

    def test_content_type_conflicts_on_both_sides(self) -> None:
        for side, field in [("request", "postData"), ("response", "content")]:
            item = entry()
            item[side][field] = {"mimeType": "application/json", "text": "{}"}
            item[side]["headers"] = [{"name": "Content-Type", "value": "text/plain"}]
            self.assertIn("HAR_REPRESENTATION_CONFLICT", codes(parse(item)))
            item[side]["headers"][0]["value"] = "Application/JSON"
            self.assertNotIn("HAR_REPRESENTATION_CONFLICT", codes(parse(item)))

    def test_diagnostics_never_interpolate_captured_values(self) -> None:
        item = entry()
        item["request"].update(url="SECRET", headers=[{"SECRET": "SECRET"}],
                               postData={"mimeType": "SECRET", "encoding": "SECRET",
                                         "params": [{"name": "SECRET", "value": None}]})
        item["response"]["content"] = {"encoding": "SECRET", "text": "SECRET"}
        capture = parse(item)
        self.assertNotIn("SECRET", repr(capture.diagnostics))
        self.assertTrue(all(d.entity_id == capture.exchanges[0].id for d in capture.diagnostics))
        self.assertTrue(all(d.location.startswith("/") for d in capture.diagnostics))

    def test_parsing_never_accesses_network_or_executes_text(self) -> None:
        item = entry()
        item["comment"] = "__import__('os').system('false')"
        with patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            self.assertEqual(parse(item).exchanges[0].raw, item)

    def test_mime_parameter_values_are_not_all_case_insensitive(self) -> None:
        item = entry()
        item["request"].update(
            headers=[{"name": "Content-Type", "value": "multipart/form-data; boundary=ABC"}],
            postData={"mimeType": "multipart/form-data; boundary=abc", "text": "--ABC"})
        self.assertIn("HAR_REPRESENTATION_CONFLICT", codes(parse(item)))

    def test_malformed_form_params_and_metadata_remain_invalid(self) -> None:
        cases = [None, {}, [None], [{"name": "x"}], [{"name": "x", "value": False}],
                 [{"name": "x", "fileName": False}],
                 [{"name": "x", "value": "", "contentType": []}]]
        for params in cases:
            item = entry()
            item["request"]["postData"] = {"mimeType": "multipart/form-data", "params": params}
            body = parse(item).exchanges[0].request_body
            self.assertEqual(body.availability, "invalid")
            self.assertIn("HAR_INVALID_FIELD", {d.code for d in body.diagnostics})

    def test_file_params_without_value_are_unsupported_not_invalid(self) -> None:
        item = entry()
        item["request"]["postData"] = {"mimeType": "multipart/form-data",
                                      "params": [{"name": "upload", "fileName": "local.txt"}]}
        body = parse(item).exchanges[0].request_body
        self.assertEqual(body.availability, "unavailable")
        self.assertNotIn("HAR_INVALID_FIELD", {d.code for d in body.diagnostics})

    def test_form_charset_is_not_guessed(self) -> None:
        item = entry()
        post = {"mimeType": "application/x-www-form-urlencoded; charset=ISO-8859-1",
                "params": [{"name": "x", "value": "é"}]}
        item["request"]["postData"] = post
        self.assertIsNone(parse(item).exchanges[0].request_body.text)
        post["text"] = "x=%C3%A9"
        self.assertIn("HAR_REPRESENTATION_CONFLICT", codes(parse(item)))

    def test_base64_with_non_utf8_charset_is_not_reinterpreted(self) -> None:
        item = entry()
        item["response"]["content"] = {"mimeType": "text/plain; charset=ISO-8859-1",
                                       "encoding": "base64", "text": "w6k="}
        body = parse(item).exchanges[0].response_body
        self.assertEqual(body.availability, "present")
        self.assertEqual(body.view_status, "nontext")
        self.assertIsNone(body.text)

    def test_binary_header_metadata_without_content_mime_is_respected(self) -> None:
        item = entry()
        item["response"].update(content={"text": "ASCII"},
                                headers=[{"name": "Content-Type", "value": "image/png"}])
        self.assertIsNone(parse(item).exchanges[0].response_body.text)

    def test_escaped_surrogates_are_preserved_but_not_usable_text(self) -> None:
        raw = b'{"log":{"entries":[{"response":{"content":{"text":"\\ud800"}}}]}}'
        capture = HarImporter().parse(raw)
        self.assertEqual(capture.raw_bytes, raw)
        self.assertEqual(capture.exchanges[0].response_body.availability, "invalid")

    def test_cookie_attributes_and_zero_duration_survive(self) -> None:
        item = entry()
        item["time"] = 0
        item["request"]["cookies"] = [{"name": "sid", "value": "SECRET", "httpOnly": True,
                                       "secure": True, "_vendor": {"extra": 1}}] * 2
        capture = parse(item)
        exchange = capture.exchanges[0]
        self.assertEqual(exchange.raw, item)
        self.assertIsNotNone(exchange.start_time)
        self.assertEqual(exchange.start_time, exchange.end_time)
        self.assertNotIn("HAR_INVALID_FIELD", codes(capture))

    def test_missing_optional_fields_are_not_structural_errors(self) -> None:
        capture = parse(entry())
        self.assertNotIn("HAR_INVALID_FIELD", codes(capture))
        self.assertNotIn("HAR_REPRESENTATION_CONFLICT", codes(capture))

    def test_invalid_percent_escapes_cannot_prove_query_equivalence(self) -> None:
        item = entry()
        item["request"].update(url="https://example.invalid/?x=%GG",
                               queryString=[{"name": "x", "value": "%GG"}])
        self.assertIn("HAR_REPRESENTATION_CONFLICT", codes(parse(item)))

    def test_json_parser_recursion_error_is_typed_without_exception_context(self) -> None:
        with patch("har2load.importer.json.loads", side_effect=RecursionError("SECRET")):
            with self.assertRaises(Har2LoadError) as caught:
                HarImporter().parse(b"{}")
        self.assertEqual(caught.exception.code, "HAR_INVALID_JSON")
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_url_parser_normalization_cannot_hide_a_conflict(self) -> None:
        for url in ["https://example.invalid/?x=a\nb", " https://example.invalid/?x=ab", False]:
            item = entry()
            item["request"].update(url=url, queryString=[{"name": "x", "value": "ab"}])
            self.assertIn("HAR_REPRESENTATION_CONFLICT", codes(parse(item)))

    def test_unknown_fields_are_not_interpreted_using_another_objects_schema(self) -> None:
        item = entry()
        item["request"].update(statusText=123, headers=[{"name": "X", "value": "Y", "path": 1}])
        item["response"].update(method=False, queryString={"vendor": "data"})
        capture = parse(item)
        self.assertEqual(capture.exchanges[0].raw, item)
        self.assertNotIn("HAR_INVALID_FIELD", codes(capture))

    def test_timezone_offsets_and_missing_times(self) -> None:
        item = entry()
        start = parse(item).exchanges[0].start_time
        for timestamp in ["2026-11-01T12:00:00Z", "2026-11-01T15:00:00+03:00"]:
            item["startedDateTime"] = timestamp
            self.assertEqual(parse(item).exchanges[0].start_time, start)
        del item["time"]
        self.assertIsNone(parse(item).exchanges[0].end_time)
        del item["startedDateTime"]
        self.assertIsNone(parse(item).exchanges[0].start_time)

    def test_request_plain_empty_and_malformed_body_fields(self) -> None:
        item = entry()
        for text in ["", "%41", "SGVsbG8=", "雪"]:
            item["request"]["postData"] = {"text": text}
            self.assertEqual(parse(item).exchanges[0].request_body.text, text)
        for key in ["text", "mimeType", "encoding", "params", "size", "compression"]:
            item["response"]["content"] = {key: False}
            self.assertEqual(parse(item).exchanges[0].response_body.availability, "invalid")

    def test_python_datetime_extensions_do_not_validate_non_iso_timestamps(self) -> None:
        item = entry()
        for separator in ["X", "😀", "\x00"]:
            item["startedDateTime"] = f"2026-11-01{separator}12:00:00+00:00"
            self.assertIsNone(parse(item).exchanges[0].start_time)
