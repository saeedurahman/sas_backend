# Open Food Facts family - Pakistani product coverage probe

Generated: 2026-11-20 09:38 UTC  
User-Agent: `PakPOS-catalog-probe/1.0 (contact: set-PROBE_CONTACT@example.invalid)`  
HTTP requests made: 97 (<= 1/sec, backoff on 429/503)  
Run time: 309s

## License / attribution

- Open Food Facts / Open Beauty Facts / Open Products Facts **database**: Open Database License (**ODbL 1.0**) - attribution + share-alike for derived databases.
- Individual database **contents** (data): Database Contents License (DbCL).
- Product **images**: **CC BY-SA 3.0** (some are CC BY-SA 4.0) - attribute and share alike; do not assume they can be bundled into a proprietary catalog without compliance.
- Required credit if used: "Data from Open Food Facts / Open Beauty Facts / Open Products Facts (openfoodfacts.org), ODbL; images CC BY-SA."
- Implication for PakPOS: a read-only *lookup fallback* (query at runtime, show the source) is the lowest-risk use. Copying the data into our own DB makes it a derived database subject to share-alike on that dataset.

## Totals per database

| DB | v2 country=Pakistan (reported count) | fetched | complete? | 896-prefix (in fetched set) | 896-prefix via Search-a-licious (food only) |
|---|---:|---:|:--:|---:|---:|
| Open Food Facts | 1959 | 999 | NO (capped/errors) | 424 | 1171 (fetched 1171) |
| Open Beauty Facts | 194 | 194 | yes | 45 | n/a |
| Open Products Facts | 47 | 47 | yes | 13 | n/a |

## Field completeness

### Open Food Facts

**A. countries_tags = pakistan** - 999 items

| Field | Count | % |
|---|---:|---:|
| product_name | 705 | 70.6% |
| brand | 505 | 50.6% |
| quantity / pack size | 418 | 41.8% |
| categories | 399 | 39.9% |
| >=1 image | 751 | 75.2% |

**B. barcode starts with 896 (union of country-set 896 + prefix query)** - 1211 items

| Field | Count | % |
|---|---:|---:|
| product_name | 1001 | 82.7% |
| brand | 529 | 43.7% |
| quantity / pack size | 301 | 24.9% |
| categories | 311 | 25.7% |
| >=1 image | 919 | 75.9% |

**A ∪ B (distinct)** - 1786 items

| Field | Count | % |
|---|---:|---:|
| product_name | 1389 | 77.8% |
| brand | 797 | 44.6% |
| quantity / pack size | 543 | 30.4% |
| categories | 513 | 28.7% |
| >=1 image | 1348 | 75.5% |

### Open Beauty Facts

**A. countries_tags = pakistan** - 194 items

| Field | Count | % |
|---|---:|---:|
| product_name | 123 | 63.4% |
| brand | 70 | 36.1% |
| quantity / pack size | 85 | 43.8% |
| categories | 82 | 42.3% |
| >=1 image | 167 | 86.1% |

**B. barcode starts with 896 (union of country-set 896 + prefix query)** - 45 items

| Field | Count | % |
|---|---:|---:|
| product_name | 28 | 62.2% |
| brand | 22 | 48.9% |
| quantity / pack size | 23 | 51.1% |
| categories | 26 | 57.8% |
| >=1 image | 39 | 86.7% |

**A ∪ B (distinct)** - 194 items

| Field | Count | % |
|---|---:|---:|
| product_name | 123 | 63.4% |
| brand | 70 | 36.1% |
| quantity / pack size | 85 | 43.8% |
| categories | 82 | 42.3% |
| >=1 image | 167 | 86.1% |

### Open Products Facts

**A. countries_tags = pakistan** - 47 items

| Field | Count | % |
|---|---:|---:|
| product_name | 26 | 55.3% |
| brand | 16 | 34.0% |
| quantity / pack size | 16 | 34.0% |
| categories | 16 | 34.0% |
| >=1 image | 39 | 83.0% |

**B. barcode starts with 896 (union of country-set 896 + prefix query)** - 13 items

| Field | Count | % |
|---|---:|---:|
| product_name | 7 | 53.8% |
| brand | 5 | 38.5% |
| quantity / pack size | 5 | 38.5% |
| categories | 5 | 38.5% |
| >=1 image | 11 | 84.6% |

**A ∪ B (distinct)** - 47 items

| Field | Count | % |
|---|---:|---:|
| product_name | 26 | 55.3% |
| brand | 16 | 34.0% |
| quantity / pack size | 16 | 34.0% |
| categories | 16 | 34.0% |
| >=1 image | 39 | 83.0% |

### All databases combined (distinct barcodes, A ∪ B)

**combined** - 2006 items

| Field | Count | % |
|---|---:|---:|
| product_name | 1523 | 75.9% |
| brand | 871 | 43.4% |
| quantity / pack size | 636 | 31.7% |
| categories | 596 | 29.7% |
| >=1 image | 1535 | 76.5% |

## Top 30 brands (combined A ∪ B)

| # | Brand | Items |
|--:|---|--:|
| 1 | nestlé | 33 |
| 2 | knorr | 27 |
| 3 | matco foods | 27 |
| 4 | lay's | 26 |
| 5 | lu | 23 |
| 6 | sabroso | 17 |
| 7 | sabirs | 16 |
| 8 | big bird | 14 |
| 9 | dawn foods | 13 |
| 10 | peak freans | 11 |
| 11 | national | 10 |
| 12 | peek freans | 10 |
| 13 | bisconni | 10 |
| 14 | cadbury | 8 |
| 15 | coca-cola | 7 |
| 16 | pronuben baby | 7 |
| 17 | hilal | 7 |
| 18 | nestle | 7 |
| 19 | ciranda | 7 |
| 20 | k&ns | 7 |
| 21 | indomie | 6 |
| 22 | candyland | 5 |
| 23 | pepsi | 5 |
| 24 | cutees | 5 |
| 25 | mitchell's | 5 |
| 26 | carrefour | 5 |
| 27 | beaubliss | 5 |
| 28 | dawn | 4 |
| 29 | frito lay | 4 |
| 30 | lipton | 4 |

## Top 20 categories (combined A ∪ B)

| # | Category tag | Items |
|--:|---|--:|
| 1 | snacks | 157 |
| 2 | plant-based-foods-and-beverages | 155 |
| 3 | plant-based-foods | 137 |
| 4 | sweet-snacks | 97 |
| 5 | beverages-and-beverages-preparations | 73 |
| 6 | beverages | 71 |
| 7 | cereals-and-potatoes | 64 |
| 8 | salty-snacks | 53 |
| 9 | condiments | 49 |
| 10 | biscuits-and-cakes | 48 |
| 11 | appetizers | 48 |
| 12 | non-food-products | 48 |
| 13 | chips-and-fries | 45 |
| 14 | crisps | 45 |
| 15 | incorrect-product-type | 42 |
| 16 | biscuits | 41 |
| 17 | open-beauty-facts | 41 |
| 18 | meats-and-their-products | 40 |
| 19 | meats | 40 |
| 20 | biscuits-and-crackers | 39 |

<details><summary>Open Beauty Facts: top brands / categories</summary>

Brands: beaubliss (5); tibet (3); nivea (3); dove (3); rivaj (2); ponds (2); revlon (2); l'oréal (2); colgate (2); vince (1); bonus (1); saeed ghani (1); derma (1); bb cream (1); armaghan (1); creme of nature (1); saniderm (1); unilever (1); perfect skin lady (1); healthy care (1); johnson & johnson (1); johnson’s (1); vaseline (1); lotion (1); wokali professional (1); new clear men (1); golden pearl (1); maybelline new york (1); dettol (1); pond's (1)

Categories: open-beauty-facts (46); non-food-products (45); incorrect-product-type (40); Cosmetic products (27); hair (10); shampoos (8); hygiene (7); soaps (5); Face serums (2); suncare (2); in-sun-protections (2); sunscreen (2); Face moisturizers (2); Cosmetics (2); body (2); hand-creams (2); toothpastes (2); accessories (2); Medicine (2); plant-based-foods (2)

</details>

<details><summary>Open Products Facts: top brands / categories</summary>

Brands: al prince vibes (2); gourmet (1); jk (1); power full (1); haleeb (1); wells (1); leaf (1); pall mall (1); dollar (1); nutrifactor (1); velo (1); dunhill (1); brand (1); zak designs (1); master (1)

Categories: tobacco-products (5); incorrect-product-type (3); non-food-products (3); open-products-facts (3); cigarettes (3); Tobacco (2); school (1); Processed potato chips (1); Energy Drink (1); Dairy (1); Tea (1); تبغ (1); Supplements (1); Stain removers (1); Category (1); home-garden (1); kitchen-dining (1); food-beverage-carriers (1); water-bottles (1); 78-72-5 (1)

</details>

## Lookup path test: /api/v2/product/{barcode}

Sampled 40 random 896-prefix barcodes (seed 1).

- found: **38/40** (95.0%)
- with a product_name: 29/40
- 503 responses encountered (incl. retried): **0**, 429: 17
- failed after retries: 0
- response time: min 0.52s / median 0.59s / p90 2.27s / max 3.17s

| Barcode | DB | HTTP | found | time (s) | attempts | 503s | name |
|---|---|--:|:--:|--:|--:|--:|---|
| 8961014261073 | off | 404 | no | 1.52 | 1 | 0 |  |
| 896514789963244 | off | 200 | yes | 1.55 | 1 | 0 |  |
| 8961008209548 | off | 200 | yes | 2.96 | 1 | 0 | everyday milk powder |
| 8964000039830 | opf | 200 | yes | 0.54 | 1 | 0 | Safoon |
| 8961014033847 | off | 200 | yes | 0.81 | 1 | 0 | Chatt Patta instant noodles |
| 8964002347599 | off | 200 | yes | 0.55 | 1 | 0 | Wavy |
| 8964001300120 | off | 200 | yes | 0.55 | 1 | 0 | Donut |
| 8964001753667 | off | 200 | yes | 0.54 | 1 | 0 | Golden Pearl |
| 8964000749906 | obf | 200 | yes | 1.51 | 1 | 0 |  |
| 8961102515040 | off | 200 | yes | 0.58 | 1 | 0 | Thunder Jelly |
| 8961008300450 | off | 200 | yes | 1.93 | 1 | 0 | everyday Kashmiri tea |
| 8964002346806 | off | 200 | yes | 1.54 | 1 | 0 | Texas BBQ |
| 8961003020216 | off | 200 | yes | 0.55 | 1 | 0 |  |
| 8964000834015 | off | 200 | yes | 0.53 | 1 | 0 |  |
| 8964001166856 | off | 200 | yes | 0.53 | 1 | 0 | swad chat patta |
| 89686010831 | off | 200 | yes | 0.52 | 1 | 0 | Mi goreng Fried Noodles |
| 89600078 | off | 200 | yes | 0.86 | 1 | 0 |  |
| 8964001248774 | off | 200 | yes | 0.54 | 1 | 0 | Giant sour chew |
| 8964000062302 | off | 200 | yes | 3.17 | 1 | 0 | Chocolate Bliss Hazelnut |
| 8963442326263 | off | 200 | yes | 0.56 | 2 | 0 | Grapefruit |
| 8966000075610 | obf | 200 | yes | 0.56 | 1 | 0 | Niacinamide Serum |
| 8961014018196 | off | 200 | yes | 1.25 | 1 | 0 | Custard |
| 8964000177433 | off | 200 | yes | 0.59 | 2 | 0 | Chicken Spring Roll |
| 8961003021794 | off | 200 | yes | 0.57 | 2 | 0 | Bakeri Nankhatai |
| 8961003010217 | off | 200 | yes | 0.63 | 2 | 0 | Lu Tuc Cookies 84 GM |
| 8961003011771 | off | 200 | yes | 0.52 | 2 | 0 |  |
| 8964002881024 | off | 200 | yes | 0.55 | 2 | 0 |  |
| 8960160000185 | off | 404 | no | 0.53 | 2 | 0 |  |
| 8964000760208 | off | 200 | yes | 0.73 | 2 | 0 | Nimko |
| 8961700412314 | off | 200 | yes | 0.87 | 2 | 0 | Peques 3 con cacao y cereales |
| 8964001078685 | obf | 200 | yes | 0.59 | 1 | 0 | face wash |
| 8961003020315 | off | 200 | yes | 2.27 | 2 | 0 | Candi Original |
| 8964002755981 | off | 200 | yes | 0.52 | 2 | 0 | Gluco |
| 8962669061360 | off | 200 | yes | 0.56 | 2 | 0 | Frutos secos |
| 8964001242307 | off | 200 | yes | 0.87 | 2 | 0 | Magic Chicken Nuggers |
| 8964002347636 | off | 200 | yes | 2.98 | 2 | 0 |  |
| 8964003590574 | off | 200 | yes | 2.82 | 1 | 0 | Cake Up Double Chocolate |
| 8963755300660 | off | 200 | yes | 0.78 | 2 | 0 |  |
| 8964000577370 | off | 200 | yes | 1.55 | 2 | 0 | Slim+ Herbal Tea |
| 8963471480080 | off | 200 | yes | 0.52 | 2 | 0 | Jus de pomme |

## 20 sample rows

| Barcode | Name | Brand | Quantity | Categories | Image | DB | Match |
|---|---|---|---|---|:--:|---|---|
| 5292824791638 | Qourma Masala | Lazzat Bhara |  | Qourma Masala from Lazzat Bhara | yes | off | country |
| 8964001076056 | Stillman's Fairness Cream | ESTD 1891 | 28 gms |  | yes | off | country |
| 08160130 | bigglychee | Murre brewery | 12 | Dairy desserts, Quark with 30% fat in dry mass | yes | off | country |
| 8961003544811 | Zeera Plus |  |  | Biscuits | yes | off | country |
| 4903333172955 | Ghana Milk Chocolate | Lotte | 50 g | Milk chocolates, Cocoa powders | yes | off | country |
| 8964000346334 | Olper's Full Cream Milk | Olper's | 1.5 l | Whole milks | yes | off | country |
| 8964000090343 | lays Masal | Lay's | best | Potato crisps | yes | off | country |
| 8964000122075 | Hamdard Rooh Afza | Hamdard |  | beverages, unsweetened-beverages | yes | off | prefix896 |
| 8964002755974 | Gluco | Gluco, Peak Freans |  | Biscuits | yes | off | country |
| 8961101784508 | Tulsi | Tulsi |  |  | yes | off | prefix896 |
| 8809418750604 | elizavecca Hayluronic acid | elizavecca milky piggy Hayluronic acid |  |  | yes | off | country |
| 3111111500970 | Whole grain gliten free flower |  |  |  | yes | off | country |
| 8964000184295 | Jus de mangue |  |  |  | yes | off | prefix896 |
| 0620514006130 | National tomato ketchup | National | 250 g | Ketchup, Groceries | yes | off | country |
| 8961102430459 | مهران صلصة البيتزا |  |  |  | yes | off | prefix896 |
| 8964000039830 | Safoon |  | 475ml |  | yes | opf | country |
| 8964001541325 | Paratha | Sabirs, Sabroso | 40g, 5 Pieces | Flatbreads | yes | off | country |
| 0012000800085 | Pepsi | Cola | 400 | Colas, Sweetened beverages | yes | off | country |
| 8966000028029 | Irganic Brown Long Grain Rice |  |  | plant-based-foods-and-beverages, plant-based-foods | yes | off | prefix896 |
| 8964000074503 | Adam's Yoghurt 200g |  |  | Yogurts | yes | off | country |

## Errors / retries

HTTP status counts: 200: 65, 401: 4, 404: 2, 429: 17, 503: 9

31 error events (first 60):

- off country p2: attempt 1 HTTP 503
- off country p2: attempt 2 HTTP 503
- off country p4: attempt 1 HTTP 503
- off country p4: attempt 2 HTTP 503
- off country p5: attempt 1 HTTP 503
- off country p5: attempt 2 HTTP 503
- off country p6: attempt 1 HTTP 503
- off country p11: attempt 1 HTTP 401
- off country p11: attempt 2 HTTP 401
- off country p11: attempt 3 HTTP 503
- off country p11: attempt 4 HTTP 401
- off country p11: attempt 5 HTTP 401
- off country p11: attempt 6 HTTP 503
- off country p11: GAVE UP after 6 attempts
- lookup off 8963442326263: attempt 1 HTTP 429
- lookup off 8964000177433: attempt 1 HTTP 429
- lookup off 8961003021794: attempt 1 HTTP 429
- lookup off 8961003010217: attempt 1 HTTP 429
- lookup off 8961003011771: attempt 1 HTTP 429
- lookup off 8964002881024: attempt 1 HTTP 429
- lookup off 8960160000185: attempt 1 HTTP 429
- lookup off 8964000760208: attempt 1 HTTP 429
- lookup off 8961700412314: attempt 1 HTTP 429
- lookup off 8961003020315: attempt 1 HTTP 429
- lookup off 8964002755981: attempt 1 HTTP 429
- lookup off 8962669061360: attempt 1 HTTP 429
- lookup off 8964001242307: attempt 1 HTTP 429
- lookup off 8964002347636: attempt 1 HTTP 429
- lookup off 8963755300660: attempt 1 HTTP 429
- lookup off 8964000577370: attempt 1 HTTP 429
- lookup off 8963471480080: attempt 1 HTTP 429

## Conclusion

- Distinct Pakistani items found (country tag OR 896 prefix, all 3 DBs): **2006**, of which **1523 have a product name** (75.9%); 461 have name+brand+pack size.
- 896-prefix items: 1257 (1027 named). Country-tagged items often carry non-896 barcodes (imports sold in Pakistan), so 896 alone under-counts local brands in other DBs.
- Lookup path `/api/v2/product/{barcode}`: 38/40 found (95.0%); 0 503s seen during lookups, 9 in total. The public OFF servers do return transient 503 / HTML 'temporarily unavailable' pages, so any integration needs timeouts, retries and a cache.
- Verdict: USABLE as a *best-effort fallback / suggestion source* (not as the primary catalog). Names are present for most items but pack size / category / image coverage is partial (see tables). Pakistan has ~1M+ SKUs in retail; a few thousand entries is a small fraction, so expect a low hit rate on a random shop barcode - useful as autofill, not as truth.
- Licensing: ODbL/CC BY-SA attribution + share-alike (see top of report).
