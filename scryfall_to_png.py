#!/usr/bin/env python3
# Bulk-powered Scryfall → Shopify image resolver

import csv
import gzip
import json
import os
import re
import pickle
import requests
import tkinter as tk
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, ttk

BULK_META_URL = "https://api.scryfall.com/bulk-data"
BULK_TYPE = "all_cards"
BULK_FILENAME = "scryfall_all_cards.jsonl.gz"
USER_AGENT = {"User-Agent": "ShopifyScryfalltoPNG (example@example.com)"}
META_FILENAME = "bulk_metadata.json"
INDEX_FILENAME = "scryfall_all_cards_index_en.pkl"


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
    with open(path, "wb") as f:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                if progress_callback:
                    progress_callback(downloaded, compressed_size)

    if log_callback:
        log_callback("Download complete.")

    save_bulk_metadata({"updated_at": updated_at})


def load_bulk_data(path: str, log_callback=None):
    if log_callback:
        log_callback("Loading bulk data...")
    cards = []
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            cards.append(json.loads(line))
    if log_callback:
        log_callback(f"Loaded {len(cards)} cards.")
    return cards


def build_index(cards, log_callback=None):
    if log_callback:
        log_callback("Indexing bulk data...")

    # Primary indices
    triple = {}          # (name, set, collector) -> card
    double = {}          # (name, set) -> card
    single = {}          # name -> card

    oracle_triple = {}   # (oracle_id, set, collector) -> card
    oracle_double = {}   # (oracle_id, set) -> card
    oracle_single = {}   # oracle_id -> card

    # New: collector-first indices
    coll_set = {}        # (set, collector) -> card
    coll_only = {}       # collector -> list of cards (for rare cases)

    for c in cards:
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

        # Collector-first indices
        coll_set[(set_l, collector_l)] = c
        coll_only.setdefault(collector_l, []).append(c)

        # Name-based indices
        if name_l and set_l and collector_l:
            triple[(name_l, set_l, collector_l)] = c
        if name_l and set_l:
            double[(name_l, set_l)] = c
        single[name_l] = c

        # Printed name indices
        if printed_l:
            if printed_l and set_l and collector_l:
                triple[(printed_l, set_l, collector_l)] = c
            if printed_l and set_l:
                double[(printed_l, set_l)] = c
            single[printed_l] = c

        # Face names and printed face names
        if "card_faces" in c:
            for face in c["card_faces"]:
                fn = face.get("name", "")
                if fn:
                    fn_l = fn.lower()
                    if fn_l and set_l and collector_l:
                        triple[(fn_l, set_l, collector_l)] = c
                    if fn_l and set_l:
                        double[(fn_l, set_l)] = c
                    single[fn_l] = c

                pf = face.get("printed_name", "")
                if pf:
                    pf_l = pf.lower()
                    if pf_l and set_l and collector_l:
                        triple[(pf_l, set_l, collector_l)] = c
                    if pf_l and set_l:
                        double[(pf_l, set_l)] = c
                    single[pf_l] = c

        # Oracle-based indices
        if oracle_l and set_l and collector_l:
            oracle_triple[(oracle_l, set_l, collector_l)] = c
        if oracle_l and set_l:
            oracle_double[(oracle_l, set_l)] = c
        if oracle_l:
            oracle_single[oracle_l] = c

        # Scryfall ID indices (reuse oracle_* maps)
        if scry_l and set_l and collector_l:
            oracle_triple[(scry_l, set_l, collector_l)] = c
        if scry_l and set_l:
            oracle_double[(scry_l, set_l)] = c
        if scry_l:
            oracle_single[scry_l] = c

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

def save_index(
    triple,
    double,
    single,
    oracle_triple,
    oracle_double,
    oracle_single,
    coll_set,
    coll_only,
    meta,
    log_callback=None,
):
    payload = {
        "meta_updated_at": meta.get("updated_at") if meta else None,
        "triple": triple,
        "double": double,
        "single": single,
        "oracle_triple": oracle_triple,
        "oracle_double": oracle_double,
        "oracle_single": oracle_single,
        "coll_set": coll_set,
        "coll_only": coll_only,
    }
    with open(INDEX_FILENAME, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
    if log_callback:
        log_callback("Index saved.")


def load_index(meta, log_callback=None):
    if not os.path.exists(INDEX_FILENAME) or not meta:
        return None
    try:
        with open(INDEX_FILENAME, "rb") as f:
            payload = pickle.load(f)
    except Exception:
        return None
    if payload.get("meta_updated_at") != meta.get("updated_at"):
        if log_callback:
            log_callback("Cached index stale.")
        return None
    if log_callback:
        log_callback("Loaded cached index.")
    return (
        payload["triple"],
        payload["double"],
        payload["single"],
        payload["oracle_triple"],
        payload["oracle_double"],
        payload["oracle_single"],
        payload["coll_set"],
        payload["coll_only"],
    )


def extract_name(title: str) -> str:
    base = title.split(" - ")[0].strip()
    while True:
        m = re.search(r"\s*\(([^()]*)\)\s*$", base)
        if not m:
            break
        base = base[:m.start()].strip()
    return base


def extract_name_candidates(title: str) -> list[str]:
    parts = title.split(" - ")
    if len(parts) == 3:
        mid = parts[1].strip()
        while True:
            m = re.search(r"\s*\(([^()]*)\)\s*$", mid)
            if not m:
                break
            mid = mid[:m.start()].strip()
        return [mid]
    return [extract_name(title)]


def extract_set_code(title: str) -> str | None:
    m = re.search(r"\(([^()]*)\)\s*$", title)
    return m.group(1).strip().lower() if m else None


def normalize_set_code(code: str | None) -> str | None:
    if not code:
        return None
    return {"list": "plist"}.get(code, code)


def extract_collector_number(body_html: str) -> str | None:
    m = re.search(r"<strong>\s*#:\s*</strong>\s*([0-9A-Za-z\-\/]+)", body_html, flags=re.IGNORECASE)
    return m.group(1).strip() if m else None


def normalize_collector_number(num: str | None) -> str | None:
    if not num:
        return None
    return num.lstrip("0")


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

    # 1. (oracle_id, set, collector)
    if oracle_l and set_l and collector_l:
        card = oracle_triple.get((oracle_l, set_l, collector_l))
        if card:
            return card

    # 2. (set, collector)
    if set_l and collector_l:
        card = coll_set.get((set_l, collector_l))
        if card:
            return card

    # 3. (name, set, collector)
    if name_l and set_l and collector_l:
        card = triple.get((name_l, set_l, collector_l))
        if card:
            return card

    # 4. (oracle_id, set)
    if oracle_l and set_l:
        card = oracle_double.get((oracle_l, set_l))
        if card:
            return card

    # 5. (name, set)
    if name_l and set_l:
        card = double.get((name_l, set_l))
        if card:
            return card

    # 6. (oracle_id)
    if oracle_l:
        card = oracle_single.get(oracle_l)
        if card:
            return card

    # 7. (name)
    if name_l:
        card = single.get(name_l)
        if card:
            return card

    # 8. Prefix match on name
    if name_l:
        for key in single.keys():
            if key.startswith(name_l):
                return single[key]

    return None


def prefer_language(card, name_candidates, single_index, collector_number):
    english = all(re.match(r"[A-Za-z0-9\s'!?,.-]+$", nc) for nc in name_candidates)
    oracle_id = card.get("oracle_id")
    collector_l = collector_number

    def same_coll(c):
        return c.get("collector_number") == collector_l

    if english:
        if oracle_id and collector_l:
            for c2 in single_index.values():
                if (
                    c2.get("oracle_id") == oracle_id
                    and c2.get("lang") == "en"
                    and same_coll(c2)
                ):
                    return c2
        return card

    printed_target = name_candidates[0].lower()

    if oracle_id and collector_l:
        for c2 in single_index.values():
            if c2.get("oracle_id") == oracle_id and same_coll(c2):
                pn = c2.get("printed_name", "")
                if pn and pn.lower() == printed_target:
                    return c2

    return card


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


class ScryfallBulkGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Bulk-Powered Scryfall Image Extractor")
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

        self.create_widgets()

    def create_widgets(self):
        self.load_btn = tk.Button(self, text="Load / Update Scryfall Bulk", command=self.load_bulk)
        self.load_btn.pack(pady=10)

        self.select_btn = tk.Button(self, text="Select Shopify CSV", command=self.select_file, state="disabled")
        self.select_btn.pack(pady=10)

        self.progress = ttk.Progressbar(self, orient="horizontal", length=700, mode="determinate")
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
        try:
            meta = load_bulk_metadata()
            self.log_message("Checking bulk file cache...")

            if not bulk_file_is_fresh(BULK_FILENAME, meta):
                self.log_message("Bulk file missing or stale. Downloading...")
                self.progress["value"] = 0
                download_bulk_file(
                    BULK_FILENAME,
                    progress_callback=self.update_download_progress,
                    log_callback=self.log_message,
                )
                meta = load_bulk_metadata()
            else:
                self.log_message("Bulk file is fresh.")

            idx = load_index(meta, log_callback=self.log_message)
            if idx:
                (
                    self.triple,
                    self.double,
                    self.single,
                    self.oracle_triple,
                    self.oracle_double,
                    self.oracle_single,
                    self.coll_set,
                    self.coll_only,
                ) = idx
                self.log_message("Index ready (cached).")
                self.select_btn.config(state="normal")
                return

            self.progress["value"] = 0
            self.progress["mode"] = "indeterminate"
            self.progress.start(10)
            self.update_idletasks()

            cards = load_bulk_data(BULK_FILENAME, log_callback=self.log_message)
            (
                self.triple,
                self.double,
                self.single,
                self.oracle_triple,
                self.oracle_double,
                self.oracle_single,
                self.coll_set,
                self.coll_only,
            ) = build_index(cards, log_callback=self.log_message)

            save_index(
                self.triple,
                self.double,
                self.single,
                self.oracle_triple,
                self.oracle_double,
                self.oracle_single,
                self.coll_set,
                self.coll_only,
                meta,
                log_callback=self.log_message,
            )

            self.progress.stop()
            self.progress["mode"] = "determinate"
            self.progress["value"] = 0

            self.log_message("Bulk data loaded and indexed.")
            self.select_btn.config(state="normal")

        except Exception as e:
            self.progress.stop()
            self.progress["mode"] = "determinate"
            self.progress["value"] = 0
            messagebox.showerror("Bulk Load Error", str(e))

    def select_file(self):
        file_path = filedialog.askopenfilename(
            title="Select Shopify CSV",
            filetypes=[("CSV Files", "*.csv")],
        )
        if not file_path:
            messagebox.showerror("Error", "No file selected.")
            return
        self.process_csv(file_path)

    def process_csv(self, input_path: str):
        base_dir = os.path.dirname(os.path.abspath(input_path))
        base_name = os.path.basename(input_path)
        output_path = os.path.join(base_dir, f"NEW_{base_name}")

        self.log_message(f"Output will be written to: {output_path}")

        with open(input_path, newline="", encoding="utf-8") as infile:
            reader = list(csv.DictReader(infile))

        fieldnames = list(reader[0].keys())
        output_rows = []
        success = 0
        fail = 0

        handle_to_card = {}

        total = len(reader)
        self.progress["maximum"] = total
        self.progress["value"] = 0
        self.progress["mode"] = "determinate"

        for idx, row in enumerate(reader, start=1):
            self.progress["value"] = idx
            self.update_idletasks()

            vendor = row.get("Vendor", "")
            title = row.get("Title", "")
            body_html = row.get("Body (HTML)", "") or ""

            is_image_only_row = (vendor == "" and title == "" and body_html == "")

            if vendor != "Magic: The Gathering" and not is_image_only_row:
                output_rows.append(row)
                continue

            handle = row.get("Handle", "")
            self.log_message(f"Processing: {title}")

            name_candidates = extract_name_candidates(title)
            raw_set = extract_set_code(title)
            set_code = normalize_set_code(raw_set)
            collector_number = normalize_collector_number(extract_collector_number(body_html))

            scryfall_id_field = "Scryfall ID (product.metafields.custom.scryfall_id)"
            oracle_id = row.get(scryfall_id_field, "").strip() or None

            self.log_message(
                f"  DEBUG PRE-MATCH: name_candidates={name_candidates}, "
                f"raw_set={raw_set}, set_code={set_code}, "
                f"collector_number={collector_number}, oracle_id={oracle_id}"
            )

            if is_image_only_row:
                card = handle_to_card.get(handle)
            else:
                card = None
                for cand in name_candidates:
                    card = match_card_collector_first(
                        self.triple,
                        self.double,
                        self.single,
                        self.oracle_triple,
                        self.oracle_double,
                        self.oracle_single,
                        self.coll_set,
                        self.coll_only,
                        cand,
                        set_code,
                        collector_number,
                        oracle_id=oracle_id,
                    )
                    if card:
                        break
                if card:
                    card = prefer_language(card, name_candidates, self.single, collector_number)
                    handle_to_card[handle] = card

            if not card:
                fail += 1
                self.log_message("  ✘ MATCH FAILED")
                output_rows.append(row)
                continue

            png_urls = extract_png_urls(card)
            if not png_urls:
                fail += 1
                self.log_message("  ✘ NO PNG FOUND")
                output_rows.append(row)
                continue

            if is_image_only_row and "card_faces" in card:
                row["Image Src"] = png_urls[1] if len(png_urls) > 1 else png_urls[0]
                row["Image Position"] = "2"
            else:
                row["Image Src"] = png_urls[0]
                row["Image Position"] = "1"

            output_rows.append(row)
            success += 1

        with open(output_path, "w", newline="", encoding="utf-8") as outfile:
            writer = csv.DictWriter(outfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(output_rows)

        messagebox.showinfo(
            "Complete",
            f"Finished processing.\n\nSuccess: {success}\nFailed: {fail}\n\nSaved to:\n{output_path}",
        )

        self.log_message("Processing complete.")


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

if __name__ == "__main__":
    app = ScryfallBulkGUI()
    app.mainloop()
