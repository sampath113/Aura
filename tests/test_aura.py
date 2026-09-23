"""AURA's test suite - run with:  python -m unittest discover -s tests -t .

No third-party test runner is required, and nothing here touches the network.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aura.answer import answer_question, compose_extractive, verify_answer  # noqa: E402
from aura.chunk import chunk_pages  # noqa: E402
from aura.citations import citation_label, parse_labels, validate  # noqa: E402
from aura.index import BM25Index, reciprocal_rank_fusion  # noqa: E402
from aura.ingest import ingest_docx, ingest_file, ingest_table, _docx_stdlib  # noqa: E402
from aura.models import AuraError, Page  # noqa: E402
from aura.retrieve import Retriever  # noqa: E402
from aura.store import Library  # noqa: E402
from aura.text import keyphrases, split_sentences, stem, tokenize  # noqa: E402


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
            self.assertLessEqual(len(chunk.text), 300 + 120)
            self.assertGreater(len(chunk.text), 60)
        page_two = next(chunk for chunk in chunks if chunk.page == 2)
        self.assertIn("Page two sentence", page_two.text)
        rebuilt = pages[1].text[page_two.start:page_two.end]
        self.assertEqual(rebuilt.strip(), page_two.text.strip())

    def test_overlap_exists_between_neighbours(self):
        text = "\n\n".join("Paragraph {} with a reasonably long body of text to fill space."
                           .format(i) for i in range(24))
        chunks = chunk_pages([Page(number=1, text=text)], "d", "f.txt",
                             target_chars=400, overlap_chars=120, min_chars=80)
        self.assertGreater(len(chunks), 3)
        tail_words = set(tokenize(chunks[0].text)) & set(tokenize(chunks[1].text))
        self.assertTrue(tail_words, "consecutive chunks should share some overlap")
        all_words = set(tokenize(" ".join(chunk.text for chunk in chunks)))
        self.assertIn("paragraph", all_words)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
