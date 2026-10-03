#!/usr/bin/env python3
"""
Export Arvind New GenZ data to a rich Excel workbook.
Sheets:
  1. Product Catalog   – Current master data for all 17 products
  2. Daily Snapshots   – Price + Stock per product per snapshot date
  3. Sales Analytics   – Units sold, revenue, ROS, stock status per date
  4. Change History    – Full event-by-event change log
  5. Size Inventory    – Current size-wise breakdown
"""

import os
import database
import json
from datetime import datetime, timezone, timedelta
from openpyxl import Workbook
from openpyxl.styles import (
    PatternFill, Font, Alignment, Border, Side, GradientFill
)
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import ColorScaleRule, DataBarRule

IST = timezone(timedelta(hours=5, minutes=30))


# ── Palette ────────────────────────────────────────────────────────────────
H_FILL   = PatternFill("solid", fgColor="0F172A")   # dark header
H_FONT   = Font(name="Calibri", bold=True, color="FFFFFF", size=10)
ALT_FILL = PatternFill("solid", fgColor="F8FAFC")   # alternating row
NORM_FILL= PatternFill("solid", fgColor="FFFFFF")
BOLD     = Font(name="Calibri", bold=True, size=10)
REG      = Font(name="Calibri", size=10)
RED_FILL = PatternFill("solid", fgColor="FEE2E2")
GRN_FILL = PatternFill("solid", fgColor="DCFCE7")
YLW_FILL = PatternFill("solid", fgColor="FEF9C3")
BLU_FILL = PatternFill("solid", fgColor="DBEAFE")
PRP_FILL = PatternFill("solid", fgColor="EDE9FE")
ORG_FILL = PatternFill("solid", fgColor="FEF3C7")
THIN_BD  = Border(
    left  =Side(style='thin', color='E2E8F0'),
    right =Side(style='thin', color='E2E8F0'),
    top   =Side(style='thin', color='E2E8F0'),
    bottom=Side(style='thin', color='E2E8F0'),
)
TITLE_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
CENTER = Alignment(horizontal="center", vertical="center")
WRAP   = Alignment(horizontal="left",   vertical="top", wrap_text=True)


def _fmt_dt(dt):
    if dt is None:
        return ""
    if hasattr(dt, "strftime"):
        return dt.strftime("%d %b %Y, %I:%M %p")
    return str(dt)


def _fmt_date(dt):
    if dt is None:
        return ""
    if hasattr(dt, "strftime"):
        return dt.strftime("%d %b %Y")
    return str(dt)


def _sign(v):
    return f"+{v}" if v > 0 else str(v)


def set_header(ws, row, cols, fill=H_FILL, font=H_FONT):
    for ci, col in enumerate(cols, 1):
        c = ws.cell(row=row, column=ci, value=col)
        c.fill = fill
        c.font = font
        c.alignment = CENTER
        c.border = THIN_BD


def set_cell(ws, row, col, value, bold=False, fill=None, align=None, number_format=None, font_color="000000"):
    c = ws.cell(row=row, column=col, value=value)
    c.font = Font(name="Calibri", bold=bold, size=10, color=font_color)
    if fill:
        c.fill = fill
    c.alignment = align or Alignment(horizontal="left", vertical="center")
    c.border = THIN_BD
    if number_format:
        c.number_format = number_format
    return c


def col_width(ws, widths):
    for ci, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(ci)].width = w


# ── Connect ────────────────────────────────────────────────────────────────
db_inst = database.Database()
conn = db_inst._get_connection()
cur = conn.cursor()

# ── Fetch base data ────────────────────────────────────────────────────────
cur.execute("""
    SELECT product_id, title, brand, primary_color, selling_price, mrp,
           discount_percentage, average_rating, total_ratings_count,
           total_reviews_count, category, is_in_stock, product_url
    FROM products ORDER BY brand ASC, product_id ASC
""")
products = [dict(r) for r in cur.fetchall()]

cur.execute("""
    SELECT product_id, size, inventory_count, available, raw_inventory_count
    FROM product_sizes ORDER BY product_id ASC, id ASC
""")
sizes_all = [dict(r) for r in cur.fetchall()]

# Group sizes by product
from collections import defaultdict
sizes_by_pid = defaultdict(list)
for s in sizes_all:
    sizes_by_pid[s['product_id']].append(s)

cur.execute("""
    SELECT product_id, snapshot_date, selling_price, mrp, discount_percentage, total_stock
    FROM daily_inventory_snapshots
    ORDER BY snapshot_date ASC, product_id ASC
""")
snapshots = [dict(r) for r in cur.fetchall()]

cur.execute("""
    SELECT product_id, analytics_date, units_sold, revenue_generated,
           stock_added, price_delta, ros, stock_status
    FROM daily_sales_analytics
    ORDER BY analytics_date ASC, product_id ASC
""")
analytics = [dict(r) for r in cur.fetchall()]

cur.execute("""
    SELECT id, product_id, recorded_at, event_num, categories_changed,
           stock_old, stock_new, stock_delta, color_name, size_changes,
           price_old, price_new, price_delta,
           discount_amt_old, discount_amt_new, discount_pct_old, discount_pct_new,
           rating_old, rating_new, reviews_old, reviews_new
    FROM product_change_history
    ORDER BY product_id ASC, event_num DESC
""")
history = [dict(r) for r in cur.fetchall()]


# ── Create workbook ────────────────────────────────────────────────────────
wb = Workbook()
wb.remove(wb.active)  # remove default blank sheet


# ===========================================================================
# SHEET 1 — Product Catalog (Master Data)
# ===========================================================================
ws1 = wb.create_sheet("📦 Product Catalog")
ws1.freeze_panes = "B2"
ws1.sheet_view.showGridLines = False

# Title row
ws1.row_dimensions[1].height = 30
ws1.merge_cells("A1:O1")
tc = ws1["A1"]
tc.value = "GHAN ARROW — Arrow Sport & U.S. Polo Assn. | Master Product Catalog"
tc.fill = H_FILL
tc.font = TITLE_FONT
tc.alignment = CENTER

# Column Headers
headers = [
    "Product ID", "Brand", "Title", "Color", "Category",
    "Selling Price (₹)", "MRP (₹)", "Discount %",
    "Discount Amt (₹)", "Avg Rating", "Total Ratings",
    "Reviews", "Total Stock (Units)", "In Stock", "Myntra URL"
]
set_header(ws1, 2, headers)

col_width(ws1, [14, 18, 52, 14, 12, 18, 14, 14, 16, 12, 14, 10, 18, 10, 55])

brand_fills = {
    "Arrow Sport": BLU_FILL,
    "U.S. Polo Assn.": PRP_FILL,
}

for ri, p in enumerate(products, 3):
    pid     = p['product_id']
    sizes   = sizes_by_pid[pid]
    tot_stk = sum(s['inventory_count'] for s in sizes if s.get('available'))
    disc_a  = round((p['mrp'] or 0) - (p['selling_price'] or 0), 2)
    fill    = brand_fills.get(p['brand'], NORM_FILL)
    alt     = ALT_FILL if ri % 2 == 0 else NORM_FILL

    row_data = [
        pid, p['brand'], p['title'], p['primary_color'], p['category'],
        p['selling_price'], p['mrp'], p['discount_percentage'],
        disc_a, p['average_rating'], p['total_ratings_count'],
        p['total_reviews_count'], tot_stk,
        "Yes" if p['is_in_stock'] else "No",
        p.get('product_url', f"https://www.myntra.com/{pid}")
    ]
    for ci, val in enumerate(row_data, 1):
        c = ws1.cell(row=ri, column=ci, value=val)
        c.font = REG
        c.fill = alt
        c.alignment = Alignment(horizontal="left", vertical="center")
        c.border = THIN_BD
        if ci == 6:  # price
            c.number_format = "₹#,##0.00"
        elif ci == 7:  # MRP
            c.number_format = "₹#,##0.00"
        elif ci == 9:  # disc amt
            c.number_format = "₹#,##0.00"
        elif ci == 13:  # stock — color
            if tot_stk <= 0:
                c.fill = RED_FILL
                c.font = Font(name="Calibri", bold=True, size=10, color="991B1B")
            elif tot_stk < 20:
                c.fill = YLW_FILL
                c.font = Font(name="Calibri", bold=True, size=10, color="92400E")
            else:
                c.fill = GRN_FILL
                c.font = Font(name="Calibri", bold=True, size=10, color="14532D")
        elif ci == 14:  # in stock
            if val == "Yes":
                c.fill = GRN_FILL
                c.font = Font(name="Calibri", bold=True, size=10, color="14532D")
            else:
                c.fill = RED_FILL
                c.font = Font(name="Calibri", bold=True, size=10, color="991B1B")

ws1.auto_filter.ref = f"A2:O{len(products)+2}"


# ===========================================================================
# SHEET 2 — Daily Snapshots (Price + Stock per date)
# ===========================================================================
ws2 = wb.create_sheet("📅 Daily Snapshots")
ws2.freeze_panes = "E3"
ws2.sheet_view.showGridLines = False

ws2.merge_cells("A1:J1")
tc2 = ws2["A1"]
tc2.value = "GHAN ARROW — Daily Snapshot History (Price, MRP, Discount, Stock) per Run"
tc2.fill = H_FILL
tc2.font = TITLE_FONT
tc2.alignment = CENTER

snap_headers = [
    "Snapshot Date", "Snapshot Time", "Product ID", "Brand", "Product Title",
    "Color", "Selling Price (₹)", "MRP (₹)", "Discount %", "Total Stock (Units)"
]
set_header(ws2, 2, snap_headers)
col_width(ws2, [22, 16, 14, 18, 50, 14, 18, 14, 14, 18])

pid_to_prod = {p['product_id']: p for p in products}
prev_snap_by_pid = {}

for ri, s in enumerate(snapshots, 3):
    pid   = s['product_id']
    prod  = pid_to_prod.get(pid, {})
    dt    = s['snapshot_date']
    date_str = _fmt_date(dt)
    time_str = dt.strftime("%I:%M:%S %p") if hasattr(dt, "strftime") else ""

    prev  = prev_snap_by_pid.get(pid)
    stk_fill = NORM_FILL
    if prev and s['total_stock'] < prev['total_stock']:
        stk_fill = RED_FILL
    elif prev and s['total_stock'] > prev['total_stock']:
        stk_fill = GRN_FILL

    alt = ALT_FILL if ri % 2 == 0 else NORM_FILL
    row_data = [
        date_str, time_str, pid,
        prod.get('brand', ''), prod.get('title', ''), prod.get('primary_color', ''),
        s['selling_price'], s['mrp'], s['discount_percentage'], s['total_stock']
    ]
    for ci, val in enumerate(row_data, 1):
        c = ws2.cell(row=ri, column=ci, value=val)
        c.font = REG
        c.fill = stk_fill if ci == 10 else alt
        c.alignment = Alignment(horizontal="left", vertical="center")
        c.border = THIN_BD
        if ci == 7:
            c.number_format = "₹#,##0.00"
        elif ci == 8:
            c.number_format = "₹#,##0.00"

    prev_snap_by_pid[pid] = s

ws2.auto_filter.ref = f"A2:J{len(snapshots)+2}"


# ===========================================================================
# SHEET 3 — Sales Analytics (Units Sold, Revenue, ROS)
# ===========================================================================
ws3 = wb.create_sheet("💰 Sales Analytics")
ws3.freeze_panes = "D3"
ws3.sheet_view.showGridLines = False

ws3.merge_cells("A1:K1")
tc3 = ws3["A1"]
tc3.value = "GHAN ARROW — Daily Sales Analytics (Units Sold, Revenue, ROS, Status) per Run"
tc3.fill = H_FILL
tc3.font = TITLE_FONT
tc3.alignment = CENTER

sa_headers = [
    "Analytics Date", "Analytics Time", "Product ID", "Brand", "Product Title",
    "Color", "Units Sold", "Revenue (₹)", "Stock Added",
    "Price Delta (₹)", "ROS (units/day)", "Stock Status"
]
set_header(ws3, 2, sa_headers)
col_width(ws3, [22, 16, 14, 18, 50, 14, 14, 16, 14, 16, 16, 16])

status_fills = {
    "FAST_MOVER": GRN_FILL,
    "HEALTHY"   : BLU_FILL,
    "LOW_STOCK" : YLW_FILL,
    "OOS"       : RED_FILL,
    "RESTOCKED" : PRP_FILL,
}

for ri, a in enumerate(analytics, 3):
    pid  = a['product_id']
    prod = pid_to_prod.get(pid, {})
    dt   = a['analytics_date']
    date_str = _fmt_date(dt)
    time_str = dt.strftime("%I:%M:%S %p") if hasattr(dt, "strftime") else ""
    status = a.get('stock_status', '') or ''
    status_fill = status_fills.get(status, NORM_FILL)
    alt = ALT_FILL if ri % 2 == 0 else NORM_FILL

    row_data = [
        date_str, time_str, pid,
        prod.get('brand', ''), prod.get('title', ''), prod.get('primary_color', ''),
        a['units_sold'], a['revenue_generated'], a['stock_added'],
        a['price_delta'], a['ros'], status
    ]
    for ci, val in enumerate(row_data, 1):
        c = ws3.cell(row=ri, column=ci, value=val)
        c.font = REG
        c.fill = status_fill if ci == 12 else alt
        c.alignment = Alignment(horizontal="left", vertical="center")
        c.border = THIN_BD
        if ci == 8:
            c.number_format = "₹#,##0.00"

ws3.auto_filter.ref = f"A2:L{len(analytics)+2}"


# ===========================================================================
# SHEET 4 — Change History (Full event log)
# ===========================================================================
ws4 = wb.create_sheet("📝 Change History")
ws4.freeze_panes = "D3"
ws4.sheet_view.showGridLines = False

ws4.merge_cells("A1:U1")
tc4 = ws4["A1"]
tc4.value = "GHAN ARROW — Complete Product Change History (Price, Stock, Discount, Rating, Reviews)"
tc4.fill = H_FILL
tc4.font = TITLE_FONT
tc4.alignment = CENTER

ch_headers = [
    "Event Date", "Event Time", "Product ID", "Brand", "Title", "Color Name",
    "Event #", "Categories Changed",
    "Stock Old", "Stock New", "Stock Delta",
    "Price Old (₹)", "Price New (₹)", "Price Delta (₹)",
    "Disc Amt Old (₹)", "Disc Amt New (₹)",
    "Disc % Old", "Disc % New",
    "Rating Old", "Rating New",
    "Reviews Old", "Reviews New",
    "Size Changes"
]
set_header(ws4, 2, ch_headers)
col_width(ws4, [
    22, 14, 14, 18, 48, 14,
    10, 30,
    12, 12, 12,
    16, 16, 16,
    16, 16,
    12, 12,
    12, 12,
    12, 12,
    60
])

cat_fills = {
    "Stock":   BLU_FILL,
    "Pricing": GRN_FILL,
    "Reviews": YLW_FILL,
}

for ri, h in enumerate(history, 3):
    pid  = h['product_id']
    prod = pid_to_prod.get(pid, {})
    dt   = h['recorded_at']
    date_str = _fmt_date(dt)
    time_str = dt.strftime("%I:%M:%S %p") if hasattr(dt, "strftime") else ""
    cats     = h['categories_changed'] or []
    if isinstance(cats, list):
        cats_str = ", ".join(cats)
    else:
        cats_str = str(cats)

    sz = h['size_changes']
    if isinstance(sz, str):
        try:
            sz = json.loads(sz)
        except:
            sz = []
    elif not isinstance(sz, list):
        sz = []
    size_str = " | ".join(
        f"{s['size']}: {s.get('old','?')}→{s.get('new','?')} ({_sign(s.get('delta',0))})"
        for s in sz
    ) if sz else ""

    delta = h.get('stock_delta') or 0
    alt = ALT_FILL if ri % 2 == 0 else NORM_FILL
    stk_fill = RED_FILL if delta < 0 else (GRN_FILL if delta > 0 else alt)

    row_data = [
        date_str, time_str, pid,
        prod.get('brand', ''), prod.get('title', ''),
        h['color_name'] or prod.get('primary_color', ''),
        f"#{h['event_num']}", cats_str,
        h['stock_old'], h['stock_new'], delta,
        h['price_old'], h['price_new'], h['price_delta'],
        h['discount_amt_old'], h['discount_amt_new'],
        h['discount_pct_old'], h['discount_pct_new'],
        h['rating_old'], h['rating_new'],
        h['reviews_old'], h['reviews_new'],
        size_str
    ]
    for ci, val in enumerate(row_data, 1):
        c = ws4.cell(row=ri, column=ci, value=val)
        c.font = REG
        fill_to_use = alt
        if ci == 11:   # stock delta
            fill_to_use = stk_fill
        elif ci == 8:  # cats
            primary_cat = cats[0] if cats else None
            fill_to_use = cat_fills.get(primary_cat, alt)
        c.fill = fill_to_use
        c.alignment = Alignment(horizontal="left", vertical="center", wrap_text=(ci == 23))
        c.border = THIN_BD
        if ci in (12, 13, 14, 15, 16):
            c.number_format = "₹#,##0.00"
    ws4.row_dimensions[ri].height = 30

ws4.auto_filter.ref = f"A2:W{len(history)+2}"


# ===========================================================================
# SHEET 5 — Size-Wise Inventory (Current Snapshot)
# ===========================================================================
ws5 = wb.create_sheet("📏 Size Inventory")
ws5.freeze_panes = "E2"
ws5.sheet_view.showGridLines = False

ws5.merge_cells("A1:I1")
tc5 = ws5["A1"]
tc5.value = "GHAN ARROW — Current Size-Wise Inventory Breakdown (Latest Snapshot)"
tc5.fill = H_FILL
tc5.font = TITLE_FONT
tc5.alignment = CENTER

si_headers = [
    "Product ID", "Brand", "Title", "Color", "Size",
    "Inventory Count", "Raw Scraped Count", "Available", "Stock Level"
]
set_header(ws5, 2, si_headers)
col_width(ws5, [14, 18, 50, 14, 10, 18, 18, 12, 16])

ri = 3
for p in products:
    pid   = p['product_id']
    sizes = sizes_by_pid[pid]
    for s in sizes:
        cnt   = s['inventory_count'] or 0
        raw   = s.get('raw_inventory_count')
        avail = "Yes" if s.get('available') else "No"
        level = "OOS" if cnt == 0 else ("Low" if cnt <= 5 else ("Med" if cnt <= 20 else "High"))
        level_fill = {
            "OOS": RED_FILL, "Low": YLW_FILL, "Med": ORG_FILL, "High": GRN_FILL
        }.get(level, NORM_FILL)
        alt = ALT_FILL if ri % 2 == 0 else NORM_FILL

        row_data = [
            pid, p['brand'], p['title'], p['primary_color'],
            s['size'], cnt, raw or "—", avail, level
        ]
        for ci, val in enumerate(row_data, 1):
            c = ws5.cell(row=ri, column=ci, value=val)
            c.font = REG
            c.fill = level_fill if ci == 9 else alt
            c.alignment = Alignment(horizontal="left", vertical="center")
            c.border = THIN_BD
        ri += 1

ws5.auto_filter.ref = f"A2:I{ri}"


# ===========================================================================
# Save
# ===========================================================================
now = datetime.now(IST)
filename = f"Ghan_Arrow_Export_{now.strftime('%Y-%m-%d_%H%M')}.xlsx"
output_path = os.path.join(os.path.expanduser("~"), "Desktop", filename)
wb.save(output_path)
print(f"\n✅ Excel workbook exported successfully!")
print(f"📁 File: {output_path}")
print(f"\n📊 Sheets created:")
print(f"   1. 📦 Product Catalog  — {len(products)} products with full specs")
print(f"   2. 📅 Daily Snapshots  — {len(snapshots)} snapshot rows across {len(set(s['snapshot_date'] for s in snapshots))} runs")
print(f"   3. 💰 Sales Analytics  — {len(analytics)} daily sales analytics rows")
print(f"   4. 📝 Change History   — {len(history)} change events across all 17 products")
print(f"   5. 📏 Size Inventory   — {len(sizes_all)} size rows current warehouse stock")
