import logging
import math
import os
import json
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

WIDTH = 800
HEIGHT = 480
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
FONTS_DIR = os.path.join(BASE_DIR, 'static', 'fonts')
DATA_DIR = os.environ.get('TRMNL_DATA_DIR', os.path.join(BASE_DIR, 'instance'))
OUTPUT_IMAGE_PATH = os.path.join(DATA_DIR, 'trmnl_display.png')
OUTPUT_BMP_PATH = os.path.join(DATA_DIR, 'trmnl_display.bmp')
TELEMETRY_PATH = os.path.join(DATA_DIR, 'trmnl_telemetry.json')
LEGACY_TELEMETRY_PATH = os.path.join(BASE_DIR, 'static', 'trmnl_telemetry.json')

_render_lock = threading.Lock()
_telemetry_lock = threading.Lock()


def _atomic_write_file(target_path: str, write_callback):
    """Write via a temporary file in the target directory and atomically swap into place."""
    parent_dir = os.path.dirname(target_path) or '.'
    os.makedirs(parent_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix='.tmp_trmnl_', dir=parent_dir)
    try:
        with os.fdopen(fd, 'wb') as tmp_file:
            write_callback(tmp_file)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_path, target_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_trmnl_data(app):
    """Fetch today's canonical solar stats, 7-day trend, and status for TRMNL rendering."""
    from server.config import load_config, get_gateway_token_status
    from server.dashboard_services import get_target_timezone, get_today_summary
    from server.db import DailyStats

    config = load_config()
    trmnl_config = config.get('trmnl', {})
    title = trmnl_config.get('title', 'DAILY SOLAR STATUS')

    tz = get_target_timezone()
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    today_local = now_local.date()

    token_status = get_gateway_token_status(config, now_utc=now_utc)
    token_warning = None
    if token_status.get('is_expired'):
        token_warning = "TOKEN EXPIRED"
    elif token_status.get('is_expiring_soon'):
        days_rem = token_status.get('days_remaining')
        token_warning = f"TOKEN {days_rem}D" if days_rem is not None else "TOKEN EXPIRING"

    with app.app_context():
        summary = get_today_summary(now_utc=now_utc)

        production = summary['production_kwh']
        consumption = summary['consumption_kwh']
        export_val = summary['export_kwh']
        import_val = summary['import_kwh']
        net_grid = summary['net_grid_kwh']
        coverage = summary['solar_coverage_pct']
        is_export = summary['is_net_export']
        is_stale = summary['is_stale']
        has_readings = summary['has_readings']
        has_data = summary['has_data']
        is_interpolated = summary['is_interpolated']
        direct_use = round(max(0.0, production - export_val), 2)

        if summary['last_reading_utc']:
            last_dt = datetime.fromisoformat(summary['last_reading_utc']).replace(tzinfo=timezone.utc).astimezone(tz)
            last_updated_str = last_dt.strftime('%b %d, %I:%M %p')
        else:
            last_updated_str = "No readings yet"

        if abs(net_grid) < 0.05:
            net_grid_str = "0.0"
            net_grid_unit = "kWh"
            net_grid_badge = "BALANCED"
        elif is_export:
            net_grid_str = f"+{net_grid:.1f}"
            net_grid_unit = "kWh Export"
            net_grid_badge = "EXPORT"
        else:
            net_grid_str = f"-{abs(net_grid):.1f}"
            net_grid_unit = "kWh Import"
            net_grid_badge = "IMPORT"

        if not has_readings:
            status_label = "NO DATA"
        elif is_stale:
            status_label = "STALE"
        elif not has_data:
            status_label = "AWAITING DATA"
        elif is_interpolated:
            status_label = "ESTIMATED"
        else:
            status_label = "ACTIVE"

        # Query last 7 local calendar days for the 7-day trend bar chart
        start_7d = today_local - timedelta(days=6)
        rows_7d = (
            DailyStats.query
            .filter(DailyStats.date >= start_7d, DailyStats.date <= today_local)
            .order_by(DailyStats.date.asc())
            .all()
        )
        rows_by_date = {r.date: r for r in rows_7d}
        daily_history = []
        if rows_by_date:
            for offset in range(7):
                day_d = start_7d + timedelta(days=offset)
                row = rows_by_date.get(day_d)
                is_today = (day_d == today_local)
                daily_history.append({
                    "date": day_d.isoformat(),
                    "label": "TODAY" if is_today else day_d.strftime('%a').upper()[:3],
                    "is_today": is_today,
                    "production": round(float(row.production_kwh or 0.0), 2) if row else 0.0,
                    "consumption": round(float(row.consumption_kwh or 0.0), 2) if row else 0.0,
                    "has_data": row is not None,
                    "is_interpolated": bool(row.is_interpolated) if row else False,
                })

        return {
            "title": title,
            "date_label": today_local.strftime('%b %d'),
            "production": production,
            "consumption": consumption,
            "direct_use": direct_use,
            "export": export_val,
            "import": import_val,
            "net_grid_kwh": net_grid,
            "net_grid_str": net_grid_str,
            "net_grid_unit": net_grid_unit,
            "net_grid_badge": net_grid_badge,
            "is_export": is_export,
            "coverage": coverage,
            "last_updated": last_updated_str,
            "is_stale": is_stale,
            "has_data": has_data,
            "is_interpolated": is_interpolated,
            "status_label": status_label,
            "token_warning": token_warning,
            "daily_history": daily_history,
        }


def _truncate_text(draw, text: str, font, max_w: int) -> str:
    """Truncate text with ellipsis if its rendered width exceeds max_w."""
    if not text or max_w <= 0:
        return ""
    bbox = draw.textbbox((0, 0), text, font=font)
    if (bbox[2] - bbox[0]) <= max_w:
        return text
    ellipsis = "..."
    trimmed = text
    while trimmed:
        trimmed = trimmed[:-1]
        cand = trimmed.rstrip() + ellipsis
        cb = draw.textbbox((0, 0), cand, font=font)
        if (cb[2] - cb[0]) <= max_w:
            return cand
    return ""


def _draw_brand_bolt(draw, x: int, y: int, size: int = 28):
    """Draw a crisp 1-bit rounded square badge with a white lightning bolt inside."""
    draw.rounded_rectangle([x, y, x + size, y + size], radius=6, fill=0)
    # Lightning bolt polygon scaled to badge size
    pts = [
        (x + size * 0.56, y + size * 0.14),
        (x + size * 0.24, y + size * 0.55),
        (x + size * 0.47, y + size * 0.55),
        (x + size * 0.42, y + size * 0.86),
        (x + size * 0.76, y + size * 0.45),
        (x + size * 0.53, y + size * 0.45),
    ]
    draw.polygon([(int(round(px)), int(round(py))) for px, py in pts], fill=255)


def _draw_sun_icon(draw, cx: int, cy: int):
    """Draw a crisp 1-bit mini sun icon centered at (cx, cy)."""
    r = 4
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=0, width=2)
    for angle_deg in range(0, 360, 45):
        rad = math.radians(angle_deg)
        x1 = cx + int(round(math.cos(rad) * 6.5))
        y1 = cy + int(round(math.sin(rad) * 6.5))
        x2 = cx + int(round(math.cos(rad) * 9.0))
        y2 = cy + int(round(math.sin(rad) * 9.0))
        draw.line([(x1, y1), (x2, y2)], fill=0, width=2)


def _draw_house_icon(draw, cx: int, cy: int):
    """Draw a crisp 1-bit mini house icon centered at (cx, cy)."""
    # Roof
    draw.line([(cx - 8, cy), (cx, cy - 7), (cx + 8, cy)], fill=0, width=2)
    # Walls + base
    draw.line([(cx - 6, cy - 1), (cx - 6, cy + 7), (cx + 6, cy + 7), (cx + 6, cy - 1)], fill=0, width=2)
    # Door
    draw.rectangle([cx - 2, cy + 2, cx + 2, cy + 7], fill=0)


def _draw_pill_badge(draw, right_x: int, top_y: int, text: str, font, *, inverted: bool = True):
    """Draw a right-aligned pill badge and return its bounding box (x1, y1, x2, y2)."""
    pad_x = 8
    pad_y = 3
    tb = draw.textbbox((0, 0), text, font=font)
    tw = tb[2] - tb[0]
    th = tb[3] - tb[1]
    bw = tw + 2 * pad_x
    bh = max(18, th + 2 * pad_y)
    x1 = right_x - bw
    y1 = top_y
    x2 = right_x
    y2 = top_y + bh
    if inverted:
        draw.rounded_rectangle([x1, y1, x2, y2], radius=bh // 2, fill=0)
        text_fill = 255
    else:
        draw.rounded_rectangle([x1, y1, x2, y2], radius=bh // 2, fill=255, outline=0, width=2)
        text_fill = 0
    tx = x1 + pad_x - tb[0]
    ty = y1 + (bh - th) // 2 - tb[1]
    draw.text((tx, ty), text, fill=text_fill, font=font)
    return (x1, y1, x2, y2)


def _draw_dotted_hline(draw, x1: int, x2: int, y: int, dash: int = 2, gap: int = 4):
    """Draw a crisp 1-bit dotted horizontal line."""
    x = x1
    while x < x2:
        draw.line([(x, y), (min(x + dash - 1, x2), y)], fill=0, width=1)
        x += dash + gap


def _draw_hatched_bar(draw, x1: int, y1: int, x2: int, y2: int, step: int = 5):
    """Draw a pure 1-bit outlined bar with crisp 45-degree diagonal hatching (zero dithering noise)."""
    if y2 <= y1 or x2 <= x1:
        return
    draw.rectangle([x1, y1, x2, y2], fill=255, outline=0, width=2)
    inner_x1 = x1 + 2
    inner_x2 = x2 - 2
    inner_y1 = y1 + 2
    inner_y2 = y2 - 2
    if inner_x2 <= inner_x1 or inner_y2 <= inner_y1:
        return
    w = inner_x2 - inner_x1
    h = inner_y2 - inner_y1
    # Draw diagonal lines (bottom-left to top-right) clipped strictly inside the bar
    for d in range(2, w + h, step):
        # Line equation: (px - inner_x1) + (py - inner_y1) = d
        px1 = max(inner_x1, inner_x1 + d - h)
        py1 = inner_y1 + d - (px1 - inner_x1)
        px2 = min(inner_x2, inner_x1 + d)
        py2 = inner_y1 + d - (px2 - inner_x1)
        if px1 <= px2 and inner_y1 <= py2 <= inner_y2 and inner_y1 <= py1 <= inner_y2:
            draw.line([(px1, py1), (px2, py2)], fill=0, width=1)


def _draw_value_with_unit(draw, x: int, baseline_y: int, num_str: str, unit_str: str, max_w: int, num_fonts, unit_font):
    """
    Draw a large numeric value + unit anchored cleanly to baseline_y, stepping down font sizes
    if needed so values never overflow max_w and always align horizontally across sibling cards.
    """
    u_bbox = draw.textbbox((0, 0), unit_str, font=unit_font) if unit_str else (0, 0, 0, 0)
    u_w = (u_bbox[2] - u_bbox[0]) if unit_str else 0
    gap = 3 if unit_str == "%" else (6 if unit_str else 0)

    chosen_font = num_fonts[-1]
    chosen_bbox = draw.textbbox((0, 0), num_str, font=chosen_font)
    for candidate_font in num_fonts:
        nb = draw.textbbox((0, 0), num_str, font=candidate_font)
        nw = nb[2] - nb[0]
        if nw + gap + u_w <= max_w:
            chosen_font = candidate_font
            chosen_bbox = nb
            break

    n_w = chosen_bbox[2] - chosen_bbox[0]
    num_y = baseline_y - chosen_bbox[3]
    draw.text((x - chosen_bbox[0], num_y), num_str, fill=0, font=chosen_font)

    if unit_str:
        unit_y = baseline_y - u_bbox[3]
        draw.text((x + n_w + gap - u_bbox[0], unit_y), unit_str, fill=0, font=unit_font)


def render_trmnl_solar(data: dict, output_png_path: str = None, output_bmp_path: str = None):
    """Renders 800x480 1-bit monochrome e-ink screen synced with the Enphase Dashboard."""
    if output_png_path is None:
        output_png_path = OUTPUT_IMAGE_PATH
    if output_bmp_path is None:
        output_bmp_path = OUTPUT_BMP_PATH

    img = Image.new("L", (WIDTH, HEIGHT), color=255)
    draw = ImageDraw.Draw(img)

    # Load single consolidated font at clean typographic sizes
    FONT_FILE = os.path.join(FONTS_DIR, "MonaspaceKrypton-SemiBold.otf")
    font_title = ImageFont.truetype(FONT_FILE, 23)
    font_time = ImageFont.truetype(FONT_FILE, 14)
    font_label = ImageFont.truetype(FONT_FILE, 13)
    font_pill = ImageFont.truetype(FONT_FILE, 11)
    font_num_lg = ImageFont.truetype(FONT_FILE, 44)
    font_num_md = ImageFont.truetype(FONT_FILE, 36)
    font_num_sm = ImageFont.truetype(FONT_FILE, 29)
    font_unit = ImageFont.truetype(FONT_FILE, 16)
    font_sub = ImageFont.truetype(FONT_FILE, 11)
    font_split_num = ImageFont.truetype(FONT_FILE, 22)
    font_split_unit = ImageFont.truetype(FONT_FILE, 12)
    font_axis = ImageFont.truetype(FONT_FILE, 10)
    font_footer = ImageFont.truetype(FONT_FILE, 13)

    pad = 24
    inner_w = WIDTH - 2 * pad  # 752 px

    # ------------------------------------------------------------------
    # 1. Header (Brand bolt + Title + Right-aligned Timestamp + Rule)
    # ------------------------------------------------------------------
    header_y = 20
    _draw_brand_bolt(draw, pad, header_y, size=28)

    time_str = str(data.get("last_updated") or "")
    time_bbox = draw.textbbox((0, 0), time_str, font=font_time) if time_str else (0, 0, 0, 0)
    time_w = (time_bbox[2] - time_bbox[0]) if time_str else 0
    time_h = (time_bbox[3] - time_bbox[1]) if time_str else 0
    if time_str:
        time_y = header_y + (28 - time_h) // 2 - time_bbox[1]
        draw.text((WIDTH - pad - time_w - time_bbox[0], time_y), time_str, fill=0, font=font_time)

    title_x = pad + 38
    max_title_w = (WIDTH - pad - time_w - 16) - title_x
    raw_title = str(data.get("title", "DAILY SOLAR STATUS")).upper()
    title_str = _truncate_text(draw, raw_title, font_title, max_title_w)
    if title_str:
        t_bbox = draw.textbbox((0, 0), title_str, font=font_title)
        t_h = t_bbox[3] - t_bbox[1]
        draw.text((title_x - t_bbox[0], header_y + (28 - t_h) // 2 - t_bbox[1]), title_str, fill=0, font=font_title)

    header_line_y = 58
    draw.rectangle([pad, header_line_y, WIDTH - pad, header_line_y + 2], fill=0)

    # ------------------------------------------------------------------
    # 2. Top Hero Row: 3 Equal Cards (SOLAR | USAGE | NET GRID)
    # ------------------------------------------------------------------
    hero_y = 72
    hero_h = 132
    gap_x = 16
    col_w = (inner_w - 2 * gap_x) // 3  # 240 px

    production = float(data.get("production", 0.0) or 0.0)
    consumption = float(data.get("consumption", 0.0) or 0.0)
    export_val = float(data.get("export", 0.0) or 0.0)
    import_val = float(data.get("import", 0.0) or 0.0)
    direct_use = float(data.get("direct_use", max(0.0, production - export_val)) or 0.0)
    coverage = min(100.0, max(0.0, float(data.get("coverage", 0.0) or 0.0)))

    # Derive net grid display cleanly for both new and legacy render payloads
    if "net_grid_badge" in data:
        net_badge = str(data["net_grid_badge"]).upper()
        net_num_str = str(data.get("net_grid_str", "0.0"))
    else:
        raw_net_str = str(data.get("net_grid_str", "+0.0"))
        is_exp = bool(data.get("is_export", not raw_net_str.startswith("-")))
        try:
            net_f = float(raw_net_str)
        except ValueError:
            net_f = export_val - import_val
        if abs(net_f) < 0.05:
            net_num_str = "0.0"
            net_badge = "BALANCED"
        elif is_exp:
            net_num_str = f"+{abs(net_f):.1f}"
            net_badge = "EXPORT"
        else:
            net_num_str = f"-{abs(net_f):.1f}"
            net_badge = "IMPORT"

    hero_cards = [
        {
            "label": "SOLAR",
            "num": f"{production:.1f}",
            "unit": "kWh",
            "sub": f"DIRECT {direct_use:.1f} kWh",
            "icon": "sun",
        },
        {
            "label": "USAGE",
            "num": f"{consumption:.1f}",
            "unit": "kWh",
            "sub": f"FROM GRID {import_val:.1f} kWh",
            "icon": "house",
        },
        {
            "label": "NET GRID",
            "num": net_num_str,
            "unit": "kWh",
            "sub": f"EXP {export_val:.1f} / IMP {import_val:.1f}",
            "badge": net_badge,
        },
    ]

    num_baseline_y = hero_y + 92
    sub_y = hero_y + 107

    for idx, card in enumerate(hero_cards):
        bx = pad + idx * (col_w + gap_x)
        by = hero_y
        bx2 = bx + col_w
        by2 = by + hero_h

        draw.rounded_rectangle([bx, by, bx2, by2], radius=10, fill=255, outline=0, width=2)
        draw.text((bx + 14, by + 13), card["label"], fill=0, font=font_label)

        if card.get("icon") == "sun":
            _draw_sun_icon(draw, bx2 - 22, by + 20)
        elif card.get("icon") == "house":
            _draw_house_icon(draw, bx2 - 22, by + 20)
        elif card.get("badge"):
            badge_inverted = card["badge"] != "BALANCED"
            _draw_pill_badge(draw, bx2 - 12, by + 11, card["badge"], font_pill, inverted=badge_inverted)

        _draw_value_with_unit(
            draw,
            x=bx + 14,
            baseline_y=num_baseline_y,
            num_str=card["num"],
            unit_str=card["unit"],
            max_w=col_w - 28,
            num_fonts=(font_num_lg, font_num_md, font_num_sm),
            unit_font=font_unit,
        )

        sub_txt = _truncate_text(draw, card["sub"], font_sub, col_w - 28)
        draw.text((bx + 14, sub_y), sub_txt, fill=0, font=font_sub)

    # ------------------------------------------------------------------
    # 3. Lower Visual Band: Left 7-Day Trend Chart | Right Coverage & Split
    # ------------------------------------------------------------------
    band_y = 218
    band_h = 200
    left_w = 444
    right_x = pad + left_w + gap_x
    right_w = inner_w - left_w - gap_x  # 292 px

    # --- 3A. Left Card: 7-Day Solar vs. Usage Bar Chart ---
    lx1, ly1, lx2, ly2 = pad, band_y, pad + left_w, band_y + band_h
    draw.rounded_rectangle([lx1, ly1, lx2, ly2], radius=10, fill=255, outline=0, width=2)

    draw.text((lx1 + 14, ly1 + 12), "7-DAY TREND (kWh)", fill=0, font=font_label)

    # Inline legend (top-right inside left card): [■] Solar  [▨] Usage
    usage_lbl = "USAGE"
    solar_lbl = "SOLAR"
    ul_bb = draw.textbbox((0, 0), usage_lbl, font=font_axis)
    sl_bb = draw.textbbox((0, 0), solar_lbl, font=font_axis)
    ul_w = ul_bb[2] - ul_bb[0]
    sl_w = sl_bb[2] - sl_bb[0]
    leg_y = ly1 + 14

    ux_text = lx2 - 14 - ul_w
    draw.text((ux_text - ul_bb[0], leg_y - ul_bb[1]), usage_lbl, fill=0, font=font_axis)
    _draw_hatched_bar(draw, ux_text - 15, leg_y - 1, ux_text - 5, leg_y + 9, step=4)

    sx_text = ux_text - 27 - sl_w
    draw.text((sx_text - sl_bb[0], leg_y - sl_bb[1]), solar_lbl, fill=0, font=font_axis)
    draw.rectangle([sx_text - 15, leg_y - 1, sx_text - 5, leg_y + 9], fill=0)

    daily_history = data.get("daily_history") or []
    chart_left = lx1 + 42
    chart_right = lx2 - 16
    chart_top = ly1 + 42
    chart_bottom = ly2 - 30
    chart_h = chart_bottom - chart_top

    if daily_history:
        history_slice = daily_history[-7:]
        raw_max = max(
            [max(float(d.get("production", 0.0) or 0.0), float(d.get("consumption", 0.0) or 0.0)) for d in history_slice]
            or [10.0]
        )
        # Round axis max cleanly up to a multiple of 5 or 10 kWh
        if raw_max <= 10.0:
            axis_max = max(5.0, math.ceil(raw_max / 5.0) * 5.0)
        elif raw_max <= 50.0:
            axis_max = math.ceil(raw_max / 10.0) * 10.0
        else:
            axis_max = math.ceil(raw_max / 20.0) * 20.0

        # Horizontal reference gridlines (max, half-max, baseline)
        for frac, val in ((1.0, axis_max), (0.5, axis_max / 2.0)):
            gy = chart_bottom - int(round(chart_h * frac))
            _draw_dotted_hline(draw, chart_left, chart_right, gy, dash=2, gap=4)
            tick_txt = f"{int(round(val))}"
            tb = draw.textbbox((0, 0), tick_txt, font=font_axis)
            tw = tb[2] - tb[0]
            th = tb[3] - tb[1]
            draw.text((chart_left - 6 - tw - tb[0], gy - th // 2 - tb[1]), tick_txt, fill=0, font=font_axis)

        # Solid baseline + '0' label
        draw.line([(chart_left, chart_bottom), (chart_right, chart_bottom)], fill=0, width=2)
        z_bb = draw.textbbox((0, 0), "0", font=font_axis)
        draw.text(
            (chart_left - 6 - (z_bb[2] - z_bb[0]) - z_bb[0], chart_bottom - (z_bb[3] - z_bb[1]) // 2 - z_bb[1]),
            "0",
            fill=0,
            font=font_axis,
        )

        n_days = len(history_slice)
        slot_w = (chart_right - chart_left) / float(n_days)
        bar_w = max(8, min(17, int(slot_w * 0.32)))
        bar_gap = 3

        for idx_d, day in enumerate(history_slice):
            cx = int(round(chart_left + (idx_d + 0.5) * slot_w))
            p_val = max(0.0, float(day.get("production", 0.0) or 0.0))
            c_val = max(0.0, float(day.get("consumption", 0.0) or 0.0))

            p_h = int(round((p_val / axis_max) * chart_h)) if p_val > 0 else 0
            c_h = int(round((c_val / axis_max) * chart_h)) if c_val > 0 else 0
            if p_val > 0.05:
                p_h = max(3, p_h)
            if c_val > 0.05:
                c_h = max(4, c_h)

            # Solar bar (left of pair, solid black)
            sx1 = cx - bar_gap // 2 - bar_w
            sx2 = sx1 + bar_w
            if p_h > 0:
                draw.rectangle([sx1, chart_bottom - p_h, sx2, chart_bottom], fill=0)

            # Usage bar (right of pair, crisp diagonal-hatched 1-bit bar)
            ux1 = cx + (bar_gap - bar_gap // 2)
            ux2 = ux1 + bar_w
            if c_h > 0:
                _draw_hatched_bar(draw, ux1, chart_bottom - c_h, ux2, chart_bottom, step=5)

            # Day label under baseline (highlight TODAY with inverted pill)
            lbl = str(day.get("label", ""))
            is_today = bool(day.get("is_today", False))
            lb = draw.textbbox((0, 0), lbl, font=font_axis)
            lw = lb[2] - lb[0]
            lh = lb[3] - lb[1]
            lbl_top = chart_bottom + 7
            if is_today:
                px1 = cx - lw // 2 - 4
                px2 = cx + (lw - lw // 2) + 4
                py1 = lbl_top - 2
                py2 = lbl_top + lh + 3
                draw.rounded_rectangle([px1, py1, px2, py2], radius=4, fill=0)
                draw.text((cx - lw // 2 - lb[0], lbl_top - lb[1]), lbl, fill=255, font=font_axis)
            else:
                draw.text((cx - lw // 2 - lb[0], lbl_top - lb[1]), lbl, fill=0, font=font_axis)
    else:
        empty_msg = "Awaiting multi-day history"
        eb = draw.textbbox((0, 0), empty_msg, font=font_label)
        ew = eb[2] - eb[0]
        eh = eb[3] - eb[1]
        draw.text(
            (lx1 + (left_w - ew) // 2 - eb[0], ly1 + (band_h - eh) // 2 - eb[1]),
            empty_msg,
            fill=0,
            font=font_label,
        )

    # --- 3B. Right Card: Solar Coverage + Progress Bar + Export/Import Split ---
    rx1, ry1, rx2, ry2 = right_x, band_y, right_x + right_w, band_y + band_h
    draw.rounded_rectangle([rx1, ry1, rx2, ry2], radius=10, fill=255, outline=0, width=2)

    draw.text((rx1 + 14, ry1 + 12), "SOLAR COVERAGE", fill=0, font=font_label)

    # Coverage % on left + direct/consumption ratio on right
    cov_baseline_y = ry1 + 78
    _draw_value_with_unit(
        draw,
        x=rx1 + 14,
        baseline_y=cov_baseline_y,
        num_str=f"{coverage:.0f}",
        unit_str="%",
        max_w=132,
        num_fonts=(font_num_lg, font_num_md, font_num_sm),
        unit_font=font_unit,
    )

    ratio_txt = f"{direct_use:.1f}/{consumption:.1f} kWh"
    rb = draw.textbbox((0, 0), ratio_txt, font=font_sub)
    rw = rb[2] - rb[0]
    draw.text((rx2 - 14 - rw - rb[0], cov_baseline_y - rb[3] - 2), ratio_txt, fill=0, font=font_sub)

    # Crisp 1-bit Solar Coverage progress bar with 50% midpoint notch
    bar_x1 = rx1 + 14
    bar_x2 = rx2 - 14
    bar_y1 = ry1 + 92
    bar_h_px = 18
    bar_y2 = bar_y1 + bar_h_px
    draw.rounded_rectangle([bar_x1, bar_y1, bar_x2, bar_y2], radius=5, fill=255, outline=0, width=2)

    fill_inner_w = (bar_x2 - bar_x1) - 6
    fill_w = int(round(fill_inner_w * (coverage / 100.0)))
    if fill_w > 0:
        draw.rounded_rectangle(
            [bar_x1 + 3, bar_y1 + 3, bar_x1 + 3 + fill_w, bar_y2 - 3],
            radius=3,
            fill=0,
        )

    # Subtle midpoint tick at 50%
    mid_x = (bar_x1 + bar_x2) // 2
    tick_color = 255 if (bar_x1 + 3 + fill_w) > mid_x else 0
    draw.line([(mid_x, bar_y1 + 3), (mid_x, bar_y2 - 3)], fill=tick_color, width=1)

    # Horizontal dotted divider between coverage and Export/Import split
    split_div_y = ry1 + 124
    _draw_dotted_hline(draw, rx1 + 14, rx2 - 14, split_div_y, dash=2, gap=4)

    # Export / Import side-by-side detail
    half_w = (right_w - 28) // 2
    split_mid_x = rx1 + right_w // 2
    draw.line([(split_mid_x, split_div_y + 10), (split_mid_x, ry2 - 14)], fill=0, width=1)

    split_items = [
        {"label": "EXPORT", "num": f"{export_val:.1f}", "x": rx1 + 14},
        {"label": "IMPORT", "num": f"{import_val:.1f}", "x": split_mid_x + 12},
    ]
    split_baseline_y = ry2 - 18
    for item in split_items:
        draw.text((item["x"], split_div_y + 11), item["label"], fill=0, font=font_sub)
        _draw_value_with_unit(
            draw,
            x=item["x"],
            baseline_y=split_baseline_y,
            num_str=item["num"],
            unit_str="kWh",
            max_w=half_w - 8,
            num_fonts=(font_split_num, font_unit, font_label),
            unit_font=font_split_unit,
        )

    # ------------------------------------------------------------------
    # 4. Footer Bar (Branding + Optional Token Warning + Freshness Status)
    # ------------------------------------------------------------------
    bar_h = 30
    footer_y = HEIGHT - pad - bar_h + 4  # y = 430..460
    draw.rounded_rectangle([pad, footer_y, WIDTH - pad, footer_y + bar_h], radius=6, fill=0)

    footer_text = "ENPHASE DASHBOARD"
    f_bbox = draw.textbbox((0, 0), footer_text, font=font_footer)
    f_h = f_bbox[3] - f_bbox[1]
    f_y = footer_y + (bar_h - f_h) // 2 - f_bbox[1]
    draw.text((pad + 14 - f_bbox[0], f_y), footer_text, fill=255, font=font_footer)

    # Freshness status indicator (right-aligned)
    status_str = str(data.get("status_label", "ACTIVE")).upper()
    is_stale = bool(data.get("is_stale", False))
    a_bbox = draw.textbbox((0, 0), status_str, font=font_footer)
    a_w = a_bbox[2] - a_bbox[0]

    circ_r = 4
    circ_x = WIDTH - pad - 14 - a_w - 12
    circ_y = footer_y + bar_h // 2
    if is_stale or status_str != "ACTIVE":
        draw.ellipse([circ_x - circ_r, circ_y - circ_r, circ_x + circ_r, circ_y + circ_r], fill=0, outline=255, width=2)
    else:
        draw.ellipse([circ_x - circ_r, circ_y - circ_r, circ_x + circ_r, circ_y + circ_r], fill=255)
    draw.text((WIDTH - pad - 14 - a_w - a_bbox[0], f_y), status_str, fill=255, font=font_footer)

    # Optional Gateway Token Expiry Warning pill inside the footer bar
    token_warning = data.get("token_warning")
    if token_warning:
        warn_str = str(token_warning).upper()
        wb = draw.textbbox((0, 0), warn_str, font=font_pill)
        ww = wb[2] - wb[0]
        wh = wb[3] - wb[1]
        pill_w = ww + 14
        pill_h = 18
        pill_x2 = circ_x - 16
        pill_x1 = pill_x2 - pill_w
        pill_y1 = footer_y + (bar_h - pill_h) // 2
        pill_y2 = pill_y1 + pill_h
        if pill_x1 > pad + 14 + (f_bbox[2] - f_bbox[0]) + 16:
            draw.rounded_rectangle([pill_x1, pill_y1, pill_x2, pill_y2], radius=9, fill=255)
            draw.text(
                (pill_x1 + 7 - wb[0], pill_y1 + (pill_h - wh) // 2 - wb[1]),
                warn_str,
                fill=0,
                font=font_pill,
            )

    # Convert to 1-bit monochrome image (crisp pixel rendering without dither speckles)
    monochrome_img = img.convert("1", dither=Image.Dither.NONE)

    with _render_lock:
        _atomic_write_file(output_png_path, lambda f: monochrome_img.save(f, format="PNG"))
        if output_bmp_path:
            _atomic_write_file(output_bmp_path, lambda f: monochrome_img.save(f, format="BMP"))

    logger.info("TRMNL solar screen rendered to %s", output_png_path)
    return output_png_path


def update_trmnl_display(app):
    """Updates the rendered TRMNL display image with latest data."""
    try:
        data = get_trmnl_data(app)
        return render_trmnl_solar(data)
    except Exception:
        logger.exception("Error updating TRMNL display")
        return None


def _default_telemetry() -> dict:
    return {
        "last_seen": None,
        "mac_id": None,
        "fw_version": None,
        "battery_voltage": None,
        "battery_pct": None,
        "wifi_quality": None,
        "rssi": None,
        "logs": []
    }


def _load_trmnl_telemetry_unlocked() -> dict:
    for path in (TELEMETRY_PATH, LEGACY_TELEMETRY_PATH):
        if path and os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        merged = _default_telemetry()
                        merged.update(data)
                        return merged
            except Exception:
                pass
    return _default_telemetry()


def load_trmnl_telemetry() -> dict:
    """Loads the last recorded TRMNL device status, telemetry, and error logs."""
    with _telemetry_lock:
        return _load_trmnl_telemetry_unlocked()


def _save_trmnl_telemetry_unlocked(data: dict):
    payload = json.dumps(data, indent=2).encode('utf-8')
    _atomic_write_file(TELEMETRY_PATH, lambda f: f.write(payload))


def save_trmnl_telemetry(data: dict):
    """Atomically persists TRMNL device status and logs to instance/trmnl_telemetry.json."""
    try:
        with _telemetry_lock:
            _save_trmnl_telemetry_unlocked(data)
    except Exception:
        logger.exception("Error saving TRMNL telemetry")


def record_trmnl_checkin(headers: dict) -> dict:
    """Extracts device telemetry from TRMNL request headers and stores it safely."""
    mac_id = headers.get('ID') or headers.get('Id')
    fw_ver = headers.get('FW-Version') or headers.get('Fw-Version')
    battery = headers.get('Battery-Voltage') or headers.get('Battery_Voltage')
    rssi = headers.get('RSSI') or headers.get('Rssi')

    from server.dashboard_services import get_target_timezone
    tz = get_target_timezone()
    now_local = datetime.now(timezone.utc).astimezone(tz)
    short_time_str = now_local.strftime('%b %d, %I:%M %p')

    with _telemetry_lock:
        telemetry = _load_trmnl_telemetry_unlocked()
        telemetry["last_seen"] = short_time_str
        if mac_id:
            telemetry["mac_id"] = mac_id
        if fw_ver:
            telemetry["fw_version"] = fw_ver
        if battery:
            telemetry["battery_voltage"] = battery
            try:
                v = float(battery)
                telemetry["battery_pct"] = min(100, max(0, int((v - 3.3) / (4.2 - 3.3) * 100)))
            except (ValueError, TypeError):
                telemetry["battery_pct"] = None
        if rssi:
            telemetry["rssi"] = rssi
            try:
                r = int(rssi)
                if r >= -60:
                    telemetry["wifi_quality"] = "Great"
                elif r >= -70:
                    telemetry["wifi_quality"] = "Good"
                elif r >= -80:
                    telemetry["wifi_quality"] = "Fair"
                else:
                    telemetry["wifi_quality"] = "Weak"
            except (ValueError, TypeError):
                telemetry["wifi_quality"] = None

        try:
            _save_trmnl_telemetry_unlocked(telemetry)
        except Exception:
            logger.exception("Error saving TRMNL telemetry")

    logger.info(
        "[TRMNL] Check-in: MAC=%s, FW=%s, Battery=%sV, RSSI=%sdBm at %s",
        mac_id, fw_ver, battery, rssi, short_time_str,
    )
    return telemetry


def record_trmnl_log_message(headers: dict, message) -> dict:
    """Appends incoming error / log report from the TRMNL device safely."""
    from server.dashboard_services import get_target_timezone
    tz = get_target_timezone()
    now_local = datetime.now(timezone.utc).astimezone(tz)
    short_time_str = now_local.strftime('%b %d, %I:%M:%S %p')
    entry = {
        "timestamp": short_time_str,
        "mac_id": headers.get('ID') or headers.get('Id'),
        "message": message
    }

    with _telemetry_lock:
        telemetry = _load_trmnl_telemetry_unlocked()
        telemetry.setdefault("logs", []).append(entry)
        telemetry["logs"] = telemetry["logs"][-30:]  # Keep last 30 logs
        try:
            _save_trmnl_telemetry_unlocked(telemetry)
        except Exception:
            logger.exception("Error saving TRMNL telemetry")

    logger.info("[TRMNL LOG] %s [%s]: %s", short_time_str, headers.get('ID') or headers.get('Id'), message)
    return telemetry
