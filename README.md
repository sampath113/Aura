# AURA - Agentic Multimodal Research Assistant

An offline-first study assistant. Add your PDFs, lecture notes, spreadsheets and
photos of the whiteboard; AURA indexes them **on your own machine** and answers
questions with citations back to the exact page they came from:

> **Q:** where do the light reactions happen?
> **A:** The light reactions occur in the thylakoid membrane and produce ATP and
> NADPH. [S1: bio.pdf, p. 7]

No account, no API key, no upload. The only thing that ever leaves the machine
is nothing.

---

## Why it is built this way

| Decision | Reason |
|---|---|
| Pure-Python BM25 index | always available, instant, no model download |
| Optional dense embeddings | semantic search when `fastembed` is installed, silently skipped otherwise |
| Optional local LLM (`llama-cpp-python` + a `.gguf`) | written answers when you have a model; sentence-level extraction from your sources when you don't |
| Local web UI instead of Tkinter/Qt | no GUI toolkit to install, identical UI on desktop and phone, and it is testable headlessly |
| Every answer validated against its evidence | a research assistant that invents references is worse than useless |

The pipeline is `ingest -> chunk (page-aware) -> BM25 (+dense) -> RRF + MMR -> answer -> verify citations`.

## Layout

```
aura_main.py            desktop entry point (the build server looks for this name)
aura.spec               PyInstaller recipe; ships aura/webui inside the executable
requirements.txt        the only hard dependencies (all prebuilt wheels)
requirements-llm.txt    optional: local .gguf models
requirements-embeddings.txt
aura/
  config.py             defaults, settings file, data directory
  models.py             Page / Document / Chunk / Hit / Answer dataclasses
  text.py               tokenising, sentence splitting, stemming, keyphrases
  chunk.py              page-aware chunking with sentence overlap
  index.py              BM25 + reciprocal rank fusion
  dense.py              optional semantic embeddings (never imported eagerly)
  retrieve.py           hybrid search + MMR, sentence ranking
  citations.py          [S1: bio.pdf, p. 7] formatting, parsing, validation
  answer.py             grounded prompt, extractive answerer, answer verification
  llm.py                llama.cpp / OpenAI-compatible / none
  ingest.py             PDF, DOCX, text, Markdown, CSV/TSV, images
  store.py              the Library: persistence + orchestration
  server.py             local HTTP API + static UI host
  webui/                the single-page interface (served by server.py)
android/                AURA Pocket - the phone client (Gradle project)
tests/test_aura.py      dependency-free test suite
```

`aura/` has **no `__init__.py` on purpose**: Python 3 namespace packages do not
need one, and the editor this project is maintained in cannot store files whose
name starts with an underscore. Do not "fix" it by adding one; `import aura`
already works, as does PyInstaller.

There is also no `.gitignore` in this folder for the same reason. If you want
one, create it in the repo root with:

```
__pycache__/
*.pyc
aura_data/
dist/
build/
*.spec.bak
```

## Run it

```bash
python aura_main.py                     # start AURA and open the browser
python aura_main.py --add notes.pdf     # add a document first
python aura_main.py --add-folder ./sem5 # ingest a whole folder
python aura_main.py --list              # what is in the library
python aura_main.py --ask "what is ATP?" # answer one question and exit
python aura_main.py --lan               # also let your phone connect
python aura_main.py --no-dense --rebuild
```

The library lives in `~/.aura` (override with `--data-dir` or `AURA_DATA_DIR`).

## The phone client (AURA Pocket)

`../android/` is a Gradle project. It bundles one screen - a connection dialog -
and then loads **the same web UI** from the AURA server on your local network, so
there is only ever one interface to maintain. Start the desktop app with `--lan`,
read the printed URL (`http://192.168.x.x:8765`), type it into the app once.

## Building the installers

The repo is laid out for the AURA build console (`perchance.org/aura-build-console`)
and its Colab build server:

* the desktop entry point is `aura_main.py` (one of the names the server probes for);
* `requirements.txt` at the repo root is installed into the interpreter that freezes the app;
* any directory holding `settings.gradle` is treated as the Android project - here `android/`;
* `aura.spec` is picked up automatically, which is what bundles `aura/webui` into `AURA.exe`.

`python -m unittest discover -s tests -t .` runs the suite.

## Optional extras

```bash
pip install -r requirements-llm.txt          # then set the .gguf path in Settings
pip install -r requirements-embeddings.txt   # semantic search (downloads a small model)
```

OCR for images needs a system binary as well as the Python glue:

```bash
pip install pytesseract          # plus: apt install tesseract-ocr / the Windows installer
```

Without an OCR engine, images are still catalogued and cited as figures - they
just do not contribute text.

## Honest limitations

* Non-paginated formats (DOCX, TXT, CSV) cite *section numbers*, not printed page
  numbers, because those formats have no pages.
* Scanned PDFs need OCR; the ingest step says so explicitly instead of silently
  indexing an empty page.
* Dense retrieval and the local LLM are opt-in: the default install stays small
  and runs on a machine with nothing but Python.
* The Android app is a client for the desktop engine, not an on-device model.
  Running a quantised LLM natively on Android would be the next step, and it is
  deliberately out of scope here.
