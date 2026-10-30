"""Drawing kit for the Round 1 one-slider: colours, text, shapes, curves,
native charts and the pieces every design shares. Every number comes from
numbers.json through facts(); nothing here holds a result of its own."""

import io
import math

import qrcode
from pptx.chart.data import CategoryChartData, XyChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE, XL_LABEL_POSITION, XL_LEGEND_POSITION, XL_TICK_LABEL_POSITION
from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt
from lxml import etree

FONT, SERIF = "Calibri", "Cambria"

# One colour means ours, everywhere. The rivals keep the colours of the repo's charts.
INK, MUTED, FAINT = "14213D", "56606E", "8A929E"
PANEL, LINE, SEG = "F3F6FA", "DCE3EC", "C9D1DC"
OURS, OURS_MID, OURS_TINT, OURS_LIGHT = "6250D6", "A99FEA", "ECE9FB", "B3A8FF"
CAKE, RULES, RULES_TINT, NOQOS = "2A78D6", "E0612A", "FBE4D9", "A7ACB3"
CHIP, CHIP_INK, SUN = "FFF0BF", "6B4E00", "FFC93C"
WASH, WASH_SUN = "EEF4FC", "FFF6DA"
WHITE = "FFFFFF"
NIGHT, NIGHT_2, NIGHT_LINE, NIGHT_MUTED = "0E1530", "18224A", "2E3A66", "A6B0CF"

LEFT, CENTER, RIGHT = PP_ALIGN.LEFT, PP_ALIGN.CENTER, PP_ALIGN.RIGHT
TOP, MIDDLE, BOTTOM = MSO_ANCHOR.TOP, MSO_ANCHOR.MIDDLE, MSO_ANCHOR.BOTTOM
S = MSO_SHAPE


def rgb(h):
    return RGBColor.from_string(h)


def pct(v, digits=0):
    return f"{100 * v:.{digits}f}%"


class Canvas:
    def __init__(self, slide):
        self.slide = slide

    def box(self, x, y, w, h, fill=None, line=None, shape=S.RECTANGLE, radius=None, line_w=0.75, name=None,
            rotation=None, dash=False):
        s = self.slide.shapes.add_shape(shape, Inches(x), Inches(y), Inches(w), Inches(h))
        if radius is not None:
            s.adjustments[0] = radius
        if fill:
            s.fill.solid()
            s.fill.fore_color.rgb = rgb(fill)
        else:
            s.fill.background()
        if line:
            s.line.color.rgb = rgb(line)
            s.line.width = Pt(line_w)
            if dash:
                s.line.dash_style = 4
        else:
            s.line.fill.background()
        s.shadow.inherit = False
        if rotation is not None:
            s.rotation = rotation
        if name:
            s.name = name
        return s

    def text(self, x, y, w, h, paragraphs, size=10, color=INK, bold=False, align=LEFT, anchor=TOP, shape=None,
             margin=0.0, spacing=None, line_spacing=None, font=FONT, italic=False, name=None):
        """paragraphs: a string, or a list of paragraphs, each a string or a
        list of (text, {size, color, bold, font, italic}) runs."""
        if shape is None:
            shape = self.slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
        tf = shape.text_frame
        tf.word_wrap = True
        tf.auto_size = None
        tf.vertical_anchor = anchor
        for side in ("left", "right", "top", "bottom"):
            setattr(tf, f"margin_{side}", Inches(margin))
        if isinstance(paragraphs, str):
            paragraphs = [paragraphs]
        for i, para in enumerate(paragraphs):
            p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
            p.alignment = align
            if spacing:
                p.space_after = Pt(spacing)
            if line_spacing:
                p.line_spacing = line_spacing
            runs = [(para, {})] if isinstance(para, str) else para
            for text, opt in runs:
                r = p.add_run()
                r.text = text
                f = r.font
                f.name = opt.get("font", font)
                f.size = Pt(opt.get("size", size))
                f.bold = opt.get("bold", bold)
                f.italic = opt.get("italic", italic)
                f.color.rgb = rgb(opt.get("color", color))
        if name:
            shape.name = name
        return shape

    def label(self, shape, paragraphs, **kw):
        """Text inside a shape already drawn."""
        kw.setdefault("align", CENTER)
        kw.setdefault("anchor", MIDDLE)
        return self.text(0, 0, 0, 0, paragraphs, shape=shape, **kw)

    def line(self, x1, y1, x2, y2, color=LINE, width=1.0, dash=False):
        c = self.slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(x1), Inches(y1), Inches(x2), Inches(y2))
        c.line.color.rgb = rgb(color)
        c.line.width = Pt(width)
        if dash:
            c.line.dash_style = 4
        return c

    def path(self, pts, color, width, closed=False, fill=None, name=None, dash=False, arrow=False):
        emu = [(int(Inches(x)), int(Inches(y))) for x, y in pts]
        fb = self.slide.shapes.build_freeform(emu[0][0], emu[0][1], scale=1.0)
        fb.add_line_segments(emu[1:], close=closed)
        s = fb.convert_to_shape()
        if fill:
            s.fill.solid()
            s.fill.fore_color.rgb = rgb(fill)
        else:
            s.fill.background()
        if color:
            s.line.color.rgb = rgb(color)
            s.line.width = Pt(width)
            if dash:
                s.line.dash_style = 4
            # round ends and joins, so thick curves look drawn, not cut
            ln = s.line._get_or_add_ln()
            ln.set("cap", "rnd")
            etree.SubElement(ln, qn("a:round"))
            if arrow:
                tail = etree.SubElement(ln, qn("a:tailEnd"))
                tail.set("type", "triangle")
                tail.set("w", "med")
                tail.set("len", "med")
        else:
            s.line.fill.background()
        s.shadow.inherit = False
        if name:
            s.name = name
        return s

    def picture(self, stream, x, y, w, h):
        return self.slide.shapes.add_picture(stream, Inches(x), Inches(y), Inches(w), Inches(h))


def bezier(p0, p3, n=36, pull=0.5):
    """An S-curve from p0 to p3 that leaves and arrives horizontally."""
    (x0, y0), (x3, y3) = p0, p3
    dx = (x3 - x0) * pull
    p1, p2 = (x0 + dx, y0), (x3 - dx, y3)
    return cubic(p0, p1, p2, p3, n)


def cubic(p0, p1, p2, p3, n=36):
    pts = []
    for i in range(n + 1):
        t = i / n
        a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
        pts.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return pts


def blob(c, cx, cy, rx, ry, fill, wobble=(0.06, 0.04, 0.05), n=90, name="Wash"):
    """A soft, slightly irregular wash, like the template's airy background."""
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        r = 1 + wobble[0] * math.sin(2 * a + 0.7) + wobble[1] * math.sin(3 * a + 2.1) + wobble[2] * math.cos(5 * a)
        pts.append((cx + rx * r * math.cos(a), cy + ry * r * math.sin(a)))
    return c.path(pts, None, 0, closed=True, fill=fill, name=name)


def laptop(c, x, y, scale=1.0, ink=INK, screen=WHITE, person=OURS):
    """A laptop with a person on a call on its screen; (x, y) is the screen's top left."""
    k = scale
    c.box(x, y, 1.0 * k, 0.66 * k, fill=screen, line=ink, shape=S.ROUNDED_RECTANGLE, radius=0.08, line_w=1.75 * k)
    c.box(x + 0.41 * k, y + 0.13 * k, 0.18 * k, 0.18 * k, fill=person, shape=S.OVAL)
    c.box(x + 0.32 * k, y + 0.34 * k, 0.36 * k, 0.22 * k, fill=person, shape=S.ROUND_2_SAME_RECTANGLE)
    c.box(x - 0.12 * k, y + 0.68 * k, 1.24 * k, 0.09 * k, fill=ink, shape=S.ROUNDED_RECTANGLE, radius=0.5)


def qr_png(link, dark=False):
    img = qrcode.make(link, border=1, box_size=12)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def drop_placeholders(slide):
    for shape in list(slide.placeholders):
        shape._element.getparent().remove(shape._element)


def send_to_back(shape):
    """Behind everything, logos included (they stay visible above a light shape)."""
    tree = shape._element.getparent()
    tree.remove(shape._element)
    tree.insert(2, shape._element)


# ---------------------------------------------------------------- the numbers, as words

def facts(n):
    t = n["total_cost"]["value"]
    s6 = n["s6_late_frames_pct"]["value"]
    rv = n["runs_vs_cake"]
    verdicts = n["scenario_verdicts"]["value"]
    seeds, planned = n["seeds"]["value"], n["seeds"]["planned"]
    return {
        "seeds": seeds, "planned": planned, "interim": seeds < planned,
        "interim_text": f"Interim: {seeds} of {planned} seeds, emulated" if seeds < planned else "Emulated testbed",
        "scenarios": n["scenarios"]["value"],
        "less": pct(n["less_damage_than_cake"]["value"]),
        "total": {k: f"{v:.2f}" for k, v in t.items()}, "total_raw": t,
        "s6_cake": f"{s6['B2']:.0f}%", "s6_rules": f"{s6['B4']:.0f}%", "s6_ours": f"{s6['O2']:.0f}%",
        "s6_noqos": f"{s6['B0']:.0f}%",
        "q_bulk": pct(n["cloud_gaming_to_bulk"]["qosify"]), "o_bulk": pct(n["cloud_gaming_to_bulk"]["ours"]),
        "acc": pct(n["accuracy"]["value"]), "f1": f"{n['macro_f1']['value']:.2f}",
        "rules_acc": pct(n["rules_accuracy"]["value"]), "rules_f1": f"{n['rules_accuracy']['macro_f1']:.2f}",
        "flows": f"{n['held_out']['flows']:,}", "sessions": f"{n['held_out']['sessions']}",
        "ms": f"{n['classify_ms']['value']:.1f}", "classes": n["classes"]["value"],
        "eq_better": f"{rv['wins'] + rv['ties']} of {rv['runs']}", "runs": rv["runs"],
        "wins": sum(v["verdict"] == "better" for v in verdicts.values()),
        "ties": sum(v["verdict"] == "tie" for v in verdicts.values()),
        "worse_rules": n["runs_worse_than_rules"]["value"],
        "drop_ms": f"{n['s2_reaction_ms']['value']:.0f}",
        "wd": f"{n['watchdog_s']['min']:.2f}", "wd_limit": f"{n['watchdog_s']['limit']:.0f}",
        "valid": f"{n['runs_valid']['valid']} of {n['runs_valid']['runs']}",
        "tests": n["unit_tests"]["value"],
        "sharing_at": n["s6_timeline"]["sharing_starts_s"],
        "recall": n["recall"]["value"],
    }


HEADLINE = "A small classifier tells CAKE which flow is the call"
MESSAGE = ("So calls and games stay usable when they share a device with downloads, "
           "where fair queueing and threshold rules both fail")
CLAIM_WIN = "where a call or game shares its device with bulk traffic or the line speed changes"
CLASS_NAMES = {"multimedia_conferencing": "Video call", "realtime_interactive": "Game", "standard": "Web",
               "multimedia_streaming": "Streaming", "low_priority_data": "Background", "high_throughput_data": "Download",
               "telephony": "Voice call"}
SHORT = {"S1": "Call +\ndownload", "S2": "Line\ndrops", "S3": "TVs +\nconsole", "S4": "Game +\nbackup",
         "S5": "Busy\nevening", "S6": "Call +\nfile sharing", "S7": "Game +\nupdate", "S8": "Flows\nchange",
         "S10": "Controller\nkilled"}
PIPELINE = lambda f: [("First 8 packets", "sizes and gaps, no payload"),  # noqa: E731
                      ("LightGBM", f"{f['classes']} classes, {f['ms']} ms"),
                      ("Least harm", "lowest expected damage"),
                      ("Safety shield", "bounded changes"),
                      ("CAKE tin", "Voice, Video, Best Effort or Bulk")]


def scenario_order(n):
    v = n["scenario_verdicts"]["value"]
    return [(s, v[s]) for s in sorted(v, key=lambda s: int(s[1:]))]


# ---------------------------------------------------------------- shared pieces

def chip(c, f, x, y, w=2.48, h=0.34, fill=CHIP, ink=CHIP_INK):
    b = c.box(x, y, w, h, fill=fill, shape=S.ROUNDED_RECTANGLE, radius=0.5, name="Interim")
    c.label(b, f["interim_text"], size=10, bold=True, color=ink)
    return b


def scenario_dots(c, n, x0, y, step, on=OURS, off=FAINT, ink=MUTED, track=SEG, hollow=WHITE, size=0.26):
    order = scenario_order(n)
    c.line(x0, y, x0 + step * (len(order) - 1), y, color=track, width=1.5)
    for i, (s, v) in enumerate(order):
        win = v["verdict"] == "better"
        x = x0 + i * step
        c.box(x - size / 2, y - size / 2, size, size, fill=on if win else hollow, line=on if win else off,
              shape=S.OVAL, line_w=1.5)
        c.text(x - step / 2 - 0.02, y + size / 2 + 0.03, step + 0.04, 0.34, SHORT.get(s, s).split("\n"), size=9,
               color=on if win else ink, bold=win, align=CENTER)


def footer(c, n, f, link, y=6.93, dark=False, guards=True, width=9.9, qr_x=12.2):
    ink, muted, faint = (WHITE, NIGHT_MUTED, NIGHT_MUTED) if dark else (INK, MUTED, FAINT)
    dot = ("   ·   ", {"color": faint})
    if guards:
        c.text(0.5, y, width, 0.22,
               [[(f"Line drop 100 to 20 Mbit/s caught in {f['drop_ms']} ms", {}), dot,
                 (f"Watchdog back to plain CAKE in {f['wd']} s (limit {f['wd_limit']} s)", {}), dot,
                 ("QoE check rolls back bad changes", {}), dot, (f"{f['tests']} unit tests", {})]],
               size=9.5, color=ink)
    status = (f"Interim: {f['seeds']} of {f['planned']} seeds. " if f["interim"] else f"{f['seeds']} seeds. ") + \
        (f"{f['valid']} runs valid in an emulated home testbed on Linux CAKE; rootless, one command to reproduce; "
         "not yet on router hardware")
    c.text(0.5, y + 0.22, width, 0.22, status, size=9, color=muted)
    link_block(c, link, qr_x, y - 0.15, dark=dark)


def link_block(c, link, qr_x, y, dark=False, size=0.62, text_w=1.8):
    pic = c.picture(qr_png(link), qr_x, y, size, size)
    c.text(qr_x - text_w - 0.1, y + 0.02, text_w, size - 0.04,
           [[("How it works, in full", {"bold": True, "size": 10})], [(link, {"size": 10, "color": OURS_LIGHT if dark else OURS})]],
           color=WHITE if dark else INK, align=RIGHT, anchor=MIDDLE)
    return pic


# ---------------------------------------------------------------- native charts

def _chart_text(chart, color=MUTED, size=9):
    chart.font.name = FONT
    chart.font.size = Pt(size)
    chart.font.color.rgb = rgb(color)


def _quiet_axis(axis, color=MUTED, size=9, visible=True):
    axis.visible = visible
    axis.has_major_gridlines = False
    axis.has_minor_gridlines = False
    axis.format.line.fill.background()
    axis.tick_labels.font.size = Pt(size)
    axis.tick_labels.font.color.rgb = rgb(color)
    axis.tick_labels.font.name = FONT


def plot_layout(chart, x, y, w, h):
    """Pin the inner plot area to fractions of the chart frame, so shapes can
    be placed on the data."""
    plot_area = chart._chartSpace.find(qn("c:chart")).find(qn("c:plotArea"))
    old = plot_area.find(qn("c:layout"))
    if old is not None:
        plot_area.remove(old)
    xml = (f'<c:layout xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"><c:manualLayout>'
           f'<c:layoutTarget val="inner"/><c:xMode val="edge"/><c:yMode val="edge"/>'
           f'<c:x val="{x}"/><c:y val="{y}"/><c:w val="{w}"/><c:h val="{h}"/></c:manualLayout></c:layout>')
    plot_area.insert(0, etree.fromstring(xml))


COST_ROWS = [("No QoS", "B0", NOQOS), ("Plain CAKE", "B2", CAKE), ("Threshold rules", "B4", RULES),
             ("Ours, simple rule", "O1", OURS_MID), ("Ours, full system", "O2", OURS)]


def cost_chart(c, n, x, y, w, h, dark=False, label_size=10, rows=COST_ROWS, ours=OURS):
    t = n["total_cost"]["value"]
    rows = [(name, t[k], ours if k == "O2" else colour) for name, k, colour in rows][::-1]
    data = CategoryChartData()
    data.categories = [r[0] for r in rows]
    data.add_series("Flows left unusable", [round(r[1], 2) for r in rows])
    gf = c.slide.shapes.add_chart(XL_CHART_TYPE.BAR_CLUSTERED, Inches(x), Inches(y), Inches(w), Inches(h), data)
    gf.name = "Flows left unusable"
    chart = gf.chart
    ink = WHITE if dark else INK
    _chart_text(chart, NIGHT_MUTED if dark else MUTED)
    chart.has_legend = False
    chart.has_title = False
    plot = chart.plots[0]
    plot.gap_width = 45
    plot.vary_by_categories = False
    plot.has_data_labels = True
    labels = plot.data_labels
    labels.number_format, labels.number_format_is_linked = "0.00", False
    labels.position = XL_LABEL_POSITION.OUTSIDE_END
    labels.font.size, labels.font.name, labels.font.bold = Pt(label_size), FONT, True
    labels.font.color.rgb = rgb(ink)
    series = plot.series[0]
    for i, (_, _, colour) in enumerate(rows):
        point = series.points[i]
        point.format.fill.solid()
        point.format.fill.fore_color.rgb = rgb(colour)
    _quiet_axis(chart.category_axis, color=ink, size=label_size)
    _quiet_axis(chart.value_axis, visible=False)
    chart.value_axis.maximum_scale = max(r[1] for r in rows) * 1.18
    chart.value_axis.minimum_scale = 0
    return chart


def timeline_chart(c, n, x, y, w, h, dark=False, legend=True, width=2.25, ours=OURS, layout=None, grid=LINE):
    tl = n["s6_timeline"]["value"]
    data = XyChartData()
    for config, label in (("B2", "Plain CAKE"), ("O2", "Ours")):
        series = data.add_series(label)
        for t, v in sorted(((int(t), v) for t, v in tl[config].items())):
            series.add_data_point(t, round(v, 1))
    gf = c.slide.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER_LINES_NO_MARKERS, Inches(x), Inches(y), Inches(w),
                                  Inches(h), data)
    gf.name = "S6 late frames per second"
    chart = gf.chart
    muted, ink = (NIGHT_MUTED, WHITE) if dark else (MUTED, INK)
    _chart_text(chart, muted)
    chart.has_title = False
    chart.has_legend = legend
    if legend:
        chart.legend.position = XL_LEGEND_POSITION.TOP
        chart.legend.include_in_layout = False
        chart.legend.font.size = Pt(9.5)
        chart.legend.font.color.rgb = rgb(ink)
    for series, colour in zip(chart.plots[0].series, (CAKE, ours)):
        series.format.line.color.rgb = rgb(colour)
        series.format.line.width = Pt(width)
        series.smooth = False
    va, ca = chart.value_axis, chart.category_axis
    _quiet_axis(va, color=muted)
    va.minimum_scale, va.maximum_scale, va.major_unit = 0, 100, 50
    va.tick_labels.number_format, va.tick_labels.number_format_is_linked = '0"%"', False
    va.has_major_gridlines = True
    va.major_gridlines.format.line.color.rgb = rgb(NIGHT_LINE if dark else grid)
    va.major_gridlines.format.line.width = Pt(0.5)
    _quiet_axis(ca, color=muted)
    last = max(int(t) for t in tl["B2"])
    ca.minimum_scale, ca.maximum_scale, ca.major_unit = 0, last + 1, 50
    ca.tick_labels.number_format, ca.tick_labels.number_format_is_linked = '0" s"', False
    ca.format.line.color.rgb = rgb(NIGHT_LINE if dark else SEG)
    ca.tick_label_position = XL_TICK_LABEL_POSITION.LOW
    if layout:
        plot_layout(chart, *layout)
    return chart, last + 1


def cover(slide, info):
    for shape in slide.shapes:
        if not shape.has_text_frame:
            continue
        if shape.text_frame.text.strip() == "Case Study No." and info.get("case_study"):
            run = shape.text_frame.paragraphs[0].runs[-1]
            run.text = run.text.rstrip() + " " + str(info["case_study"])
        elif shape.is_placeholder and shape.name.startswith("Subtitle") and info.get("team"):
            tf = shape.text_frame
            lines = [line for line in [*info["team"], info.get("college", "")] if line]
            for i, line in enumerate(lines):
                p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
                p.alignment = CENTER
                r = p.add_run()
                r.text = line
                r.font.size = Pt(20)
                r.font.bold = i == len(lines) - 1 and bool(info.get("college"))


# ---------------------------------------------------------------- how it decides: classify, re-classify, place

TIN = {"voice": "Voice", "video": "Video", "best_effort": "Best Effort", "bulk": "Bulk"}
KIND = {"multimedia_conferencing": "video call", "telephony": "voice call", "low_priority_data": "background transfer",
        "realtime_interactive": "game", "standard": "web", "multimedia_streaming": "streaming",
        "high_throughput_data": "download"}


def mech(n):
    m = n["mechanism"]
    fl, rl = m["first_label"], m["relabel"]
    return {
        "first": f"{KIND[fl['class']]}, {fl['confidence']}% sure, into {TIN[fl['tin']]}",
        "first_conf": f"{fl['confidence']}%", "first_kind": KIND[fl["class"]], "first_tin": TIN[fl["tin"]],
        "re_from": f"{KIND[rl['first_class']]} ({rl['first_confidence']}%)", "re_from_tin": TIN[rl["first_tin"]],
        "re_after": f"{rl['after_s']:.1f} s", "re_to": KIND[rl["class"]], "re_to_tin": TIN[rl["tin"]],
        "re_story": (f"File sharing first looked like a {KIND[rl['first_class']]} ({rl['first_confidence']}%) and went "
                     f"to {TIN[rl['first_tin']]}. {rl['after_s']:.1f} s later it left a {KIND[rl['first_class']]}'s "
                     f"normal range, was asked again and moved to {TIN[rl['tin']]}"),
        "damage": m["damage"]["value"],
    }


STEPS = ("Classify", "Re-classify", "Place by least damage")


def step_text(f, m):
    return [
        (f"First 8 packets, sizes and gaps only, no payload. LightGBM names one of {f['classes']} classes in "
         f"{f['ms']} ms"),
        "Every flow is checked against its class's normal range and asked again when it drifts out",
        "Tin with the least expected damage: how sure it is of each class times the measured damage of each "
        "tin. A safety shield bounds every change",
    ]


def damage_rows(c, m, x, y, label_w=0.95, cell=0.62, h=0.24, dark=False, size=9):
    """Two rows of the measured damage table, the tin the engine picks filled."""
    ink, muted = (WHITE, NIGHT_MUTED) if dark else (INK, MUTED)
    tins = ["voice", "video", "best_effort", "bulk"]
    heads = ["Voice", "Video", "B. Effort", "Bulk"]
    for j, t in enumerate(heads):
        c.text(x + label_w + j * cell, y, cell, h, t, size=size, color=muted, align=CENTER, anchor=MIDDLE)
    for i, (cls, label) in enumerate((("multimedia_conferencing", "Video call"), ("low_priority_data", "File sharing"))):
        row = m["damage"][cls]
        best = min(tins, key=lambda t: row[t])
        yy = y + (i + 1) * (h + 0.03)
        c.text(x, yy, label_w - 0.05, h, label, size=size, color=ink, bold=True, anchor=MIDDLE)
        for j, t in enumerate(tins):
            pick = t == best
            b = c.box(x + label_w + j * cell + 0.02, yy, cell - 0.04, h, fill=OURS if pick else (NIGHT_2 if dark else WHITE),
                      line=None if pick else (NIGHT_LINE if dark else LINE), shape=S.ROUNDED_RECTANGLE, radius=0.3)
            c.label(b, f"{row[t]:g}", size=size, bold=pick, color=WHITE if pick else ink)


def relabel_line(c, m, x, y, w, dark=False, size=9):
    """A flow's labels over time: first label, then the recheck that moved it."""
    ink, muted, track = (WHITE, NIGHT_MUTED, NIGHT_LINE) if dark else (INK, MUTED, SEG)
    c.line(x + 0.1, y + 0.1, x + w - 0.1, y + 0.1, color=track, width=1.5)
    c.box(x, y, 0.2, 0.2, fill=WHITE if not dark else NIGHT, line=OURS_LIGHT if dark else OURS, shape=S.OVAL, line_w=1.75)
    c.box(x + w - 0.2, y, 0.2, 0.2, fill=OURS_LIGHT if dark else OURS, shape=S.OVAL)
    c.text(x + w / 2 - 0.6, y - 0.22, 1.2, 0.2, f"+{m['re_after']}", size=size, bold=True,
           color=OURS_LIGHT if dark else OURS, align=CENTER)
    c.text(x - 0.05, y + 0.24, w / 2, 0.4, [[(m["re_from"], {"bold": True, "color": ink})], [(f"to {m['re_from_tin']}", {})]],
           size=size, color=muted)
    c.text(x + w / 2, y + 0.24, w / 2 + 0.05, 0.4, [[(f"re-asked: {m['re_to']}", {"bold": True, "color": ink})],
           [(f"to {m['re_to_tin']}", {})]], size=size, color=muted, align=RIGHT)


def mechanism(c, n, f, x, y, w, dark=False, visuals=True, title=None, gap=0.3):
    """Classify, re-classify, place by least damage: three columns, each with a
    real example from the runs."""
    m = mech(n)
    ink, muted = (WHITE, NIGHT_MUTED) if dark else (INK, MUTED)
    accent = OURS_LIGHT if dark else OURS
    col = (w - 2 * gap) / 3
    texts = step_text(f, m)
    if title:
        c.text(x, y - 0.32, w, 0.26, title, size=10, bold=True, color=accent)
    for i, (head, body) in enumerate(zip(STEPS, texts)):
        x0 = x + i * (col + gap)
        dot = c.box(x0, y, 0.3, 0.3, fill=accent, shape=S.OVAL)
        c.label(dot, str(i + 1), size=11, bold=True, color=NIGHT if dark else WHITE)
        c.text(x0 + 0.4, y - 0.01, col - 0.4, 0.32, head, size=12, bold=True, color=ink, anchor=MIDDLE)
        c.text(x0, y + 0.36, col, 0.46, body, size=9.5 if visuals else 9, color=muted)
        if i < 2:
            c.text(x0 + col + 0.02, y, gap - 0.04, 0.3, "→", size=14, bold=True, color=FAINT, align=CENTER,
                   anchor=MIDDLE)
        if not visuals:
            continue
        vy = y + 0.86
        if i == 0:
            laptop_y = vy + 0.05
            c.text(x0, laptop_y, col, 0.5, [[("The call: ", {"bold": True, "color": ink}),
                   (m["first_kind"], {"bold": True, "color": accent}), (f", {m['first_conf']} sure", {}),
                   (f", into {m['first_tin']} in its first moments", {})],
                   [(f"{f['acc']} accuracy, macro-F1 {f['f1']} on {f['flows']} held-out flows", {})]],
                   size=9, color=muted)
        elif i == 1:
            relabel_line(c, m, x0 + 0.05, vy + 0.25, col - 0.1, dark=dark)
        else:
            damage_rows(c, m, x0, vy - 0.02, label_w=0.95, cell=min(0.62, (col - 0.95) / 4), dark=dark)


def reclass_loop(c, start, end, color=OURS, label=None, label_at=None, dark=False):
    """A dashed arrow back to the classifier: flows are asked again."""
    (x0, y0), (x1, y1) = start, end
    pts = cubic(start, (x0 - 0.5, y0 - 0.6), (x1 + 0.6, y1 - 0.55), end, n=30)
    c.path(pts, color, 1.5, dash=True, arrow=True)
    if label:
        lx, ly = label_at
        c.text(lx, ly, 1.3, 0.22, label, size=9.5, bold=True, color=color, align=CENTER)
