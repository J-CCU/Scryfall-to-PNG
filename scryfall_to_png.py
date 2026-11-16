# Scryfall -> Shopify image extractor (PNG only) with template output

import csv
import uuid
from pathlib import Path
import gzip
import json
import os
import sys
import shutil
import re
import pickle
import queue
import threading
import time
import requests
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk

# ------------------------------------------------------------
# CONSTANTS
# ------------------------------------------------------------

BULK_META_URL = "https://api.scryfall.com/bulk-data"
BULK_TYPE = "all_cards"

# Persistent writable data lives OUTSIDE the packaged executable or .app.
# No bulk database/index files need to be included in PyInstaller builds.
def get_data_directory() -> Path:
    if sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or
                    (Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_DATA_HOME") or
                    (Path.home() / ".local" / "share"))
    path = root / "ScryfallToPNG"
    path.mkdir(parents=True, exist_ok=True)
    return path

DATA_DIR = get_data_directory()
BULK_FILENAME = str(DATA_DIR / "scryfall_all_cards.jsonl.gz")
META_FILENAME = str(DATA_DIR / "bulk_metadata.json")
# Bump INDEX_VERSION whenever the cached index layout changes so old caches rebuild.
INDEX_VERSION = 2
INDEX_FILENAME = str(DATA_DIR / f"scryfall_all_cards_index_v{INDEX_VERSION}.pkl")
# Version 1 pickled every full card dict (~3 GB); it is never read, only removed.
LEGACY_INDEX_FILENAME = str(DATA_DIR / "scryfall_all_cards_index_en.pkl")
USER_AGENT = {"User-Agent": "ShopifyScryfallImageExtractor/2.0 (Scryfall bulk data)"}

def migrate_existing_cache(log_callback=None):
    """One-time copy of old adjacent-to-script caches for normal .py installs.
    No cache is bundled and no attempt is made to write into an app bundle.
    """
    if getattr(sys, "frozen", False):
        return
    source_dir = Path(__file__).resolve().parent
    if source_dir == DATA_DIR:
        return
    for name in ("scryfall_all_cards.jsonl.gz", "bulk_metadata.json",
                 os.path.basename(INDEX_FILENAME)):
        source = source_dir / name
        destination = DATA_DIR / name
        if not source.is_file() or destination.exists():
            continue
        temp = destination.with_name(destination.name + ".migrating")
        try:
            with source.open("rb") as inp, temp.open("wb") as out:
                shutil.copyfileobj(inp, out, 1024 * 1024)
            os.replace(temp, destination)
            if log_callback:
                log_callback(f"Reused existing cache: {name}")
        except OSError as exc:
            temp.unlink(missing_ok=True)
            if log_callback:
                log_callback(f"Cannot migrate {name}: {exc}; will rebuild if needed")

# Template fieldnames (must match product_template.csv exactly)

TEMPLATE_FIELDNAMES = [
    "Title",
    "URL handle",
    "Description",
    "Vendor",
    "Product category",
    "Type",
    "Tags",
    "Published on online store",
    "Status",
    "SKU",
    "Barcode",
    "Option1 name",
    "Option1 value",
    "Option1 Linked To",
    "Option2 name",
    "Option2 value",
    "Option2 Linked To",
    "Option3 name",
    "Option3 value",
    "Option3 Linked To",
    "Price",
    "Compare-at price",
    "Cost per item",
    "Charge tax",
    "Tax code",
    "Unit price total measure",
    "Unit price total measure unit",
    "Unit price base measure",
    "Unit price base measure unit",
    "Inventory tracker",
    "Inventory quantity",
    "Continue selling when out of stock",
    "Weight value (grams)",
    "Weight unit for display",
    "Requires shipping",
    "Fulfillment service",
    "Product image URL",
    "Image position",
    "Image alt text",
    "Variant image URL",
    "Gift card",
    "SEO title",
    "SEO description",
    "Color (product.metafields.shopify.color-pattern)",
    "Google Shopping / Google product category",
    "Google Shopping / Gender",
    "Google Shopping / Age group",
    "Google Shopping / Manufacturer part number (MPN)",
    "Google Shopping / Ad group name",
    "Google Shopping / Ads labels",
    "Google Shopping / Condition",
    "Google Shopping / Custom product",
    "Google Shopping / Custom label 0",
    "Google Shopping / Custom label 1",
    "Google Shopping / Custom label 2",
    "Google Shopping / Custom label 3",
    "Google Shopping / Custom label 4",
]

# ------------------------------------------------------------
# BULK METADATA
# ------------------------------------------------------------

def load_bulk_metadata():
    if not os.path.exists(META_FILENAME):
        return None
    try:
        with open(META_FILENAME, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def save_bulk_metadata(meta):
    with open(META_FILENAME, "w", encoding="utf-8") as f:
        json.dump(meta, f)

def bulk_file_is_fresh(path: str, meta: dict | None) -> bool:
    if not os.path.exists(path) or not meta:
        return False
    mtime = datetime.fromtimestamp(os.path.getmtime(path))
    return datetime.now() - mtime < timedelta(hours=24)

def download_bulk_file(path: str, progress_callback=None, log_callback=None):
    if log_callback:
        log_callback("Requesting bulk metadata from Scryfall...")
    resp = requests.get(BULK_META_URL, headers=USER_AGENT, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("data", [])
    url = None
    compressed_size = None
    updated_at = None
    for item in items:
        if item.get("type") == BULK_TYPE:
            url = item.get("jsonl_download_uri")
            compressed_size = item.get("compressed_size")
            updated_at = item.get("updated_at")
            break
    if not url or not compressed_size or not updated_at:
        raise RuntimeError("All Cards bulk data not found or incomplete.")
    if log_callback:
        mb = compressed_size / (1024 * 1024)
        log_callback(f"Downloading All Cards bulk file (~{mb:.1f} MB)...")
    r = requests.get(url, stream=True, headers=USER_AGENT, timeout=120)
    r.raise_for_status()
    downloaded = 0
    partial_path = path + ".download"
    try:
        with open(partial_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, compressed_size)
        if downloaded != compressed_size:
            raise RuntimeError(f"Incomplete bulk download: {downloaded}/{compressed_size} bytes")
        os.replace(partial_path, path)
    finally:
        r.close()
        if os.path.exists(partial_path):
            os.remove(partial_path)
    if log_callback:
        log_callback("Download complete.")
    save_bulk_metadata({"updated_at": updated_at})

# Card fields read by indexing, matching and output (build_index, card_names,
# match_printing, resolve_printing, describe_printing_conflicts,
# prefer_language, extract_png_urls, process_csv). Everything else in a
# Scryfall card record is dropped so the in-memory and cached index stay small.
CARD_FIELDS = ("id", "oracle_id", "name", "printed_name", "lang", "set",
               "collector_number", "finishes")
FACE_FIELDS = ("name", "printed_name")
# Long loops hand the GIL to the GUI thread every this many items.
YIELD_EVERY = 5000

def slim_card(card, _shared={}):
    slim = {k: card[k] for k in CARD_FIELDS if k in card}
    for k in ("lang", "set"):
        if isinstance(slim.get(k), str):
            slim[k] = sys.intern(slim[k])
    if isinstance(slim.get("finishes"), list):
        key = tuple(slim["finishes"])
        slim["finishes"] = _shared.setdefault(key, list(key))
    png = (card.get("image_uris") or {}).get("png")
    if png:
        slim["image_uris"] = {"png": png}
    if "card_faces" in card:
        faces = []
        for face in card["card_faces"] or []:
            f = {k: face[k] for k in FACE_FIELDS if k in face}
            png = (face.get("image_uris") or {}).get("png")
            if png:
                f["image_uris"] = {"png": png}
            faces.append(f)
        slim["card_faces"] = faces
    return slim

def load_bulk_data(path: str, log_callback=None):
    if log_callback:
        log_callback("Loading bulk data...")
    cards = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for n, line in enumerate(f, start=1):
            cards.append(slim_card(json.loads(line)))
            if n % YIELD_EVERY == 0:
                time.sleep(0)
    if log_callback:
        log_callback(f"Loaded {len(cards)} cards.")
    return cards

# ------------------------------------------------------------
# INDEX BUILDING
# ------------------------------------------------------------

def build_index(cards, log_callback=None):
    if log_callback:
        log_callback("Indexing bulk data...")
    triple = {}
    double = {}
    single = {}
    oracle_triple = {}
    oracle_double = {}
    oracle_single = {}
    coll_set = {}
    coll_only = {}
    for n, c in enumerate(cards, start=1):
        if n % YIELD_EVERY == 0:
            time.sleep(0)
        name = c.get("name", "")
        printed = c.get("printed_name", "")
        set_code = c.get("set", "")
        collector = c.get("collector_number", "")
        oracle_id = c.get("oracle_id", "")
        scryfall_id = c.get("id", "")
        lang = c.get("lang", "")
        if not name or not set_code or not collector:
            continue
        name_l = name.lower()
        printed_l = printed.lower() if printed else None
        set_l = set_code.lower()
        collector_l = collector
        oracle_l = oracle_id if oracle_id else None
        scry_l = scryfall_id if scryfall_id else None
        coll_set[(set_l, collector_l)] = c
        coll_only.setdefault(collector_l, []).append(c)
        triple[(name_l, set_l, collector_l)] = c
        double[(name_l, set_l)] = c
        single[name_l] = c
        if printed_l:
            triple[(printed_l, set_l, collector_l)] = c
            double[(printed_l, set_l)] = c
            single[printed_l] = c
        if "card_faces" in c:
            for face in c["card_faces"]:
                fn = face.get("name", "")
                if fn:
                    fn_l = fn.lower()
                    triple[(fn_l, set_l, collector_l)] = c
                    double[(fn_l, set_l)] = c
                    single[fn_l] = c
                pf = face.get("printed_name", "")
                if pf:
                    pf_l = pf.lower()
                    triple[(pf_l, set_l, collector_l)] = c
                    double[(pf_l, set_l)] = c
                    single[pf_l] = c
        if oracle_l:
            oracle_single[oracle_l] = c
            oracle_double[(oracle_l, set_l)] = c
            oracle_triple[(oracle_l, set_l, collector_l)] = c
        if scry_l:
            oracle_single[scry_l] = c
            oracle_double[(scry_l, set_l)] = c
            oracle_triple[(scry_l, set_l, collector_l)] = c
    if log_callback:
        log_callback("Index built.")
    return (
        triple,
        double,
        single,
        oracle_triple,
        oracle_double,
        oracle_single,
        coll_set,
        coll_only,
    )

# The cache holds the slimmed card list (see slim_card) as a small header
# followed by fixed-size pickled chunks. One huge pickle.load/dump would hold
# the GIL for minutes and freeze the window; chunks let the GUI run in between.
# The lookup dicts are rebuilt from the list with build_index on load.
INDEX_CHUNK = 20000

def save_index(cards, meta, log_callback=None):
    header = {
        "index_version": INDEX_VERSION,
        "meta_updated_at": meta.get("updated_at") if meta else None,
        "count": len(cards),
    }
    temp = INDEX_FILENAME + ".tmp"
    try:
        with open(temp, "wb") as f:
            pickle.dump(header, f, protocol=pickle.HIGHEST_PROTOCOL)
            for start in range(0, len(cards), INDEX_CHUNK):
                pickle.dump(cards[start:start + INDEX_CHUNK], f,
                            protocol=pickle.HIGHEST_PROTOCOL)
                time.sleep(0)
        os.replace(temp, INDEX_FILENAME)
    finally:
        if os.path.exists(temp):
            os.remove(temp)
    if log_callback:
        log_callback("Index saved.")

def load_index(meta, log_callback=None):
    if os.path.exists(LEGACY_INDEX_FILENAME):
        try:
            os.remove(LEGACY_INDEX_FILENAME)
            if log_callback:
                log_callback("Removed old-format index cache; it will be rebuilt.")
        except OSError:
            pass
    if not os.path.exists(INDEX_FILENAME) or not meta:
        return None
    try:
        with open(INDEX_FILENAME, "rb") as f:
            header = pickle.load(f)
            if (not isinstance(header, dict)
                    or header.get("index_version") != INDEX_VERSION):
                return None
            if header.get("meta_updated_at") != meta.get("updated_at"):
                if log_callback:
                    log_callback("Cached index stale.")
                return None
            cards = []
            while len(cards) < header["count"]:
                cards.extend(pickle.load(f))
                time.sleep(0)
    except Exception:
        return None
    if log_callback:
        log_callback("Loaded cached index.")
    return cards
    
# ------------------------------------------------------------
# NAME / SET / COLLECTOR EXTRACTION
# ------------------------------------------------------------

def strip_trailing_parenthetical(text: str) -> str:
    """Remove trailing parenthetical labels; always make forward progress."""
    text = text.strip()
    pattern = re.compile(r"\s+\([^()]+\)\s*$")
    while text:
        match = pattern.search(text)
        if not match:
            break
        shortened = text[:match.start()].strip()
        if len(shortened) >= len(text):
            break
        text = shortened
    return text

def extract_name(title: str) -> str:
    return strip_trailing_parenthetical(title.split(" - ")[0])

def extract_name_candidates(title: str) -> list[str]:
    parts = title.split(" - ")
    if len(parts) == 3:
        return [strip_trailing_parenthetical(parts[1])]
    return [extract_name(title)]

def extract_set_code(title: str) -> str | None:
    match = re.search(r"\(([^()]+)\)\s*$", title)
    return match.group(1).strip().lower() if match else None

def normalize_set_code(code: str | None) -> str | None:
    if not code:
        return None
    code = re.sub(r"\s+", " ", code.strip().lower())
    if re.fullmatch(r"(?:friday night magic|fnm)(?:\s*[-/]?\s*(?:foil|nonfoil|non-foil))?", code):
        return "fnm"
    return {"list": "plst"}.get(code, code)

def extract_collector_number(body_html: str) -> str | None:
    m = re.search(
        r"<strong>\s*#:\s*</strong>\s*([0-9A-Za-z-\/]+)",
        body_html,
        flags=re.IGNORECASE,
    )
    return m.group(1).strip() if m else None

def normalize_collector_number(num: str | None) -> str | None:
    if not num:
        return None
    return num.strip().lower().lstrip("0") or "0"

# ------------------------------------------------------------
# MATCHING LOGIC
# ------------------------------------------------------------

def match_card_collector_first(
    triple,
    double,
    single,
    oracle_triple,
    oracle_double,
    oracle_single,
    coll_set,
    coll_only,
    name,
    set_code,
    collector_number,
    oracle_id=None,
):
    name_l = name.lower() if name else None
    set_l = set_code.lower() if set_code else None
    collector_l = collector_number if collector_number else None
    oracle_l = oracle_id if oracle_id else None
    if oracle_l and set_l and collector_l:
        card = oracle_triple.get((oracle_l, set_l, collector_l))
        if card:
            return card
    if set_l and collector_l:
        card = coll_set.get((set_l, collector_l))
        if card:
            return card
    if name_l and set_l and collector_l:
        card = triple.get((name_l, set_l, collector_l))
        if card:
            return card
    if oracle_l and set_l:
        card = oracle_double.get((oracle_l, set_l))
        if card:
            return card
    if name_l and set_l:
        card = double.get((name_l, set_l))
        if card:
            return card
    if oracle_l:
        card = oracle_single.get(oracle_l)
        if card:
            return card
    if name_l:
        card = single.get(name_l)
        if card:
            return card
    if name_l:
        for key in single.keys():
            if key.startswith(name_l):
                return single[key]
    return None

def prefer_language(card, name_candidates, coll_only, collector_number):

    """
    Always prefer English printings when available.
    Falls back to the matched card if no English version exists.
    """
    
    if not collector_number:
        return card
    
    # All printings with this collector number

    variants = coll_only.get(collector_number, [])
    if not variants:
        return card

    # Try to find English version with same oracle_id

    oracle_id = card.get("oracle_id")
    if oracle_id:
        for c in variants:
            if (c.get("oracle_id") == oracle_id and c.get("lang") == "en"
                    and c.get("set") == card.get("set")) :
                return c
            
    # If oracle_id missing or inconsistent, fallback to any English printing

    for c in variants:
        if c.get("lang") == "en":
            return c
        
    # No English version exists → return original

    return card

def card_names(card):
    names = [card.get("name", ""), card.get("printed_name", "")]
    for face in card.get("card_faces", []):
        names.extend((face.get("name", ""), face.get("printed_name", "")))
    return {n.casefold().strip() for n in names if n}

def make_printing_index(coll_only):
    
    """Keep every printing, instead of overwriting name/set keys."""

    index = {}
    seen = set()
    for group in coll_only.values():
        for card in group:
            identity = card.get("id") or id(card)
            if identity in seen:
                continue
            seen.add(identity)
            set_code = (card.get("set") or "").casefold()
            for name in card_names(card):
                index.setdefault((set_code, name), []).append(card)
    return index

def make_exact_id_index(coll_only):
    
    """Index Scryfall printing IDs and Oracle IDs without losing variants."""
    
    by_printing_id = {}
    by_oracle_id = {}
    seen = set()
    for group in coll_only.values():
        for c in group:
            identifier = str(c.get("id") or "").strip().lower()
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            by_printing_id[identifier] = c
            oracle = str(c.get("oracle_id") or "").strip().lower()
            if oracle:
                by_oracle_id.setdefault(oracle, []).append(c)
    return by_printing_id, by_oracle_id

def describe_printing_conflicts(card, names, set_code, collector, finish):
    
    """Informational contradictions; an exact printing ID is authoritative."""
    
    messages = []
    if names and not any(n.casefold().strip() in card_names(card) for n in names):
        messages.append("name")
    if set_code and str(card.get("set", "")).casefold() != set_code.casefold():
        messages.append("set")
    if collector is not None and normalize_collector_number(str(card.get("collector_number", ""))) != collector:
        messages.append("collector")
    if finish and finish not in (card.get("finishes") or []):
        messages.append("finish")
    return messages

def resolve_printing(exact_index, oracle_index, printing_index, names, set_code,
                     collector=None, finish=None, scryfall_id=None):
    
    """Prefer exact printing ID; never silently substitute an unrelated printing."""
    
    supplied = str(scryfall_id or "").strip().lower()
    if supplied:
        exact = exact_index.get(supplied)
        if exact:
            if not extract_png_urls(exact):
                return None, "exact printing ID has no PNG image", []
            return exact, "exact Scryfall printing ID", describe_printing_conflicts(
                exact, names, set_code, collector, finish)
        # An oracle ID describes a card, not a physical printing. It must still
        # be disambiguated by set, collector number, finish and name.
        if supplied in oracle_index:
            choices = oracle_index[supplied]
            choices = [c for c in choices if (not set_code or str(c.get("set", "")).casefold() == set_code.casefold())
                       and (collector is None or normalize_collector_number(str(c.get("collector_number", ""))) == collector)
                       and (not finish or finish in (c.get("finishes") or []))
                       and (not names or any(n.casefold().strip() in card_names(c) for n in names))
                       and extract_png_urls(c)]
            # Same English preference as match_printing.
            english = [c for c in choices if c.get("lang") == "en"]
            if english:
                choices = english
            if len(choices) == 1:
                return choices[0], "oracle ID with verified printing", []
            return None, "oracle ID printing ambiguous or incompatible", []
        # A genuine unknown Scryfall ID should not silently match another printing.
        return None, "Scryfall ID absent from local bulk index (update bulk data)", []
    card = match_printing(printing_index, names, set_code, collector=collector, finish=finish)
    if card:
        return card, "name/set/collector/finish", []
    return None, "no unambiguous compatible printing", []

def extract_requested_finish(title, row=None):

    """Returns foil/nonfoil/None. Missing finish does NOT imply nonfoil."""

    values = [title]
    if row:
        values.extend(str(row.get(k, "")) for k in (
            "Option1 Value", "Option2 Value", "Option3 Value", "Tags"
        ))
    value = " ".join(values)
    if re.search(r"\b(?:non[ -]?foil|not[ -]?foil|regular finish)\b", value, re.I):
        return "nonfoil"
    if re.search(r"\bfoil(?:ed)?\b", value, re.I):
        return "foil"
    return None

def match_printing(printing_index, names, set_code, collector=None,
                   finish=None, scryfall_id=None):

    """
    Resolve identity + set + collector + finish together, without cross-set drift.
    Explicit printing constraints are mandatory. An ambiguous match remains unmatched.
    A Scryfall printing ID can disambiguate records, but must satisfy constraints.
    """

    if not set_code:
        return None
    unique = {}
    for name in names:
        for card in printing_index.get((set_code.casefold(), name.casefold().strip()), []):
            unique[card.get("id") or id(card)] = card
    choices = list(unique.values())
    if collector is not None:
        choices = [c for c in choices if
                   normalize_collector_number(str(c.get("collector_number", ""))) == collector]
    if finish:
        choices = [c for c in choices if finish in (c.get("finishes") or [])]
    if scryfall_id:

        # The supplied field may be a printing ID or an oracle ID.

        matching = [c for c in choices if c.get("id") == scryfall_id]
        if matching:
            choices = matching
        else:
            matching = [c for c in choices if c.get("oracle_id") == scryfall_id]
            if matching:
                choices = matching
            else:
                return None
    choices = [c for c in choices if extract_png_urls(c)]
    if not choices:
        return None
    english = [c for c in choices if c.get("lang") == "en"]
    if english:
        choices = english

    # A Scryfall record may advertise both foil and nonfoil. That is not ambiguity:
    # the record represents the same physical artwork for both finishes.

    printing_keys = {(c.get("set"), c.get("collector_number"),
                      c.get("name")) for c in choices}
    if len(printing_keys) != 1:
        return None
    return sorted(choices, key=lambda c: str(c.get("id", "")))[0]

def extract_png_urls(card):
    if "card_faces" in card and card["card_faces"]:
        urls = []
        for i, face in enumerate(card["card_faces"]):
            iu = face.get("image_uris")
            if iu and iu.get("png"):
                url = iu["png"]
                if i == 1:
                    url = url.replace("/front/", "/back/")
                urls.append(url)
        if urls:
            return urls
    iu = card.get("image_uris")
    if iu and iu.get("png"):
        return [iu["png"]]
    return []

# ------------------------------------------------------------
# GUI
# ------------------------------------------------------------

# Shopify export columns that only a variant row fills in.
VARIANT_FIELDS = ("Option1 Value", "Option2 Value", "Option3 Value",
                  "Variant SKU", "Variant Price", "Variant Barcode")

class ScryfallBulkGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Scryfall Image Extractor")
        self.geometry("800x550")
        self.resizable(False, False)
        self.triple = None
        self.double = None
        self.single = None
        self.oracle_triple = None
        self.oracle_double = None
        self.oracle_single = None
        self.coll_set = None
        self.coll_only = None
        self.printing_index = {}
        self.exact_id_index = {}
        self.oracle_id_index = {}
        self.create_widgets()

    def create_widgets(self):
        self.load_btn = tk.Button(
            self,
            text="Load / Update Scryfall Bulk",
            command=self.load_bulk,
        )
        self.load_btn.pack(pady=10)
        self.select_btn = tk.Button(
            self,
            text="Select Shopify CSV",
            command=self.select_file,
            state="disabled",
        )
        self.select_btn.pack(pady=10)
        self.progress = ttk.Progressbar(
            self,
            orient="horizontal",
            length=700,
            mode="determinate",
        )
        self.progress.pack(pady=10)
        self.log = tk.Text(self, height=25, width=100, state="disabled")
        self.log.pack(pady=10)

    def log_message(self, msg: str):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def update_download_progress(self, downloaded, total):
        self.progress["maximum"] = total
        self.progress["value"] = downloaded
        self.update_idletasks()

    def load_bulk(self):

        # Loading and indexing run on a worker thread so the window stays
        # responsive. The worker never touches Tk; it queues UI calls that the
        # main thread runs from an after() poll.

        self._select_state_before_load = str(self.select_btn.cget("state"))
        self.load_btn.config(state="disabled")
        self.select_btn.config(state="disabled")
        self._ui_queue = queue.Queue()
        threading.Thread(target=self._load_bulk_worker, daemon=True).start()
        self.after(50, self._drain_ui_queue)

    def _ui(self, func, *args, **kwargs):
        self._ui_queue.put((func, args, kwargs))

    def _drain_ui_queue(self):
        while True:
            try:
                func, args, kwargs = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            if func is None:
                self.load_btn.config(state="normal")
                return
            try:
                func(*args, **kwargs)
            except Exception as exc:
                self.log_message(f"UI update failed: {exc}")
        self.after(50, self._drain_ui_queue)

    def _set_progress(self, **options):
        for key, value in options.items():
            self.progress[key] = value

    def _load_bulk_worker(self):
        log = lambda msg: self._ui(self.log_message, msg)
        try:
            migrate_existing_cache(log)
            meta = load_bulk_metadata()
            log(f"Persistent Scryfall data directory: {DATA_DIR}")
            log("Checking bulk file cache...")

            # An existing local bulk file without metadata can be reused when
            # offline, but rebuilding metadata is preferred when online.
            
            adopted_local_file = False
            if meta is None and os.path.exists(BULK_FILENAME):
                log("Existing bulk file without metadata; using local data.")
                meta = {"updated_at": "local-existing-file"}
                save_bulk_metadata(meta)
                adopted_local_file = True
            if not bulk_file_is_fresh(BULK_FILENAME, meta):
                log("Bulk cache is missing or older than 24 hours; checking Scryfall...")
                self._ui(self._set_progress, value=0)
                unchanged = False
                if adopted_local_file:
                    # Keep previously downloaded data instead of forcing a huge
                    # first-run re-download solely because metadata is missing.
                    # Later stale checks compare against Scryfall as usual.
                    unchanged = True
                elif os.path.exists(BULK_FILENAME) and meta:
                    try:
                        response = requests.get(BULK_META_URL, headers=USER_AGENT, timeout=30)
                        response.raise_for_status()
                        item = next((v for v in response.json().get("data", [])
                                     if v.get("type") == BULK_TYPE), None)
                        unchanged = bool(item and item.get("updated_at") == meta.get("updated_at"))
                    except requests.RequestException as exc:
                        log(f"Metadata check unavailable; using cached data: {exc}")
                        unchanged = True
                if unchanged:
                    os.utime(BULK_FILENAME, None)
                    log("Reusing existing bulk data.")
                else:
                    download_bulk_file(
                        BULK_FILENAME,
                        progress_callback=lambda done, total: self._ui(
                            self.update_download_progress, done, total),
                        log_callback=log,
                    )
                meta = load_bulk_metadata()
            else:
                log("Bulk file is fresh.")
            self._ui(self._set_progress, value=0, mode="indeterminate")
            self._ui(self.progress.start, 10)
            cards = load_index(meta, log_callback=log)
            cached = cards is not None
            if not cached:
                cards = load_bulk_data(BULK_FILENAME, log_callback=log)
                save_index(cards, meta, log_callback=log)
            (
                self.triple,
                self.double,
                self.single,
                self.oracle_triple,
                self.oracle_double,
                self.oracle_single,
                self.coll_set,
                self.coll_only,
            ) = build_index(cards, log_callback=log)
            self.printing_index = make_printing_index(self.coll_only)
            self.exact_id_index, self.oracle_id_index = make_exact_id_index(self.coll_only)
            del cards
            self._ui(self.progress.stop)
            self._ui(self._set_progress, mode="determinate", value=0)
            log("Index ready (cached)." if cached else "Bulk data loaded and indexed.")
            self._ui(self.select_btn.config, {"state": "normal"})
        except Exception as e:
            self._ui(self.progress.stop)
            self._ui(self._set_progress, mode="determinate", value=0)
            log(f"Bulk load failed: {e}")
            self._ui(self.select_btn.config, {"state": self._select_state_before_load})
            self._ui(messagebox.showerror, "Bulk Load Error", str(e))
        finally:
            self._ui(None)

    def select_file(self):
        file_path = filedialog.askopenfilename(
            title="Select Shopify CSV",
            filetypes=[("CSV Files", "*.csv")],
        )
        if not file_path:
            messagebox.showerror("Error", "No file selected.")
            return

        # Processing runs on a worker thread with the same UI queue as loading.

        self.load_btn.config(state="disabled")
        self.select_btn.config(state="disabled")
        self._ui_queue = queue.Queue()
        threading.Thread(target=self._process_csv_worker, args=(file_path,),
                         daemon=True).start()
        self.after(50, self._drain_ui_queue)

    def _process_csv_worker(self, file_path):
        try:
            self.process_csv(file_path)
        except Exception as e:
            self._ui(self.log_message, f"Processing failed: {e}")
            self._ui(messagebox.showerror, "Processing Error", str(e))
        finally:
            self._ui(self.select_btn.config, {"state": "normal"})
            self._ui(None)

    def build_template_row(self, src_row: dict) -> dict:
        
        """Map a products_export row into product_template format."""

        r = src_row
        out = {
            "Title": r.get("Title", ""),
            "URL handle": r.get("Handle", ""),
            "Description": r.get("Body (HTML)", ""),
            "Vendor": r.get("Vendor", ""),
            "Product category": r.get("Product Category", ""),
            "Type": r.get("Type", ""),
            "Tags": r.get("Tags", ""),
            "Published on online store": r.get("Published", ""),
            "Status": r.get("Status", ""),
            "SKU": r.get("Variant SKU", ""),
            "Barcode": r.get("Variant Barcode", ""),
            "Option1 name": r.get("Option1 Name", ""),
            "Option1 value": r.get("Option1 Value", ""),
            "Option1 Linked To": r.get("Option1 Linked To", ""),
            "Option2 name": r.get("Option2 Name", ""),
            "Option2 value": r.get("Option2 Value", ""),
            "Option2 Linked To": r.get("Option2 Linked To", ""),
            "Option3 name": r.get("Option3 Name", ""),
            "Option3 value": r.get("Option3 Value", ""),
            "Option3 Linked To": r.get("Option3 Linked To", ""),
            "Price": r.get("Variant Price", ""),
            "Compare-at price": r.get("Variant Compare At Price", ""),
            "Cost per item": r.get("Cost per item", ""),
            "Charge tax": r.get("Variant Taxable", ""),
            "Tax code": r.get("Variant Tax Code", ""),
            "Unit price total measure": r.get("Unit Price Total Measure", ""),
            "Unit price total measure unit": r.get("Unit Price Total Measure Unit", ""),
            "Unit price base measure": r.get("Unit Price Base Measure", ""),
            "Unit price base measure unit": r.get("Unit Price Base Measure Unit", ""),
            "Inventory tracker": r.get("Variant Inventory Tracker", ""),
            "Inventory quantity": r.get("Variant Inventory Qty", ""),
            "Continue selling when out of stock": "deny",
        }
        out["Weight value (grams)"] = r.get("Variant Grams", "")
        out["Weight unit for display"] = r.get("Variant Weight Unit", "")
        out["Requires shipping"] = r.get("Variant Requires Shipping", "")
        out["Fulfillment service"] = r.get("Variant Fulfillment Service", "")
        out["Product image URL"] = r.get("Image Src", "")
        out["Image position"] = r.get("Image Position", "")
        out["Image alt text"] = r.get("Image Alt Text", "") or r.get("Image alt text", "")
        out["Variant image URL"] = r.get("Variant Image", "")
        out["Gift card"] = r.get("Gift Card", "")
        out["SEO title"] = r.get("SEO Title", "")
        out["SEO description"] = r.get("SEO Description", "")
        out["Color (product.metafields.shopify.color-pattern)"] = r.get(
            "Color (product.metafields.shopify.color-pattern)", ""
        )
        out["Google Shopping / Google product category"] = r.get(
            "Google Shopping / Google Product Category", ""
        )
        out["Google Shopping / Gender"] = r.get("Google Shopping / Gender", "")
        out["Google Shopping / Age group"] = r.get("Google Shopping / Age Group", "")
        out["Google Shopping / Manufacturer part number (MPN)"] = r.get(
            "Google Shopping / MPN", ""
        )
        out["Google Shopping / Ad group name"] = ""
        out["Google Shopping / Ads labels"] = ""
        out["Google Shopping / Condition"] = r.get("Google Shopping / Condition", "")
        out["Google Shopping / Custom product"] = r.get(
            "Google Shopping / Custom Product", ""
        )
        out["Google Shopping / Custom label 0"] = r.get(
            "Google Shopping / Custom Label 0", ""
        )
        out["Google Shopping / Custom label 1"] = r.get(
            "Google Shopping / Custom Label 1", ""
        )
        out["Google Shopping / Custom label 2"] = r.get(
            "Google Shopping / Custom Label 2", ""
        )
        out["Google Shopping / Custom label 3"] = r.get(
            "Google Shopping / Custom Label 3", ""
        )
        out["Google Shopping / Custom label 4"] = r.get(
            "Google Shopping / Custom Label 4", ""
        )
        for key in TEMPLATE_FIELDNAMES:
            out.setdefault(key, "")
        return out

    def process_csv(self, input_path: str):
        base_dir = os.path.dirname(os.path.abspath(input_path))
        base_name = os.path.basename(input_path)
        output_path = os.path.join(base_dir, f"NEW_{base_name}")

        # Runs on a worker thread: all Tk access goes through the UI queue.

        log = lambda msg: self._ui(self.log_message, msg)
        log(f"Output will be written to: {output_path}")
        with open(input_path, newline="", encoding="utf-8-sig") as infile:
            reader = list(csv.DictReader(infile))
        output_rows = []
        success = 0
        
        # Diagnostics remain in the GUI log; no extra review CSV is created.
        
        fail = 0
        handle_to_card = {}
        total = len(reader)
        self._ui(self._set_progress, maximum=total, value=0, mode="determinate")
        for idx, row in enumerate(reader, start=1):
            self._ui(self._set_progress, value=idx)
            vendor = row.get("Vendor", "")
            title = row.get("Title", "")
            body_html = row.get("Body (HTML)", "") or ""
            is_continuation_row = (vendor == "" and title == "" and body_html == "")

            # Shopify exports put each further variant of a product on its own
            # row with blank Title/Vendor/Body; such a row carries variant data
            # and is passed through unchanged. Only a continuation row with
            # image fields and no variant data is an extra image row.

            is_variant_row = is_continuation_row and any(
                (row.get(k) or "").strip() for k in VARIANT_FIELDS)
            is_image_only_row = (is_continuation_row and not is_variant_row and any(
                (row.get(k) or "").strip() for k in ("Image Src", "Image Position")))
            if is_continuation_row and not is_image_only_row:
                output_rows.append(self.build_template_row(row))
                continue
            if vendor != "Magic: The Gathering" and not is_image_only_row:
                if row.get("Image Src") and not (row.get("Image Alt Text") or "").strip():
                    row["Image Alt Text"] = extract_name(title) or title.strip()
                output_rows.append(self.build_template_row(row))
                continue
            handle = row.get("Handle", "")
            log(f"Processing: {title}")
            name_candidates = extract_name_candidates(title)
            raw_set = extract_set_code(title)
            set_code = normalize_set_code(raw_set)
            collector_number = normalize_collector_number(
                extract_collector_number(body_html)
            )
            scryfall_id_field = "Scryfall ID (product.metafields.custom.scryfall_id)"
            oracle_id = row.get(scryfall_id_field, "").strip() or None
            log(
                f"  DEBUG PRE-MATCH: name_candidates={name_candidates}, "
                f"raw_set={raw_set}, set_code={set_code}, "
                f"collector_number={collector_number}, oracle_id={oracle_id}"
            )
            match_method = ""
            conflicts = []
            if is_image_only_row:
                card = handle_to_card.get(handle)
                match_method = "prior image/handle" if card else "missing prior image/handle"
            else:
                finish = extract_requested_finish(title, row)
                card, match_method, conflicts = resolve_printing(
                    self.exact_id_index, self.oracle_id_index, self.printing_index,
                    name_candidates, set_code, collector=collector_number,
                    finish=finish, scryfall_id=oracle_id)
                if card:
                    handle_to_card[handle] = card
                    log(f"  MATCH: {match_method} | card_id={card.get('id')}")
                    if conflicts:
                        log("  WARNING: CSV conflicts with exact Scryfall printing: " + ", ".join(conflicts))
                else:
                    log("  MATCH UNRESOLVED: " + match_method)
            if not card:
                fail += 1
                log("  X MATCH FAILED")
                output_rows.append(self.build_template_row(row))
                continue
            png_urls = extract_png_urls(card)
            if not png_urls:
                fail += 1
                log("  X NO PNG FOUND")
                output_rows.append(self.build_template_row(row))
                continue
            if is_image_only_row and "card_faces" in card:
                row["Image Src"] = png_urls[1] if len(png_urls) > 1 else png_urls[0]
                row["Image Position"] = "2"
            else:
                row["Image Src"] = png_urls[0]
                row["Image Position"] = "1"

            # Preserve provided alt text. When absent, derive a useful card name
            # rather than leaving a populated image without accessibility text.
            
            if not (row.get("Image Alt Text") or "").strip():
                if is_image_only_row:
                    faces = card.get("card_faces") or []
                    alt_name = (faces[1].get("name", "") if len(faces) > 1 else "")
                    alt_name = alt_name or card.get("name", "")
                else:
                    alt_name = (name_candidates[0] if name_candidates else "") or card.get("name", "")
                row["Image Alt Text"] = alt_name.strip() or "Magic: The Gathering card"
            output_rows.append(self.build_template_row(row))
            success += 1
            
        # A row may contain keys not listed in the Shopify template. Explicitly
        # project it onto the columns we write, so csv.DictWriter cannot crash
        # after all matching work is complete. Keep the variant-image field.
        
        export_fields = list(TEMPLATE_FIELDNAMES)
        for required in ("Product image URL", "Image position", "Image alt text", "Variant image URL"):
            if required not in export_fields:
                export_fields.append(required)
        normalized_rows = [
            {column: row.get(column, "") for column in export_fields}
            for row in output_rows
        ]
        
        # Never leave behind a partially written Shopify import after an error.
        
        import tempfile
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", newline="", encoding="utf-8-sig", suffix=".csv",
                prefix=".scryfall_export_", dir=base_dir, delete=False
            ) as outfile:
                temp_path = outfile.name
                writer = csv.DictWriter(outfile, fieldnames=export_fields)
                writer.writeheader()
                writer.writerows(normalized_rows)
            os.replace(temp_path, output_path)
        except Exception:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
            raise
        self._ui(
            messagebox.showinfo,
            "Complete",
            f"Finished processing.\n\nSuccess: {success}\nFailed: {fail}\n\nSaved to:\n{output_path}",
        )
        log("Processing complete.")

# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

if __name__ == "__main__":
    app = ScryfallBulkGUI()
    app.mainloop()