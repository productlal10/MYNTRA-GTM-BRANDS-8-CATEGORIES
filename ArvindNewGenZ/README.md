# Women's Ethnic Wear Scraper & Intelligence Platform

This repository is dedicated solely to scraping and analyzing **Women's Ethnic Wear** on Myntra for the approved brand allowlist.

## Included Scope
- **Women's Ethnic Wear Only** (Sarees, Kurtis & Kurtas, Salwar Suits, Lehengas, Sharara & Gharara, Ethnic Dresses, Dupattas, Ethnic Bottoms, Co-ords, Blouses)
- **Approved Brands**:
  1. Libas
  2. Jaypore
  3. Aurelia
  4. Biba
  5. Soch
  6. Aramya
  7. Suta
  8. Lakshita
  9. Aachho
- **15 Primary Ethnic Categories & 60+ Subcategories**:
  - Sarees (Banarasi, Kanjivaram, Silk, Cotton, Chanderi, Organza, Georgette, Bandhani, etc.)
  - Kurtis & Kurtas (Straight, A-Line, Anarkali, Flared, Angrakha, Short/Long, Chikankari, etc.)
  - Salwar Suits & Sets (Palazzo Suits, Churidar Sets, Anarkali Sets, Pant Suits)
  - Lehengas & Cholis (Bridal, Festive, Floral, Party Wear)
  - Sharara & Gharara Sets
  - Ethnic Dresses & Gowns
  - Dupattas & Shawls
  - Ethnic Bottom Wear (Palazzos, Shararas, Skirts, Salwars, Pants)
  - Blouses & Ethnic Tops
  - Bridal & Wedding Wear
  - Regional & Handloom Crafts
- **PostgreSQL Database**: Dedicated `women_ethnic_myntra_data` database
- **Web UI & REST API**: Dashboard, Price Intel, Color Intel, Ethnic Category Intel, Fabric & Weave Intel, Brands, Product Inspection Drawer, and Comparison.

## Quick Start
```bash
python3 main.py
```

## Safe Test Run
```bash
python3 main.py --brands "Libas" --max-pages-per-brand 1 --limit-products 5 --dry-run --no-snapshot
```

## Start Analytics Dashboard
```bash
python3 server.py
```
Visit http://127.0.0.1:3009/
