# Scryfall to PNG

A desktop tool that fills Magic: The Gathering product image URLs in a Shopify product export with official Scryfall PNG card images.

It matches each product to its exact Scryfall printing using a local copy of Scryfall's bulk data, writes the PNG image URL into the image column, and saves a new CSV in Shopify's product import template layout.

The tool doesn't download image files. It writes Scryfall PNG image URLs into the CSV, and Shopify fetches the images when you import it.

It is for Shopify store owners who list Magic singles and want official card images without uploading them by hand.

## Quick start

```
python3 -m pip install requests      # Windows: py -m pip install requests
python3 scryfall_to_png.py           # Windows: py scryfall_to_png.py
```

In the window (titled "Scryfall Image Extractor"), click **Load / Update Scryfall Bulk**, wait for "Index ready" in the log, then click **Select Shopify CSV** and pick your product export. The result is saved next to it as `NEW_<export name>`.

## Features

- Downloads Scryfall's **All Cards** bulk data once and caches it. The cache is refreshed when it is more than 24 hours old and Scryfall has published a newer file.
- Builds a lookup index and caches it to disk, so later launches start quickly.
- Matches each card by:
  1. Scryfall printing ID, when the CSV's Scryfall ID column has one. This match takes priority.
  2. Oracle ID (same column), narrowed down by set, collector number, finish and name to exactly one printing, preferring the English printing when other languages also match.
  3. Without an ID: name + set code, plus collector number and finish when they are present. The set code is required.
- Leaves a product unmatched instead of guessing when the match is ambiguous. It never swaps in a printing from a different set.
- Prefers English printings when there's more than one choice.
- Handles double-faced cards. The front face goes on the product row. The back face goes on an image-only row that has the same Handle.
- Fills in `Image Alt Text` when it's blank.
- Passes non-Magic products through without matching. Their columns are remapped to the template, and a blank `Image Alt Text` is filled from the title when the row has an image.
- Writes the output CSV in one step at the end, so a failed run never leaves a half-written file.

## Requirements

Running from source needs the following; the prebuilt app needs only internet access and disk space.

- Python 3.10 or newer, with Tkinter (included with the python.org installers; on Linux install `python3-tk`, with Homebrew Python on macOS `brew install python-tk`)
- [requests](https://pypi.org/project/requests/)
- Internet access the first time you load the bulk data, and for later refreshes
- About 600 MB of free disk space for the cached bulk file and index, and about 1.5 GB of free memory while loading

```
# Windows
py -m pip install requests

# macOS / Linux
python3 -m pip install requests
```

## How to run

### Prebuilt app (macOS, Apple Silicon)

1. Unzip `Scryfall to PNG.app.zip`.
2. Open `Scryfall to PNG.app`. The app isn't notarized, so macOS may block it the first time: right-click it and choose **Open**, or click **Open Anyway** in System Settings > Privacy & Security.

The prebuilt app is for Apple Silicon (arm64) Macs only. On Windows, Intel Macs or Linux, run from source.

### From source

```
# Windows
py scryfall_to_png.py

# macOS / Linux
python3 scryfall_to_png.py
```

### Workflow

1. Click **Load / Update Scryfall Bulk**. The first run downloads the bulk file and builds the index, which can take several minutes. Later runs reuse the cache. The log shows each step.
2. When loading finishes, **Select Shopify CSV** becomes clickable. Click it and choose your Shopify product export.
3. The app processes each row and logs the match result. When it's done, it shows how many rows succeeded and failed, and where the file was saved.
4. Import the new CSV into Shopify.

## Input

A Shopify **product export** CSV, encoded as UTF-8 (with or without a byte-order mark). These columns are used:

| Column | Used for |
|---|---|
| `Vendor` | Only rows with exactly `Magic: The Gathering` are matched. Other rows pass through without matching. |
| `Title` | Card name and set code. See the title format below. |
| `Body (HTML)` | Collector number, read from `<strong>#:</strong> 123` |
| `Handle` | Links image-only rows (such as the back faces of double-faced cards) to their product. Also copied to `URL handle`. |
| `Scryfall ID (product.metafields.custom.scryfall_id)` | Optional. A Scryfall printing ID or Oracle ID. Gives the most precise match. |
| `Option1 Value`, `Option2 Value`, `Option3 Value`, `Tags` | Finish detection, together with the title |
| `Image Src`, `Image Position`, `Image Alt Text` | Overwritten or filled in with the matched image |

**Title format.**

- The set code goes in parentheses at the end of the title, for example `Lightning Bolt (M10)`.
- The card name is the text before the first ` - `. If the title has exactly two ` - ` separators, the middle part is used as the name instead.
- Trailing parenthetical labels are stripped from the name.
- Two set codes are mapped to Scryfall's codes:
  - `FNM` or `Friday Night Magic` becomes `fnm`
  - `List` becomes `plst`

**Finish.**

- `foil` or `foiled` means foil.
- `non-foil`, `nonfoil`, `not foil` or `regular finish` means nonfoil.
- If neither appears, the finish isn't used to filter matches.

**Variant rows.** A row with blank Vendor, Title and Body (HTML) that has variant data (`Option1`-`Option3 Value`, `Variant SKU`, `Variant Price` or `Variant Barcode`) is another variant of the product above it, as in Shopify's export. It is copied to the output unchanged and isn't counted as a success or failure.

**Image-only rows.** A row with blank Vendor, Title and Body (HTML), no variant data, and `Image Src` or `Image Position` filled in is treated as an extra image row for a product earlier in the file with the same `Handle`. For a double-faced card, it gets the back-face PNG and `Image Position` 2. For any other card, it gets the front PNG and `Image Position` 1.

## Output

The output is a new CSV named `NEW_` plus the input file name (for example `products_export.csv` becomes `NEW_products_export.csv`), saved in the same folder as the input. If that file already exists, it's overwritten. The file is UTF-8 with a BOM.

The columns follow Shopify's product import template, in this order:

- **Product:** `Title`, `URL handle`, `Description`, `Vendor`, `Product category`, `Type`, `Tags`, `Published on online store`, `Status`
- **Variant and options:** `SKU`, `Barcode`, `Option1`-`Option3` name / value / Linked To
- **Pricing and tax:** `Price`, `Compare-at price`, `Cost per item`, `Charge tax`, `Tax code`, the unit price measure fields
- **Inventory and shipping:** `Inventory tracker`, `Inventory quantity`, `Continue selling when out of stock`, `Weight value (grams)`, `Weight unit for display`, `Requires shipping`, `Fulfillment service`
- **Images:** `Product image URL`, `Image position`, `Image alt text`, `Variant image URL`
- **Other:** `Gift card`, `SEO title`, `SEO description`, `Color (product.metafields.shopify.color-pattern)`
- **Google Shopping:** the `Google Shopping / ...` fields, including Custom label 0-4

Values for most columns are copied from the matching export column. For example, `Handle` becomes `URL handle` and `Variant Price` becomes `Price`. Exceptions:

- For each matched card:
  - `Product image URL` is set to the Scryfall PNG URL.
  - `Image position` is set to 1 for the front face, or 2 for the back face of a double-faced card.
- `Google Shopping / Ad group name` and `Google Shopping / Ads labels` are always blank.
- `Barcode` is copied from `Variant Barcode`.
- `Inventory quantity` is copied from `Variant Inventory Qty`.
- `Continue selling when out of stock` is always set to `deny`.

Rows that don't match are still written to the output with their original image data. The reason they failed is shown in the GUI log. No separate error report is created. The success and failure counts in the final message cover Magic and image-only rows only; non-Magic rows and variant rows aren't counted.

## Settings, config and cache

There's no settings file. The app stores its cache in a per-user data folder:

| Platform | Location |
|---|---|
| macOS | `~/Library/Application Support/ScryfallToPNG/` |
| Windows | `%LOCALAPPDATA%\ScryfallToPNG\` |
| Linux | `$XDG_DATA_HOME/ScryfallToPNG/` or `~/.local/share/ScryfallToPNG/` |

| File | Purpose |
|---|---|
| `scryfall_all_cards.jsonl.gz` | Scryfall All Cards bulk data |
| `bulk_metadata.json` | `updated_at` timestamp of the cached bulk file |
| `scryfall_all_cards_index_v2.pkl` | Cached lookup index (only the card fields the matcher uses, about 140 MB), rebuilt when the bulk data changes. An older `scryfall_all_cards_index_en.pkl` (about 3 GB) is deleted on the next load. |

**When the cache refreshes.** When the bulk file is more than 24 hours old, the app checks Scryfall's metadata and downloads a new file only if Scryfall has published a newer one. If Scryfall can't be reached, the app uses the cached file.

If the bulk file exists but `bulk_metadata.json` is missing (for example, after copying in an old cache), the app marks the local file as `local-existing-file` and reuses it for that load instead of downloading straight away. From the next load more than 24 hours later, it checks Scryfall and downloads a current file (or keeps the local one if Scryfall can't be reached). To get current data immediately, delete `scryfall_all_cards.jsonl.gz`.

**Older cache files.** When run from source, each load copies any of these three files that it finds next to the script into the data folder, if that file isn't already there.

**Forcing a full re-download.** Delete `scryfall_all_cards.jsonl.gz`, or the whole `ScryfallToPNG` folder.

## Building the app with PyInstaller

The prebuilt app is a windowed PyInstaller `.app` bundle for arm64 with PyInstaller's default icon and bundle identifier and no extra data files. It bundles its own Python (3.14). The bulk data isn't bundled; it's downloaded at runtime. The original spec file isn't in this repository. This command builds an equivalent app:

```
# macOS
python3 -m pip install pyinstaller requests
python3 -m PyInstaller --windowed --name "Scryfall to PNG" scryfall_to_png.py

# Windows
py -m pip install pyinstaller requests
py -m PyInstaller --windowed --name "Scryfall to PNG" scryfall_to_png.py
```

If you have the original `Scryfall to PNG.spec`, put it next to `scryfall_to_png.py` and run `pyinstaller "Scryfall to PNG.spec"`.

The output goes to `dist/`. PyInstaller doesn't cross-compile, so build on the platform you're targeting.

## Troubleshooting

| Problem | Fix |
|---|---|
| **Select Shopify CSV** is greyed out | Click **Load / Update Scryfall Bulk** first and wait for "Index ready" or "Bulk data loaded and indexed" in the log. |
| "Bulk Load Error" or a network error on first run | The first run needs internet access to `api.scryfall.com` and Scryfall's bulk-data download host. Check your connection or firewall, then try again. |
| "Incomplete bulk download" | The download was interrupted. Click **Load / Update Scryfall Bulk** again. |
| A loading or indexing error after a crash or full disk | Delete the `ScryfallToPNG` data folder and load again. |
| "MATCH UNRESOLVED: no unambiguous compatible printing" | Check that the title ends with the right set code in parentheses, and that `Body (HTML)` has the collector number. Adding the Scryfall ID metafield gives a precise match. |
| "Scryfall ID absent from local bulk index" | The card is newer than your cached bulk data, or the ID is wrong. Delete `scryfall_all_cards.jsonl.gz` from the data folder and load again to download current data. (Deleting only `bulk_metadata.json` is not enough: the app then keeps the old file for another 24 hours before checking for updates.) |
| "oracle ID printing ambiguous or incompatible" | An Oracle ID covers many printings. Use the printing ID, or make sure the set, collector number and finish identify exactly one printing. |
| "missing prior image/handle" | An image-only row's Handle doesn't match a product row that matched earlier in the file. |
| "Processing Error" after choosing the CSV | Processing stopped on an error; the message is shown in the dialog and the log. The usual cause is encoding: the file must be UTF-8. |
| macOS says the app is damaged or from an unidentified developer | The app isn't notarized. Right-click it and choose **Open**, or allow it in System Settings > Privacy & Security. |

## File guide

| File | What it is |
|---|---|
| `scryfall_to_png.py` | Full source: GUI, bulk download and cache, indexing, matching, CSV export |
| `Scryfall to PNG.app.zip` | Prebuilt macOS app bundle, zipped |
| `.gitattributes` | Git line-ending settings |
| `README.md` | This document |
