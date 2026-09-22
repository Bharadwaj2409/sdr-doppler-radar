#!/usr/bin/env python3
"""
RADAR + OFFLINE SATELLITE MAP
PyQt5 application.

The satellite MBTiles is the BASE MAP. The radar sector, origin,
range rings, bearing line and target are painted ON TOP of the map
using the same Web-Mercator coordinate transform.

Native satellite zoom comes from the MBTiles metadata. The viewer
allows overzoom above native maximum; overzoom only magnifies the
highest native-resolution imagery.

UDP packet format (kept from the original GUI):
    repeated little-endian float32 triples:
        Doppler (Hz), Velocity (m/s), Signal (dBFS)
"""

import sys
import math
import time
import socket
import struct
import sqlite3
from collections import OrderedDict

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QPointF, QRectF
from PyQt5.QtGui import (
    QPainter, QColor, QPen, QBrush, QPolygonF, QFont,
    QRadialGradient, QImage
)
from PyQt5.QtWidgets import (
    QCheckBox,
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QToolButton, QFrame, QLineEdit, QSpinBox,
    QDoubleSpinBox, QSlider, QFormLayout, QSizePolicy, QGroupBox,
    QMessageBox, QScrollArea
)


# ============================================================================
# CONFIG
# ============================================================================

MBTILES_PATH = (
    r"C:\Users\shubh\OneDrive\Documents\VIKRAM\OfflineMap\tiles"
    r"\satellite-2017-11-02_asia_india.mbtiles"
)

TILE_SIZE = 256
VIEWER_MAX_ZOOM = 19
DEFAULT_ZOOM = 12

DEFAULT_RADAR_LAT = 17.385000
DEFAULT_RADAR_LON = 78.486700

MIN_MERCATOR_LAT = -85.05112878
MAX_MERCATOR_LAT = 85.05112878

TILE_CACHE_LIMIT = 500
EARTH_RADIUS_M = 6371008.8


# ============================================================================
# GEO / WEB MERCATOR
# ============================================================================

def clamp_lat(lat):
    return max(MIN_MERCATOR_LAT, min(MAX_MERCATOR_LAT, lat))


def lon_lat_to_world(lon, lat, zoom):
    lat = clamp_lat(lat)
    world = TILE_SIZE * (2 ** zoom)
    x = (lon + 180.0) / 360.0 * world
    lat_rad = math.radians(lat)
    y = (
        1.0 - math.asinh(math.tan(lat_rad)) / math.pi
    ) / 2.0 * world
    return x, y


def world_to_lon_lat(x, y, zoom):
    world = TILE_SIZE * (2 ** zoom)
    lon = x / world * 360.0 - 180.0
    n = math.pi - 2.0 * math.pi * y / world
    lat = math.degrees(math.atan(math.sinh(n)))
    return lon, lat


def destination_point(lat, lon, bearing_deg, distance_m):
    """Geographic destination: bearing 0=N, 90=E."""
    ang = distance_m / EARTH_RADIUS_M
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)
    b = math.radians(bearing_deg)

    lat2 = math.asin(
        math.sin(lat1) * math.cos(ang)
        + math.cos(lat1) * math.sin(ang) * math.cos(b)
    )

    lon2 = lon1 + math.atan2(
        math.sin(b) * math.sin(ang) * math.cos(lat1),
        math.cos(ang) - math.sin(lat1) * math.sin(lat2)
    )

    return (
        math.degrees(lat2),
        (math.degrees(lon2) + 540.0) % 360.0 - 180.0
    )


def meters_per_pixel(lat, zoom):
    """Web-Mercator ground resolution at latitude."""
    world = TILE_SIZE * (2 ** zoom)
    return (
        math.cos(math.radians(lat))
        * 2.0 * math.pi * EARTH_RADIUS_M
        / world
    )


# ============================================================================
# LOGIN
# ============================================================================

class LoginWindow(QMainWindow):
    VALID_USERNAME = "Edgeforce"
    VALID_PASSWORD = "12345"

    login_successful = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Login - Radar Sector Scanner")
        self.resize(1000, 650)
        self.setStyleSheet("background-color: #05100b;")

        central = QWidget()
        self.setCentralWidget(central)

        outer_v = QVBoxLayout(central)
        outer_v.addStretch(1)

        outer_h = QHBoxLayout()
        outer_h.addStretch(1)

        card = QFrame()
        card.setFixedWidth(360)
        card.setStyleSheet("""
            QFrame {
                background-color: #0a1f15;
                border: 1px solid #1f8a56;
                border-radius: 10px;
            }
            QLabel {
                color: #d8ffe9;
                font-size: 13px;
                background: transparent;
                border: none;
            }
            QLineEdit {
                background-color: #0f3d28;
                color: #d8ffe9;
                border: 1px solid #1f8a56;
                border-radius: 4px;
                padding: 8px;
                font-size: 13px;
            }
            QPushButton {
                color: #d8ffe9;
                background-color: #0f3d28;
                border: 1px solid #1f8a56;
                border-radius: 4px;
                padding: 8px 12px;
                font-size: 13px;
            }
            QPushButton:hover { background-color: #17603d; }
        """)

        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(30, 30, 30, 30)
        card_layout.setSpacing(14)

        title = QLabel("Radar Sector Scanner")
        title.setStyleSheet(
            "color:#6dffb0;font-size:18px;font-weight:bold;"
            "background:transparent;border:none;"
        )
        title.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(title)

        subtitle = QLabel("Please sign in to continue")
        subtitle.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(subtitle)

        form = QFormLayout()
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("Username")
        self.pass_edit = QLineEdit()
        self.pass_edit.setPlaceholderText("Password")
        self.pass_edit.setEchoMode(QLineEdit.Password)
        form.addRow("Username:", self.user_edit)
        form.addRow("Password:", self.pass_edit)
        card_layout.addLayout(form)

        self.error_label = QLabel("")
        self.error_label.setStyleSheet(
            "color:#ff5555;font-size:12px;background:transparent;border:none;"
        )
        self.error_label.setAlignment(Qt.AlignCenter)
        card_layout.addWidget(self.error_label)

        self.login_btn = QPushButton("Login")
        self.login_btn.setDefault(True)
        card_layout.addWidget(self.login_btn)

        outer_h.addWidget(card)
        outer_h.addStretch(1)
        outer_v.addLayout(outer_h)
        outer_v.addStretch(1)

        self.login_btn.clicked.connect(self._try_login)
        self.pass_edit.returnPressed.connect(self._try_login)
        self.user_edit.returnPressed.connect(
            lambda: self.pass_edit.setFocus()
        )

    def _try_login(self):
        if (
            self.user_edit.text().strip() == self.VALID_USERNAME
            and self.pass_edit.text() == self.VALID_PASSWORD
        ):
            self.login_successful.emit()
        else:
            self.error_label.setText("Invalid username or password.")
            self.pass_edit.clear()
            self.pass_edit.setFocus()


# ============================================================================
# UDP RECEIVER
# ============================================================================

class UdpReceiver(QThread):
    data_received = pyqtSignal(float, float, float)
    status_changed = pyqtSignal(str, bool)

    VECTOR_SIZE = 12
    BUFFER_SIZE = 65536
    RCVBUF_BYTES = 8 * 1024 * 1024

    def __init__(self, ip="0.0.0.0", port=5005, parent=None):
        super().__init__(parent)
        self.ip = ip
        self.port = port
        self._running = False
        self.sock = None
        self.emit_interval = 0.1

    def set_emit_rate_hz(self, hz):
        hz = max(0.2, min(60.0, hz))
        self.emit_interval = 1.0 / hz

    def run(self):
        self._running = True

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
            )
            self.sock.setsockopt(
                socket.SOL_SOCKET, socket.SO_RCVBUF, self.RCVBUF_BYTES
            )
            self.sock.bind((self.ip, self.port))
            self.sock.settimeout(0.5)
            self.status_changed.emit(
                f"Listening on {self.ip}:{self.port}", False
            )
        except OSError as e:
            self.status_changed.emit(f"Bind failed: {e}", True)
            self._running = False
            return

        last_emit = 0.0

        while self._running:
            try:
                data, _addr = self.sock.recvfrom(self.BUFFER_SIZE)
            except socket.timeout:
                continue
            except OSError:
                break

            if len(data) < self.VECTOR_SIZE:
                continue

            n = len(data) // self.VECTOR_SIZE
            start = (n - 1) * self.VECTOR_SIZE

            try:
                doppler_hz, velocity_ms, signal_dbfs = struct.unpack(
                    "<fff", data[start:start + self.VECTOR_SIZE]
                )
            except struct.error:
                continue

            now = time.time()
            if now - last_emit >= self.emit_interval:
                self.data_received.emit(
                    signal_dbfs, doppler_hz, velocity_ms
                )
                last_emit = now

        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass

        self.status_changed.emit("Listener stopped", False)

    def stop(self):
        self._running = False
        self.wait(1500)


# ============================================================================
# MBTILES
# ============================================================================

class MBTilesReader:
    def __init__(self, filename):
        self.filename = filename

        uri = "file:" + filename.replace("\\", "/") + "?mode=ro"
        self.db = sqlite3.connect(
            uri, uri=True, check_same_thread=False
        )
        self.db.execute("PRAGMA query_only=ON")

        self.metadata = dict(
            self.db.execute(
                "SELECT name,value FROM metadata"
            ).fetchall()
        )

        self.minzoom = int(float(self.metadata.get("minzoom", 0)))
        self.native_maxzoom = int(
            float(self.metadata.get("maxzoom", 13))
        )
        self.scheme = self.metadata.get("scheme", "tms").lower()
        self.cache = OrderedDict()

        self.bounds = self._parse_bounds(
            self.metadata.get("bounds")
        )
        self.center = self._parse_center(
            self.metadata.get("center")
        )

    @staticmethod
    def _parse_bounds(value):
        if not value:
            return None
        try:
            p = [float(x) for x in value.split(",")]
            return tuple(p) if len(p) == 4 else None
        except Exception:
            return None

    @staticmethod
    def _parse_center(value):
        if not value:
            return None
        try:
            p = [float(x) for x in value.split(",")]
            if len(p) >= 2:
                return p[0], p[1], p[2] if len(p) >= 3 else DEFAULT_ZOOM
        except Exception:
            pass
        return None

    def _native_tile(self, z, x, y_xyz):
        n = 2 ** z
        x %= n

        if y_xyz < 0 or y_xyz >= n:
            return None

        y_db = n - 1 - y_xyz if self.scheme == "tms" else y_xyz
        key = (z, x, y_db)

        if key in self.cache:
            image = self.cache.pop(key)
            self.cache[key] = image
            return image

        row = self.db.execute(
            """
            SELECT tile_data
            FROM tiles
            WHERE zoom_level=? AND tile_column=? AND tile_row=?
            LIMIT 1
            """,
            (z, x, y_db)
        ).fetchone()

        if row is None:
            return None

        image = QImage()
        if not image.loadFromData(row[0]):
            return None

        self.cache[key] = image
        while len(self.cache) > TILE_CACHE_LIMIT:
            self.cache.popitem(last=False)

        return image

    def display_tile(self, z, x, y):
        """
        For z <= native maximum, return the native tile.
        For overzoom, return the appropriate sub-rectangle of the
        highest native-resolution tile.
        """
        if z <= self.native_maxzoom:
            img = self._native_tile(z, x, y)
            if img is None:
                return None, None
            return img, QRectF(0, 0, img.width(), img.height())

        delta = z - self.native_maxzoom
        scale = 2 ** delta

        parent_x = x // scale
        parent_y = y // scale

        img = self._native_tile(
            self.native_maxzoom, parent_x, parent_y
        )
        if img is None:
            return None, None

        part = TILE_SIZE / scale
        sx = (x % scale) * part
        sy = (y % scale) * part

        return img, QRectF(sx, sy, part, part)

    def close(self):
        self.db.close()


# ============================================================================
# GEOGRAPHIC RADAR OVERLAY
# ============================================================================

class GeoRadarMapWidget(QWidget):
    coordinate_changed = pyqtSignal(float, float)
    zoom_changed = pyqtSignal(int)

    def __init__(self, tiles, parent=None):
        super().__init__(parent)

        self.tiles = tiles
        self.setMinimumSize(650, 500)
        self.setMouseTracking(True)

        # ---------------- Map camera ----------------
        self.zoom = DEFAULT_ZOOM
        self.center_lat = DEFAULT_RADAR_LAT
        self.center_lon = DEFAULT_RADAR_LON

        if self.tiles.center:
            self.center_lon = self.tiles.center[0]
            self.center_lat = self.tiles.center[1]
            self.zoom = int(round(self.tiles.center[2]))

        self.zoom = max(
            self.tiles.minzoom,
            min(VIEWER_MAX_ZOOM, self.zoom)
        )

        # ---------------- Radar GPS position ----------------
        self.radar_lat = DEFAULT_RADAR_LAT
        self.radar_lon = DEFAULT_RADAR_LON

        # ---------------- Sector geometry ----------------
        # Geographic bearing convention:
        #   0   = North
        #   90  = East
        #   180 = South
        #   270 = West
        self.start_angle = 45.0
        self.end_angle = 135.0
        self.bearing_offset = 0.0

        # THIS is the actual physical radius of the radar sector in metres.
        # It is deliberately independent of the map zoom.
        self.max_range_m = 500.0

        # Number of range circles/arcs inside the sector.
        self.ring_count = 5

        # Number of radial spokes inside the sector.
        self.spoke_count = 5

        # Visual controls.
        self.sector_opacity = 35
        self.ring_opacity = 105
        self.boundary_opacity = 220
        self.spoke_opacity = 70
        self.sector_border_width = 2
        self.ring_width = 1
        self.spoke_width = 1

        self.show_rings = True
        self.show_spokes = True
        self.show_range_labels = True
        self.show_bearing_labels = True
        self.show_origin = True
        self.show_target_line = True

        # ---------------- Target ----------------
        self.target_visible = False
        self.target_lat = None
        self.target_lon = None
        self.target_range_m = 0.0
        self.target_bearing = 90.0
        self.target_signal = None
        self.target_velocity = None
        self.target_size = 8

        # ---------------- Mouse ----------------
        self.dragging = False
        self.last_mouse = None
        self.mouse_lat = None
        self.mouse_lon = None

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)
        self.timer.start(33)

    # ==================================================================
    # CONFIGURATION
    # ==================================================================

    def set_radar_position(self, lat, lon):
        self.radar_lat = float(lat)
        self.radar_lon = float(lon)
        self.update()

    def set_sector(self, start_deg, end_deg):
        start = float(start_deg) % 360.0
        end = float(end_deg) % 360.0

        # Preserve the useful "0..360" sector behavior.
        # If equal, treat it as a full 360 degree circle.
        if abs(end - start) < 0.0001:
            end = start + 360.0
        elif end <= start:
            end += 360.0

        self.start_angle = start
        self.end_angle = end
        self.update()

    def set_bearing_offset(self, value):
        self.bearing_offset = float(value)
        self.update()

    def set_max_range(self, value):
        self.max_range_m = max(1.0, float(value))
        self.fit_radar_circle(self.max_range_m)

    def set_ring_count(self, value):
        self.ring_count = max(0, min(50, int(value)))
        self.update()

    def set_spoke_count(self, value):
        self.spoke_count = max(0, min(50, int(value)))
        self.update()

    def set_visuals(
        self,
        sector_opacity,
        ring_opacity,
        boundary_opacity,
        spoke_opacity,
        border_width,
        ring_width,
        spoke_width,
        show_rings,
        show_spokes,
        show_range_labels,
        show_bearing_labels,
        show_origin,
        show_target_line
    ):
        self.sector_opacity = int(sector_opacity)
        self.ring_opacity = int(ring_opacity)
        self.boundary_opacity = int(boundary_opacity)
        self.spoke_opacity = int(spoke_opacity)
        self.sector_border_width = max(1, int(border_width))
        self.ring_width = max(1, int(ring_width))
        self.spoke_width = max(1, int(spoke_width))

        self.show_rings = bool(show_rings)
        self.show_spokes = bool(show_spokes)
        self.show_range_labels = bool(show_range_labels)
        self.show_bearing_labels = bool(show_bearing_labels)
        self.show_origin = bool(show_origin)
        self.show_target_line = bool(show_target_line)

        self.update()

    def set_target(
        self,
        visible,
        bearing,
        range_m,
        signal_dbfs,
        velocity_ms
    ):
        self.target_visible = bool(visible)

        # Bearing supplied to this method is the sector-relative bearing.
        # Apply the configurable physical mounting/heading offset here.
        self.target_bearing = (
            float(bearing) + self.bearing_offset
        ) % 360.0

        self.target_range_m = max(0.0, float(range_m))
        self.target_signal = signal_dbfs
        self.target_velocity = velocity_ms

        if self.target_visible:
            self.target_lat, self.target_lon = destination_point(
                self.radar_lat,
                self.radar_lon,
                self.target_bearing,
                self.target_range_m
            )
        else:
            self.target_lat = None
            self.target_lon = None

        self.update()

    # ==================================================================
    # EFFECTIVE GEOGRAPHIC SECTOR
    # ==================================================================

    def effective_start(self):
        return self.start_angle + self.bearing_offset

    def effective_end(self):
        return self.end_angle + self.bearing_offset

    def effective_span(self):
        return self.end_angle - self.start_angle

    def effective_center(self):
        return (
            self.effective_start()
            + self.effective_span() / 2.0
        )

    # ==================================================================
    # CAMERA
    # ==================================================================

    def center_world(self):
        return lon_lat_to_world(
            self.center_lon,
            self.center_lat,
            self.zoom
        )

    def fit_radar_circle(self, radius_m=None):
        """Zoom and center so the physical radar circle/sector is large on screen.

        The radar radius is geographic (metres), so the required map zoom is
        calculated from Web-Mercator ground resolution rather than using a
        fixed zoom number. This keeps the radar circle visually similar even
        when the configured range changes.
        """
        if radius_m is None:
            radius_m = self.max_range_m

        radius_m = max(1.0, float(radius_m))

        # Leave some margin around the radar circle. 42% of the smaller
        # viewport dimension is used for the radius.
        viewport = max(200.0, min(self.width(), self.height()))
        desired_radius_px = max(80.0, viewport * 0.42)

        # Ground resolution needed to put radius_m at desired_radius_px.
        target_mpp = radius_m / desired_radius_px

        # Web-Mercator resolution:
        # mpp = cos(lat) * 2*pi*R / (256 * 2^zoom)
        lat_factor = max(
            0.01,
            math.cos(math.radians(clamp_lat(self.radar_lat)))
        )
        numerator = (
            lat_factor * 2.0 * math.pi * EARTH_RADIUS_M
        )
        zoom_float = math.log2(
            numerator / (TILE_SIZE * target_mpp)
        )

        zoom = int(round(zoom_float))
        zoom = max(
            self.tiles.minzoom,
            min(VIEWER_MAX_ZOOM, zoom)
        )

        self.zoom = zoom
        self.center_lat = clamp_lat(self.radar_lat)
        self.center_lon = float(self.radar_lon)
        self.zoom_changed.emit(self.zoom)
        self.update()

    def set_center(self, lat, lon, zoom=None):
        self.center_lat = clamp_lat(float(lat))
        self.center_lon = float(lon)

        if zoom is not None:
            self.zoom = max(
                self.tiles.minzoom,
                min(VIEWER_MAX_ZOOM, int(zoom))
            )
            self.zoom_changed.emit(self.zoom)

        self.update()

    def _pan(self, dx, dy):
        cx, cy = self.center_world()
        cx -= dx
        cy -= dy

        lon, lat = world_to_lon_lat(
            cx, cy, self.zoom
        )

        self.center_lon = lon
        self.center_lat = clamp_lat(lat)

    # ==================================================================
    # MOUSE
    # ==================================================================

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.dragging = True
            self.last_mouse = event.pos()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.dragging = False
            self.setCursor(Qt.ArrowCursor)

    def mouseMoveEvent(self, event):
        p = event.pos()
        cx, cy = self.center_world()

        wx = cx + p.x() - self.width() / 2.0
        wy = cy + p.y() - self.height() / 2.0

        lon, lat = world_to_lon_lat(
            wx, wy, self.zoom
        )

        self.mouse_lon = lon
        self.mouse_lat = lat
        self.coordinate_changed.emit(lat, lon)

        if self.dragging:
            self._pan(
                p.x() - self.last_mouse.x(),
                p.y() - self.last_mouse.y()
            )
            self.last_mouse = p

        self.update()

    def wheelEvent(self, event):
        d = event.angleDelta().y()
        if not d:
            return

        old_z = self.zoom
        new_z = (
            min(VIEWER_MAX_ZOOM, old_z + 1)
            if d > 0
            else max(self.tiles.minzoom, old_z - 1)
        )

        if new_z == old_z:
            return

        # Keep the geographic point under the mouse fixed.
        mx = event.position().x()
        my = event.position().y()

        old_cx, old_cy = self.center_world()
        wx = old_cx + mx - self.width() / 2.0
        wy = old_cy + my - self.height() / 2.0

        lon, lat = world_to_lon_lat(
            wx, wy, old_z
        )

        self.zoom = new_z

        nwx, nwy = lon_lat_to_world(
            lon, lat, new_z
        )

        self.center_lon, self.center_lat = world_to_lon_lat(
            nwx - mx + self.width() / 2.0,
            nwy - my + self.height() / 2.0,
            new_z
        )

        self.zoom_changed.emit(self.zoom)
        self.update()

    # ==================================================================
    # GEOGRAPHIC PROJECTION
    # ==================================================================

    def geo_to_screen(self, lat, lon, left, top):
        wx, wy = lon_lat_to_world(
            lon, lat, self.zoom
        )

        return QPointF(
            wx - left,
            wy - top
        )

    # ==================================================================
    # GEOGRAPHIC RADAR SHAPES
    # ==================================================================

    def _sector_points(self, radius_m):
        span = self.effective_span()
        steps = max(32, int(span / 2.0))

        points = [
            (self.radar_lat, self.radar_lon)
        ]

        for i in range(steps + 1):
            bearing = (
                self.effective_start()
                + span * i / steps
            )

            points.append(
                destination_point(
                    self.radar_lat,
                    self.radar_lon,
                    bearing,
                    radius_m
                )
            )

        return points

    def _arc_points(self, radius_m):
        """
        One range ring is an ARC INSIDE THE RADAR SECTOR, rather than
        a complete 360-degree circle.
        """
        span = self.effective_span()
        steps = max(32, int(span / 2.0))

        return [
            destination_point(
                self.radar_lat,
                self.radar_lon,
                self.effective_start()
                + span * i / steps,
                radius_m
            )
            for i in range(steps + 1)
        ]

    def _bearing_point(self, bearing, radius_m):
        return destination_point(
            self.radar_lat,
            self.radar_lon,
            bearing,
            radius_m
        )

    # ==================================================================
    # PAINTING
    # ==================================================================

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setRenderHint(
            QPainter.SmoothPixmapTransform,
            False
        )

        painter.fillRect(
            self.rect(),
            QColor(8, 14, 12)
        )

        cx, cy = self.center_world()

        left = cx - self.width() / 2.0
        top = cy - self.height() / 2.0
        right = cx + self.width() / 2.0
        bottom = cy + self.height() / 2.0

        self._draw_tiles(
            painter,
            left,
            top,
            right,
            bottom
        )

        self._draw_radar_overlay(
            painter,
            left,
            top
        )

        self._draw_hud(painter)

        painter.end()

    def _draw_tiles(
        self,
        painter,
        left,
        top,
        right,
        bottom
    ):
        first_x = math.floor(left / TILE_SIZE)
        last_x = math.floor(right / TILE_SIZE)
        first_y = math.floor(top / TILE_SIZE)
        last_y = math.floor(bottom / TILE_SIZE)

        n = 2 ** self.zoom

        for ty in range(first_y, last_y + 1):
            if ty < 0 or ty >= n:
                continue

            for tx in range(first_x, last_x + 1):
                wx = tx % n

                image, source = self.tiles.display_tile(
                    self.zoom,
                    wx,
                    ty
                )

                if image is None:
                    continue

                target = QRectF(
                    tx * TILE_SIZE - left,
                    ty * TILE_SIZE - top,
                    TILE_SIZE,
                    TILE_SIZE
                )

                painter.drawImage(
                    target,
                    image,
                    source
                )

    def _draw_radar_overlay(
        self,
        painter,
        left,
        top
    ):
        # ==============================================================
        # ORIGIN
        # ==============================================================

        origin = self.geo_to_screen(
            self.radar_lat,
            self.radar_lon,
            left,
            top
        )

        # ==============================================================
        # SECTOR FILL
        # ==============================================================

        sector_geo = self._sector_points(
            self.max_range_m
        )

        sector_screen = [
            self.geo_to_screen(
                lat,
                lon,
                left,
                top
            )
            for lat, lon in sector_geo
        ]

        painter.setBrush(
            QBrush(
                QColor(
                    0,
                    255,
                    120,
                    self.sector_opacity
                )
            )
        )

        painter.setPen(
            QPen(
                QColor(
                    0,
                    255,
                    150,
                    self.boundary_opacity
                ),
                self.sector_border_width
            )
        )

        painter.drawPolygon(
            QPolygonF(sector_screen)
        )

        # ==============================================================
        # RANGE ARCS
        # ==============================================================

        if self.show_rings and self.ring_count > 0:
            for i in range(1, self.ring_count + 1):
                radius = (
                    self.max_range_m
                    * i
                    / self.ring_count
                )

                ring_geo = self._arc_points(radius)

                ring_screen = [
                    self.geo_to_screen(
                        lat,
                        lon,
                        left,
                        top
                    )
                    for lat, lon in ring_geo
                ]

                painter.setBrush(Qt.NoBrush)

                painter.setPen(
                    QPen(
                        QColor(
                            0,
                            255,
                            150,
                            self.ring_opacity
                        ),
                        self.ring_width,
                        Qt.DashLine
                    )
                )

                painter.drawPolyline(
                    QPolygonF(ring_screen)
                )

                # Range label.
                if self.show_range_labels:
                    label_geo = self._bearing_point(
                        self.effective_start(),
                        radius
                    )

                    lp = self.geo_to_screen(
                        label_geo[0],
                        label_geo[1],
                        left,
                        top
                    )

                    painter.setPen(
                        QPen(
                            QColor(
                                80,
                                255,
                                180,
                                min(
                                    255,
                                    self.ring_opacity + 70
                                )
                            )
                        )
                    )

                    painter.setFont(
                        QFont("Consolas", 8)
                    )

                    painter.drawText(
                        lp + QPointF(5, -4),
                        f"{radius:.0f} m"
                    )

        # ==============================================================
        # RADIAL SPOKES
        # ==============================================================

        if self.show_spokes and self.spoke_count > 0:
            span = self.effective_span()

            # Number requested means interior spokes.
            # Boundaries are always drawn separately.
            for i in range(1, self.spoke_count + 1):
                bearing = (
                    self.effective_start()
                    + span * i
                    / (self.spoke_count + 1)
                )

                lat, lon = self._bearing_point(
                    bearing,
                    self.max_range_m
                )

                p = self.geo_to_screen(
                    lat,
                    lon,
                    left,
                    top
                )

                painter.setPen(
                    QPen(
                        QColor(
                            0,
                            255,
                            150,
                            self.spoke_opacity
                        ),
                        self.spoke_width
                    )
                )

                painter.drawLine(
                    origin,
                    p
                )

                # Bearing labels on spoke ends.
                if self.show_bearing_labels:
                    painter.setPen(
                        QPen(
                            QColor(
                                150,
                                255,
                                210,
                                self.spoke_opacity + 50
                            )
                        )
                    )

                    painter.setFont(
                        QFont("Consolas", 8)
                    )

                    painter.drawText(
                        p + QPointF(4, -4),
                        f"{bearing % 360:.0f}°"
                    )

        # ==============================================================
        # SECTOR BOUNDARIES
        # ==============================================================

        for bearing in (
            self.effective_start(),
            self.effective_end()
        ):
            lat, lon = self._bearing_point(
                bearing,
                self.max_range_m
            )

            p = self.geo_to_screen(
                lat,
                lon,
                left,
                top
            )

            painter.setPen(
                QPen(
                    QColor(
                        0,
                        255,
                        170,
                        self.boundary_opacity
                    ),
                    max(
                        2,
                        self.sector_border_width
                    )
                )
            )

            painter.drawLine(
                origin,
                p
            )

        # ==============================================================
        # CENTER BEARING
        # ==============================================================

        center_bearing = self.effective_center()

        lat, lon = self._bearing_point(
            center_bearing,
            self.max_range_m
        )

        center_end = self.geo_to_screen(
            lat,
            lon,
            left,
            top
        )

        painter.setPen(
            QPen(
                QColor(
                    0,
                    255,
                    220,
                    180
                ),
                2,
                Qt.DashDotLine
            )
        )

        painter.drawLine(
            origin,
            center_end
        )

        # ==============================================================
        # TARGET
        # ==============================================================

        if (
            self.target_visible
            and self.target_lat is not None
        ):
            target = self.geo_to_screen(
                self.target_lat,
                self.target_lon,
                left,
                top
            )

            size = max(3, int(self.target_size))

            painter.setPen(
                QPen(
                    QColor(
                        255,
                        40,
                        40,
                        240
                    ),
                    2
                )
            )

            painter.setBrush(
                QBrush(
                    QColor(
                        255,
                        30,
                        30,
                        220
                    )
                )
            )

            painter.drawEllipse(
                target,
                size,
                size
            )

            painter.setBrush(Qt.NoBrush)

            painter.setPen(
                QPen(
                    QColor(
                        255,
                        100,
                        100,
                        220
                    ),
                    1
                )
            )

            painter.drawEllipse(
                target,
                size + 8,
                size + 8
            )

            if self.show_target_line:
                painter.setPen(
                    QPen(
                        QColor(
                            255,
                            80,
                            80,
                            160
                        ),
                        1,
                        Qt.DashLine
                    )
                )

                painter.drawLine(
                    origin,
                    target
                )

            painter.setPen(
                QPen(
                    QColor(
                        255,
                        170,
                        170
                    )
                )
            )

            painter.setFont(
                QFont(
                    "Consolas",
                    9,
                    QFont.Bold
                )
            )

            painter.drawText(
                target + QPointF(14, -8),
                (
                    f"TARGET\n"
                    f"R {self.target_range_m:.1f} m\n"
                    f"B {self.target_bearing:.1f}°"
                )
            )

        # ==============================================================
        # ORIGIN MARKER
        # ==============================================================

        if self.show_origin:
            painter.setPen(
                QPen(
                    QColor(
                        255,
                        255,
                        255
                    ),
                    2
                )
            )

            painter.setBrush(
                QBrush(
                    QColor(
                        0,
                        255,
                        150
                    )
                )
            )

            painter.drawEllipse(
                origin,
                7,
                7
            )

            painter.setBrush(Qt.NoBrush)

            painter.setPen(
                QPen(
                    QColor(
                        0,
                        255,
                        180,
                        220
                    ),
                    1
                )
            )

            painter.drawEllipse(
                origin,
                14,
                14
            )

            painter.drawLine(
                origin + QPointF(-20, 0),
                origin + QPointF(20, 0)
            )

            painter.drawLine(
                origin + QPointF(0, -20),
                origin + QPointF(0, 20)
            )

            painter.setPen(
                QPen(
                    QColor(
                        180,
                        255,
                        220
                    )
                )
            )

            painter.setFont(
                QFont(
                    "Consolas",
                    8,
                    QFont.Bold
                )
            )

            painter.drawText(
                origin + QPointF(12, 24),
                (
                    f"RADAR\n"
                    f"{self.radar_lat:.6f}, "
                    f"{self.radar_lon:.6f}"
                )
            )

    def _draw_hud(self, painter):
        painter.setFont(
            QFont(
                "Consolas",
                9,
                QFont.Bold
            )
        )

        painter.setPen(
            QPen(
                QColor(
                    255,
                    255,
                    235
                )
            )
        )



        painter.drawText(
            12,
            20,
            f"OFFLINE SATELLITE | Z{self.zoom}"
        )

        painter.drawText(
            12,
            38,
            (
                f"RADAR GPS "
                f"{self.radar_lat:.6f}, "
                f"{self.radar_lon:.6f}"
            )
        )

        painter.drawText(
            12,
            56,
            (
                f"SECTOR "
                f"{self.effective_start() % 360:.1f}° - "
                f"{self.effective_end() % 360:.1f}° | "
                f"RADIUS {self.max_range_m:.1f} m | "
                f"RINGS {self.ring_count}"
            )
        )

        painter.drawText(
            12,
            74,
            (
                f"SPOKES {self.spoke_count} | "
                f"OFFSET {self.bearing_offset:+.1f}°"
            )
        )

        if self.mouse_lat is not None:
            painter.setFont(
                QFont("Consolas", 9)
            )

            painter.drawText(
                12,
                self.height() - 14,
                (
                    f"CURSOR "
                    f"{self.mouse_lat:.6f}, "
                    f"{self.mouse_lon:.6f}"
                )
            )

        if self.zoom > self.tiles.native_maxzoom:
            painter.setPen(
                QPen(
                    QColor(
                        255,
                        220,
                        100
                    )
                )
            )

            painter.drawText(
                self.width() - 280,
                20,
                (
                    f"OVERZOOM | NATIVE Z"
                    f"{self.tiles.native_maxzoom}"
                )
            )


# ============================================================================
# SETTINGS
# ============================================================================

class CollapsibleSection(QFrame):
    """Tree/accordion style settings block."""
    def __init__(self, title, expanded=False, parent=None):
        super().__init__(parent)
        self.button = QToolButton()
        self.button.setText(title)
        self.button.setCheckable(True)
        self.button.setChecked(expanded)
        self.button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.button.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.button.toggled.connect(self._toggle)
        self.body = QWidget()
        self.layout_body = QVBoxLayout(self.body)
        self.layout_body.setContentsMargins(8, 6, 8, 8)
        self.layout_body.setSpacing(5)
        root=QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(0)
        root.addWidget(self.button); root.addWidget(self.body)
        self.body.setVisible(expanded)
    def _toggle(self, checked):
        self.body.setVisible(checked)
        self.button.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
    def addLayout(self, layout): self.layout_body.addLayout(layout)
    def addWidget(self, widget): self.layout_body.addWidget(widget)


class SettingsPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QFrame { background-color:#0c1512; border:1px solid #0f3d28; border-radius:6px; }
            QLabel { color:#a9ffcf; }
            QToolButton { color:#6dffb0; background:#0f3d28; border:1px solid #14513a; padding:7px; text-align:left; font-weight:bold; }
            QToolButton:hover { background:#17603d; }
            QLineEdit,QSpinBox,QDoubleSpinBox { background:#061109; color:#d8ffe9; border:1px solid #14513a; padding:3px; border-radius:3px; }
            QPushButton { background:#0f3d28; color:#d8ffe9; border:1px solid #1f8a56; border-radius:4px; padding:5px 10px; }
            QPushButton:hover { background:#17603d; }
            QCheckBox { color:#a9ffcf; spacing:6px; }
        """)
        outer=QVBoxLayout(self); outer.setContentsMargins(6,6,6,6); outer.setSpacing(5)
        title=QLabel('RADAR / MAP SETTINGS'); f=QFont(); f.setBold(True); f.setPointSize(13); title.setFont(f); outer.addWidget(title)
        outer.addWidget(QLabel('▶ / ▼ Click a section to expand or collapse it.'))
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); scroll.setFrameShape(QFrame.NoFrame)
        body=QWidget(); bl=QVBoxLayout(body); bl.setContentsMargins(3,3,3,3); bl.setSpacing(5)

        def section(title, expanded=False):
            x=CollapsibleSection(title, expanded); bl.addWidget(x); return x
        def form_section(sec):
            f=QFormLayout(); f.setLabelAlignment(Qt.AlignLeft); sec.addLayout(f); return f

        sec=section('UDP CONNECTION', True); f=form_section(sec)
        self.ip_edit=QLineEdit('0.0.0.0'); self.port_spin=QSpinBox(); self.port_spin.setRange(1,65535); self.port_spin.setValue(5005)
        self.rate_spin=QDoubleSpinBox(); self.rate_spin.setRange(.2,60); self.rate_spin.setValue(10); self.rate_spin.setSuffix(' Hz')
        self.connect_btn=QPushButton('Connect / Listen'); self.connect_btn.setCheckable(True)
        f.addRow('IP:',self.ip_edit); f.addRow('Port:',self.port_spin); f.addRow('GUI update:',self.rate_spin); f.addRow(self.connect_btn)
        info=QLabel('Packet: little-endian float32 × 3\nDoppler Hz | Velocity m/s | Signal dBFS'); info.setWordWrap(True); f.addRow(info)

        sec=section('DETECTION', False); f=form_section(sec)
        self.threshold_spin=QDoubleSpinBox(); self.threshold_spin.setRange(-150,0); self.threshold_spin.setDecimals(2); self.threshold_spin.setValue(-50); self.threshold_spin.setSuffix(' dBFS')
        f.addRow('Signal threshold:',self.threshold_spin)

        sec=section('RADAR SECTOR GEOMETRY', True); f=form_section(sec)
        self.start_spin=QDoubleSpinBox(); self.start_spin.setRange(0,360); self.start_spin.setDecimals(1); self.start_spin.setValue(320); self.start_spin.setSuffix('°')
        self.end_spin=QDoubleSpinBox(); self.end_spin.setRange(0,360); self.end_spin.setDecimals(1); self.end_spin.setValue(45); self.end_spin.setSuffix('°')
        self.offset_spin=QDoubleSpinBox(); self.offset_spin.setRange(-360,360); self.offset_spin.setDecimals(1); self.offset_spin.setValue(0); self.offset_spin.setSuffix('°')
        self.range_spin=QDoubleSpinBox(); self.range_spin.setRange(1,1000000); self.range_spin.setDecimals(1); self.range_spin.setValue(5000); self.range_spin.setSuffix(' m')
        self.ring_count_spin=QSpinBox(); self.ring_count_spin.setRange(0,100); self.ring_count_spin.setValue(5)
        self.spoke_count_spin=QSpinBox(); self.spoke_count_spin.setRange(0,100); self.spoke_count_spin.setValue(5)
        f.addRow('Start bearing:',self.start_spin); f.addRow('End bearing:',self.end_spin); f.addRow('Bearing offset:',self.offset_spin); f.addRow('Sector radius:',self.range_spin); f.addRow('Range circles:',self.ring_count_spin); f.addRow('Inside spokes:',self.spoke_count_spin)

        sec=section('SECTOR DISPLAY CONTROL', False); f=form_section(sec)
        self.sector_opacity_spin=QSpinBox(); self.sector_opacity_spin.setRange(0,255); self.sector_opacity_spin.setValue(35)
        self.ring_opacity_spin=QSpinBox(); self.ring_opacity_spin.setRange(0,255); self.ring_opacity_spin.setValue(105)
        self.boundary_opacity_spin=QSpinBox(); self.boundary_opacity_spin.setRange(0,255); self.boundary_opacity_spin.setValue(220)
        self.spoke_opacity_spin=QSpinBox(); self.spoke_opacity_spin.setRange(0,255); self.spoke_opacity_spin.setValue(70)
        self.border_width_spin=QSpinBox(); self.border_width_spin.setRange(1,10); self.border_width_spin.setValue(2)
        self.ring_width_spin=QSpinBox(); self.ring_width_spin.setRange(1,6); self.ring_width_spin.setValue(1)
        self.spoke_width_spin=QSpinBox(); self.spoke_width_spin.setRange(1,6); self.spoke_width_spin.setValue(1)
        f.addRow('Sector opacity:',self.sector_opacity_spin); f.addRow('Ring opacity:',self.ring_opacity_spin); f.addRow('Boundary opacity:',self.boundary_opacity_spin); f.addRow('Spoke opacity:',self.spoke_opacity_spin); f.addRow('Boundary width:',self.border_width_spin); f.addRow('Ring width:',self.ring_width_spin); f.addRow('Spoke width:',self.spoke_width_spin)

        sec=section('TARGET / VELOCITY', False); f=form_section(sec)
        self.vel_min_spin=QDoubleSpinBox(); self.vel_min_spin.setRange(-1000,1000); self.vel_min_spin.setDecimals(2); self.vel_min_spin.setValue(-5); self.vel_min_spin.setSuffix(' m/s')
        self.vel_max_spin=QDoubleSpinBox(); self.vel_max_spin.setRange(-1000,1000); self.vel_max_spin.setDecimals(2); self.vel_max_spin.setValue(5); self.vel_max_spin.setSuffix(' m/s')
        self.target_size_spin=QSpinBox(); self.target_size_spin.setRange(2,30); self.target_size_spin.setValue(8)
        self.blink_slider=QSlider(Qt.Horizontal); self.blink_slider.setRange(1,10); self.blink_slider.setValue(3)
        self.lifetime_spin=QDoubleSpinBox(); self.lifetime_spin.setRange(.15,10); self.lifetime_spin.setSingleStep(.1); self.lifetime_spin.setValue(.8); self.lifetime_spin.setSuffix(' s')
        f.addRow('Velocity min:',self.vel_min_spin); f.addRow('Velocity max:',self.vel_max_spin); f.addRow('Target size:',self.target_size_spin); f.addRow('Blink:',self.blink_slider); f.addRow('Fade:',self.lifetime_spin)

        sec=section('RADAR GEOGRAPHIC POSITION', False); f=form_section(sec)
        self.radar_lat_spin=QDoubleSpinBox(); self.radar_lat_spin.setRange(-90,90); self.radar_lat_spin.setDecimals(8); self.radar_lat_spin.setValue(DEFAULT_RADAR_LAT)
        self.radar_lon_spin=QDoubleSpinBox(); self.radar_lon_spin.setRange(-180,180); self.radar_lon_spin.setDecimals(8); self.radar_lon_spin.setValue(DEFAULT_RADAR_LON)
        self.center_map_btn=QPushButton('Center Map on Radar')
        f.addRow('Latitude:',self.radar_lat_spin); f.addRow('Longitude:',self.radar_lon_spin); f.addRow(self.center_map_btn)

        sec=section('VISIBILITY', False); f=form_section(sec)
        self.show_rings_cb=QCheckBox('Range circles'); self.show_rings_cb.setChecked(True)
        self.show_spokes_cb=QCheckBox('Inside spokes'); self.show_spokes_cb.setChecked(True)
        self.show_range_labels_cb=QCheckBox('Range labels'); self.show_range_labels_cb.setChecked(True)
        self.show_bearing_labels_cb=QCheckBox('Bearing labels'); self.show_bearing_labels_cb.setChecked(True)
        self.show_origin_cb=QCheckBox('Radar origin'); self.show_origin_cb.setChecked(True)
        self.show_target_line_cb=QCheckBox('Target line'); self.show_target_line_cb.setChecked(True)
        for w in (self.show_rings_cb,self.show_spokes_cb,self.show_range_labels_cb,self.show_bearing_labels_cb,self.show_origin_cb,self.show_target_line_cb): f.addRow(w)

        sec=section('MAP VIEW', False); f=form_section(sec)
        self.map_zoom_spin=QDoubleSpinBox(); self.map_zoom_spin.setRange(0,VIEWER_MAX_ZOOM); self.map_zoom_spin.setDecimals(0); self.map_zoom_spin.setValue(DEFAULT_ZOOM)
        self.fit_map_btn=QPushButton('Zoom to Radar Circle'); self.tile_debug_cb=QCheckBox('Tile debug')
        f.addRow('Zoom:',self.map_zoom_spin); f.addRow(self.fit_map_btn); f.addRow(self.tile_debug_cb)

        self.apply_btn=QPushButton('APPLY SETTINGS'); self.apply_btn.setMinimumHeight(44); self.apply_btn.setStyleSheet('font-weight:bold;')
        bl.addWidget(self.apply_btn); bl.addStretch(1)
        scroll.setWidget(body); outer.addWidget(scroll,1)
        self.setMinimumWidth(360); self.setMaximumWidth(460)


# ============================================================================
# MAIN WINDOW
# ============================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle(
            "Radar Sector Scanner - Geographic Offline Map"
        )
        self.resize(1500, 900)
        self.setStyleSheet(
            "background-color:#05100b;"
        )

        self.udp_thread = None

        try:
            self.tiles = MBTilesReader(
                MBTILES_PATH
            )
        except Exception as e:
            QMessageBox.critical(
                self,
                "MBTiles Error",
                (
                    "Could not open the satellite MBTiles file.\n\n"
                    f"{MBTILES_PATH}\n\n"
                    f"{e}"
                )
            )
            raise

        central = QWidget()
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ==============================================================
        # TOP BAR
        # ==============================================================

        top = QHBoxLayout()

        self.settings_btn = QToolButton()
        self.settings_btn.setText("⚙ Settings")
        self.settings_btn.setCheckable(True)

        self.settings_btn.setStyleSheet("""
            QToolButton {
                color:#d8ffe9;
                background-color:#0f3d28;
                border:1px solid #1f8a56;
                border-radius:4px;
                padding:6px 12px;
            }
            QToolButton:checked {
                background-color:#17603d;
            }
        """)

        top.addWidget(
            self.settings_btn
        )

        self.center_btn = QPushButton(
            "CENTER RADAR"
        )

        self.zoom_out_btn = QPushButton("−")
        self.zoom_in_btn = QPushButton("+")

        self.zoom_out_btn.setFixedWidth(40)
        self.zoom_in_btn.setFixedWidth(40)

        for button in (
            self.center_btn,
            self.zoom_out_btn,
            self.zoom_in_btn
        ):
            button.setStyleSheet("""
                QPushButton {
                    color:#d8ffe9;
                    background-color:#0f3d28;
                    border:1px solid #1f8a56;
                    border-radius:4px;
                    padding:6px 12px;
                }
                QPushButton:hover {
                    background-color:#17603d;
                }
            """)

            top.addWidget(button)

        self.status_label = QLabel(
            "Not connected"
        )

        self.status_label.setStyleSheet(
            "color:#6dffb0;"
            "font-family:Consolas;"
            "padding-left:12px;"
        )

        top.addWidget(
            self.status_label,
            1
        )

        root.addLayout(top)

        # ==============================================================
        # MAP + SETTINGS
        # ==============================================================

        body = QHBoxLayout()

        self.map = GeoRadarMapWidget(
            self.tiles
        )

        self.settings = SettingsPanel()
        self.settings.hide()

        body.addWidget(
            self.map,
            1
        )

        body.addWidget(
            self.settings
        )

        root.addLayout(
            body,
            1
        )

        # ==============================================================
        # SIGNALS
        # ==============================================================

        self.settings_btn.toggled.connect(
            self.settings.setVisible
        )

        self.map.zoom_changed.connect(
            lambda z: self.settings.map_zoom_spin.setValue(z)
            if hasattr(self.settings, "map_zoom_spin") else None
        )

        self.center_btn.clicked.connect(
            self.center_on_radar
        )

        self.zoom_out_btn.clicked.connect(
            lambda: self._button_zoom(-1)
        )

        self.zoom_in_btn.clicked.connect(
            lambda: self._button_zoom(1)
        )

        self.settings.connect_btn.clicked.connect(
            self._toggle_connection
        )

        self.settings.apply_btn.clicked.connect(
            self.apply_settings
        )

        self.settings.center_map_btn.clicked.connect(
            self.center_on_radar
        )

        self.settings.fit_map_btn.clicked.connect(
            self.center_on_radar
        )

        if hasattr(self.settings, "map_zoom_spin"):
            self.settings.map_zoom_spin.valueChanged.connect(
                self._settings_zoom_changed
            )

        self.apply_settings()

    def _settings_zoom_changed(self, value):
        # Manual zoom from the settings panel; keep the radar geographic
        # center fixed while changing only map magnification.
        z = max(
            self.tiles.minzoom,
            min(VIEWER_MAX_ZOOM, int(round(value)))
        )
        self.map.set_center(
            self.map.center_lat,
            self.map.center_lon,
            z
        )

    # ==================================================================
    # FULLSCREEN
    # ==================================================================

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F11:
            self.toggle_fullscreen()
            event.accept()
            return
        if event.key() == Qt.Key_Escape and self.isFullScreen():
            self.showNormal()
            event.accept()
            return
        super().keyPressEvent(event)

    # ==================================================================
    # APPLY SETTINGS
    # ==================================================================

    def apply_settings(self):
        s = self.settings

        # Geographic radar origin.
        self.map.set_radar_position(
            s.radar_lat_spin.value(),
            s.radar_lon_spin.value()
        )

        # Sector angular limits.
        self.map.set_sector(
            s.start_spin.value(),
            s.end_spin.value()
        )

        # Physical sector radius in metres.
        self.map.set_max_range(
            s.range_spin.value()
        )

        # Physical radar heading / mounting correction.
        self.map.set_bearing_offset(
            s.offset_spin.value()
        )

        # Number of range arcs and radial spokes.
        self.map.set_ring_count(
            s.ring_count_spin.value()
        )

        self.map.set_spoke_count(
            s.spoke_count_spin.value()
        )

        # Visual controls.
        self.map.set_visuals(
            s.sector_opacity_spin.value(),
            s.ring_opacity_spin.value(),
            s.boundary_opacity_spin.value(),
            s.spoke_opacity_spin.value(),
            s.border_width_spin.value(),
            s.ring_width_spin.value(),
            s.spoke_width_spin.value(),
            s.show_rings_cb.isChecked(),
            s.show_spokes_cb.isChecked(),
            s.show_range_labels_cb.isChecked(),
            s.show_bearing_labels_cb.isChecked(),
            s.show_origin_cb.isChecked(),
            s.show_target_line_cb.isChecked()
        )

        self.map.target_size = (
            s.target_size_spin.value()
        )

        # Map view: the radar-focused view is calculated from the
        # physical radar range instead of forcing a fixed zoom value.
        if hasattr(s, "map_zoom_spin"):
            s.map_zoom_spin.setValue(self.map.zoom)

        self.center_on_radar()

    def center_on_radar(self):
        lat = self.settings.radar_lat_spin.value()
        lon = self.settings.radar_lon_spin.value()

        self.map.set_radar_position(lat, lon)
        self.map.fit_radar_circle(self.settings.range_spin.value())

    def _button_zoom(self, direction):
        z = self.map.zoom + direction

        z = max(
            self.tiles.minzoom,
            min(
                VIEWER_MAX_ZOOM,
                z
            )
        )

        self.map.set_center(
            self.map.center_lat,
            self.map.center_lon,
            z
        )

    # ==================================================================
    # UDP
    # ==================================================================

    def _toggle_connection(self, checked):
        if checked:
            ip = (
                self.settings.ip_edit.text().strip()
                or "0.0.0.0"
            )

            port = (
                self.settings.port_spin.value()
            )

            self.udp_thread = UdpReceiver(
                ip,
                port
            )

            self.udp_thread.set_emit_rate_hz(
                self.settings.rate_spin.value()
            )

            self.udp_thread.data_received.connect(
                self._on_data
            )

            self.udp_thread.status_changed.connect(
                self._on_status
            )

            self.udp_thread.start()

            self.settings.connect_btn.setText(
                "Disconnect"
            )

            self.settings.ip_edit.setEnabled(False)
            self.settings.port_spin.setEnabled(False)

        else:
            if self.udp_thread:
                self.udp_thread.stop()
                self.udp_thread = None

            self.settings.connect_btn.setText(
                "Connect / Listen"
            )

            self.settings.ip_edit.setEnabled(True)
            self.settings.port_spin.setEnabled(True)

            self.map.set_target(
                False,
                0,
                0,
                None,
                None
            )

    def _on_data(
        self,
        signal_dbfs,
        doppler_hz,
        velocity_ms
    ):
        s = self.settings

        threshold = (
            s.threshold_spin.value()
        )

        detected = (
            signal_dbfs > threshold
        )

        vmin = (
            s.vel_min_spin.value()
        )

        vmax = (
            s.vel_max_spin.value()
        )

        max_range = (
            s.range_spin.value()
        )

        if vmax <= vmin:
            vmax = vmin + 0.1

        # Keep the original radar behavior:
        # velocity_min -> near/origin
        # velocity_max -> far/max radius.
        frac = (
            velocity_ms - vmin
        ) / (
            vmax - vmin
        )

        frac = max(
            0.02,
            min(
                0.98,
                frac
            )
        )

        range_m = (
            frac * max_range
        )

        # Target stays on the center of the configured sector.
        start = s.start_spin.value()
        end = s.end_spin.value()

        if abs(end - start) < 0.0001:
            span = 360.0
        elif end <= start:
            span = end + 360.0 - start
        else:
            span = end - start

        bearing = (
            start
            + span / 2.0
        )

        self.map.set_target(
            detected,
            bearing,
            range_m,
            signal_dbfs,
            velocity_ms
        )

        self.status_label.setText(
            (
                f"Signal {signal_dbfs:.2f} dBFS | "
                f"Doppler {doppler_hz:.2f} Hz | "
                f"Velocity {velocity_ms:.3f} m/s | "
                f"Range {range_m:.2f} m | "
                f"Bearing "
                f"{(bearing + s.offset_spin.value()) % 360:.1f}°"
            )
        )

    def _on_status(
        self,
        message,
        is_error
    ):
        color = (
            "#ff5555"
            if is_error
            else "#6dffb0"
        )

        self.status_label.setStyleSheet(
            f"color:{color};"
            "font-family:Consolas;"
            "padding-left:12px;"
        )

        self.status_label.setText(
            message
        )

    # ==================================================================
    # CLOSE
    # ==================================================================

    def closeEvent(self, event):
        if self.udp_thread:
            self.udp_thread.stop()
            self.udp_thread = None

        try:
            self.tiles.close()
        except Exception:
            pass

        event.accept()


# ============================================================================
# MAIN
# ============================================================================

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    login = LoginWindow()
    holder = {}

    def on_success():
        holder["window"] = MainWindow()
        holder["window"].show()
        login.close()

    login.login_successful.connect(on_success)
    login.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
