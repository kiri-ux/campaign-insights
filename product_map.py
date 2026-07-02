"""
product_map.py
Vici product legend: workbook product value -> (abbreviation, hex).
Used to render compact colored pills with a hover tooltip (full name).
"""

# Keyed by the value that appears in the workbook's Product / Product 2 columns.
PRODUCT_MAP = {
    "Display": ("D", "#Bb8b76"),
    "Social Mirror": ("SM", "#E0B0FF"),
    "Social Mirror CTV": ("SMC", "#9966CC"),
    "CTV": ("CTV", "#7FFFD4"),
    "Native Display": ("ND", "#bf3a7a"),
    "Native Video": ("NV", "#a14796"),
    "Online Audio": ("OA", "#f6dc75"),
    "Audio": ("OA", "#f6dc75"),
    "Video": ("V", "#ff4d45"),
    # extra legend entries in case they appear
    "CTV + Video": ("CV", "#008080"),
    "Geo-Framing": ("GF", "#2E8B57"),
    "Connected TV": ("CTV", "#7FFFD4"),
    "Dynamic": ("DY", "#98FB98"),
    "Website Visitor ID": ("ID", "#e6e827"),
    "Performance Max": ("PM", "#fa8bed"),
    "Pay-Per-Click": ("PPC", "#fcb500"),
    "YouTube": ("YT", "#fe0908"),
    "Amazon Premium Display": ("AD", "#fd8c52"),
    "Search Engine Optimization": ("SEO", "#ffdfe9"),
    "Reputation Management": ("RM", "#bbaefe"),
}


def _fg(hexstr):
    """Pick black or white text for contrast against a hex background."""
    h = hexstr.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    return "#12213a" if lum > 0.6 else "#ffffff"


def build_pmap():
    """value -> {abbr, hex, fg, full} for the template pill macro."""
    out = {}
    for value, (abbr, hx) in PRODUCT_MAP.items():
        out[value] = {"abbr": abbr, "hex": hx, "fg": _fg(hx), "full": value}
    return out
