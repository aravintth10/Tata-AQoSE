"""Ten designs for slide 2 of the Round 1 submission, all drawn from the same
numbers. Each takes (slide, numbers, link); build_slide.py picks one."""

import math
import random

from pptx.oxml.ns import qn
from lxml import etree

from slide_kit import (BOTTOM, CAKE, CENTER, CHIP, CLAIM_WIN, CLASS_NAMES, FAINT, HEADLINE, INK, LEFT, LINE, MESSAGE,
                       MIDDLE, MUTED, NIGHT, NIGHT_2, NIGHT_LINE, NIGHT_MUTED, NOQOS, OURS, OURS_LIGHT, OURS_MID,
                       OURS_TINT, PIPELINE, RIGHT, RULES, RULES_TINT, S, SEG, SERIF, SHORT, SUN, TOP, WASH, WASH_SUN,
                       WHITE, Canvas, bezier, blob, chip, cost_chart, cubic, drop_placeholders, facts, footer, laptop,
                       link_block, mech, mechanism, relabel_line, damage_rows, scenario_dots, scenario_order,
                       send_to_back, timeline_chart, STEPS)


def _steps(c, n, f, x, y, w, row_h=0.62, dark=False, numerals=False, font=None, size=9.5, head_size=11.5):
    """Classify, re-classify, place by least damage, as a numbered list with real examples."""
    m = mech(n)
    ink, muted, accent = (WHITE, NIGHT_MUTED, OURS_LIGHT) if dark else (INK, MUTED, OURS)
    bodies = [f"First 8 packets, sizes and gaps, no payload. LightGBM, {f['classes']} classes, {f['ms']} ms; "
              f"{f['acc']} accuracy, macro-F1 {f['f1']} on {f['flows']} held-out flows",
              f"Checked against its class's normal range. File sharing first looked like a {m['re_from']}; "
              f"{m['re_after']} later it was re-asked and moved to Bulk",
              "Tin with the least expected damage: class chances times measured damage. A safety shield bounds "
              "every change"]
    for i, (head, body) in enumerate(zip(STEPS, bodies)):
        yy = y + i * row_h
        if numerals:
            c.text(x, yy - 0.05, 0.45, 0.5, str(i + 1), size=28, bold=True, font=font or SERIF, color=OURS_MID)
        else:
            dot = c.box(x, yy + 0.02, 0.28, 0.28, fill=accent, shape=S.OVAL)
            c.label(dot, str(i + 1), size=10.5, bold=True, color=NIGHT if dark else WHITE)
        c.text(x + 0.45, yy, w - 0.45, row_h, [[(head, {"bold": True, "size": head_size, "color": ink})],
               [(body, {})]], size=size, color=muted)


def _misfiled(c, n, lane_x, voice_y, bulk_y, x_from, x_to, dark=False):
    """The re-classify step drawn in the tins: a flow first filed in Voice,
    asked again when it drifted, and moved to Bulk."""
    m = mech(n)
    grey, accent = ("5A6690", OURS_LIGHT) if dark else (FAINT, OURS)
    c.path([(x_from, voice_y), (x_to, voice_y)], grey, 1.6, dash=True)
    c.path(cubic((x_to, voice_y), (x_to + 0.55, voice_y), (x_to + 0.55, bulk_y - 0.05), (x_to + 0.1, bulk_y - 0.05),
                 n=24), accent, 1.75, arrow=True)
    be_y = voice_y + 2 * (bulk_y - voice_y) / 3  # the Best Effort lane, empty in this picture
    c.text(x_to + 0.5, be_y - 0.13, 1.7, 0.26, [[("re-classified ", {"bold": True}), (f"+{m['re_after']}", {})]],
           size=9, color=accent, anchor=MIDDLE)
    return m


def _start(slide, n):
    drop_placeholders(slide)
    return Canvas(slide), facts(n)


def _title(c, f, size=30, width=9.8, color=INK, sub=MUTED, chip_at=(10.35, 1.02), y=0.92, font=None):
    kw = {"font": font} if font else {}
    c.text(0.5, y, width, 0.6, [[("A small classifier tells CAKE which flow is ", {}), ("the call", {"color": OURS})]],
           size=size, bold=True, color=color, name="Title", **kw)
    c.text(0.5, y + 0.58, 12.3, 0.36, MESSAGE, size=14, color=sub)
    if chip_at:
        chip(c, f, *chip_at)


def _alpha(shape, percent):
    """Make a solid fill partly see-through (percent opaque)."""
    fill = shape.fill._xPr.find(qn("a:solidFill"))
    clr = fill[0]
    a = etree.SubElement(clr, qn("a:alpha"))
    a.set("val", str(int(percent * 1000)))


def _stat(c, x, y, w, big, caption, big_size=30, color=OURS, cap_size=9.5, cap_color=MUTED, h_big=0.62, h_cap=0.5,
          align=LEFT, font=None, bold_caption=None):
    kw = {"font": font} if font else {}
    runs = big if isinstance(big, list) else [(big, {"color": color})]
    c.text(x, y, w, h_big, [runs], size=big_size, bold=True, anchor=BOTTOM, align=align, **kw)
    paras = []
    if bold_caption:
        paras.append([(bold_caption, {"bold": True, "size": cap_size + 1.5, "color": INK if cap_color == MUTED else cap_color})])
    paras.append([(caption, {})])
    c.text(x, y + h_big + 0.02, w, h_cap, paras, size=cap_size, color=cap_color, align=align)


def _flows(c, start_x, start_ys, end, color=SEG, call_color=OURS, call_index=0, width=1.25, call_width=3.0,
           spread=0.02):
    """Lines from one device to a point; the call drawn last, on top."""
    order = [i for i in range(len(start_ys)) if i != call_index] + [call_index]
    for k, i in enumerate(order):
        call = i == call_index
        c.path(bezier((start_x, start_ys[i]), (end[0], end[1] - spread * 5 + spread * (i % 11))),
               call_color if call else color, call_width if call else width)


# ======================================================================= 1. Flow

def d01_flow(slide, n, link):
    c, f = _start(slide, n)
    blob(c, 6.4, 3.05, 6.6, 1.35, WASH)
    blob(c, 12.4, 1.9, 1.3, 0.75, WASH_SUN, wobble=(0.08, 0.05, 0.03))
    _title(c, f)

    mid = 3.0
    lx, ly = 0.75, mid - 0.4
    laptop(c, lx, ly)
    c.text(0.45, ly + 0.86, 1.65, 0.48, [[("One laptop", {"bold": True})],
           [("a video call + 10 file-sharing connections", {"color": MUTED, "size": 9})]], size=10, align=CENTER)
    node_x, node_r = 5.75, 0.6
    _flows(c, lx + 1.02, [mid - 0.3 + 0.06 * i for i in range(11)], (node_x - node_r + 0.02, mid))
    c.text(2.15, 1.86, 2.3, 0.7, [[("1/11", {"bold": True, "size": 24, "color": OURS})],
                                  [("of the laptop's share is all fair queueing gives the call", {"size": 10})]],
           anchor=BOTTOM)
    c.text(2.15, 3.36, 2.5, 1.0,
           [[("Threshold rules: ", {"bold": True}), ("over 200 packets/s means Bulk, and HD calls send 400 to 600", {})],
            [(f["q_bulk"], {"bold": True, "size": 16, "color": RULES}), (" qosify    ", {}),
             (f["o_bulk"], {"bold": True, "size": 16, "color": OURS}), (" ours", {})],
            [("of cloud-game flows sent to Bulk", {"size": 9, "color": MUTED})]], size=9.5)

    node = c.box(node_x - node_r, mid - node_r, 2 * node_r, 2 * node_r, fill=OURS, shape=S.OVAL)
    c.label(node, [[("LightGBM", {"bold": True, "size": 13})], [(f"{f['classes']} classes", {})], [(f"{f['ms']} ms", {})]],
            size=9.5, color=WHITE)
    c.text(node_x - 0.85, mid - node_r - 0.5, 1.7, 0.45, [[("First 8 packets", {"bold": True})],
           [("sizes and gaps, no payload", {"color": MUTED, "size": 9})]], size=10, align=CENTER, anchor=BOTTOM)
    c.text(node_x - 1.05, mid + node_r + 0.06, 2.1, 0.75,
           [[(f["acc"], {"bold": True, "color": OURS, "size": 12}), (" accuracy   ", {}),
             (f["f1"], {"bold": True, "color": OURS, "size": 12}), (" macro-F1", {})],
            [(f"on {f['flows']} held-out flows. Video calls {_pct(f, 'multimedia_conferencing')}, games "
              f"{_pct(f, 'realtime_interactive')} found", {"color": MUTED, "size": 9})]], size=9.5, align=CENTER)

    lane_x, lane_w, lane_h = 8.05, 4.78, 0.34
    tins = ["Voice", "Video", "Best Effort", "Bulk"]
    lane_y = {t: mid - 0.86 + i * 0.44 for i, t in enumerate(tins)}
    for t in tins:
        lane = c.box(lane_x, lane_y[t], lane_w, lane_h, fill=WHITE, line=LINE, shape=S.ROUNDED_RECTANGLE, radius=0.5)
        c.label(lane, t, size=9.5, bold=True, color=CAKE, align=RIGHT, margin=0.14)
    c.text(lane_x, lane_y["Voice"] - 0.3, 2.5, 0.26, "CAKE's four tins", size=10, bold=True, color=CAKE)
    video_y, bulk_y = lane_y["Video"] + lane_h / 2, lane_y["Bulk"] + lane_h / 2
    out_x, end_x = node_x + node_r - 0.02, lane_x + lane_w - 2.15
    for i in range(10):
        y1 = bulk_y - 0.09 + 0.02 * i
        c.path(bezier((out_x, mid - 0.05 + 0.01 * i), (lane_x + 1.1, y1)) + [(end_x, y1)], SEG, 1.25)
    c.path(bezier((out_x, mid), (lane_x + 1.1, video_y)) + [(end_x, video_y)], OURS, 3.0)
    c.text(end_x + 0.07, lane_y["Video"], 1.0, lane_h, "the call", size=10, bold=True, color=OURS, anchor=MIDDLE)
    c.text(end_x + 0.07, lane_y["Bulk"], 1.0, lane_h, "file sharing", size=9.5, color=MUTED, anchor=MIDDLE)
    m = _misfiled(c, n, lane_x, lane_y["Voice"] + lane_h / 2, bulk_y, lane_x + 0.3, lane_x + 1.55)
    c.text(lane_x, lane_y["Bulk"] + lane_h + 0.06, lane_w, 0.62,
           [[("Re-classify  ", {"bold": True, "color": OURS}), (m["re_story"], {})],
            [("Place  ", {"bold": True, "color": OURS}), ("the tin with least expected damage: class chances times "
              "measured damage, then a safety shield", {})]], size=9, color=INK)

    top = 4.55
    c.text(0.45, top - 0.12, 2.6, 0.95, f["less"], size=66, bold=True, color=OURS, anchor=MIDDLE)
    c.text(0.5, top + 0.84, 2.45, 0.7, [[("less damage than plain CAKE", {"bold": True, "size": 12})],
           [(f"flows left unusable over {f['scenarios']} home scenarios, {f['total']['B2']} down to "
             f"{f['total']['O2']}", {"size": 9, "color": MUTED})]], size=12)
    cost_chart(c, n, 2.95, top - 0.12, 3.6, 1.72)
    c.text(6.85, top - 0.05, 2.4, 0.7, [[(f["s6_cake"], {"color": CAKE}), (" → ", {"color": FAINT}),
                                        (f["s6_ours"], {"color": OURS})]], size=36, bold=True, anchor=MIDDLE)
    c.text(6.9, top + 0.66, 2.2, 0.9, [[("late frames on the HD call", {"bold": True, "size": 12})],
           [(f"while its laptop shares files. Median of {f['seeds']} seeds; the chart is one run, sharing starts at "
             f"{f['sharing_at']} s", {"size": 9, "color": MUTED})]], size=12)
    timeline_chart(c, n, 9.15, top - 0.12, 3.7, 1.62)

    sy = 6.38
    scenario_dots(c, n, 3.35, sy, 0.78)
    c.text(0.5, sy - 0.3, 2.65, 0.8, [[("Wins ", {"bold": True, "color": OURS}), (CLAIM_WIN + ". ", {}),
           ("Ties ", {"bold": True, "color": MUTED}), ("where plain CAKE already copes", {})]], size=9.5, anchor=MIDDLE)
    c.text(10.4, sy - 0.32, 2.45, 0.84, [[(f["eq_better"], {"bold": True, "size": 18, "color": OURS})],
           [("runs equal or better than plain CAKE. ", {}),
            (f"{f['worse_rules']} worse than threshold rules", {"bold": True})]], size=9.5, anchor=MIDDLE)
    footer(c, n, f, link)


def _pct(f, cls):
    return f"{100 * f['recall'][cls]:.0f}%"


# ======================================================================= 2. Split

def d02_split(slide, n, link):
    c, f = _start(slide, n)
    _title(c, f, size=28)
    top, bottom, seam = 2.0, 5.88, 13.333 / 2
    send_to_back(c.box(0, top, seam, bottom - top, fill="F1F3F6", name="Today"))
    send_to_back(c.box(seam, top, 13.333 - seam, bottom - top, fill="F0EDFD", name="With ours"))

    def side(x0, ours):
        c.text(x0, top + 0.12, 5.6, 0.25, "WITH OUR CLASSIFIER" if ours else "TODAY", size=10, bold=True,
               color=OURS if ours else MUTED)
        c.text(x0, top + 0.36, 5.6, 0.34, "CAKE is told which flow is the call" if ours else
               "Plain CAKE, or CAKE with threshold rules", size=16, bold=True)
        lx, ly = x0 + 0.15, top + 0.95
        laptop(c, lx, ly, scale=0.9)
        lanes_x = x0 + 2.75 if not ours else x0 + 2.45
        names = ["Video", "Best Effort", "Bulk"]
        ys = {t: top + 0.85 + i * 0.36 for i, t in enumerate(names)}
        for t in names:
            lane = c.box(lanes_x, ys[t], 2.75, 0.27, fill=WHITE, line=LINE, shape=S.ROUNDED_RECTANGLE, radius=0.5)
            c.label(lane, t, size=9, bold=True, color=CAKE, align=RIGHT, margin=0.1)
        starts = [ly + 0.08 + 0.05 * i for i in range(11)]
        sx = lx + 0.93
        if ours:
            for i in range(1, 11):
                yb = ys["Bulk"] + 0.135 - 0.05 + 0.01 * i
                c.path(bezier((sx, starts[i]), (lanes_x + 0.2, yb)) + [(lanes_x + 1.6, yb)], SEG, 1.1)
            yv = ys["Video"] + 0.135
            c.path(bezier((sx, starts[0]), (lanes_x + 0.2, yv)) + [(lanes_x + 1.6, yv)], OURS, 3.0)
        else:
            for i in range(11):
                yb = ys["Best Effort"] + 0.135 - 0.05 + 0.01 * i
                c.path(bezier((sx, starts[i]), (lanes_x + 0.2, yb)) + [(lanes_x + 1.6, yb)],
                       CAKE if i == 5 else SEG, 1.6 if i == 5 else 1.1)
        c.text(lx - 0.25, ly + 0.72, 1.4, 0.4, "call + 10 file-sharing connections", size=9, color=MUTED, align=CENTER)

    side(0.5, False)
    side(seam + 0.8, True)
    # Mirrored evidence rows.
    rows = [((f["s6_cake"], CAKE), "of the HD call's frames late, plain CAKE and threshold rules alike",
             (f["s6_ours"], OURS), "late frames on the same call"),
            ((f["q_bulk"], RULES), "of cloud-game flows sent to Bulk by qosify's rules",
             (f["o_bulk"], OURS), "of cloud-game flows sent to Bulk"),
            ((f["total"]["B2"], CAKE), f"flows left unusable over {f['scenarios']} scenarios (rules {f['total']['B4']})",
             (f["total"]["O2"], OURS), f"flows left unusable, {f['less']} less. {f['eq_better']} runs equal or "
             "better than plain CAKE; ties where it already copes")]
    for i, ((lb, lc), lt, (rb, rc), rt) in enumerate(rows):
        y = top + 2.15 + i * 0.55
        size = 40 if i == 0 else 26
        for x0, big, colour, text in ((0.5, lb, lc, lt), (seam + 0.8, rb, rc, rt)):
            c.text(x0, y - (0.2 if i == 0 else 0), 1.85, 0.62 if i == 0 else 0.5, big, size=size, bold=True,
                   color=colour, anchor=MIDDLE)
            c.text(x0 + 1.9, y, 3.75, 0.5, text, size=10.5, color=INK, anchor=MIDDLE)
    node = c.box(seam - 0.66, top + 0.82, 1.32, 1.32, fill=OURS, line=WHITE, shape=S.OVAL, line_w=3)
    c.label(node, [[("LightGBM", {"bold": True, "size": 12})], [("first 8 packets", {})], [(f"{f['ms']} ms", {})]],
            size=9, color=WHITE)
    c.text(seam - 0.6, top + 2.18, 1.2, 0.42, [[(f"{f['acc']} accurate", {"bold": True})], [(f"macro-F1 {f['f1']}", {})]],
           size=9, color=OURS, align=CENTER)
    mechanism(c, n, f, 0.5, 6.02, 12.33, visuals=False)
    footer(c, n, f, link)


# ======================================================================= 3. Giant number

def d03_giant(slide, n, link):
    c, f = _start(slide, n)
    chip(c, f, 10.35, 1.02)
    c.text(0.5, 0.92, 9.6, 0.55, [[("A small classifier tells CAKE which flow is ", {}), ("the call", {"color": OURS})]],
           size=26, bold=True, font=SERIF)
    c.text(0.5, 1.45, 6.3, 0.6, MESSAGE, size=12, color=MUTED)
    c.text(0.3, 1.85, 6.4, 2.55, f["less"], size=190, bold=True, color=OURS, anchor=MIDDLE)
    c.text(0.5, 4.3, 6.0, 0.45, "less damage than plain CAKE", size=24, bold=True, font=SERIF)
    c.text(0.5, 4.75, 6.0, 0.3, f"flows left unusable over {f['scenarios']} emulated home scenarios",
           size=11, color=MUTED)
    cost_chart(c, n, 0.4, 5.05, 6.1, 1.75, label_size=9.5)

    items = [
        ([(f["s6_cake"], {"color": CAKE}), (" → ", {"color": FAINT}), (f["s6_ours"], {"color": OURS})],
         "late frames on an HD call", "whose laptop is also sharing files on 10 connections"),
        ([(f["q_bulk"], {"color": RULES}), (" → ", {"color": FAINT}), (f["o_bulk"], {"color": OURS})],
         "cloud-game flows sent to Bulk", "qosify's threshold rules, then ours"),
        ([(f["eq_better"], {"color": OURS})], "runs equal or better than plain CAKE",
         f"{f['worse_rules']} worse than threshold rules. Wins {CLAIM_WIN}"),
    ]
    y = 1.95
    for big, head, body in items:
        c.line(7.0, y, 12.83, y, color=LINE, width=0.75)
        c.text(7.0, y + 0.05, 2.5, 0.7, [big], size=28, bold=True, anchor=MIDDLE)
        c.text(9.6, y + 0.05, 3.23, 0.7, [[(head, {"bold": True, "size": 11.5})], [(body, {"color": MUTED})]],
               size=9.5, anchor=MIDDLE)
        y += 0.8
    c.line(7.0, y, 12.83, y, color=LINE, width=0.75)
    c.text(7.0, y + 0.1, 5.8, 0.32, "How it decides", size=14, bold=True, font=SERIF, color=OURS)
    _steps(c, n, f, 7.0, y + 0.5, 5.83, row_h=0.66)
    footer(c, n, f, link)


# ======================================================================= 4. Timeline hero

def d04_timeline(slide, n, link):
    c, f = _start(slide, n)
    _title(c, f)
    x, y, w, h = 0.35, 1.95, 8.6, 2.95
    lay = (0.045, 0.1, 0.94, 0.78)
    chart, tmax = timeline_chart(c, n, x, y, w, h, legend=False, width=3.0, layout=lay)
    px = lambda t: x + w * (lay[0] + lay[2] * t / tmax)  # noqa: E731
    py = lambda v: y + h * (lay[1] + lay[3] * (1 - v / 100))  # noqa: E731
    xs = px(f["sharing_at"])
    c.line(xs, py(100), xs, py(0), color=FAINT, width=1, dash=True)
    c.text(xs + 0.1, py(78), 2.4, 0.45, "File sharing starts on the call's own laptop", size=10,
           color=MUTED)
    c.text(px(62), py(100) + 0.1, 5.5, 0.5, [[("Plain CAKE  ", {"size": 13}), (f"{f['s6_cake']}", {"size": 26}),
           ("  of the call's frames late", {"size": 13})]], bold=True, color=CAKE, anchor=TOP)
    c.text(px(62), py(0) - 0.85, 5.5, 0.5, [[("Ours  ", {"size": 13}), (f"{f['s6_ours']}", {"size": 26}),
           ("  late, same traffic", {"size": 13})]], bold=True, color=OURS, anchor=BOTTOM)
    c.text(px(3), py(100) - 0.32, 2.4, 0.25, "HD video call, frames late per second", size=10, bold=True)

    stats = [(f["less"], "less damage than plain CAKE", f"flows left unusable over {f['scenarios']} scenarios, "
              f"{f['total']['B2']} to {f['total']['O2']}"),
             ([(f["q_bulk"], {"color": RULES}), (" → ", {"color": FAINT}), (f["o_bulk"], {"color": OURS})],
              "cloud-game flows sent to Bulk", "qosify's threshold rules, then ours"),
             (f["eq_better"], "runs equal or better than plain CAKE",
              f"{f['worse_rules']} worse than threshold rules; wins {CLAIM_WIN}"),
             ([(f["acc"], {"color": OURS}), ("  ", {}), (f["f1"], {"color": OURS})], "accuracy, macro-F1",
              f"on {f['flows']} held-out recorded flows ({f['sessions']} sessions)")]
    short = [f"less damage than plain CAKE ({f['total']['B2']} to {f['total']['O2']} flows left unusable)",
             "cloud-game flows sent to Bulk, qosify then ours",
             f"runs equal or better than plain CAKE, {f['worse_rules']} worse than rules; ties where CAKE copes",
             f"accuracy, macro-F1 on {f['flows']} held-out flows"]
    for i, ((big, head, body), cap) in enumerate(zip(stats, short)):
        _stat(c, 9.25, 1.88 + i * 0.78, 3.6, big, cap, big_size=24, h_big=0.42, h_cap=0.34, cap_size=9)
    mechanism(c, n, f, 0.5, 5.12, 12.33)
    footer(c, n, f, link)


# ======================================================================= 5. Night

def d05_night(slide, n, link):
    c, f = _start(slide, n)
    send_to_back(c.box(0, 0.9, 13.333, 6.6, fill=NIGHT, name="Night"))
    glow = blob(c, 6.2, 3.3, 3.2, 1.3, "1B2658", name="Glow")
    c.text(0.5, 1.05, 9.8, 0.6, [[("A small classifier tells CAKE which flow is ", {}), ("the call", {"color": OURS_LIGHT})]],
           size=30, bold=True, color=WHITE)
    c.text(0.5, 1.63, 12.3, 0.36, MESSAGE, size=14, color=NIGHT_MUTED)
    chip(c, f, 10.35, 1.15, fill=SUN, ink=NIGHT)

    mid = 3.35
    laptop(c, 0.75, mid - 0.4, ink=WHITE, screen=NIGHT_2, person=OURS_LIGHT)
    c.text(0.4, mid + 0.48, 1.75, 0.5, [[("One laptop, 9 pm", {"bold": True})],
           [("a video call + 10 file-sharing connections", {"size": 9, "color": NIGHT_MUTED})]], size=10,
           color=WHITE, align=CENTER)
    node_x, r = 5.9, 0.62
    _flows(c, 1.77, [mid - 0.3 + 0.06 * i for i in range(11)], (node_x - r + 0.02, mid), color="3A4670",
           call_color=OURS_LIGHT)
    halo = c.box(node_x - r - 0.18, mid - r - 0.18, 2 * r + 0.36, 2 * r + 0.36, fill=OURS, shape=S.OVAL)
    _alpha(halo, 30)
    node = c.box(node_x - r, mid - r, 2 * r, 2 * r, fill=OURS, shape=S.OVAL)
    c.label(node, [[("LightGBM", {"bold": True, "size": 13})], [(f"first 8 packets", {})], [(f"{f['ms']} ms", {})]],
            size=9.5, color=WHITE)
    c.text(2.2, 2.25, 3.0, 0.62, [[("1/11", {"bold": True, "size": 22, "color": OURS_LIGHT}),
           ("  of the laptop's share: all fair queueing gives the call", {"size": 10})]], color=WHITE, anchor=BOTTOM)
    c.text(2.2, 3.85, 2.9, 0.62, [[(f["q_bulk"], {"bold": True, "size": 22, "color": RULES}),
           ("  of cloud-game flows sent to Bulk by threshold rules (over 200 packets/s)", {"size": 10})]],
           color=WHITE)
    lane_x, lane_w = 8.1, 4.73
    tins = ["Voice", "Video", "Best Effort", "Bulk"]
    ly = {t: mid - 0.85 + i * 0.44 for i, t in enumerate(tins)}
    for t in tins:
        lane = c.box(lane_x, ly[t], lane_w, 0.32, fill=NIGHT_2, line=NIGHT_LINE, shape=S.ROUNDED_RECTANGLE, radius=0.5)
        c.label(lane, t, size=9.5, bold=True, color="8FB8F0", align=RIGHT, margin=0.14)
    end_x = lane_x + lane_w - 2.1
    for i in range(10):
        y1 = ly["Bulk"] + 0.16 - 0.09 + 0.02 * i
        c.path(bezier((node_x + r - 0.02, mid - 0.05 + 0.01 * i), (lane_x + 1.1, y1)) + [(end_x, y1)], "3A4670", 1.25)
    yv = ly["Video"] + 0.16
    c.path(bezier((node_x + r - 0.02, mid), (lane_x + 1.1, yv)) + [(end_x, yv)], OURS_LIGHT, 3.0)
    c.text(end_x + 0.07, ly["Video"], 1.0, 0.32, "the call", size=10, bold=True, color=OURS_LIGHT, anchor=MIDDLE)
    c.text(end_x + 0.07, ly["Bulk"], 1.0, 0.32, "file sharing", size=9.5, color=NIGHT_MUTED, anchor=MIDDLE)
    m = _misfiled(c, n, lane_x, ly["Voice"] + 0.16, ly["Bulk"] + 0.16, lane_x + 0.3, lane_x + 1.55, dark=True)
    c.text(lane_x, ly["Bulk"] + 0.36, lane_w, 0.4,
           [[("Re-classify  ", {"bold": True, "color": OURS_LIGHT}),
             (f"file sharing first looked like a {m['re_from']}, moved to Bulk {m['re_after']} later. ", {}),
             ("Place  ", {"bold": True, "color": OURS_LIGHT}), ("tin with least expected damage, safety shield", {})]],
           size=9, color=NIGHT_MUTED)

    stats = [(f["less"], "less damage than plain CAKE", f"{f['total']['B2']} to {f['total']['O2']} flows left "
              f"unusable, {f['scenarios']} scenarios"),
             ([(f["s6_cake"], {"color": "7FB0F0"}), (" → ", {"color": NIGHT_MUTED}), (f["s6_ours"], {"color": OURS_LIGHT})],
              "late frames, HD call", "laptop also sharing files"),
             (f["eq_better"], "runs equal or better than CAKE", f"{f['worse_rules']} worse than threshold rules"),
             ([(f["acc"], {"color": OURS_LIGHT}), ("  ", {}), (f["f1"], {"color": OURS_LIGHT})], "accuracy, macro-F1",
              f"{f['flows']} held-out flows")]
    for i, (big, head, body) in enumerate(stats):
        x0 = 0.5 + i * 3.12
        runs = big if isinstance(big, list) else [(big, {"color": OURS_LIGHT})]
        c.text(x0, 4.55, 3.0, 0.65, [runs], size=34, bold=True, anchor=BOTTOM)
        c.text(x0, 5.22, 2.9, 0.5, [[(head, {"bold": True, "size": 11, "color": WHITE})], [(body, {})]], size=9.5,
               color=NIGHT_MUTED)
    scenario_dots(c, n, 3.35, 6.12, 0.78, on=OURS_LIGHT, off=NIGHT_MUTED, ink=NIGHT_MUTED, track=NIGHT_LINE,
                  hollow=NIGHT)
    c.text(0.5, 5.86, 2.6, 0.55, [[("Wins ", {"bold": True, "color": OURS_LIGHT}), (CLAIM_WIN, {}),
           (". Ties elsewhere", {})]], size=9, color=NIGHT_MUTED, anchor=MIDDLE)
    footer(c, n, f, link, dark=True)


# ======================================================================= 6. Orbit

def d06_orbit(slide, n, link):
    c, f = _start(slide, n)
    _title(c, f)
    cx, cy, rx, ry, node = 6.67, 4.0, 2.0, 1.6, 0.98
    blob(c, cx, cy, rx + 0.75, ry + 0.6, WASH, wobble=(0.03, 0.03, 0.02))
    recall = sorted(f["recall"].items(), key=lambda kv: -kv[1])
    for i, (cls, r) in enumerate(recall):
        a = -math.pi / 2 + 2 * math.pi * i / len(recall)
        x, y = cx + rx * math.cos(a), cy + ry * math.sin(a)
        c.line(cx, cy, x, y, color=OURS_MID, width=1.25)
    c.box(cx - 1.22, cy - 1.02, 2.44, 2.04, line=OURS, shape=S.OVAL, line_w=1.5, dash=True)
    c.text(cx - 0.45, cy + 1.04, 0.9, 0.22, "re-classify", size=9, bold=True, color=OURS, align=CENTER)
    for i, (cls, r) in enumerate(recall):
        a = -math.pi / 2 + 2 * math.pi * i / len(recall)
        x, y = cx + rx * math.cos(a), cy + ry * math.sin(a)
        strong = r >= 0.9
        b = c.box(x - node / 2, y - node / 2, node, node, fill=OURS if strong else WHITE, line=OURS, shape=S.OVAL,
                  line_w=1.5)
        c.label(b, [[(f"{100 * r:.0f}%", {"bold": True, "size": 12})], [(CLASS_NAMES[cls], {"size": 9})]],
                color=WHITE if strong else OURS, margin=0.02)
    hub = c.box(cx - 0.78, cy - 0.78, 1.56, 1.56, fill=INK, shape=S.OVAL)
    c.label(hub, [[("LightGBM", {"bold": True, "size": 14})], [("first 8 packets", {})], [("no payload", {})],
                  [(f"{f['ms']} ms", {})]], size=9.5, color=WHITE)
    m = mech(n)
    c.text(cx - 2.5, 6.05, 5.0, 0.8, [[("Recall by class on ", {}), (f["flows"], {"bold": True}),
           (" held-out flows. Accuracy ", {}), (f["acc"], {"bold": True}), (", macro-F1 ", {}),
           (f["f1"], {"bold": True})],
           [("Re-classify: ", {"bold": True, "color": OURS}), (f"asked again when a flow drifts out of its class "
             f"(file sharing first seen as a {m['re_from']}, moved to Bulk {m['re_after']} later). ", {}),
            ("Place: ", {"bold": True, "color": OURS}), ("tin with least expected damage, through a safety shield", {})]],
           size=9, color=MUTED, align=CENTER)

    c.text(0.5, 2.05, 3.6, 0.3, "THE GAP", size=10, bold=True, color=MUTED)
    gap = [("1/11", OURS, "of its laptop's share is all fair queueing gives a call next to 10 file-sharing connections"),
           (f["q_bulk"], RULES, "of cloud-game flows sent to Bulk by threshold rules, which call anything over 200 "
            f"packets/s bulk. Ours: {f['o_bulk']}"),
           (f["s6_cake"], CAKE, "of that call's frames arrive late under plain CAKE")]
    for i, (big, colour, text) in enumerate(gap):
        _stat(c, 0.5, 2.3 + i * 1.3, 3.6, big, text, big_size=34, color=colour, cap_size=10, cap_color=INK)
    c.text(9.25, 2.05, 3.6, 0.3, "WHAT IT ACHIEVED", size=10, bold=True, color=OURS, align=RIGHT)
    won = [(f["less"], "less damage than plain CAKE", f"flows left unusable over {f['scenarios']} scenarios: "
            f"{f['total']['B2']} to {f['total']['O2']}"),
           (f["s6_ours"], "late frames on the same call", "picked out and put in the Video tin through a safety shield"),
           (f["eq_better"], "runs equal or better than plain CAKE",
            f"{f['worse_rules']} worse than threshold rules. Wins {CLAIM_WIN}; ties elsewhere")]
    for i, (big, head, text) in enumerate(won):
        _stat(c, 9.25, 2.3 + i * 1.3, 3.58, big, text, big_size=34, bold_caption=head, align=RIGHT, cap_size=9.5,
              h_cap=0.68)
    footer(c, n, f, link)


# ======================================================================= 7. Road

def d07_road(slide, n, link):
    c, f = _start(slide, n)
    _title(c, f)
    p = [(-0.4, 4.15), (4.2, 5.0), (8.6, 2.95), (13.8, 3.75)]
    k = 120
    road = cubic(*p, n=k)
    c.path(road, "D6E6FA", 40)
    c.path([(x, y + 0.3) for x, y in road], SUN, 2.0)
    c.path(road, WHITE, 2.0, dash=True)
    at = lambda x: min(road, key=lambda q: abs(q[0] - x))  # noqa: E731
    m = mech(n)
    steps = [(1.4, "1", INK, "7 pm, one laptop",
              "A call and 10 file-sharing connections. Fair queueing gives the call a slice of the laptop",
              [("1/11", {"color": OURS})], "of the laptop's share"),
             (3.95, "2", RULES, "Threshold rules guess",
              "Over 200 packets/s means Bulk, but HD calls send 400 to 600 and cloud games 1,000+",
              [(f["q_bulk"], {"color": RULES})], "of cloud games to Bulk (qosify)"),
             (6.5, "3", OURS, "Classify",
              f"First 8 packets, sizes and gaps, no payload. LightGBM names the flow in {f['ms']} ms",
              [(f["acc"], {"color": OURS}), ("  ", {}), (f["f1"], {"color": OURS})], "accuracy, macro-F1"),
             (9.05, "4", OURS, "Re-classify",
              f"Asked again when it drifts: file sharing first seen as a {m['re_from']}",
              [(f"+{m['re_after']}", {"color": OURS})], "re-asked, moved to Bulk"),
             (11.6, "5", OURS, "Place by least damage",
              "The tin with least expected damage, through a safety shield. The call goes to Video",
              [(f["s6_cake"], {"color": CAKE}), (" → ", {"color": FAINT}), (f["s6_ours"], {"color": OURS})],
              "late frames on the call")]
    for x, num, colour, head, body, big, cap in steps:
        _, y = at(x)
        dot = c.box(x - 0.25, y - 0.25, 0.5, 0.5, fill=colour, line=WHITE, shape=S.OVAL, line_w=2.5)
        c.label(dot, num, size=13, bold=True, color=WHITE)
        c.text(x - 1.22, 1.98, 2.44, 0.95, [[(head, {"bold": True, "size": 12, "color": colour})], [(body, {})]],
               size=9.5, align=CENTER, anchor=BOTTOM)
        c.text(x - 1.22, 4.95, 2.44, 0.55, [big], size=26, bold=True, align=CENTER, anchor=BOTTOM)
        c.text(x - 1.22, 5.5, 2.44, 0.25, cap, size=9.5, color=MUTED, align=CENTER)

    c.text(0.5, 5.95, 2.0, 0.75, f["less"], size=40, bold=True, color=OURS, anchor=MIDDLE)
    c.text(2.3, 6.0, 2.2, 0.7, [[("less damage than plain CAKE", {"bold": True, "size": 11})],
           [(f"{f['total']['B2']} to {f['total']['O2']} flows left unusable over {f['scenarios']} scenarios",
             {"size": 9, "color": MUTED})]], size=11, anchor=MIDDLE)
    scenario_dots(c, n, 5.0, 6.12, 0.62, size=0.22)
    c.text(10.4, 5.92, 2.45, 0.8, [[(f["eq_better"], {"bold": True, "size": 16, "color": OURS})],
           [(f"runs equal or better than CAKE, {f['worse_rules']} worse than rules", {})]], size=9.5, anchor=MIDDLE)
    footer(c, n, f, link)


# ======================================================================= 8. Heatmap

def _blue(v, vmax=2.4):
    stops = [(0.0, (246, 248, 251)), (0.25, (205, 226, 251)), (0.6, (109, 167, 236)), (1.0, (37, 106, 191)),
             (1.6, (16, 66, 129)), (2.4, (11, 33, 70))]
    v = max(0.0, min(v, vmax))
    for (a, ca), (b, cb) in zip(stops, stops[1:]):
        if v <= b:
            k = (v - a) / (b - a)
            return "".join(f"{round(x + (y - x) * k):02X}" for x, y in zip(ca, cb)), v >= 0.9
    return "0B2146", True


def d08_heatmap(slide, n, link):
    c, f = _start(slide, n)
    _title(c, f)
    order = scenario_order(n)
    rows = [("No QoS", "B0"), ("Plain CAKE", "B2"), ("Threshold rules", "B4"), ("Ours, simple rule", "O1"),
            ("Ours, full system", "O2")]
    x0, y0, lw, cw, rh, tw = 4.75, 2.42, 1.45, 0.62, 0.5, 1.15
    c.text(x0, y0 - 0.5, lw - 0.08, 0.48, ["Flows left unusable", "per scenario"], size=10, bold=True, align=RIGHT,
           anchor=BOTTOM)
    for j, (s, v) in enumerate(order):
        c.text(x0 + lw + j * cw - 0.06, y0 - 0.42, cw + 0.12, 0.4, SHORT.get(s, s).split("\n"), size=9, color=MUTED,
               align=CENTER, anchor=BOTTOM)
    c.text(x0 + lw + len(order) * cw + 0.12, y0 - 0.3, tw, 0.28, "all 9", size=9, bold=True, color=MUTED)
    tmax = max(f["total_raw"].values())
    for i, (name, k) in enumerate(rows):
        y = y0 + i * rh
        ours = k.startswith("O")
        c.text(x0, y, lw - 0.08, rh, name, size=10, bold=k == "O2", color=OURS if ours else INK, align=RIGHT,
               anchor=MIDDLE)
        for j, (s, v) in enumerate(order):
            val = v["cost"][k]
            fill, dark = _blue(val)
            b = c.box(x0 + lw + j * cw + 0.02, y + 0.02, cw - 0.04, rh - 0.04, fill=fill)
            c.label(b, f"{val:.2f}", size=9.5, color=WHITE if dark else INK, bold=k == "O2")
        total = f["total_raw"][k]
        bx = x0 + lw + len(order) * cw + 0.12
        c.box(bx, y + 0.12, max(0.04, (tw - 0.45) * total / tmax), rh - 0.24,
              fill={"B0": NOQOS, "B2": CAKE, "B4": RULES, "O1": OURS_MID, "O2": OURS}[k])
        c.text(bx + (tw - 0.45) * total / tmax + 0.05, y, 0.5, rh, f"{total:.2f}", size=10, bold=True, anchor=MIDDLE)
    yo = y0 + 4 * rh
    c.box(x0 - 0.05, yo - 0.02, lw + len(order) * cw + tw + 0.2, rh + 0.04, line=OURS, line_w=2,
          shape=S.ROUNDED_RECTANGLE, radius=0.12)
    for j, (s, v) in enumerate(order):
        win = v["verdict"] == "better"
        c.text(x0 + lw + j * cw, yo + rh + 0.05, cw, 0.22, "Win" if win else "Tie", size=9, bold=win,
               color=OURS if win else FAINT, align=CENTER)
    c.text(x0, yo + rh + 0.3, 8.1, 0.25, f"Median of {f['seeds']} seeds; darker is worse. Win or tie: ours against "
           "plain CAKE. Ties are where plain CAKE already copes", size=9, color=MUTED)

    # Left: the gap and the build, as one numbered line down the side.
    c.text(0.5, 1.98, 3.9, 0.25, "Why CAKE needs telling", size=11, bold=True)
    c.text(0.5, 2.28, 3.95, 0.95,
           [[("1/11 ", {"bold": True, "color": OURS, "size": 13}),
             ("of its laptop is all fair queueing gives a call beside 10 file-sharing connections. ", {}),
             (f"{f['q_bulk']} ", {"bold": True, "color": RULES, "size": 13}),
             ("of cloud-game flows go to Bulk under threshold rules (over 200 packets/s)", {})]], size=9.5)
    c.text(0.5, 3.3, 3.9, 0.25, "What we built: how it decides", size=11, bold=True)
    _steps(c, n, f, 0.5, 3.66, 3.95, row_h=0.78, size=9, head_size=11)
    c.text(0.5, 6.02, 3.95, 0.3, "Then CAKE's Voice, Video, Best Effort or Bulk tin", size=9, color=MUTED)

    stats = [(f["less"], "less damage than plain CAKE"),
             ([(f["s6_cake"], {"color": CAKE}), (" → ", {"color": FAINT}), (f["s6_ours"], {"color": OURS})],
              "late frames, HD call sharing its laptop"),
             (f["eq_better"], f"runs equal or better than CAKE, {f['worse_rules']} worse than rules")]
    for i, (big, cap) in enumerate(stats):
        _stat(c, 4.75 + i * 2.75, 5.68, 2.65, big, cap, big_size=26, h_big=0.5, h_cap=0.4, cap_color=INK)
    footer(c, n, f, link)


# ======================================================================= 9. Queue tubes

def _tins(c, x0, y0, call_tin, rng, label):
    tins = ["Voice", "Video", "Best Effort", "Bulk"]
    w, gap, h = 0.62, 0.22, 1.55
    for i, t in enumerate(tins):
        x = x0 + i * (w + gap)
        c.box(x, y0, w, h, fill="F4F7FB", line=SEG, shape=S.CAN, line_w=1.25)
        c.text(x - 0.15, y0 + h + 0.04, w + 0.3, 0.22, t, size=9, bold=True, color=CAKE, align=CENTER)
        dots = []
        if t == "Bulk":
            dots += [SEG] * 16
        if t == call_tin:
            dots += [OURS] * 6
        rng.shuffle(dots)
        for k, colour in enumerate(dots):
            col, row = k % 3, k // 3
            c.box(x + 0.08 + col * 0.165 + rng.uniform(-0.02, 0.02), y0 + h - 0.27 - row * 0.165, 0.13, 0.13,
                  fill=colour, shape=S.OVAL)
    c.text(x0, y0 - 0.32, 4 * w + 3 * gap, 0.26, label, size=10.5, bold=True)


def d09_tubes(slide, n, link):
    c, f = _start(slide, n)
    _title(c, f)
    rng = random.Random(7)
    c.text(0.5, 2.0, 7.6, 0.3, [[("An HD call ", {"bold": True, "color": OURS}), ("(purple) and 10 file-sharing "
           "connections ", {}), ("(grey) ", {"bold": True, "color": FAINT}), ("leave one laptop. Where do they go?", {})]],
           size=11)
    _tins(c, 0.5, 2.85, "Bulk", rng, "Threshold rules: over 200 packets/s is Bulk")
    _tins(c, 4.55, 2.85, "Video", rng, "Our classifier: first 8 packets, no payload")
    c.text(3.95, 3.3, 0.6, 0.6, "vs", size=16, bold=True, color=FAINT, align=CENTER, anchor=MIDDLE)
    _stat(c, 0.5, 4.75, 3.3, [(f["s6_rules"], {"color": RULES})], "of the call's frames late (plain CAKE: "
          f"{f['s6_cake']})", big_size=40, cap_size=10, cap_color=INK, h_big=0.7)
    _stat(c, 4.55, 4.75, 3.3, [(f["s6_ours"], {"color": OURS})], "of the call's frames late", big_size=40, cap_size=10,
          cap_color=INK, h_big=0.7)
    m = mech(n)
    lines = [f"first 8 packets, no payload; LightGBM, {f['ms']} ms; {f['acc']} accuracy, macro-F1 {f['f1']}",
             f"file sharing first looked like a {m['re_from']}; re-asked {m['re_after']} later, moved to Bulk",
             f"tin with least expected damage, safety shield. Cloud games to Bulk: qosify {f['q_bulk']}, "
             f"ours {f['o_bulk']}"]
    for i, (head, body) in enumerate(zip(STEPS, lines)):
        yy = 5.72 + i * 0.36
        dot = c.box(0.5, yy + 0.03, 0.24, 0.24, fill=OURS, shape=S.OVAL)
        c.label(dot, str(i + 1), size=9, bold=True, color=WHITE)
        c.text(0.85, yy, 7.3, 0.3, [[(head + "  ", {"bold": True, "color": INK}), (body, {})]], size=9.5,
               color=MUTED, anchor=MIDDLE)

    blob(c, 10.75, 4.2, 2.35, 2.3, WASH, wobble=(0.04, 0.03, 0.03))
    c.text(8.55, 1.95, 4.3, 1.0, f["less"], size=64, bold=True, color=OURS, anchor=MIDDLE)
    c.text(8.6, 2.95, 4.2, 0.5, [[("less damage than plain CAKE", {"bold": True, "size": 13})],
           [(f"flows left unusable over {f['scenarios']} emulated home scenarios", {"size": 9, "color": MUTED})]],
           size=13)
    cost_chart(c, n, 8.45, 3.5, 4.4, 1.8, label_size=9.5)
    c.text(8.6, 5.4, 4.2, 1.0, [[(f["eq_better"], {"bold": True, "size": 18, "color": OURS})],
           [(f"runs equal or better than plain CAKE; {f['worse_rules']} worse than threshold rules. "
             f"Wins {CLAIM_WIN}, ties elsewhere", {})]], size=9.5)
    footer(c, n, f, link)


# ======================================================================= 10. Magazine

def d10_magazine(slide, n, link):
    c, f = _start(slide, n)
    c.text(0.5, 0.95, 8.0, 0.95, "Which flow is the call?", size=50, bold=True, font=SERIF)
    c.text(0.5, 1.85, 7.6, 0.75, [[("A small trained classifier tells ", {}), ("CAKE", {"bold": True}),
           (", so calls and games stay usable when they share a device with downloads, where fair queueing and "
            "threshold rules both fail", {})]], size=15, color=MUTED, font=SERIF, italic=True)

    c.text(0.5, 2.85, 3.55, 0.3, "The problem", size=13, bold=True, font=SERIF, color=OURS)
    c.text(0.5, 3.2, 3.55, 2.0,
           [[("A laptop on a call while a file-sharing client keeps ", {}), ("10 connections", {"bold": True}),
             (" open. Fair queueing gives the call ", {}), ("1/11", {"bold": True, "color": OURS}),
             (" of the laptop's share: ", {}), (f"{f['s6_cake']} of its frames late", {"bold": True, "color": CAKE}),
             (" under plain CAKE.", {})],
            [("Threshold rules call anything over 200 packets/s bulk. HD calls send 400 to 600; qosify sends ", {}),
             (f"{f['q_bulk']} of cloud-game flows", {"bold": True, "color": RULES}), (" to Bulk.", {})]],
           size=11, spacing=6)
    c.text(4.45, 2.85, 3.8, 0.3, "How it decides", size=13, bold=True, font=SERIF, color=OURS)
    _steps(c, n, f, 4.45, 3.22, 3.85, row_h=0.9, numerals=True)
    c.text(0.5, 5.35, 3.55, 0.9, [[(f["acc"], {"bold": True, "size": 22, "color": OURS, "font": SERIF}),
           ("  accuracy   ", {}), (f["f1"], {"bold": True, "size": 22, "color": OURS, "font": SERIF}), ("  macro-F1", {})],
           [(f"on {f['flows']} held-out recorded flows from {f['sessions']} sessions. Threshold rules: "
             f"{f['rules_acc']}, {f['rules_f1']}", {"color": MUTED, "size": 9})]], size=10)
    scenario_dots(c, n, 0.75, 6.42, 0.66, size=0.22)
    c.text(6.85, 6.1, 1.7, 0.6, [[("Filled: ", {"bold": True, "color": OURS}), ("wins over plain CAKE. ", {}),
           ("Hollow: ", {"bold": True, "color": MUTED}), ("ties", {})]], size=9, color=MUTED, anchor=MIDDLE)

    chip(c, f, 8.95, 1.0, w=3.88)
    stats = [(f["less"], "less damage than plain CAKE", f"{f['total']['B2']} to {f['total']['O2']} flows left unusable "
              f"over {f['scenarios']} scenarios"),
             ([(f["s6_cake"], {"color": CAKE}), (" → ", {"color": FAINT}), (f["s6_ours"], {"color": OURS})],
              "late frames on the HD call", "laptop also sharing files"),
             ([(f["q_bulk"], {"color": RULES}), (" → ", {"color": FAINT}), (f["o_bulk"], {"color": OURS})],
              "cloud games sent to Bulk", "qosify's rules, then ours"),
             (f["eq_better"], "runs equal or better than plain CAKE", f"{f['worse_rules']} worse than threshold rules"),
             (f"{f['drop_ms']} ms", "to catch a line drop", f"watchdog back to plain CAKE in {f['wd']} s")]
    y = 1.55
    for big, head, body in stats:
        c.line(8.95, y, 12.83, y, color=INK, width=0.75)
        runs = big if isinstance(big, list) else [(big, {"color": OURS})]
        c.text(8.95, y + 0.06, 1.95, 0.85, [runs], size=26, bold=True, font=SERIF, anchor=MIDDLE)
        c.text(10.9, y + 0.08, 1.93, 0.85, [[(head, {"bold": True, "size": 10.5})], [(body, {"color": MUTED})]],
               size=9, anchor=MIDDLE)
        y += 0.98
    c.line(8.95, y, 12.83, y, color=INK, width=0.75)
    footer(c, n, f, link, guards=False)


DESIGNS = {1: ("Flow", d01_flow), 2: ("Split", d02_split), 3: ("Giant number", d03_giant),
           4: ("Timeline hero", d04_timeline), 5: ("Night", d05_night), 6: ("Orbit", d06_orbit), 7: ("Road", d07_road),
           8: ("Heatmap", d08_heatmap), 9: ("Queue tubes", d09_tubes), 10: ("Magazine", d10_magazine)}
