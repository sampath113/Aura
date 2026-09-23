"""AURA's test suite - run with:  python -m unittest discover -s tests -t .

No third-party test runner is required, and nothing here touches the network.
"""
from __future__ import annotations

import hashlib
import io
import os
import struct
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aura import catalog, desktop, downloads, llama_server  # noqa: E402
from aura.answer import (answer_question, build_digest, compose_closest, compose_extractive,  # noqa: E402
                         verify_answer, wants_overview)
from aura.chunk import chunk_pages  # noqa: E402
from aura.citations import citation_label, parse_labels, validate  # noqa: E402
from aura.index import BM25Index, reciprocal_rank_fusion  # noqa: E402
from aura.ingest import ingest_docx, ingest_file, ingest_table, _docx_stdlib  # noqa: E402
from aura.jobs import Jobs  # noqa: E402
from aura.llama_server import Manager, build_argv  # noqa: E402
from aura.models import AuraError, Page  # noqa: E402
from aura.retrieve import Retriever  # noqa: E402
from aura.store import Library  # noqa: E402
from aura.text import keyphrases, split_sentences, stem, tokenize  # noqa: E402
from aura_main import parse_args  # noqa: E402


def wait_until(predicate, timeout: float = 10.0) -> bool:
    """Poll `predicate` until it is true (used for background jobs)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


PHOTOSYNTHESIS = (
    "Photosynthesis converts light energy into chemical energy. "
    "Chlorophyll absorbs light most strongly in the blue and red parts of the spectrum. "
    "The light reactions occur in the thylakoid membrane and produce ATP and NADPH. "
    "The Calvin cycle then fixes carbon dioxide into glucose using that ATP and NADPH."
)

CITRIC = (
    "The citric acid cycle takes place in the mitochondrial matrix. "
    "Each turn of the cycle produces three NADH, one FADH2 and one GTP. "
    "Oxidative phosphorylation then uses the electron transport chain to make ATP."
)


class TextTests(unittest.TestCase):
    def test_tokens_drop_stopwords_and_stem(self):
        tokens = tokenize("The mitochondria ARE producing energies")
        self.assertNotIn("the", tokens)
        self.assertNotIn("are", tokens)
        self.assertIn("mitochondria", tokens)
        self.assertIn("produc", tokens)

    def test_stem_is_conservative(self):
        self.assertEqual(stem("studies"), "study")
        self.assertEqual(stem("running"), "run")
        self.assertEqual(stem("class"), "class")
        self.assertEqual(stem("analysis"), "analysis")

    def test_sentence_offsets(self):
        text = "First sentence. Second one! Third?"
        pieces = split_sentences(text)
        self.assertEqual(len(pieces), 3)
        for sentence, start, end in pieces:
            self.assertEqual(text[start:end].strip(), sentence.strip())

    def test_keyphrases_find_quoted_and_capitalised(self):
        phrases = keyphrases('Explain "light reactions" and the Calvin Cycle briefly')
        self.assertIn("light reactions", phrases)
        self.assertTrue(any("Calvin" in phrase for phrase in phrases))


class ChunkTests(unittest.TestCase):
    def test_chunks_keep_page_and_offsets(self):
        pages = [Page(number=1, text=". ".join(["Sentence number {}".format(i) for i in range(40)])),
                 Page(number=2, text=". ".join(["Page two sentence {}".format(i) for i in range(40)]))]
        chunks = chunk_pages(pages, "doc1", "notes.pdf", target_chars=300, overlap_chars=80,
                            min_chars=80)
        self.assertGreater(len(chunks), 4)
        self.assertEqual({chunk.page for chunk in chunks}, {1, 2})
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text), 500)
            self.assertGreater(len(chunk.text), 60)
        page_two = next(chunk for chunk in chunks if chunk.page == 2)
        self.assertIn("Page two sentence", page_two.text)
        # start/end must cover every sentence of the passage inside its own page
        for chunk in chunks:
            region = pages[chunk.page - 1].text[chunk.start:chunk.end]
            for sentence, _start, _end in split_sentences(chunk.text):
                self.assertIn(sentence, region)

    def test_overlap_exists_between_neighbours(self):
        text = "\n\n".join("Paragraph {} with a reasonably long body of text to fill space."
                           .format(i) for i in range(24))
        chunks = chunk_pages([Page(number=1, text=text)], "d", "f.txt",
                             target_chars=400, overlap_chars=120, min_chars=80)
        self.assertGreater(len(chunks), 3)
        for earlier, later in zip(chunks, chunks[1:]):
            tail = split_sentences(earlier.text)[-1][0]
            self.assertIn(tail, later.text,
                          "the last sentence of a passage should be carried into the next one")
        all_words = set(tokenize(" ".join(chunk.text for chunk in chunks)))
        self.assertIn("paragraph", all_words)

    def test_offsets_stay_page_relative_across_blocks(self):
        page = Page(number=1, text="Memory Notes\n\nFirst paragraph sentence one. Sentence two."
                                   "\n\nSecond paragraph sits here.")
        chunks = chunk_pages([page], "d", "f.txt", target_chars=500, overlap_chars=0, min_chars=1)
        self.assertTrue(chunks)
        for chunk in chunks:
            region = page.text[chunk.start:chunk.end]
            self.assertIn(split_sentences(chunk.text)[0][0], region)

    def test_unpunctuated_wall_of_text_is_split(self):
        blob = " ".join("word{}".format(i) for i in range(800))
        chunks = chunk_pages([Page(number=1, text=blob)], "d", "dump.pdf", target_chars=900,
                             overlap_chars=150, min_chars=120)
        self.assertGreaterEqual(len(chunks), 3)
        words = set()
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text), 1000)
            words.update(chunk.text.split(" "))
        self.assertEqual(len(words), 800)


class IndexTests(unittest.TestCase):
    def make_chunks(self):
        pages = [Page(number=1, text=PHOTOSYNTHESIS), Page(number=2, text=CITRIC)]
        return chunk_pages(pages, "doc", "biology.pdf", target_chars=400, overlap_chars=0,
                           min_chars=1)

    def test_bm25_ranks_the_relevant_passage_first(self):
        chunks = self.make_chunks()
        index = BM25Index().build(chunks)
        results = index.search("thylakoid membrane light reactions", limit=5)
        self.assertTrue(results)
        top_id, top_score = results[0]
        top = next(chunk for chunk in chunks if chunk.chunk_id == top_id)
        self.assertIn("thylakoid", top.text.lower())
        self.assertGreater(top_score, 0)

    def test_index_round_trips_through_json(self):
        chunks = self.make_chunks()
        index = BM25Index().build(chunks)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "index.json"
            index.save(path)
            restored = BM25Index.load(path)
        self.assertEqual(restored.search("mitochondrial matrix", limit=3),
                         index.search("mitochondrial matrix", limit=3))

    def test_rrf_prefers_documents_high_in_both_lists(self):
        fused = reciprocal_rank_fusion([["a", "b", "c"], ["c", "a", "d"]])
        self.assertEqual(max(fused, key=fused.get), "a")

    def test_retriever_returns_labelled_hits(self):
        chunks = self.make_chunks()
        retriever = Retriever(chunks, settings={"top_k": 2})
        hits = retriever.search("what makes ATP in the mitochondrion", k=2)
        self.assertEqual(len(hits), 2)
        self.assertEqual([hit.label for hit in hits], ["S1", "S2"])
        self.assertTrue(all(hit.chunk.page in (1, 2) for hit in hits))


class CitationTests(unittest.TestCase):
    def test_canonical_format(self):
        self.assertEqual(citation_label("S1", "bio.pdf", 7), "[S1: bio.pdf, p. 7]")

    def test_parse_and_validate(self):
        chunks = chunk_pages([Page(number=3, text=PHOTOSYNTHESIS)], "d", "bio.pdf",
                             target_chars=900, overlap_chars=0, min_chars=1)
        retriever = Retriever(chunks, settings={"top_k": 3})
        hits = retriever.search("chlorophyll spectrum", k=3)
        text = "Chlorophyll absorbs blue and red light [S1: bio.pdf, p. 3]. Extra [S9]."
        checks = validate(text, hits)
        self.assertEqual(parse_labels(text), ["S1", "S9"])
        self.assertEqual(checks["unknown"], ["S9"])
        self.assertFalse(checks["ok"])


class AnswerTests(unittest.TestCase):
    def setUp(self):
        pages = [Page(number=1, text=PHOTOSYNTHESIS), Page(number=2, text=CITRIC)]
        self.chunks = chunk_pages(pages, "d", "bio.pdf", target_chars=500, overlap_chars=0,
                                  min_chars=1)
        self.retriever = Retriever(self.chunks, settings={"top_k": 3})

    def test_extractive_answer_cites_sources(self):
        hits = self.retriever.search("where do the light reactions happen", k=3)
        text = compose_extractive("where do the light reactions happen", hits)
        self.assertIn("[S", text)
        self.assertIn("thylakoid", text.lower())

    def test_answer_without_backend_is_cited_and_verified(self):
        hits = self.retriever.search("what does the Calvin cycle do", k=3)
        answer = answer_question("what does the Calvin cycle do", hits, {}, backend=None)
        self.assertEqual(answer.mode, "extractive")
        self.assertTrue(answer.citations)
        self.assertTrue(answer.checks["ok"], answer.checks)
        self.assertFalse(answer.checks["unknown"])

    def test_no_evidence_is_reported_clearly(self):
        answer = answer_question("what is the capital of France", [], {})
        self.assertEqual(answer.mode, "no-evidence")
        self.assertIn("could not find", answer.text.lower())
        self.assertFalse(answer.text.startswith('"'), "the template should not start with a quote")

    def test_overview_questions_are_recognised(self):
        self.assertTrue(wants_overview("summarise the key definitions"))
        self.assertTrue(wants_overview("give me an overview of this document"))
        self.assertTrue(wants_overview("what is this pdf about"))
        self.assertFalse(wants_overview("where do the light reactions happen"))

    def test_off_topic_question_shows_the_closest_passages(self):
        hits = self.retriever.fallback("what is the capital of France", k=3)
        self.assertTrue(hits)
        self.assertEqual(hits[0].label, "S1")
        text = compose_closest("what is the capital of France", hits)
        self.assertIn("closest", text.lower())
        self.assertIn("[S1", text)

    def test_extractive_answer_drops_weak_matches(self):
        hits = self.retriever.search("where do the light reactions happen", k=3)
        text = compose_extractive("where do the light reactions happen", hits)
        self.assertIn("thylakoid", text.lower())
        self.assertNotIn("citric acid cycle", text.lower())

    def test_unknown_labels_are_stripped(self):
        hits = self.retriever.search("citric acid cycle", k=1)

        class FakeBackend:
            name = "fake"

            def available(self):
                return True

            def generate(self, messages, **kwargs):
                return "The cycle runs in the matrix [S1]. It also makes gold [S7]."

        answer = answer_question("where does the citric acid cycle run", hits, {}, backend=FakeBackend())
        self.assertIn("[S1", answer.text)
        self.assertNotIn("[S7]", answer.text)
        self.assertEqual(answer.checks["removed_labels"], ["S7"])
        self.assertTrue(any("invented" in note for note in answer.checks["notes"]))

    def test_verify_flags_uncited_answers(self):
        hits = self.retriever.search("citric acid cycle", k=1)
        checks = verify_answer("This sentence has no citation at all.", hits)
        self.assertIn("the answer cites no sources", checks["notes"])


class IngestTests(unittest.TestCase):
    def test_text_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "notes.txt"
            path.write_text("Heading\n\n" + PHOTOSYNTHESIS, encoding="utf-8")
            document, pages = ingest_file(path)
        self.assertEqual(document.kind, "text")
        self.assertEqual(document.name, "notes.txt")
        self.assertTrue(any("Chlorophyll" in page.text for page in pages))

    def test_csv_file_becomes_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "marks.csv"
            path.write_text("name,mark\nAsha,88\nRavi,91\n", encoding="utf-8")
            document, pages = ingest_file(path)
            pages_out, notes = ingest_table(path)
        self.assertEqual(document.kind, "table")
        self.assertTrue(any("Asha" in page.text for page in pages_out))
        self.assertEqual(notes, [])

    def test_docx_via_builtin_zip_reader(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "notes.docx"
            document_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>Calvin cycle</w:t></w:r></w:p>"
                "<w:p><w:r><w:t>fixes carbon dioxide into glucose</w:t></w:r></w:p></w:body></w:document>")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("[Content_Types].xml", "<Types/>")
                archive.writestr("word/document.xml", document_xml)
            pages = _docx_stdlib(path)
            document, pages_via_api = ingest_file(path)
        self.assertTrue(pages)
        self.assertIn("Calvin cycle", pages[0].text)
        self.assertIn("glucose", pages_via_api[0].text)
        self.assertEqual(document.kind, "docx")

    def test_binary_garbage_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "blob.bin"
            path.write_bytes(b"\x00\x01\x02\x03" * 400)
            with self.assertRaises(AuraError):
                ingest_file(path)

    def test_missing_file_is_rejected(self):
        with self.assertRaises(AuraError):
            ingest_file("/definitely/not/here.pdf")


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "bio.txt").write_text(PHOTOSYNTHESIS, encoding="utf-8")
        (self.root / "chem.txt").write_text(CITRIC, encoding="utf-8")
        self.library = Library(root=self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_add_search_and_cite_the_right_document(self):
        self.library.add_file(self.root / "bio.txt")
        self.library.add_file(self.root / "chem.txt")
        stats = self.library.stats()
        self.assertEqual(stats["documents"], 2)
        self.assertGreaterEqual(stats["chunks"], 2)

        answer = self.library.ask("where do the light reactions take place", k=2)
        self.assertTrue(answer.citations)
        self.assertIn("thylakoid", answer.text.lower())
        first_label = answer.citations[0]
        first_hit = next(hit for hit in answer.hits if hit.label == first_label)
        self.assertEqual(first_hit.chunk.doc_name, "bio.txt")
        self.assertEqual(first_hit.chunk.page, 1)

    def test_library_persists_across_instances(self):
        self.library.add_file(self.root / "bio.txt")
        reopened = Library(root=self.root)
        self.assertEqual(reopened.stats()["documents"], 1)
        hits = reopened.search("chlorophyll", k=1)
        self.assertTrue(hits)
        self.assertEqual(hits[0].chunk.doc_name, "bio.txt")

    def test_remove_document(self):
        document = self.library.add_file(self.root / "bio.txt")
        self.assertTrue(self.library.remove(document.doc_id))
        self.assertEqual(self.library.stats()["documents"], 0)
        self.assertEqual(self.library.search("chlorophyll"), [])

    def test_add_folder_ingests_every_text_file(self):
        added = self.library.add_folder(self.root)
        self.assertEqual(len(added), 2)
        names = {document.name for document in added}
        self.assertEqual(names, {"bio.txt", "chem.txt"})

    def test_duplicate_add_replaces_the_document(self):
        self.library.add_file(self.root / "bio.txt")
        self.library.add_file(self.root / "bio.txt")
        self.assertEqual(self.library.stats()["documents"], 1)

    def test_settings_round_trip(self):
        from aura import config

        saved = config.save_settings(self.root, {"top_k": 9})
        self.assertEqual(saved["top_k"], 9)
        self.assertEqual(config.load_settings(self.root)["top_k"], 9)
        self.assertEqual(config.load_settings(self.root)["chunk_chars"],
                         config.DEFAULTS["chunk_chars"])

    def test_off_topic_question_falls_back_to_the_nearest_passages(self):
        self.library.add_file(self.root / "bio.txt")
        answer = self.library.ask("what is the capital of France", k=3)
        self.assertEqual(answer.mode, "closest")
        self.assertTrue(answer.hits)
        self.assertTrue(answer.citations)
        self.assertTrue(answer.checks["fallback"])
        self.assertEqual(answer.hits[0].chunk.doc_name, "bio.txt")

    def test_overview_question_returns_a_cited_outline(self):
        self.library.add_file(self.root / "bio.txt")
        answer = self.library.ask("summarise the key definitions", k=3)
        self.assertEqual(answer.mode, "outline")
        self.assertTrue(answer.citations)
        self.assertEqual({hit.chunk.doc_name for hit in answer.hits}, {"bio.txt"})

    def test_empty_library_reports_no_evidence(self):
        empty = Library(root=self.root / "empty")
        answer = empty.ask("what is photosynthesis")
        self.assertEqual(answer.mode, "no-evidence")
        self.assertEqual(answer.hits, [])

    def test_reingest_picks_up_changed_file_contents(self):
        self.library.add_file(self.root / "bio.txt")
        with open(self.root / "bio.txt", "a", encoding="utf-8") as handle:
            handle.write(" Ribosomes assemble proteins from amino acids.")
        result = self.library.reingest()
        self.assertEqual(result["reingested"], 1)
        self.assertEqual(self.library.stats()["documents"], 1)
        self.assertTrue(any("ribosome" in chunk.text.lower() for chunk in self.library.chunks))

    def test_reingest_reports_files_that_have_moved(self):
        self.library.add_file(self.root / "bio.txt")
        (self.root / "bio.txt").unlink()
        result = self.library.reingest()
        self.assertEqual(result["reingested"], 0)
        self.assertEqual(result["skipped"], ["bio.txt"])
        self.assertEqual(self.library.stats()["documents"], 1)


class CatalogTests(unittest.TestCase):
    def test_every_entry_is_downloadable_and_verifiable(self):
        for model in catalog.MODELS:
            self.assertTrue(model["files"], model["id"])
            for item in model["files"]:
                self.assertTrue(item["url"].startswith("https://huggingface.co/"), item["name"])
                self.assertGreater(item["bytes"], 1024)
                self.assertEqual(len(item["sha256"]), 64)
                int(item["sha256"], 16)  # must be hex
            self.assertGreater(model["ram_gb"], 0)
            self.assertTrue(model["note"])
            self.assertTrue(model["license"])
            self.assertEqual(model["bytes"], sum(f["bytes"] for f in model["files"]))
            self.assertEqual(model["file"], model["files"][0]["name"])

    def test_ids_and_filenames_are_unique(self):
        ids = [model["id"] for model in catalog.MODELS]
        self.assertEqual(len(ids), len(set(ids)))
        names = [item["name"] for model in catalog.MODELS for item in model["files"]]
        self.assertEqual(len(names), len(set(names)))

    def test_the_recommended_model_is_a_small_one(self):
        recommended = catalog.recommended()
        self.assertIn(recommended, catalog.MODELS)
        sizes = sorted(model["bytes"] for model in catalog.MODELS)
        self.assertLessEqual(recommended["bytes"], sizes[2])

    def test_runtime_asset_is_picked_per_platform(self):
        assets = [
            {"name": "cudart-llama-bin-win-cuda-12.4-x64.zip"},
            {"name": "llama-b11136-bin-win-cpu-x64.zip"},
            {"name": "llama-b11136-bin-win-cpu-arm64.zip"},
            {"name": "llama-b11136-bin-ubuntu-x64.tar.gz"},
            {"name": "llama-b11136-bin-macos-arm64.tar.gz"},
        ]
        self.assertEqual(catalog.runtime_asset(assets, "windows", "x64")["name"],
                         "llama-b11136-bin-win-cpu-x64.zip")
        self.assertEqual(catalog.runtime_asset(assets, "windows", "arm64")["name"],
                         "llama-b11136-bin-win-cpu-arm64.zip")
        self.assertEqual(catalog.runtime_asset(assets, "linux", "x64")["name"],
                         "llama-b11136-bin-ubuntu-x64.tar.gz")
        self.assertEqual(catalog.runtime_asset(assets, "macos", "arm64")["name"],
                         "llama-b11136-bin-macos-arm64.tar.gz")
        self.assertIsNone(catalog.runtime_asset([{"name": "something-else.zip"}], "windows", "x64"))
        self.assertIsNone(catalog.runtime_asset([], "linux", "x64"))

    def test_pinned_urls_point_at_the_platform_build(self):
        for platform_name, arch in (("windows", "x64"), ("linux", "x64"), ("macos", "arm64")):
            url = catalog.pinned_runtime_url(platform_name, arch)
            self.assertIn(catalog.PINNED_RUNTIME_TAG, url)
            self.assertTrue(url.startswith("https://github.com/ggml-org/llama.cpp/releases/download/"))
            self.assertIn(catalog.runtime_pattern(platform_name, arch), url)

    def test_the_binary_is_found_wherever_the_archive_put_it(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / "bin").mkdir()
            expected = folder / "bin" / catalog.binary_name()
            expected.write_bytes(b"#!binary")
            self.assertEqual(catalog.find_binary(folder), expected)
            self.assertIsNone(catalog.find_binary(folder / "nowhere"))


class ModelFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_a_missing_model_is_reported_as_missing(self):
        item = catalog.MODELS[0]["files"][0]
        state = catalog.file_state(item, self.root)
        self.assertEqual(state["state"], "missing")
        self.assertEqual(state["on_disk"], 0)
        self.assertEqual(state["expected"], item["bytes"])

    def test_a_wrong_sized_file_is_incomplete(self):
        item = catalog.MODELS[0]["files"][0]
        (self.root / item["name"]).write_bytes(b"short")
        self.assertEqual(catalog.file_state(item, self.root)["state"], "incomplete")

    def test_a_part_file_means_a_download_is_in_progress(self):
        item = catalog.MODELS[0]["files"][0]
        (self.root / (item["name"] + ".part")).write_bytes(b"x" * 32)
        state = catalog.file_state(item, self.root)
        self.assertEqual(state["state"], "downloading")
        self.assertEqual(state["on_disk"], 32)

    def test_a_multi_file_model_needs_every_file(self):
        model = {"id": "two", "name": "Two", "params": "1B", "quant": "Q4", "bytes": 8,
                 "ram_gb": 1, "license": "test", "note": "n", "context": 512, "repo_url": "",
                 "file": "a.gguf", "files": [
                     {"name": "a.gguf", "bytes": 4, "sha256": "", "url": ""},
                     {"name": "b.gguf", "bytes": 4, "sha256": "", "url": ""}]}
        self.assertEqual(catalog.status_of(model, self.root)["state"], "missing")
        (self.root / "a.gguf").write_bytes(b"aaaa")
        self.assertEqual(catalog.status_of(model, self.root)["state"], "partial")
        (self.root / "b.gguf").write_bytes(b"bbbb")
        self.assertEqual(catalog.status_of(model, self.root)["state"], "ready")
        self.assertTrue(catalog.status_of(model, self.root)["primary_ready"])

    def test_installed_files_include_models_aura_does_not_know(self):
        name = catalog.MODELS[0]["files"][0]["name"]
        (self.root / name).write_bytes(b"x")
        (self.root / "my-own-model.gguf").write_bytes(b"hello")
        (self.root / "half.gguf.part").write_bytes(b"1234")
        found = {item["name"]: item for item in catalog.installed_files(self.root)}
        self.assertTrue(found[name]["known"])
        self.assertFalse(found["my-own-model.gguf"]["known"])
        self.assertEqual(found["my-own-model.gguf"]["bytes"], 5)
        self.assertTrue(found["half.gguf"]["part"])
        self.assertEqual(catalog.installed_files(self.root / "nope"), [])


class FakeResponse:
    """Stands in for urllib's response object in the download tests."""

    def __init__(self, body: bytes, status: int = 200, headers=None):
        self._stream = io.BytesIO(body)
        self.status = status
        self.headers = dict(headers or {})
        self.headers.setdefault("Content-Length", str(len(body)))

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_opener(body: bytes, status: int = 200, seen=None, error=None):
    def opener(request, timeout=None):
        if seen is not None:
            seen.append(request)
        if error is not None:
            raise error
        return FakeResponse(body, status)
    return opener


BIG = b"a" * (3 << 20)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_a_download_is_verified_and_reports_progress(self):
        seen = []
        progress = []
        dest = self.root / "model.gguf"
        path = downloads.download("https://example.test/model.gguf", dest, expected_bytes=len(BIG),
                                  sha256=hashlib.sha256(BIG).hexdigest(),
                                  on_progress=lambda done, total: progress.append((done, total)),
                                  opener=fake_opener(BIG, seen=seen))
        self.assertEqual(path, dest)
        self.assertEqual(dest.read_bytes(), BIG)
        self.assertFalse((self.root / "model.gguf.part").exists())
        self.assertTrue(progress)
        self.assertEqual(progress[-1][0], len(BIG))
        self.assertIsNone(seen[0].headers.get("Range"))

    def test_a_second_attempt_resumes_with_a_range_request(self):
        half = len(BIG) // 2
        (self.root / "model.gguf.part").write_bytes(BIG[:half])
        seen = []
        path = downloads.download("https://example.test/model.gguf", self.root / "model.gguf",
                                  expected_bytes=len(BIG),
                                  opener=fake_opener(BIG[half:], status=206, seen=seen))
        self.assertEqual(path.read_bytes(), BIG)
        self.assertEqual(seen[0].headers.get("Range"), "bytes={}-".format(half))

    def test_a_server_that_ignores_the_range_restarts_the_file(self):
        half = len(BIG) // 2
        (self.root / "model.gguf.part").write_bytes(BIG[:half])
        path = downloads.download("https://example.test/model.gguf", self.root / "model.gguf",
                                  expected_bytes=len(BIG), opener=fake_opener(BIG, status=200))
        self.assertEqual(path.read_bytes(), BIG)

    def test_an_already_complete_file_is_left_alone(self):
        dest = self.root / "model.gguf"
        dest.write_bytes(BIG)
        calls = []
        path = downloads.download("https://example.test/model.gguf", dest, expected_bytes=len(BIG),
                                  sha256=hashlib.sha256(BIG).hexdigest(),
                                  opener=fake_opener(b"should not be used", seen=calls))
        self.assertEqual(path.read_bytes(), BIG)
        self.assertEqual(calls, [])

    def test_cancelling_keeps_the_part_file_for_a_resume(self):
        state = {"n": 0}

        def cancelled():
            state["n"] += 1
            return state["n"] > 1

        with self.assertRaises(downloads.DownloadCancelled):
            downloads.download("https://example.test/model.gguf", self.root / "model.gguf",
                               expected_bytes=len(BIG), cancelled=cancelled, opener=fake_opener(BIG))
        part = self.root / "model.gguf.part"
        self.assertTrue(part.exists())
        self.assertGreater(part.stat().st_size, 0)
        self.assertLess(part.stat().st_size, len(BIG))

    def test_a_checksum_mismatch_is_refused_and_the_part_is_discarded(self):
        with self.assertRaises(AuraError) as caught:
            downloads.download("https://example.test/model.gguf", self.root / "model.gguf",
                               expected_bytes=len(BIG), sha256="0" * 64, opener=fake_opener(BIG))
        self.assertIn("integrity", str(caught.exception))
        self.assertFalse((self.root / "model.gguf.part").exists())
        self.assertFalse((self.root / "model.gguf").exists())

    def test_a_truncated_body_is_reported_as_an_early_stop(self):
        with self.assertRaises(AuraError) as caught:
            downloads.download("https://example.test/model.gguf", self.root / "model.gguf",
                               expected_bytes=len(BIG) + 10, opener=fake_opener(BIG))
        self.assertIn("stopped early", str(caught.exception))

    def test_an_unreachable_host_names_itself(self):
        with self.assertRaises(AuraError) as caught:
            downloads.download("https://models.example.test/x.gguf", self.root / "x.gguf",
                               opener=fake_opener(b"", error=OSError("no route to host")))
        self.assertIn("models.example.test", str(caught.exception))

    def test_size_is_reported_before_a_download_starts(self):
        self.assertEqual(catalog.human_size(1117320736), "1.0 GB")
        self.assertEqual(catalog.human_size(2048), "2 KB")

    def test_zip_archives_are_unpacked(self):
        archive = self.root / "engine.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("llama-server", "#!binary")
            bundle.writestr("nested/notes.txt", "hello")
            bundle.writestr("empty/", "")
        written = downloads.extract_archive(archive, self.root / "engine")
        names = sorted(path.name for path in written)
        self.assertEqual(names, ["llama-server", "notes.txt"])
        self.assertTrue((self.root / "engine" / "llama-server").exists())
        if os.name != "nt":
            self.assertTrue(os.access(self.root / "engine" / "llama-server", os.X_OK))

    def test_an_archive_that_escapes_its_folder_is_refused(self):
        archive = self.root / "evil.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("../escaped.txt", "nope")
        with self.assertRaises(AuraError) as caught:
            downloads.extract_archive(archive, self.root / "engine")
        self.assertIn("outside its folder", str(caught.exception))
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_sha256_of_a_file(self):
        path = self.root / "thing.bin"
        path.write_bytes(b"abc")
        self.assertEqual(downloads.sha256_of(path),
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


class JobTests(unittest.TestCase):
    def test_a_job_reports_its_result(self):
        jobs = Jobs()
        job = jobs.run("model", "do the thing", lambda handle: {"ok": True})
        self.assertTrue(wait_until(lambda: job.state != "running" and job.state != "queued"))
        self.assertEqual(job.state, "done")
        self.assertEqual(job.result, {"ok": True})
        self.assertEqual(job.as_dict()["percent"], 100)
        self.assertTrue(job.as_dict()["done"])

    def test_a_known_failure_carries_its_message(self):
        def work(handle):
            raise AuraError("no room")

        jobs = Jobs()
        job = jobs.run("model", "fail", work)
        self.assertTrue(wait_until(lambda: job.state in ("done", "error", "cancelled")))
        self.assertEqual(job.state, "error")
        self.assertEqual(job.error, "no room")

    def test_an_unexpected_failure_is_still_captured(self):
        def work(handle):
            raise ValueError("boom")

        jobs = Jobs()
        job = jobs.run("model", "explode", work)
        self.assertTrue(wait_until(lambda: job.state in ("done", "error", "cancelled")))
        self.assertEqual(job.state, "error")
        self.assertIn("ValueError: boom", job.error)

    def test_cancelling_during_the_work_marks_it_cancelled(self):
        jobs = Jobs()
        job = jobs.run("model", "slow", lambda handle: handle.cancel())
        self.assertTrue(wait_until(lambda: job.state in ("done", "error", "cancelled")))
        self.assertEqual(job.state, "cancelled")
        self.assertTrue(job.cancelled())

    def test_a_finished_job_cannot_be_cancelled(self):
        jobs = Jobs()
        job = jobs.run("model", "quick", lambda handle: {})
        self.assertTrue(wait_until(lambda: job.state == "done"))
        self.assertFalse(job.cancel())

    def test_progress_is_clamped(self):
        jobs = Jobs()
        job = jobs.create("model", "p")
        job.set_progress(5, 10)
        self.assertEqual(job.as_dict()["percent"], 50)
        job.set_progress(50, 10)
        self.assertEqual(job.as_dict()["percent"], 100)

    def test_the_latest_job_can_be_filtered_by_kind(self):
        jobs = Jobs()
        jobs.run("engine", "engine job", lambda handle: {})
        last = jobs.run("model", "model job", lambda handle: {})
        self.assertTrue(wait_until(lambda: last.state == "done"))
        self.assertEqual(jobs.latest(("model",))["label"], "model job")
        self.assertEqual(jobs.latest(("engine",))["label"], "engine job")
        self.assertEqual(len(jobs.list()), 2)

    def test_the_registry_forgets_old_jobs(self):
        jobs = Jobs(limit=2)
        for index in range(4):
            jobs.run("model", "job {}".format(index), lambda handle: {})
        self.assertTrue(wait_until(lambda: len(jobs.list()) == 2))
        self.assertEqual(jobs.list()[0]["label"], "job 3")


class FakeProc:
    """A subprocess.Popen stand-in so the manager can be tested with no child."""

    def __init__(self, returncode=None):
        self.returncode = returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = 0


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.engine = Manager(self.root)

    def tearDown(self):
        self.engine._stop_locked()
        llama_server.reset()
        self.temp.cleanup()

    def model_file(self, name: str = "qwen.gguf") -> Path:
        path = self.root / name
        path.write_bytes(b"gguf")
        return path

    def test_argv_is_what_llama_server_expects(self):
        argv = build_argv("/opt/llama-server", "/models/m.gguf", 8123, 4096, 4)
        self.assertEqual(argv[0], "/opt/llama-server")
        self.assertEqual(argv[argv.index("-m") + 1], "/models/m.gguf")
        self.assertEqual(argv[argv.index("--port") + 1], "8123")
        self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1")
        self.assertEqual(argv[argv.index("-c") + 1], "4096")
        self.assertEqual(argv[-2:], ["-t", "4"])
        self.assertNotIn("-t", build_argv("s", "m", 1, 2, 0))

    def test_status_is_honest_before_anything_is_set_up(self):
        status = self.engine.status()
        self.assertEqual(status["state"], "stopped")
        self.assertFalse(status["engine_installed"])
        self.assertEqual(status["models_dir"], str(self.root / "models"))
        self.assertEqual(status["model_name"], "")
        self.assertIn("no local model", status["detail"])
        self.assertEqual(status["engine"]["wanted_asset"], catalog.runtime_pattern())

    def test_ensure_does_nothing_when_nothing_is_configured(self):
        self.assertIsNone(self.engine.ensure({}))
        self.assertIsNone(self.engine.ensure({"llm_backend": "auto", "llm_model_path": ""}))
        missing = self.root / "gone.gguf"
        self.assertIsNone(self.engine.ensure({"llm_backend": "auto", "llm_model_path": str(missing)}))

    def test_ensure_respects_the_extractive_setting(self):
        model = self.model_file()
        self.assertIsNone(self.engine.ensure({"llm_backend": "extractive",
                                              "llm_model_path": str(model)}))

    def test_a_model_without_an_engine_explains_itself(self):
        model = self.model_file()
        settings = {"llm_backend": "auto", "llm_model_path": str(model)}
        self.assertIsNone(self.engine.ensure(settings))
        self.assertIn("local model engine is not", self.engine.detail)
        self.assertEqual(self.engine.state, "error")

    def test_starting_without_the_engine_says_what_to_do(self):
        model = self.model_file()
        with self.assertRaises(AuraError) as caught:
            self.engine.start(model, {})
        self.assertIn("engine is not installed", str(caught.exception))
        self.assertEqual(self.engine.state, "error")

    def test_an_installed_engine_is_found_even_without_its_record(self):
        folder = self.root / "runtime" / "llama-b11136-linux-x64"
        folder.mkdir(parents=True)
        (folder / catalog.binary_name()).write_bytes(b"#!binary")
        state = self.engine.engine_state()
        self.assertTrue(state["installed"])
        self.assertEqual(Path(state["binary"]).name, catalog.binary_name())
        self.assertIn("llama-b11136-linux-x64", state["folder"])

    def test_installing_the_engine_records_it_and_cleans_up(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr(catalog.binary_name(), "#!binary")
            bundle.writestr("ggml.dll", "binary")
        payload = archive.getvalue()

        def fake_download(url, dest, **kwargs):
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(payload)
            return Path(dest)

        asset = {"name": "llama-b9999-bin-win-cpu-x64.zip",
                 "browser_download_url": "https://example.test/engine.zip"}
        with mock.patch.object(llama_server, "_resolve_runtime_asset",
                               return_value=(asset, "b9999")), \
                mock.patch.object(llama_server.downloads, "download", fake_download):
            info = self.engine.install_runtime()

        self.assertEqual(info["tag"], "b9999")
        self.assertTrue(Path(info["binary"]).exists())
        state = self.engine.engine_state()
        self.assertTrue(state["installed"])
        self.assertEqual(state["tag"], "b9999")
        self.assertEqual(state["asset"], asset["name"])
        self.assertEqual(self.engine.engine_file().name, catalog.ENGINE_INFO_NAME)
        self.assertFalse(list(self.engine.runtime_root().glob("download/*.zip")))

    def test_a_live_child_process_becomes_the_local_backend(self):
        model = self.model_file()
        self.engine.proc = FakeProc(None)
        self.engine.state = "ready"
        self.engine.port = 8123
        self.engine.model_path = str(model)
        backend = self.engine.backend()
        self.assertIsNotNone(backend)
        self.assertEqual(backend.name, "local")
        self.assertEqual(backend.url, "http://127.0.0.1:8123/v1/chat/completions")
        self.assertIn("qwen.gguf", backend.label)

    def test_a_dead_child_process_is_not_a_backend(self):
        self.engine.proc = FakeProc(1)
        self.engine.state = "ready"
        self.engine.port = 8123
        self.assertIsNone(self.engine.backend())
        self.assertEqual(self.engine.state, "error")
        self.assertEqual(self.engine.port, 0)

    def test_stopping_terminates_the_child(self):
        proc = FakeProc(None)
        self.engine.proc = proc
        self.engine.state = "ready"
        self.engine.port = 4321
        self.engine.stop()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.engine.state, "stopped")
        self.assertEqual(self.engine.port, 0)
        self.assertIsNone(self.engine.proc)

    def test_default_model_prefers_what_is_already_there(self):
        self.assertEqual(self.engine.default_model(), "")
        models = self.engine.models_dir()
        models.mkdir(parents=True)
        (models / "my-own.gguf").write_bytes(b"x")
        self.assertEqual(self.engine.default_model(), str(models / "my-own.gguf"))

    def test_one_manager_per_data_folder(self):
        first = llama_server.manager(self.root)
        self.assertIs(first, llama_server.manager(self.root))
        self.assertIsNot(first, Manager(self.root))


class FakeWebview:
    """Stands in for pywebview and records what it was asked to do."""

    def __init__(self, fail_on_start: bool = False):
        self.calls = []
        self.fail_on_start = fail_on_start

    def create_window(self, title, url, **kwargs):
        self.calls.append(("create_window", title, url, kwargs))
        return object()

    def start(self, **kwargs):
        self.calls.append(("start", kwargs))
        if self.fail_on_start:
            raise RuntimeError("pythonnet could not be loaded")


class WindowTests(unittest.TestCase):
    """The shell ladder in aura/desktop.py - no screen, browser or Windows here."""

    URL = "http://127.0.0.1:8765"

    def test_the_native_window_is_tried_first(self):
        self.assertEqual(desktop.shell_order("auto"), ["webview", "app", "browser"])
        self.assertEqual(desktop.shell_order(""), ["webview", "app", "browser"])

    def test_a_shell_can_be_forced_or_turned_off(self):
        self.assertEqual(desktop.shell_order("none"), [])
        self.assertEqual(desktop.shell_order("browser"), ["browser"])
        self.assertEqual(desktop.shell_order("app"), ["app", "browser"])
        with self.assertRaises(desktop.ShellUnavailable):
            desktop.shell_order("internet-explorer")

    def test_the_native_window_takes_the_app_size_and_allows_text_selection(self):
        module = FakeWebview()
        result = desktop.open_window(self.URL, module=module, ready=True)
        self.assertEqual(result["shell"], "webview")
        self.assertTrue(result["blocking"], "closing a native window stops AURA")
        kind, _title, url, kwargs = module.calls[0]
        self.assertEqual(kind, "create_window")
        self.assertEqual(url, self.URL)
        self.assertEqual(kwargs["width"], desktop.WINDOW_WIDTH)
        self.assertEqual(kwargs["min_size"], desktop.WINDOW_MIN)
        self.assertTrue(kwargs["text_select"], "a study app must allow copying")
        self.assertEqual(module.calls[1], ("start", {"debug": False}))

    def test_the_chosen_shell_is_reported_before_the_window_blocks(self):
        seen = []
        desktop.open_window(self.URL, module=FakeWebview(), ready=True, on_shell=seen.append)
        self.assertEqual(seen, ["webview"])

    def test_a_broken_native_window_falls_back_to_an_app_window(self):
        launched = []
        module = FakeWebview(fail_on_start=True)
        result = desktop.open_window(
            self.URL, module=module, ready=True, spawn=launched.append,
            platform_name="windows", environ={"ProgramFiles": r"C:\Program Files"},
            which=lambda name: None, exists=lambda path: path.endswith("msedge.exe"))
        self.assertEqual(result["shell"], "app")
        self.assertFalse(result["blocking"])
        self.assertIn("--app=" + self.URL, launched[0])
        self.assertTrue(any(problem.startswith("webview:") for problem in result["problems"]))

    def test_a_missing_webview_runtime_skips_the_native_window_entirely(self):
        module = FakeWebview()
        opened = []
        result = desktop.open_window(self.URL, module=module, ready=False,
                                     opener=opened.append, platform_name="linux",
                                     which=lambda name: None, exists=lambda path: False)
        self.assertEqual(result["shell"], "browser")
        self.assertEqual(opened, [self.URL])
        self.assertEqual(module.calls, [])
        self.assertIn("WebView2 runtime is missing", result["problems"][0])

    def test_no_browser_and_no_webview_still_reaches_the_default_browser(self):
        opened = []
        result = desktop.open_window(self.URL, module=None, opener=opened.append,
                                     spawn=lambda argv: None, platform_name="linux",
                                     which=lambda name: None, exists=lambda path: False)
        self.assertEqual(result["shell"], "browser")
        self.assertEqual(opened, [self.URL])

    def test_no_shell_opens_nothing_at_all(self):
        opened = []
        result = desktop.open_window(self.URL, prefer="none", opener=opened.append)
        self.assertEqual(result["shell"], "none")
        self.assertEqual(opened, [])
        self.assertIn("no window was asked for", result["detail"])

    def test_edge_is_the_first_window_worth_trying_on_windows(self):
        env = {"ProgramFiles(x86)": r"C:\Program Files (x86)",
               "ProgramFiles": r"C:\Program Files"}
        candidates = desktop.browser_candidates("windows", env, which=lambda name: None)
        self.assertTrue(candidates[0].endswith("msedge.exe"))
        self.assertTrue(candidates[1].endswith("msedge.exe"))
        found = desktop.find_browser("windows", env, which=lambda name: None,
                                     exists=lambda path: path.endswith("chrome.exe"))
        self.assertTrue(found.endswith("chrome.exe"))

    def test_linux_browsers_are_looked_up_on_the_path(self):
        seen = []

        def which(name):
            seen.append(name)
            return "/usr/bin/chromium" if name == "chromium" else None

        found = desktop.find_browser("linux", {}, which=which, exists=lambda path: True)
        self.assertEqual(found, "/usr/bin/chromium")
        self.assertIn("google-chrome", seen)

    def test_the_app_window_arguments_are_chromeless(self):
        argv = desktop.app_argv("msedge.exe", self.URL, 1200, 800, r"C:\Users\me\.aura\browser-window")
        self.assertEqual(argv[0], "msedge.exe")
        self.assertIn("--app=" + self.URL, argv)
        self.assertIn("--window-size=1200,800", argv)
        self.assertIn(r"--user-data-dir=C:\Users\me\.aura\browser-window", argv)
        self.assertIn("--no-first-run", argv)
        self.assertNotIn("--new-window", argv)

    def test_an_app_window_needs_no_profile_directory(self):
        self.assertFalse(any(a.startswith("--user-data-dir")
                             for a in desktop.app_argv("chrome", self.URL)))

    def test_describe_explains_what_happened(self):
        self.assertIn("native AURA window", desktop.describe({"shell": "webview"}))
        line = desktop.describe({"shell": "none", "detail": "nothing worked"})
        self.assertIn("no window", line)
        self.assertIn("nothing worked", line)
        skipped = desktop.describe({"shell": "app", "problems": ["webview: missing"]})
        self.assertIn("skipped webview: missing", skipped)

    def test_the_console_is_only_hidden_on_windows(self):
        if os.name != "nt":
            self.assertFalse(desktop.hide_console())
            self.assertFalse(desktop.show_console())

    def test_the_webview_runtime_check_is_a_windows_question(self):
        self.assertTrue(desktop.webview_runtime_ready("linux"))
        self.assertTrue(desktop.webview_runtime_ready("macos"))

    def test_the_exe_icon_is_a_real_multi_size_ico(self):
        path = Path(__file__).resolve().parents[1] / "aura.ico"
        self.assertTrue(path.exists(), "aura.ico ships with the build")
        data = path.read_bytes()
        reserved, kind, count = struct.unpack_from("<HHH", data, 0)
        self.assertEqual((reserved, kind), (0, 1))
        self.assertGreaterEqual(count, 5)
        sizes = set()
        for index in range(count):
            at = 6 + index * 16
            width, height, _colours, _zero, planes, depth, size, offset = \
                struct.unpack_from("<BBBBHHII", data, at)
            self.assertEqual((planes, depth), (1, 32))
            self.assertEqual(struct.unpack_from("<I", data, offset)[0], 40, "BITMAPINFOHEADER")
            self.assertLessEqual(offset + size, len(data))
            sizes.add(256 if width == 0 else width)
        self.assertIn(16, sizes)
        self.assertIn(256, sizes)

    def test_the_web_ui_icon_is_served_next_to_the_ui(self):
        icon = Path(__file__).resolve().parents[1] / "aura" / "webui" / "icon.png"
        self.assertTrue(icon.exists())
        self.assertEqual(icon.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")


class EntryPointTests(unittest.TestCase):
    """The command line the user actually types (and the build reads)."""

    def test_the_window_is_what_you_get_by_default(self):
        args = parse_args([])
        self.assertEqual(args.shell, "auto")
        self.assertFalse(args.console)
        self.assertFalse(args.no_browser)

    def test_a_shell_can_be_forced(self):
        self.assertEqual(parse_args(["--shell", "app"]).shell, "app")
        self.assertEqual(parse_args(["--shell", "browser"]).shell, "browser")
        self.assertEqual(parse_args(["--shell", "none"]).shell, "none")

    def test_the_old_no_browser_flag_still_works(self):
        self.assertTrue(parse_args(["--no-browser"]).no_browser)


class BuildRecipeTests(unittest.TestCase):
    """A syntax error in the entry point or the spec only shows up on the build
    server - several minutes and one Wine install later - so check them here."""

    def test_every_python_file_and_the_spec_compile(self):
        root = Path(__file__).resolve().parents[1]
        sources = [p for p in sorted(root.rglob("*.py")) if "__pycache__" not in str(p)]
        sources.append(root / "aura.spec")
        self.assertGreaterEqual(len(sources), 15)
        for path in sources:
            with self.subTest(path=path.name):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")


if __name__ == "__main__":
    unittest.main(verbosity=2)
