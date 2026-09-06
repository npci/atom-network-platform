# Copyright 2026 National Payments Corporation of India
# SPDX-License-Identifier: MIT

"""UML diagram generator — PlantUML-first with Pillow fallback."""
from __future__ import annotations

import logging
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PlantUML support
# ---------------------------------------------------------------------------

_PLANTUML_JAR_CANDIDATES = [
    Path("plantuml.jar"),
    Path("/opt/plantuml/plantuml.jar"),
    Path.home() / "plantuml.jar",
    Path("/usr/local/bin/plantuml.jar"),
    Path("/usr/share/plantuml/plantuml.jar"),
]


def _find_plantuml_jar() -> Optional[Path]:
    return next((p for p in _PLANTUML_JAR_CANDIDATES if p.exists()), None)


# ---------------------------------------------------------------------------
# Mermaid support (mermaid-cli / mmdc) — optional; graceful no-op if absent
# ---------------------------------------------------------------------------

_MERMAID_CLI_CANDIDATES = [
    Path("/usr/local/bin/mmdc"),
    Path("/usr/bin/mmdc"),
    Path.home() / ".npm-global/bin/mmdc",
    Path("node_modules/.bin/mmdc"),
]


def _find_mermaid_cli() -> Optional[str]:
    """Locate the mermaid-cli (`mmdc`) binary. Returns None when it isn't
    installed — the diagram dispatcher then falls back to the Pillow renderer."""
    import shutil
    return shutil.which("mmdc") or next((str(p) for p in _MERMAID_CLI_CANDIDATES if p.exists()), None)


# Company Nexus mirror of minlag/mermaid-cli (verified working on the Authority
# prod host) — the default repo when MERMAID_DOCKER_IMAGE isn't set.
# Upstream public image. Air-gapped sites override with their own mirror via
# MERMAID_IMAGE_REPO (e.g. an internal Nexus proxy).
_DEFAULT_MERMAID_REPO = "minlag/mermaid-cli"


def _resolve_mermaid_image() -> Optional[str]:
    """The mermaid-cli docker image to render with when no local mmdc exists.
    Precedence: MERMAID_DOCKER_IMAGE env var (exact image:tag) > any locally
    pulled tag of the default Nexus repo (_DEFAULT_MERMAID_REPO) > any other
    locally pulled image whose repository name contains 'mermaid-cli' (e.g. a
    bare `docker pull minlag/mermaid-cli` or the ghcr.io upstream) — so a
    plain `docker pull` of the default repo just works with zero config, and
    anything else pulled is still picked up. None when docker is unreachable
    or no matching image is present."""
    img = (os.getenv("MERMAID_DOCKER_IMAGE") or "").strip()
    if img:
        return img
    try:
        out = subprocess.run(
            ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"],
            capture_output=True, timeout=10)
        if out.returncode != 0:
            return None
        candidates = []
        for line in out.stdout.decode("utf-8", "ignore").splitlines():
            line = line.strip()
            repo, _, tag = line.rpartition(":")
            if tag and tag != "<none>" and "mermaid-cli" in repo:
                candidates.append(line)
        default_match = next(
            (c for c in candidates if c.startswith(_DEFAULT_MERMAID_REPO + ":")), None)
        return default_match or (candidates[0] if candidates else None)
    except Exception:  # noqa: BLE001 — docker absent/unreachable → not an error
        return None


def _docker_mermaid_cmd(image: str, in_path: str, output_path: str) -> list[str]:
    """`docker run` argv prefix for rendering via a mermaid-cli image — for
    hosts where installing node/chromium is impractical (air-gapped prod) but
    a docker daemon is available. Only valid when this process runs ON the
    docker host (native prod): the input/output dirs are bind-mounted at their
    own paths so the -i/-o arguments work unchanged; from inside a container
    those paths would not exist on the daemon's host — there, bake mmdc in
    with INSTALL_MERMAID=true instead."""
    in_dir = str(Path(in_path).resolve().parent)
    out_dir = str(Path(output_path).resolve().parent)
    cmd = ["docker", "run", "--rm", "--network=none",
           "-u", f"{os.getuid()}:{os.getgid()}",
           "-v", f"{in_dir}:{in_dir}"]
    if out_dir != in_dir:
        cmd += ["-v", f"{out_dir}:{out_dir}"]
    return cmd + [image]


def generate_mermaid_diagram(mermaid_source: str, output_path: str) -> Optional[str]:
    """Backward-compatible wrapper — returns the PNG path or None. See render_mermaid
    for the (path, error) form used by the generation retry."""
    path, _err = render_mermaid(mermaid_source, output_path)
    return path


def render_mermaid(mermaid_source: str, output_path: str) -> tuple[Optional[str], str]:
    """Render a Mermaid diagram to PNG via the `mmdc` CLI. Returns
    ``(path_or_None, error_text)``.

    mmdc prints its syntax/parse errors to STDERR (stdout just says "Generating
    single mermaid chart"), so on failure we capture that and return it — the
    caller can feed the real parse error back to the model for a fix-and-retry.
    Returns (None, error) when mmdc is unavailable or rendering fails, so the
    caller can fall back to the Pillow renderer."""
    import tempfile
    cli = _find_mermaid_cli()
    docker_image = None if cli else _resolve_mermaid_image()
    if not cli and not docker_image:
        logger.debug("mmdc (mermaid-cli) not found — Pillow fallback will be used")
        return None, "mmdc (mermaid-cli) not found"
    src = re.sub(r"^```(?:mermaid)?\s*", "", (mermaid_source or "").strip(), flags=re.IGNORECASE)
    src = re.sub(r"\s*```$", "", src).strip()
    if not src:
        return None, "empty mermaid source"
    in_path = None
    try:
        # Docker path: the temp .mmd must live somewhere the daemon can bind-mount.
        # The system temp dir may not be (macOS /var/folders, private TMPDIR), but
        # the output dir must be — the container writes the PNG there. So co-locate.
        tmp_dir = None if cli else str(Path(output_path).resolve().parent)
        with tempfile.NamedTemporaryFile("w", suffix=".mmd", delete=False,
                                         encoding="utf-8", dir=tmp_dir) as f:
            f.write(src)
            in_path = f.name
        mmdc_args = ["-i", in_path, "-o", output_path, "-b", "white"]
        if cli:
            cmd = [cli] + mmdc_args
            # In a container Chromium must run with --no-sandbox; a puppeteer config
            # (written by the Dockerfile's mermaid install) supplies that. Pass it when
            # present so mmdc works headless as a non-root/container user.
            cfg = os.getenv("MERMAID_PUPPETEER_CONFIG", "/etc/puppeteer-config.json")
            if Path(cfg).exists():
                cmd += ["-p", cfg]
        else:
            # No local mmdc, but a mermaid-cli docker image is available: render
            # inside the container (image entrypoint IS mmdc, and it ships its
            # own no-sandbox chromium config — no -p needed).
            cmd = _docker_mermaid_cmd(docker_image, in_path, output_path) + mmdc_args
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode == 0 and Path(output_path).exists():
            return output_path, ""
        err = (result.stderr.decode("utf-8", "ignore").strip()
               or result.stdout.decode("utf-8", "ignore").strip())
        logger.warning("mmdc render failed rc=%s: %s", result.returncode, err[:500])
        return None, err
    except subprocess.TimeoutExpired:
        logger.warning("mmdc render timed out")
        return None, "mmdc render timed out"
    except Exception as e:  # noqa: BLE001
        logger.warning("mmdc render error: %s", e)
        return None, str(e)
    finally:
        if in_path:
            Path(in_path).unlink(missing_ok=True)


def fix_activity_syntax(source: str) -> str:
    """Ensure every activity step line ending with text ends with a semicolon."""
    lines = []
    for line in source.split("\n"):
        stripped = line.strip()
        if stripped.startswith(":") and not stripped.endswith((";", ">", "]", "{")):
            line = line.rstrip() + ";"
        lines.append(line)
    return "\n".join(lines)


def generate_plantuml_diagram(plantuml_source: str, output_path: str) -> Optional[str]:
    """
    Render a PlantUML diagram to a PNG using the plantuml.jar CLI.
    Returns output_path on success, None if unavailable or failed.
    """
    jar = _find_plantuml_jar()
    if jar is None:
        logger.debug("plantuml.jar not found — Pillow fallback will be used")
        return None

    source = fix_activity_syntax(plantuml_source)
    # Inject layout pragma for better rendering
    if "!pragma layout smetana" not in source:
        source = source.replace("@startuml", "@startuml\n!pragma layout smetana", 1)

    try:
        result = subprocess.run(
            ["java", "-DPLANTUML_SECURITY_PROFILE=SANDBOX",
             "-jar", str(jar), "-pipe", "-tpng"],
            input=source.encode("utf-8"),
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning(
                "PlantUML exited %d: %s",
                result.returncode,
                result.stderr.decode("utf-8", errors="replace")[:400],
            )
            return None
        if not result.stdout:
            logger.warning("PlantUML returned empty output for diagram")
            return None
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(result.stdout)
        logger.info("PlantUML diagram saved: %s", output_path)
        return output_path
    except FileNotFoundError:
        logger.debug("java not found — Pillow fallback will be used")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("PlantUML timed out for %s", output_path)
        return None
    except Exception as e:
        logger.warning("PlantUML error: %s", e)
        return None

# ---------------------------------------------------------------------------
# Color palette
# ---------------------------------------------------------------------------

COLORS = {
    "bg": (248, 250, 252),
    "white": (255, 255, 255),
    "black": (10, 10, 10),
    "gray": (100, 100, 100),
    "light_gray": (220, 225, 230),
    "border": (180, 190, 200),
    # Actors / nodes
    "actor_bg": (219, 234, 254),     # light blue
    "actor_border": (59, 130, 246),  # blue
    "actor_text": (30, 64, 175),
    # Arrows
    "arrow_forward": (37, 99, 235),
    "arrow_backward": (234, 88, 12),
    "arrow_self": (107, 33, 168),
    # Flowchart nodes
    "start_bg": (187, 247, 208),
    "start_border": (34, 197, 94),
    "end_bg": (254, 202, 202),
    "end_border": (239, 68, 68),
    "process_bg": (219, 234, 254),
    "process_border": (59, 130, 246),
    "decision_bg": (254, 243, 199),
    "decision_border": (245, 158, 11),
    # Swimlane
    "lane_header": (30, 58, 138),
    "lane_header_text": (255, 255, 255),
    "lane_bg_even": (239, 246, 255),
    "lane_bg_odd": (248, 250, 252),
    "activity_bg": (219, 234, 254),
    "activity_border": (59, 130, 246),
    "activity_text": (30, 64, 175),
    "title": (15, 23, 42),
    "subtitle": (71, 85, 105),
    "note_bg": (255, 251, 235),
    "note_border": (245, 158, 11),
}


def _load_font(size: int = 12) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a font — falls back gracefully to default."""
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Windows/Fonts/arial.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _bold_font(size: int = 12) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Arial Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/Windows/Fonts/arialbd.ttf",
    ]
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _wrap_text(text: str, font, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """Wrap text to fit within max_width pixels."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        if bbox[2] - bbox[0] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [text]


def _multiline_text_height(lines: list[str], line_height: int = 16) -> int:
    return max(line_height, len(lines) * line_height)


def _draw_centered_text_block(
    draw: ImageDraw.ImageDraw,
    center: tuple[int, int],
    lines: list[str],
    font,
    fill: tuple,
    line_height: int = 16,
):
    total_h = _multiline_text_height(lines, line_height)
    start_y = center[1] - total_h // 2 + line_height // 2
    for idx, line in enumerate(lines):
        draw.text((center[0], start_y + idx * line_height), line, fill=fill, font=font, anchor="mm")


def _add_diagram_header(
    draw: ImageDraw.ImageDraw,
    width: int,
    title: str,
    subtitle: str = "",
    padding: int = 28,
) -> int:
    title_font = _bold_font(22)
    subtitle_font = _load_font(12)
    draw.text((padding, padding), title, fill=COLORS["title"], font=title_font)
    if subtitle:
        draw.text((padding, padding + 32), subtitle, fill=COLORS["subtitle"], font=subtitle_font)
        return padding + 58
    return padding + 34


def _draw_note_box(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, text: str, font) -> int:
    lines = _wrap_text(text, font, width - 20, draw)
    height = _multiline_text_height(lines, 16) + 16
    draw.rounded_rectangle(
        [(x, y), (x + width, y + height)],
        radius=10,
        fill=COLORS["note_bg"],
        outline=COLORS["note_border"],
        width=2,
    )
    _draw_centered_text_block(draw, (x + width // 2, y + height // 2), lines, font, COLORS["black"])
    return height


def _draw_arrow(draw: ImageDraw.ImageDraw, x1: int, y1: int, x2: int, y2: int,
                color: tuple, label: str = "", font=None, head_size: int = 10):
    """Draw a line with an arrowhead at (x2, y2)."""
    draw.line([(x1, y1), (x2, y2)], fill=color, width=2)
    # Arrowhead
    angle = math.atan2(y2 - y1, x2 - x1)
    for sign in (1, -1):
        ax = x2 - head_size * math.cos(angle - sign * math.pi / 6)
        ay = y2 - head_size * math.sin(angle - sign * math.pi / 6)
        draw.line([(x2, y2), (int(ax), int(ay))], fill=color, width=2)
    # Label
    if label and font:
        mx = (x1 + x2) // 2
        my = (y1 + y2) // 2 - 14
        draw.text((mx, my), label, fill=color, font=font, anchor="mm")


def _draw_rounded_rect(draw: ImageDraw.ImageDraw, x1: int, y1: int, x2: int, y2: int,
                        radius: int, fill: tuple, outline: tuple, width: int = 2):
    draw.rounded_rectangle([(x1, y1), (x2, y2)], radius=radius, fill=fill, outline=outline, width=width)


# ---------------------------------------------------------------------------
# Sequence diagram
# ---------------------------------------------------------------------------

def draw_sequence_diagram(spec: dict, output_path: str) -> str:
    title = spec.get("title", "Sequence Diagram")
    subtitle = spec.get("subtitle", "Interaction flow across key participants")
    actors: list[str] = spec.get("actors", ["User", "System"])
    messages: list[dict] = spec.get("messages", [])
    notes: list[str] = spec.get("notes", [])

    if not actors:
        actors = ["User", "System"]

    ACTOR_W, ACTOR_H = 150, 48
    PADDING = 56
    MSG_SPACING = 58
    TOP_MARGIN = 20
    BOTTOM_MARGIN = 70
    ACTOR_SPACING = 190

    width = PADDING * 2 + max(len(actors) - 1, 0) * ACTOR_SPACING + ACTOR_W
    n_msgs = max(len(messages), 1)
    note_count = min(len(notes), 3)
    height = 140 + TOP_MARGIN + ACTOR_H + 30 + n_msgs * MSG_SPACING + note_count * 48 + BOTTOM_MARGIN

    img = Image.new("RGB", (width, height), COLORS["bg"])
    draw = ImageDraw.Draw(img)

    font_sm = _load_font(11)
    font_md = _load_font(12)
    font_bold = _bold_font(13)
    header_y = _add_diagram_header(draw, width, title, subtitle)

    # Actor X centers
    actor_x: dict[str, int] = {}
    for i, actor in enumerate(actors):
        cx = PADDING + i * ACTOR_SPACING + ACTOR_W // 2
        actor_x[actor] = cx

    # Draw actor boxes
    for actor, cx in actor_x.items():
        x1, y1 = cx - ACTOR_W // 2, header_y
        x2, y2 = cx + ACTOR_W // 2, header_y + ACTOR_H
        _draw_rounded_rect(draw, x1, y1, x2, y2, 8, COLORS["actor_bg"], COLORS["actor_border"])
        wrapped = _wrap_text(actor, font_bold, ACTOR_W - 20, draw)
        _draw_centered_text_block(draw, (cx, (y1 + y2) // 2), wrapped, font_bold, COLORS["actor_text"])

    # Lifeline Y start
    lifeline_y_start = header_y + ACTOR_H
    lifeline_y_end = height - BOTTOM_MARGIN

    # Draw dashed lifelines
    for cx in actor_x.values():
        y = lifeline_y_start
        while y < lifeline_y_end:
            draw.line([(cx, y), (cx, min(y + 8, lifeline_y_end))], fill=COLORS["border"], width=1)
            y += 14

    # Draw messages
    msg_y = lifeline_y_start + 34
    for idx, msg in enumerate(messages, start=1):
        from_actor = msg.get("from_actor", "")
        to_actor = msg.get("to_actor", "")
        label = msg.get("label", "")
        direction = msg.get("direction", "forward")
        label = f"{idx}. {label}" if label else str(idx)

        fx = actor_x.get(from_actor, actor_x.get(actors[0], PADDING + ACTOR_W // 2))
        tx = actor_x.get(to_actor, actor_x.get(actors[-1], width - PADDING - ACTOR_W // 2))

        if from_actor == to_actor:
            # Self-call
            r = 28
            box_x1 = fx + 5
            box_y1 = msg_y - 12
            box_x2 = fx + r * 2 + 5
            box_y2 = msg_y + 12
            draw.rounded_rectangle([(box_x1, box_y1), (box_x2, box_y2)],
                                    radius=6, fill=COLORS["white"], outline=COLORS["arrow_self"], width=2)
            wrapped = _wrap_text(label, font_sm, box_x2 - box_x1 - 10, draw)
            _draw_centered_text_block(draw, ((box_x1 + box_x2) // 2, msg_y), wrapped, font_sm, COLORS["arrow_self"], 14)
        else:
            color = COLORS["arrow_forward"] if direction != "backward" else COLORS["arrow_backward"]
            _draw_arrow(draw, fx, msg_y, tx, msg_y, color, label=label, font=font_sm)

        msg_y += MSG_SPACING

    # Notes at bottom
    note_y = lifeline_y_end + 14
    for note in notes[:3]:
        note_h = _draw_note_box(draw, PADDING, note_y, width - PADDING * 2, note, font_sm)
        note_y += note_h + 10

    img.save(output_path, "PNG", dpi=(150, 150))
    return output_path


# ---------------------------------------------------------------------------
# Flowchart
# ---------------------------------------------------------------------------

def draw_flowchart(spec: dict, output_path: str) -> str:
    title = spec.get("title", "Flowchart")
    subtitle = spec.get("subtitle", "Process overview and decision flow")
    nodes: list[dict] = spec.get("nodes", [])
    edges: list[dict] = spec.get("edges", [])

    if not nodes:
        nodes = [
            {"id": "start", "label": "Start", "node_type": "start"},
            {"id": "process", "label": "Process", "node_type": "process"},
            {"id": "end", "label": "End", "node_type": "end"},
        ]
        edges = [
            {"from_node": "start", "to_node": "process", "label": ""},
            {"from_node": "process", "to_node": "end", "label": ""},
        ]

    NODE_W, NODE_H = 190, 72
    H_GAP, V_GAP = 90, 90
    PADDING = 60

    # Simple top-to-bottom layout (no cycle detection, just linear order)
    n = len(nodes)
    cols = max(1, min(3, math.ceil(math.sqrt(n))))
    rows = math.ceil(n / cols)

    width = PADDING * 2 + cols * NODE_W + (cols - 1) * H_GAP
    height = 150 + PADDING * 2 + rows * NODE_H + (rows - 1) * V_GAP

    img = Image.new("RGB", (width, height), COLORS["bg"])
    draw = ImageDraw.Draw(img)

    font_sm = _load_font(11)
    font_bold = _bold_font(12)
    header_y = _add_diagram_header(draw, width, title, subtitle)

    # Assign positions
    node_pos: dict[str, tuple[int, int]] = {}
    for i, node in enumerate(nodes):
        col = i % cols
        row = i // cols
        cx = PADDING + col * (NODE_W + H_GAP) + NODE_W // 2
        cy = header_y + 30 + row * (NODE_H + V_GAP) + NODE_H // 2
        node_pos[node["id"]] = (cx, cy)

    # Draw edges first
    for edge in edges:
        fn = edge.get("from_node", "")
        tn = edge.get("to_node", "")
        label = edge.get("label", "")
        fp = node_pos.get(fn)
        tp = node_pos.get(tn)
        if fp and tp:
            _draw_arrow(draw, fp[0], fp[1] + NODE_H // 2,
                        tp[0], tp[1] - NODE_H // 2,
                        COLORS["arrow_forward"], label=label, font=font_sm)

    # Draw nodes
    for node in nodes:
        nid = node.get("id", "")
        label = node.get("label", nid)
        ntype = node.get("node_type", "process")
        cx, cy = node_pos.get(nid, (PADDING + NODE_W // 2, PADDING + NODE_H // 2))

        x1 = cx - NODE_W // 2
        y1 = cy - NODE_H // 2
        x2 = cx + NODE_W // 2
        y2 = cy + NODE_H // 2

        if ntype == "start":
            fill, border = COLORS["start_bg"], COLORS["start_border"]
            draw.ellipse([(x1 + 20, y1), (x2 - 20, y2)], fill=fill, outline=border, width=2)
        elif ntype == "end":
            fill, border = COLORS["end_bg"], COLORS["end_border"]
            draw.ellipse([(x1 + 20, y1), (x2 - 20, y2)], fill=fill, outline=border, width=2)
            draw.ellipse([(x1 + 26, y1 + 6), (x2 - 26, y2 - 6)], fill=border)
        elif ntype == "decision":
            fill, border = COLORS["decision_bg"], COLORS["decision_border"]
            pts = [(cx, y1), (x2, cy), (cx, y2), (x1, cy)]
            draw.polygon(pts, fill=fill, outline=border)
        else:
            fill, border = COLORS["process_bg"], COLORS["process_border"]
            _draw_rounded_rect(draw, x1, y1, x2, y2, 8, fill, border)

        wrapped = _wrap_text(label, font_bold, NODE_W - 24, draw)
        _draw_centered_text_block(draw, (cx, cy), wrapped, font_bold, COLORS["black"])

    img.save(output_path, "PNG", dpi=(150, 150))
    return output_path


# ---------------------------------------------------------------------------
# Activity / swimlane diagram
# ---------------------------------------------------------------------------

def draw_activity_diagram(spec: dict, output_path: str) -> str:
    title = spec.get("title", "Activity Diagram")
    subtitle = spec.get("subtitle", "Responsibilities and handoffs across lanes")
    lanes: list[str] = spec.get("lanes", ["Lane 1", "Lane 2"])
    activities: list[dict] = spec.get("activities", [])
    edges: list[dict] = spec.get("edges", [])

    if not lanes:
        lanes = ["Lane 1", "Lane 2"]

    if not activities:
        for i, lane in enumerate(lanes):
            activities.append({"id": f"act_{i}", "label": f"Activity {i+1}", "lane": lane, "row": 0})

    LANE_HEADER_H = 48
    LANE_W = 240
    ACT_W, ACT_H = 190, 60
    ROW_H = 94
    PADDING = 24

    n_lanes = len(lanes)
    max_row = max((a.get("row", 0) for a in activities), default=0)
    n_rows = max_row + 1

    width = PADDING * 2 + n_lanes * LANE_W
    height = 140 + PADDING * 2 + LANE_HEADER_H + n_rows * ROW_H + 40

    img = Image.new("RGB", (width, height), COLORS["bg"])
    draw = ImageDraw.Draw(img)

    font_sm = _load_font(11)
    font_bold = _bold_font(13)
    header_y = _add_diagram_header(draw, width, title, subtitle)

    # Lane x ranges
    lane_x: dict[str, int] = {}
    for i, lane in enumerate(lanes):
        lx = PADDING + i * LANE_W
        lane_x[lane] = lx

        # Lane background
        bg = COLORS["lane_bg_even"] if i % 2 == 0 else COLORS["lane_bg_odd"]
        draw.rectangle([(lx, header_y + LANE_HEADER_H), (lx + LANE_W, height - PADDING)], fill=bg)

        # Lane header
        draw.rectangle([(lx, header_y), (lx + LANE_W, header_y + LANE_HEADER_H)],
                        fill=COLORS["lane_header"], outline=COLORS["border"], width=1)
        wrapped = _wrap_text(lane, font_bold, LANE_W - 20, draw)
        _draw_centered_text_block(draw, (lx + LANE_W // 2, header_y + LANE_HEADER_H // 2), wrapped, font_bold, COLORS["lane_header_text"])

        # Separator
        draw.line([(lx + LANE_W, header_y), (lx + LANE_W, height - PADDING)],
                  fill=COLORS["border"], width=1)

    # Compute activity positions
    act_pos: dict[str, tuple[int, int]] = {}
    for act in activities:
        aid = act.get("id", "")
        lane = act.get("lane", lanes[0])
        row = act.get("row", 0)
        lx = lane_x.get(lane, PADDING)
        cx = lx + LANE_W // 2
        cy = header_y + LANE_HEADER_H + row * ROW_H + ROW_H // 2
        act_pos[aid] = (cx, cy)

    # Draw edges first
    for edge in edges:
        fid = edge.get("from_id", "")
        tid = edge.get("to_id", "")
        label = edge.get("label", "")
        fp = act_pos.get(fid)
        tp = act_pos.get(tid)
        if fp and tp:
            _draw_arrow(draw, fp[0], fp[1] + ACT_H // 2,
                        tp[0], tp[1] - ACT_H // 2,
                        COLORS["arrow_forward"], label=label, font=font_sm)

    # Draw activities
    for act in activities:
        aid = act.get("id", "")
        label = act.get("label", aid)
        cx, cy = act_pos.get(aid, (PADDING + ACT_W // 2, PADDING + LANE_HEADER_H + ACT_H // 2))

        x1 = cx - ACT_W // 2
        y1 = cy - ACT_H // 2
        x2 = cx + ACT_W // 2
        y2 = cy + ACT_H // 2

        _draw_rounded_rect(draw, x1, y1, x2, y2, 8,
                           COLORS["activity_bg"], COLORS["activity_border"])

        wrapped = _wrap_text(label, font_sm, ACT_W - 16, draw)
        _draw_centered_text_block(draw, (cx, cy), wrapped, font_sm, COLORS["activity_text"])

    img.save(output_path, "PNG", dpi=(150, 150))
    return output_path


# ---------------------------------------------------------------------------
# Public dispatcher
# ---------------------------------------------------------------------------

def generate_diagram(spec: dict, diagram_type: str, output_path: str) -> Optional[str]:
    """
    Generate a diagram from a spec dict and save it to output_path.
    Tries PlantUML first (if plantuml_source is present and jar is available),
    then falls back to Pillow-based rendering.
    Returns the output_path on success, None on failure.
    """
    try:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        # --- Mermaid path (if a mermaid source is present) ---
        mermaid_source = spec.get("mermaid_source", "")
        if mermaid_source:
            result = generate_mermaid_diagram(mermaid_source, output_path)
            if result:
                return result

        # --- PlantUML path ---
        plantuml_source = spec.get("plantuml_source", "")
        if plantuml_source and "@startuml" in plantuml_source:
            result = generate_plantuml_diagram(plantuml_source, output_path)
            if result:
                return result

        # --- Pillow fallback ---
        dtype = diagram_type.lower()
        if dtype == "sequence":
            return draw_sequence_diagram(spec, output_path)
        elif dtype == "flowchart":
            return draw_flowchart(spec, output_path)
        elif dtype in ("activity", "swimlane"):
            return draw_activity_diagram(spec, output_path)
        else:
            return draw_flowchart(spec, output_path)
    except Exception as e:
        logger.error("Diagram generation failed for type '%s': %s", diagram_type, e, exc_info=True)
        return None
