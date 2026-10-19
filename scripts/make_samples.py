"""Generate the demo invoice PDFs + samples/manifest.json. Deterministic: same bytes every run."""
import json
import random
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "samples"
BUYER = ["Zephyr Retail Pvt Ltd", "No. 14, 3rd Floor, Indiranagar 100 Ft Road", "Bengaluru, Karnataka 560038",
         "GSTIN: 29AAACZ0001A1Z9"]
W, H = A4


def inr(x):
    s = f"{x:,.2f}"
    whole, dec = s.split(".")
    whole = whole.replace(",", "")
    if len(whole) > 3:  # Indian grouping 12,34,567
        head, tail = whole[:-3], whole[-3:]
        head = ",".join([head[max(i - 2, 0):i] for i in range(len(head), 0, -2)][::-1])
        whole = head + "," + tail
    return f"{whole}.{dec}"


def new_canvas(name):
    c = canvas.Canvas(str(OUT / name), pagesize=A4, invariant=1)
    c.setTitle(name)
    return c


def table(c, x, y, cols, rows, widths, head_bg):
    c.setFillColor(head_bg); c.rect(x, y - 4, sum(widths), 18, stroke=0, fill=1)
    c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 9)
    cx = x
    for col, w in zip(cols, widths):
        c.drawString(cx + 4, y + 2, col); cx += w
    c.setFillColor(colors.black); c.setFont("Helvetica", 9)
    for r in rows:
        y -= 18; cx = x
        for i, (v, w) in enumerate(zip(r, widths)):
            (c.drawRightString(cx + w - 4, y + 2, v) if i >= 2 else c.drawString(cx + 4, y + 2, v)); cx += w
        c.setStrokeColor(colors.HexColor("#dddddd")); c.line(x, y - 4, x + sum(widths), y - 4)
    return y - 10


def totals(c, x, y, pairs, bold_last=True):
    for i, (k, v) in enumerate(pairs):
        last = bold_last and i == len(pairs) - 1
        c.setFont("Helvetica-Bold" if last else "Helvetica", 10 if last else 9)
        c.drawRightString(x, y, k); c.drawRightString(x + 110, y, v); y -= 15
    return y


# ---------- Acme layout (used by H1, E3, E4) ----------
def acme(fname, name, inv_no, date, po, lines, bank, notice=None):
    c = new_canvas(fname)
    c.setFillColor(colors.HexColor("#0b4f8a")); c.rect(0, H - 90, W, 90, stroke=0, fill=1)
    c.setFillColor(colors.white); c.setFont("Helvetica-Bold", 20); c.drawString(40, H - 45, name.upper())
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 62, "42, KIADB Industrial Area, Peenya 2nd Stage, Bengaluru 560058  |  accounts@acmeoffice.in")
    c.drawString(40, H - 76, "GSTIN: 29AABCA1234F1Z5   PAN: AABCA1234F")
    c.setFont("Helvetica-Bold", 16); c.drawRightString(W - 40, H - 45, "TAX INVOICE")
    c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 9); c.drawString(40, H - 120, "Bill To")
    c.setFont("Helvetica", 9)
    for i, l in enumerate(BUYER): c.drawString(40, H - 134 - i * 12, l)
    meta = [("Invoice No:", inv_no), ("Invoice Date:", date), ("Due Date:", "Net 30"), ("Place of Supply:", "Karnataka (29)")]
    if po: meta.insert(2, ("PO Reference:", po))
    for i, (k, v) in enumerate(meta):
        c.setFont("Helvetica-Bold", 9); c.drawString(360, H - 120 - i * 14, k)
        c.setFont("Helvetica", 9); c.drawString(450, H - 120 - i * 14, v)
    rows = [[str(i + 1), d, f"{q}", inr(p), inr(q * p)] for i, (d, q, p) in enumerate(lines)]
    rows = [[r[0], r[1], r[2], r[3], r[4]] for r in rows]
    y = table(c, 40, H - 220, ["#", "Description", "Qty", "Rate (Rs.)", "Amount (Rs.)"], rows,
              [25, 255, 50, 90, 95], colors.HexColor("#0b4f8a"))
    sub = sum(q * p for _, q, p in lines); g = round(sub * 0.09, 2)
    y = totals(c, 440, y - 10, [("Taxable Value", inr(sub)), ("CGST @ 9%", inr(g)), ("SGST @ 9%", inr(g)),
                                ("Grand Total (Rs.)", inr(sub + 2 * g))])
    y -= 10
    if notice:
        c.setFillColor(colors.HexColor("#fff3cd")); c.rect(40, y - 38, W - 80, 44, stroke=0, fill=1)
        c.setFillColor(colors.HexColor("#7a5200")); c.setFont("Helvetica-Bold", 9); c.drawString(48, y - 8, "IMPORTANT NOTICE")
        c.setFont("Helvetica", 8.5); c.drawString(48, y - 22, notice); y -= 52
    c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 9); c.drawString(40, y - 10, "Bank Details for Payment")
    c.setFont("Helvetica", 9)
    for i, l in enumerate(bank): c.drawString(40, y - 24 - i * 12, l)
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(40, 60, "Terms: Payment within 30 days. Interest @18% p.a. on delayed payments. Subject to Bengaluru jurisdiction.")
    c.drawRightString(W - 40, 80, f"For {name}"); c.drawRightString(W - 40, 50, "Authorised Signatory")
    c.save()


# ---------- Northwind layout (E2): landscape-ish header, IGST ----------
def northwind(fname):
    c = new_canvas(fname)
    c.setFont("Helvetica-Bold", 22); c.setFillColor(colors.HexColor("#1f6f43")); c.drawString(40, H - 55, "NORTHWIND")
    c.setFont("Helvetica", 10); c.drawString(40, H - 70, "LOGISTICS PVT LTD")
    c.setFillColor(colors.black); c.setFont("Helvetica", 8.5)
    for i, l in enumerate(["Unit 7, Bhiwandi Logistics Park, Thane, Maharashtra 421302",
                           "GSTIN 27AACCN5678K1Z2  |  CIN U63030MH2011PTC219876"]):
        c.drawString(40, H - 86 - i * 11, l)
    c.setStrokeColor(colors.HexColor("#1f6f43")); c.setLineWidth(2); c.line(40, H - 115, W - 40, H - 115)
    c.setFont("Helvetica-Bold", 13); c.drawString(40, H - 140, "Tax Invoice (Original for Recipient)")
    c.setFont("Helvetica", 9)
    info = [("Invoice #", "NWL/26-27/0233"), ("Date", "10-Oct-2026"), ("Customer PO", "PO-2026-0291"),
            ("Service period", "01-Sep-2026 to 30-Sep-2026"), ("Place of supply", "Karnataka (29) - Inter-state")]
    for i, (k, v) in enumerate(info):
        c.drawString(330, H - 140 - i * 13, f"{k}:"); c.drawString(430, H - 140 - i * 13, v)
    c.setFont("Helvetica-Bold", 9); c.drawString(40, H - 160, "Billed to:"); c.setFont("Helvetica", 9)
    for i, l in enumerate(BUYER): c.drawString(40, H - 173 - i * 12, l)
    lines = [("FTL freight Bengaluru-Mumbai, 32ft container (SAC 996511)", 12, 15000),
             ("Last-mile distribution, Bengaluru city - Sep (SAC 996511)", 1, 60000)]
    rows = [[str(i + 1), d, str(q), inr(p), inr(q * p)] for i, (d, q, p) in enumerate(lines)]
    y = table(c, 40, H - 250, ["Sl", "Service description", "Trips", "Rate", "Amount (INR)"], rows,
              [25, 280, 40, 80, 90], colors.HexColor("#1f6f43"))
    y = totals(c, 440, y - 10, [("Taxable amount", inr(240000)), ("IGST @ 18%", inr(43200)), ("Invoice total (INR)", inr(283200))])
    c.setFont("Helvetica", 8.5)
    c.drawString(40, y - 10, "Amount in words: Rupees Two Lakh Eighty Three Thousand Two Hundred Only")
    c.drawString(40, y - 30, "Remit to: ICICI Bank, Andheri East  |  A/c 001205007788  |  IFSC ICIC0000012")
    c.drawString(40, y - 42, "This is milestone invoice 3 against the above PO.")
    c.setFont("Helvetica-Oblique", 8); c.drawString(40, 50, "TDS u/s 194C applicable. Please share TDS certificate quarterly.")
    c.save()


# ---------- Brightline (E1): phone-scanned image PDF, tax-inclusive bundle ----------
def brightline_scanned(fname):
    w, h = 1240, 1754  # A4 @150dpi
    img = Image.new("L", (w, h), 250)
    d = ImageDraw.Draw(img)
    F = "/System/Library/Fonts/Supplemental/"
    big, mid, small = (ImageFont.truetype(F + "Arial Bold.ttf", 46), ImageFont.truetype(F + "Arial.ttf", 26),
                       ImageFont.truetype(F + "Arial.ttf", 23))
    bold = ImageFont.truetype(F + "Arial Bold.ttf", 25)
    d.text((80, 80), "Brightline IT Services LLP", font=big, fill=20)
    d.text((80, 140), "#88, 2nd Cross, HSR Layout Sector 6, Bengaluru 560102", font=small, fill=40)
    d.text((80, 172), "GSTIN: 29AAKFB9012L1Z8    Ph: 080-4900 3300", font=small, fill=40)
    d.line((80, 215, 1160, 215), fill=30, width=3)
    d.text((80, 240), "INVOICE", font=big, fill=20)
    for i, (k, v) in enumerate([("Invoice No", "BL/2026/1187"), ("Date", "06/10/2026"), ("Your PO", "PO-2026-0377")]):
        d.text((760, 245 + i * 36), f"{k}:  {v}", font=mid, fill=25)
    d.text((80, 340), "To: Zephyr Retail Pvt Ltd, Indiranagar, Bengaluru", font=mid, fill=25)
    d.text((80, 374), "GSTIN 29AAACZ0001A1Z9", font=mid, fill=25)
    d.rectangle((80, 450, 1160, 500), outline=30, width=2)
    d.text((95, 462), "Particulars", font=bold, fill=20); d.text((960, 462), "Amount (Rs)", font=bold, fill=20)
    d.rectangle((80, 500, 1160, 640), outline=30, width=2)
    d.text((95, 520), "Annual IT support & AMC - FY 2026-27", font=mid, fill=25)
    d.text((95, 556), "(12 months, 45 endpoints, 8x5 helpdesk)", font=small, fill=50)
    d.text((95, 592), "Price inclusive of GST @ 18%", font=small, fill=50)
    d.text((960, 520), "1,41,600.00", font=mid, fill=25)
    d.text((620, 670), "TOTAL (incl. GST)   Rs 1,41,600.00", font=bold, fill=15)
    d.text((80, 760), "Rupees One Lakh Forty One Thousand Six Hundred only", font=small, fill=40)
    d.text((80, 840), "Bank: State Bank of India, HSR Layout", font=small, fill=40)
    d.text((80, 872), "A/c No: 38291003301   IFSC: SBIN0040211", font=small, fill=40)
    d.text((820, 1000), "for Brightline IT Services LLP", font=small, fill=40)
    d.ellipse((860, 1040, 1060, 1160), outline=90, width=4)  # rubber stamp
    d.text((893, 1072), "BRIGHTLINE", font=small, fill=110); d.text((920, 1102), "BLR-102", font=small, fill=110)
    d.text((80, 1600), "Thank you for your business!", font=small, fill=60)
    # "phone scan": skew, blur, noise, uneven lighting
    img = img.rotate(1.6, resample=Image.BICUBIC, expand=False, fillcolor=235).filter(ImageFilter.GaussianBlur(0.9))
    rnd = random.Random(7)
    px = img.load()
    for _ in range(60000):
        x, y = rnd.randrange(w), rnd.randrange(h)
        px[x, y] = max(0, min(255, px[x, y] + rnd.randint(-70, 40)))
    shade = Image.linear_gradient("L").resize((w, h)).point(lambda v: 255 - v // 6)
    img = Image.composite(img, Image.new("L", (w, h), 200), shade)
    fixed = time.strptime("2026-10-06 09:30", "%Y-%m-%d %H:%M")  # pinned so the file (and its sha256 cache key) is identical every run
    img.convert("RGB").save(OUT / fname, "PDF", resolution=150, creationDate=fixed, modDate=fixed)


# ---------- Brightline digital (E5): 2 pages, 12 lines, one line off by Rs 100, IGST on an intra-state supply ----------
def brightline_multipage(fname):
    lines = [  # (description, qty, rate, printed amount); line 7 is the typo: 120 x 145 = 17,400, printed 17,500
        ("Network switch configuration, 24-port (SAC 998316)", 4, 6500, 26000),
        ("Firewall policy audit (SAC 998316)", 1, 18000, 18000),
        ("Wi-Fi access point installation (SAC 995461)", 10, 2200, 22000),
        ("Structured cabling, per drop (SAC 995461)", 40, 650, 26000),
        ("Microsoft 365 migration, per user (SAC 998316)", 45, 400, 18000),
        ("Endpoint security setup, per device (SAC 998316)", 45, 300, 13500),
        ("Cat6 patch cable, 3 m (HSN 8544)", 120, 145, 17500),
        ("Rack dressing and labelling (SAC 995461)", 2, 4500, 9000),
        ("Backup configuration, NAS (SAC 998316)", 1, 12000, 12000),
        ("UPS health check (SAC 998719)", 6, 750, 4500),
        ("On-site engineer, after hours, per hour (SAC 998316)", 16, 1100, 17600),
        ("Project management fee (SAC 998311)", 1, 15000, 15000),
    ]
    sub = sum(a for *_, a in lines)  # 1,99,100: the vendor's own (wrong) line amounts add up, so totals look fine
    igst = round(sub * 0.18, 2)
    c = new_canvas(fname)

    def header(page):
        c.setFillColor(colors.HexColor("#5b2a86")); c.rect(40, H - 70, 6, 34, stroke=0, fill=1)
        c.setFillColor(colors.black); c.setFont("Helvetica-Bold", 18); c.drawString(54, H - 50, "Brightline IT Services LLP")
        c.setFont("Helvetica", 8.5)
        c.drawString(54, H - 64, "#88, 2nd Cross, HSR Layout Sector 6, Bengaluru 560102  |  GSTIN 29AAKFB9012L1Z8  |  LLPIN AAB-4471")
        c.setFont("Helvetica", 8); c.drawRightString(W - 40, H - 50, f"Page {page} of 2")
        c.setStrokeColor(colors.HexColor("#dddddd")); c.line(40, H - 80, W - 40, H - 80)

    header(1)
    c.setFont("Helvetica-Bold", 14); c.drawString(40, H - 108, "TAX INVOICE")
    c.setFont("Helvetica", 9)
    for i, (k, v) in enumerate([("Invoice No", "BL/2026/1212"), ("Date", "14-Oct-2026"), ("PO", "PO-2026-0498"),
                                ("Place of supply", "Karnataka (29)"), ("Terms", "Net 15")]):
        c.drawString(360, H - 108 - i * 13, f"{k}:"); c.drawString(450, H - 108 - i * 13, v)
    c.setFont("Helvetica-Bold", 9); c.drawString(40, H - 128, "Bill to / Ship to:"); c.setFont("Helvetica", 9)
    for i, l in enumerate(BUYER): c.drawString(40, H - 141 - i * 12, l)
    cols, widths = ["#", "Description", "Qty", "Rate (Rs.)", "Amount (Rs.)"], [22, 278, 40, 80, 95]
    row = lambda i, d, q, p, a: [str(i), d, str(q), inr(p), inr(a)]
    y = table(c, 40, H - 220, cols, [row(i + 1, *l) for i, l in enumerate(lines[:8])], widths, colors.HexColor("#5b2a86"))
    c.setFont("Helvetica-Oblique", 8.5); c.drawRightString(W - 40, y - 6, "continued on page 2")
    c.showPage()

    header(2)
    y = table(c, 40, H - 110, cols, [row(i + 9, *l) for i, l in enumerate(lines[8:])], widths, colors.HexColor("#5b2a86"))
    y = totals(c, 440, y - 10, [("Taxable value", inr(sub)), ("IGST @ 18%", inr(igst)), ("Invoice total (Rs.)", inr(sub + igst))])
    c.setFont("Helvetica", 8.5)
    c.drawString(40, y - 10, "Amount in words: Rupees Two Lakh Thirty Four Thousand Nine Hundred Thirty Eight Only")
    c.setFont("Helvetica-Bold", 9); c.drawString(40, y - 36, "Payment details"); c.setFont("Helvetica", 9)
    c.drawString(40, y - 50, "State Bank of India, HSR Layout  |  A/c No: 38291003301  |  IFSC: SBIN0040211")
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(40, 70, "TDS u/s 194J applicable on professional/technical services. Please quote invoice number with payment.")
    c.drawRightString(W - 40, 50, "For Brightline IT Services LLP  -  Authorised Signatory")
    c.save()


MANIFEST = [
    {"file": "h1_acme_clean.pdf", "title": "H1 · Clean invoice", "expected": "APPROVE", "reason": "",
     "scenario": "Acme stationery invoice: digital PDF, explicit PO, itemised lines, CGST + SGST."},
    {"file": "e1_brightline_scanned.pdf", "title": "E1 · Phone-scanned, GST bundled", "expected": "HOLD", "reason": "tax invoice",
     "scenario": "Skewed, noisy scan with no text layer. One bundled line 'inclusive of GST', no tax breakup."},
    {"file": "e2_northwind_overrun.pdf", "title": "E2 · Milestone over-billing", "expected": "HOLD", "reason": "remaining PO balance",
     "scenario": "Third milestone invoice on a ₹5L PO with ₹3L already billed. Asks for ₹2.4L."},
    {"file": "e3_acme_rebill.pdf", "title": "E3 · Paid invoice, new number", "expected": "REJECT", "reason": "INV-0042",
     "scenario": "An already-paid invoice resubmitted under a new number and date. Hash and number both differ."},
    {"file": "e4_acme_bank_change.pdf", "title": "E4 · Bank-change fraud", "expected": "HOLD", "reason": "bank details",
     "scenario": "Looks like Acme, has no PO, and asks to pay a 'new' bank account."},
    {"file": "e5_brightline_multipage.pdf", "title": "E5 · Two pages, two hidden errors", "expected": "HOLD", "reason": "IGST",
     "scenario": "12 lines over 2 pages. Totals add up, but one line is ₹100 off and IGST is charged within Karnataka."},
]


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    acme_bank = ["HDFC Bank, Peenya Branch", "A/c No: 50100234564521", "IFSC: HDFC0001234"]
    acme("h1_acme_clean.pdf", "Acme Office Supplies Pvt Ltd", "INV-0057", "09-Oct-2026", "PO-2026-0412",
         [("A4 copier paper 75 GSM (ream of 500)", 120, 210), ("Ball pens, blue (box of 50)", 40, 325),
          ("Whiteboard markers (set of 4)", 60, 105), ("Heavy-duty stapler", 20, 200)], acme_bank)
    acme("e3_acme_rebill.pdf", "Acme Office Supplies Pvt Ltd", "ACM/26-27/118", "08-Oct-2026", "PO-2026-0150",
         [("Printer toner cartridge 88A", 16, 1000), ("A4 paper ream", 40, 100)], acme_bank)
    acme("e4_acme_bank_change.pdf", "ACME Office Supply Pvt. Ltd.", "INV-0061", "12-Oct-2026", None,
         [("Ergonomic mesh chair, high back", 30, 4500)],
         ["Kotak Mahindra Bank, MG Road", "A/c No: 7712093349910", "IFSC: KKBK0000958"],
         notice="We have changed our banking partner. Kindly update records and remit all payments to the new account below.")
    northwind("e2_northwind_overrun.pdf")
    brightline_scanned("e1_brightline_scanned.pdf")
    brightline_multipage("e5_brightline_multipage.pdf")
    (OUT / "manifest.json").write_text(json.dumps(MANIFEST, indent=2, ensure_ascii=False))
    print("wrote", len(MANIFEST), "samples to", OUT)
