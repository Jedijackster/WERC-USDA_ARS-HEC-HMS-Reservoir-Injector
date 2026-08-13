# -*- coding: utf-8 -*-
"""
hms_dam_split_full.py
=====================
End-to-end pipeline: split HEC-HMS subbasins at NID dams using the model's
own terrain rasters, and (optionally) write the modified .basin file.

Per dam D (snapped to the stream network, inside the watershed):
    new Subbasin = D's upstream area inside its containing subbasin,
                   minus any upstream dam pieces (incremental)  -> node(D)
    node(D)      = Junction named after the dam
                   -> node(first dam downstream)  if the trace hits one
                   -> original Downstream of the containing subbasin, else
Original subbasins keep their element name; only their Area is reduced to
the residual. New-subbasin params are CLONED from the parent - regenerate
area-dependent terms (Snyder Tp, CN, ...) afterwards.

Built-in safeguards (all automatic):
    - D8 scheme auto-detected (coverage x on-stream x facc-monotonicity);
      aborts on a weak call instead of guessing.
    - Dams snapped to MAX flow accumulation within radius (not nearest cell);
      unsnapped dams are excluded with a report.
    - Shapefile <-> basin element name mismatches resolved by canvas-centroid
      matching (e.g. FlatCk_S001 <-> Subbasin-5).
    - Optional WBD ground-truth IoU validation.
    - Main-stem dams flagged when facc-implied drainage area >> in-subbasin
      piece (upstream subbasins bypass the node - review those manually).
    - Written .basin is re-parsed: every Downstream must resolve.

Outputs (out_dir):
    snapped_dams.(shp|gpkg)            proposed_split_subbasins.(shp|gpkg)
    proposed_dam_nodes.(shp|gpkg)      proposed_reaches.(shp|gpkg)
    connectivity_check.csv             <basin>_withdams.basin   [optional]
"""

import os
import re
from collections import deque

import numpy as np
import pandas as pd
import geopandas as gpd
import rasterio
import rasterio.windows
from rasterio.features import shapes as rio_shapes, rasterize
from shapely.geometry import Point, LineString, MultiPolygon, shape
from shapely.ops import unary_union
from pyproj import CRS as PyProjCRS

# =====================================================================
# CONFIG
# =====================================================================
CONFIG = {
    # ---- inputs -----------------------------------------------------
    "subbasins_shp": r"C:\Users\devkotab\Documents\USDA_Summer_Project\Duck_Clear\Duck_Clear\gis\subbasins.shp",
    "basin_file":    r"C:\Users\devkotab\Documents\USDA_Summer_Project\Duck_Clear\Duck_Clear\DCK_2016_100yr.basin",
    "dams_shp":      r"C:\Users\devkotab\Documents\USDA_Summer_Project\merged_dams\NID_Dams_merged.shp",
    "terrain_dir":   r"C:\Users\devkotab\Documents\USDA_Summer_Project\Duck_Clear\Duck_Clear\terrain\DUCK_CLEAR\01",
    "flowdir_name":   "flowdir",
    "flowaccum_name": "flowaccum",
    "streams_name":   "streams",

    # optional WBD catchments for ground-truth validation ("" to skip)
    "wbd_shp": r"C:\Users\devkotab\Documents\USDA_Summer_Project\merged_dams\WBD_merged.shp",

    # ---- fields -----------------------------------------------------
    "dam_id_field":       "NID_ID",
    "subbasin_name_field": "name",

    # ---- outputs ----------------------------------------------------
    "out_dir": r"C:\Users\devkotab\Documents\USDA_Summer_Project\Duck_Clear\Duck_Clear\automatic_dam_split",
    "vector_format": "shp",            # "shp" or "gpkg"
    "write_basin":   True,             # write <basin>_withdams.basin
    "node_type":     "Junction",       # "Junction" now; "Reservoir" later

    # ---- behavior ---------------------------------------------------
    "filter_dams_to_existing_subbasins": True,
    "dam_filter_buffer": 1000.0,       # map units (EPSG:2276 = ft)
    "max_snap_distance": 500.0,        # map units
    "stream_min_value": 0,
    "min_piece_area_mi2": 0.001,
    "flowdir_scheme": "auto",          # "auto" or a name in D8_SCHEMES
    "mainstem_area_ratio": 0.70,       # flag if piece < ratio * facc-area
    "max_trace_steps": 100_000,
    "dam_node_prefix": "DAM_",
    "dam_sub_prefix":  "SUB_",
}

# =====================================================================
# D8 SCHEMES  (code -> (drow, dcol); raster row grows downward)
# =====================================================================
D8_SCHEMES = {
    "HMS_1TO8":     {1: (0, 1), 2: (1, 1), 3: (1, 0), 4: (1, -1),
                     5: (0, -1), 6: (-1, -1), 7: (-1, 0), 8: (-1, 1)},
    "HMS_1TO8_CCW": {1: (0, 1), 2: (-1, 1), 3: (-1, 0), 4: (-1, -1),
                     5: (0, -1), 6: (1, -1), 7: (1, 0), 8: (1, 1)},
    "N_1TO8_CW":    {1: (-1, 0), 2: (-1, 1), 3: (0, 1), 4: (1, 1),
                     5: (1, 0), 6: (1, -1), 7: (0, -1), 8: (-1, -1)},
    "NE_1TO8_CCW":  {1: (-1, 1), 2: (-1, 0), 3: (-1, -1), 4: (0, -1),
                     5: (1, -1), 6: (1, 0), 7: (1, 1), 8: (0, 1)},
    "ESRI_POWER2":  {1: (0, 1), 2: (1, 1), 4: (1, 0), 8: (1, -1),
                     16: (0, -1), 32: (-1, -1), 64: (-1, 0), 128: (-1, 1)},
}

# =====================================================================
# SMALL HELPERS
# =====================================================================
def resolve_raster_path(folder, name):
    for p in [os.path.join(folder, name + ext)
              for ext in ["", ".tif", ".tiff", ".img"]]:
        if os.path.exists(p):
            return p
    raise FileNotFoundError(f"raster '{name}' not found in {folder}")


def clean_name(value, prefix="", max_len=40):
    s = re.sub(r"_+", "_",
               re.sub(r"[^A-Za-z0-9_]+", "_", str(value).strip())).strip("_")
    return (prefix + (s or "UNKNOWN"))[:max_len]


def fix_geometries(gdf):
    out = gdf.copy()
    try:
        out["geometry"] = out.geometry.make_valid()
    except Exception:
        out["geometry"] = out.geometry.buffer(0)
    return out


def sqmi_factor(crs):
    u = PyProjCRS.from_user_input(crs).axis_info[0].unit_conversion_factor
    return (u / 1609.344) ** 2


# =====================================================================
# SCHEME DETECTION  (tested: coverage x on-stream x facc-monotonicity)
# =====================================================================
def detect_flowdir_scheme(flowdir, streams_mask, flowaccum, nodata=None,
                          sample_max=200_000, rng_seed=0):
    nrows, ncols = flowdir.shape
    rows, cols = np.where(streams_mask)
    if len(rows) == 0:
        raise RuntimeError("no stream cells - cannot detect scheme")
    if len(rows) > sample_max:
        pick = np.random.default_rng(rng_seed).choice(
            len(rows), sample_max, replace=False)
        rows, cols = rows[pick], cols[pick]

    fd = flowdir[rows, cols].astype(np.int64)
    fa_here = flowaccum[rows, cols].astype(np.float64)
    ok = np.ones(len(rows), bool)
    if nodata is not None:
        ok &= fd != nodata
    n_ok = max(int(ok.sum()), 1)

    detail = {}
    for name, dmap in D8_SCHEMES.items():
        codes = np.array(list(dmap.keys()), np.int64)
        valid = ok & np.isin(fd, codes)
        cov = valid.sum() / n_ok
        if valid.sum() < 100:
            detail[name] = (0.0, cov, 0.0, 0.0)
            continue
        dr = np.zeros(len(rows), np.int64)
        dc = np.zeros(len(rows), np.int64)
        for code, (r_off, c_off) in dmap.items():
            sel = fd == code
            dr[sel], dc[sel] = r_off, c_off
        nr, nc = rows + dr, cols + dc
        inb = valid & (nr >= 0) & (nr < nrows) & (nc >= 0) & (nc < ncols)
        if not inb.any():
            detail[name] = (0.0, cov, 0.0, 0.0)
            continue
        ons = streams_mask[nr[inb], nc[inb]].mean()
        mono = (flowaccum[nr[inb], nc[inb]].astype(np.float64)
                >= fa_here[inb]).mean()
        detail[name] = (cov * ons * mono, cov, ons, mono)

    ranked = sorted(detail.items(), key=lambda kv: -kv[1][0])
    best, second = ranked[0], ranked[1]
    lines = ["Flow-direction scheme detection "
             "(score = coverage x on-stream x facc-monotonic):",
             f"  {'scheme':<14} {'score':>6} {'cover':>6} {'onstrm':>6} {'faccup':>6}"]
    for name, (s, cov, ons, mono) in ranked:
        lines.append(f"  {name:<14} {s:6.3f} {cov:6.3f} {ons:6.3f} {mono:6.3f}"
                     + ("  <== SELECTED" if name == best[0] else ""))
    report = "\n".join(lines)
    if best[1][0] < 0.80:
        raise RuntimeError(report + "\nBest score < 0.80 - grids misaligned "
                           "or inconsistent. Not proceeding.")
    if best[1][0] - second[1][0] < 0.10:
        raise RuntimeError(report + "\nTop two schemes too close - resolve "
                           "manually (set CONFIG['flowdir_scheme']).")
    return best[0], report


# =====================================================================
# DELINEATION / TRACING  (from the dry-run script, verified logic)
# =====================================================================
def delineate_upstream_mask(flowdir, outlet_rc, d8_map, valid_mask=None):
    nrows, ncols = flowdir.shape
    r0, c0 = outlet_rc
    visited = np.zeros((nrows, ncols), bool)
    if not (0 <= r0 < nrows and 0 <= c0 < ncols):
        return visited
    q = deque([(r0, c0)])
    visited[r0, c0] = True
    while q:
        r, c = q.popleft()
        for code, (dr, dc) in d8_map.items():
            nr, nc = r - dr, c - dc
            if (0 <= nr < nrows and 0 <= nc < ncols and not visited[nr, nc]
                    and (valid_mask is None or valid_mask[nr, nc])
                    and int(flowdir[nr, nc]) == code):
                visited[nr, nc] = True
                q.append((nr, nc))
    return visited


def trace_downstream_line(flowdir, start_rc, transform, d8_map,
                          stop_rc=None, max_steps=100_000):
    nrows, ncols = flowdir.shape
    r, c = start_rc
    coords, seen = [], set()
    for _ in range(max_steps):
        if not (0 <= r < nrows and 0 <= c < ncols) or (r, c) in seen:
            break
        seen.add((r, c))
        x, y = rasterio.transform.xy(transform, r, c, offset="center")
        coords.append((x, y))
        if stop_rc is not None and (r, c) == stop_rc:
            break
        code = int(flowdir[r, c])
        if code not in d8_map:
            break
        dr, dc = d8_map[code]
        r, c = r + dr, c + dc
    return LineString(coords) if len(coords) >= 2 else None


def find_first_downstream_dam(flowdir, start_rc, d8_map, dam_cell_to_id,
                              own_id, max_steps=100_000):
    nrows, ncols = flowdir.shape
    r, c = start_rc
    seen = set()
    for _ in range(max_steps):
        if not (0 <= r < nrows and 0 <= c < ncols) or (r, c) in seen:
            return ""
        seen.add((r, c))
        code = int(flowdir[r, c])
        if code not in d8_map:
            return ""
        dr, dc = d8_map[code]
        r, c = r + dr, c + dc
        hit = dam_cell_to_id.get((r, c), "")
        if hit and hit != own_id:
            return hit
    return ""


# =====================================================================
# SNAPPING  (tested: max flow accumulation within radius)
# =====================================================================
def snap_points_to_flowaccum(dams, streams_path, flowaccum_path,
                             stream_min_value, max_snap_distance):
    with rasterio.open(streams_path) as ssrc, \
         rasterio.open(flowaccum_path) as fsrc:
        streams = ssrc.read(1, masked=True)
        stream_mask = (~streams.mask) & (streams.filled(0) > stream_min_value)
        facc = fsrc.read(1, masked=True).filled(0).astype("float64")
        transform = ssrc.transform
        nrows, ncols = stream_mask.shape
        rad = int(np.ceil(max_snap_distance / abs(transform.a)))

        pts, dist, stat, sfac = [], [], [], []
        for pt in dams.geometry:
            r0, c0 = ssrc.index(pt.x, pt.y)
            r_lo, r_hi = max(r0 - rad, 0), min(r0 + rad + 1, nrows)
            c_lo, c_hi = max(c0 - rad, 0), min(c0 + rad + 1, ncols)
            sm = stream_mask[r_lo:r_hi, c_lo:c_hi]
            if not sm.any():
                pts.append(pt); dist.append(float("inf"))
                stat.append("not_snapped_too_far"); sfac.append(0.0)
                continue
            rr, cc = np.where(sm)
            gr, gc = rr + r_lo, cc + c_lo
            xs, ys = rasterio.transform.xy(transform, gr, gc, offset="center")
            xs, ys = np.asarray(xs), np.asarray(ys)
            d = np.hypot(xs - pt.x, ys - pt.y)
            in_r = d <= max_snap_distance
            if not in_r.any():
                pts.append(pt); dist.append(float(d.min()))
                stat.append("not_snapped_too_far"); sfac.append(0.0)
                continue
            fa = facc[gr[in_r], gc[in_r]]
            # keep the dominant channel (facc >= 50% of max in radius) but
            # take the NEAREST such cell: rejects wrong small tributaries
            # without sliding the point downstream along its own channel
            big = fa >= 0.5 * fa.max()
            dd = d[in_r][big]
            k = int(np.argmin(dd))
            xi = xs[in_r][big][k]; yi = ys[in_r][big][k]
            pts.append(Point(float(xi), float(yi)))
            dist.append(float(dd[k]))
            stat.append("snapped")
            sfac.append(float(fa[big][k]))
    return pts, dist, stat, sfac


# =====================================================================
# RASTER <-> VECTOR
# =====================================================================
def rasterize_geom(geom, out_shape, transform):
    return rasterize([(geom, 1)], out_shape=out_shape, transform=transform,
                     fill=0, dtype="uint8").astype(bool)


def mask_to_polygon(mask, transform):
    if mask is None or not mask.any():
        return None
    geoms = [shape(g) for g, v in
             rio_shapes(mask.astype("uint8"), mask=mask, transform=transform)
             if v == 1]
    return unary_union(geoms).buffer(0) if geoms else None


def get_window_from_geom(src, geom, pad_cells=2):
    left, bottom, right, top = geom.bounds
    rmin, cmin = src.index(left, top)
    rmax, cmax = src.index(right, bottom)
    r0 = max(min(rmin, rmax) - pad_cells, 0)
    r1 = min(max(rmin, rmax) + pad_cells + 1, src.height)
    c0 = max(min(cmin, cmax) - pad_cells, 0)
    c1 = min(max(cmin, cmax) + pad_cells + 1, src.width)
    if r1 <= r0 or c1 <= c0:
        raise RuntimeError("invalid raster window from geometry bounds")
    return rasterio.windows.Window(c0, r0, c1 - c0, r1 - r0)


# =====================================================================
# BASIN FILE  (parse, centroid-matching, write)
# =====================================================================
def read_basin_text(path):
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read().replace("\r\n", "\n")


def parse_basin_subbasins(text):
    """name -> dict(block, downstream, area, cx, cy)"""
    out = {}
    for m in re.finditer(r"(?ms)^Subbasin: (\S+).*?^End:", text):
        blk = m.group(0)
        def grab(pat):
            g = re.search(pat, blk, re.M)
            return g.group(1) if g else None
        out[m.group(1)] = {
            "block": blk,
            "downstream": grab(r"^\s*Downstream:\s*(\S+)"),
            "area": float(grab(r"^\s*Area:\s*([\d.eE+-]+)") or "nan"),
            "cx": float(grab(r"^\s*Canvas X:\s*([\d.eE+-]+)") or "nan"),
            "cy": float(grab(r"^\s*Canvas Y:\s*([\d.eE+-]+)") or "nan"),
        }
    return out


def match_shp_to_basin(sub, namef, basin_subs):
    """
    shapefile subbasin name -> basin element name.
    Exact match first; otherwise nearest UNUSED basin canvas centroid
    (handles FlatCk_S001 <-> Subbasin-5 style mismatches).
    """
    mapping, report = {}, []
    unmatched_shp = []
    used = set()
    for _, r in sub.iterrows():
        nm = str(r[namef])
        if nm in basin_subs:
            mapping[nm] = nm
            used.add(nm)
        else:
            unmatched_shp.append((nm, r.geometry.centroid))
    free = {bn: v for bn, v in basin_subs.items()
            if bn not in used and np.isfinite(v["cx"])}
    for nm, cen in unmatched_shp:
        if not free:
            report.append(f"  !! no basin element left for '{nm}'")
            continue
        bn = min(free, key=lambda b: (free[b]["cx"] - cen.x) ** 2
                                     + (free[b]["cy"] - cen.y) ** 2)
        d = ((free[bn]["cx"] - cen.x) ** 2
             + (free[bn]["cy"] - cen.y) ** 2) ** 0.5
        mapping[nm] = bn
        report.append(f"  '{nm}' -> '{bn}' (centroid match, {d:,.0f} units)")
        free.pop(bn)
    return mapping, report


def set_block_area(block, new_area):
    return re.sub(r"(^\s*Area:\s*)[\d.eE+-]+",
                  rf"\g<1>{new_area:.6f}", block, count=1, flags=re.M)


def make_subbasin_block(template_block, name, area_mi2, downstream,
                        canvas_xy, lonlat):
    blk = template_block
    blk = re.sub(r"^Subbasin: \S+", f"Subbasin: {name}", blk, 1, re.M)
    blk = re.sub(r"^(\s*Latitude Degrees:).*",
                 rf"\g<1>  {lonlat[1]:.10f}", blk, 1, re.M)
    blk = re.sub(r"^(\s*Longitude Degrees:).*",
                 rf"\g<1> {lonlat[0]:.10f}", blk, 1, re.M)
    blk = re.sub(r"^(\s*Canvas X:).*", rf"\g<1> {canvas_xy[0]:.6f}", blk, 1, re.M)
    blk = re.sub(r"^(\s*Canvas Y:).*", rf"\g<1> {canvas_xy[1]:.6f}", blk, 1, re.M)
    blk = set_block_area(blk, area_mi2)
    blk = re.sub(r"^(\s*Downstream:).*", rf"\g<1> {downstream}", blk, 1, re.M)
    return blk


def make_node_block(node_type, name, xy, downstream, desc):
    b = (f"{node_type}: {name}\n"
         f"     Description: {desc}\n"
         f"     Canvas X: {xy[0]:.6f}\n"
         f"     Canvas Y: {xy[1]:.6f}\n"
         f"     From Canvas X: {xy[0]:.6f}\n"
         f"     From Canvas Y: {xy[1]:.6f}\n")
    if downstream:
        b += f"     Downstream: {downstream}\n"
    return b + "End:"


def validate_basin_connectivity(text):
    elems, refs = {}, []
    for m in re.finditer(
            r"(?ms)^(Subbasin|Reach|Junction|Reservoir|Source|Sink|Diversion)"
            r": (\S+).*?^End:", text):
        elems[m.group(2)] = m.group(1)
        d = re.search(r"^\s*Downstream:\s*(\S+)", m.group(0), re.M)
        if d:
            refs.append((m.group(2), d.group(1)))
    bad = [(a, b) for a, b in refs if b not in elems]
    return len(elems), bad


# =====================================================================
# VECTOR WRITER (shp-safe, tested)
# =====================================================================
_SHP_RENAMES = {
    "hms_name": "HMS_NAME", "parent": "PARENT", "dam_id": "DAM_ID",
    "kind": "KIND", "down_to": "DOWN_TO", "area_mi2": "AREA_MI2",
    "node_name": "NODE_NAME", "parent_sub": "PARENT_SUB",
    "parent_dam": "PARENT_DAM", "snap_dist": "SNAP_DIST",
    "snap_stat": "SNAP_STAT", "snap_facc": "SNAP_FACC",
    "reach": "REACH", "from_node": "FROM_NODE", "to_node": "TO_NODE",
    "dam_node": "DAM_NODE", "existing_subbasin": "EXIST_SUB",
    "new_subbasin": "NEW_SUB", "new_subbasin_routes_to": "NEWSUB_TO",
    "dam_node_routes_to": "DAMND_TO", "piece_area_mi2": "AREA_MI2",
    "snap_distance": "SNAP_DIST", "snap_status": "SNAP_STAT",
    "mainstem_flag": "MAINSTEM", "facc_area_mi2": "FACC_MI2",
    "orig_x": "ORIG_X", "orig_y": "ORIG_Y",
    "snap_x": "SNAP_X", "snap_y": "SNAP_Y",
}


def write_vector(gdf, path_no_ext, fmt="shp", layer=None):
    if gdf is None or len(gdf) == 0:
        print(f"  (skip empty layer: {os.path.basename(path_no_ext)})")
        return None
    g = gdf.copy()
    if fmt.lower() == "gpkg":
        out = path_no_ext + ".gpkg"
        g.to_file(out, layer=layer or os.path.basename(path_no_ext),
                  driver="GPKG")
        print(f"  wrote {out}")
        return out
    rename, used = {}, set()
    for col in g.columns:
        if col == "geometry":
            continue
        new = _SHP_RENAMES.get(col, col[:10].upper())
        base, i = new, 1
        while new in used:
            new = (base[:8] + f"_{i}")[:10]
            i += 1
        used.add(new)
        rename[col] = new
    g = g.rename(columns=rename)
    for col in g.columns:
        if col != "geometry" and g[col].dtype == object:
            g[col] = g[col].fillna("").astype(str)
    gt = set(g.geom_type.unique())
    if gt <= {"Polygon", "MultiPolygon"} and len(gt) > 1:
        g["geometry"] = g.geometry.apply(
            lambda x: MultiPolygon([x]) if x.geom_type == "Polygon" else x)
    out = path_no_ext + ".shp"
    g.to_file(out, driver="ESRI Shapefile")
    print(f"  wrote {out} (+ .dbf/.shx/.prj)")
    return out


# =====================================================================
# MAIN
# =====================================================================
def main():
    C = CONFIG
    os.makedirs(C["out_dir"], exist_ok=True)
    fmt = C["vector_format"].lower()

    # ---------------- rasters ----------------------------------------
    flowdir_path = resolve_raster_path(C["terrain_dir"], C["flowdir_name"])
    flowaccum_path = resolve_raster_path(C["terrain_dir"], C["flowaccum_name"])
    streams_path = resolve_raster_path(C["terrain_dir"], C["streams_name"])
    print("Flowdir :", flowdir_path)
    print("Flowaccum:", flowaccum_path)
    print("Streams :", streams_path)

    with rasterio.open(flowdir_path) as src:
        flowdir = src.read(1)
        raster_crs, transform = src.crs, src.transform
        fd_nodata = src.nodata
    with rasterio.open(flowaccum_path) as src:
        flowaccum = src.read(1, masked=True).filled(0).astype("float64")
    with rasterio.open(streams_path) as src:
        sdat = src.read(1, masked=True)
        streams_mask = (~sdat.mask) & (sdat.filled(0) > C["stream_min_value"])
    if flowdir.shape != streams_mask.shape or flowdir.shape != flowaccum.shape:
        raise RuntimeError("flowdir/streams/flowaccum grids differ in shape - "
                           "they must share one grid")
    cell_area = abs(transform.a * transform.e)
    to_mi2 = sqmi_factor(raster_crs)
    print(f"Grid {flowdir.shape}, cell {abs(transform.a):g} units, "
          f"CRS {raster_crs}")

    # ---------------- flow-direction scheme --------------------------
    if str(C["flowdir_scheme"]).lower() == "auto":
        scheme, rep = detect_flowdir_scheme(flowdir, streams_mask, flowaccum,
                                            nodata=fd_nodata)
        print("\n" + rep)
    else:
        scheme = C["flowdir_scheme"]
        print(f"\nUsing configured scheme: {scheme} (set 'auto' to verify)")
    d8_map = D8_SCHEMES[scheme]
    valid_fd = np.isin(flowdir, list(d8_map.keys()))
    if fd_nodata is not None:
        valid_fd &= flowdir != fd_nodata

    # ---------------- vectors ----------------------------------------
    sub = gpd.read_file(C["subbasins_shp"])
    dams = gpd.read_file(C["dams_shp"])
    if sub.crs is None or dams.crs is None:
        raise RuntimeError("subbasins/dams shapefile missing CRS")
    sub = fix_geometries(sub.to_crs(raster_crs))
    dams = fix_geometries(dams.to_crs(raster_crs))
    idf, namef = C["dam_id_field"], C["subbasin_name_field"]
    for df, fld, what in [(dams, idf, "dam id"), (sub, namef, "subbasin name")]:
        if fld not in df.columns:
            raise RuntimeError(f"{what} field '{fld}' not in "
                               f"{list(df.columns)}")
    dams[idf] = dams[idf].astype(str).str.strip()
    sub[namef] = sub[namef].astype(str).str.strip()
    print(f"\nSubbasins: {len(sub)} | dams before filtering: {len(dams)}")

    watershed = unary_union(sub.geometry.values)
    if C["filter_dams_to_existing_subbasins"]:
        wsb = watershed.buffer(C["dam_filter_buffer"])
        dams = dams[dams.geometry.apply(wsb.covers)].copy()
        print(f"Dams within watershed (+{C['dam_filter_buffer']:g} buffer): "
              f"{len(dams)}")
        if dams.empty:
            raise RuntimeError("no dams remain after watershed filter")

    # ---------------- snap (max flowaccum) ---------------------------
    print("\nSnapping dams to max-flowaccum stream cell...")
    pts, dist, stat, sfac = snap_points_to_flowaccum(
        dams, streams_path, flowaccum_path,
        C["stream_min_value"], C["max_snap_distance"])
    snapped = dams.copy()
    snapped["orig_x"] = dams.geometry.x
    snapped["orig_y"] = dams.geometry.y
    snapped["snap_dist"] = dist
    snapped["snap_stat"] = stat
    snapped["snap_facc"] = sfac
    snapped["dam_node"] = [clean_name(v, C["dam_node_prefix"])
                           for v in snapped[idf]]
    snapped.geometry = pts
    snapped["snap_x"] = snapped.geometry.x
    snapped["snap_y"] = snapped.geometry.y

    bad = snapped[snapped.snap_stat != "snapped"]
    if len(bad):
        print(f"  EXCLUDED {len(bad)} dam(s) with no stream within "
              f"{C['max_snap_distance']:g} units: "
              f"{bad[idf].tolist()}")
    snapped = snapped[snapped.snap_stat == "snapped"].copy()
    if snapped.empty:
        raise RuntimeError("no dams snapped - check streams raster / radius")
    print(snapped[[idf, "dam_node", "snap_dist", "snap_facc"]]
          .sort_values("snap_facc", ascending=False).to_string(index=False))

    # ---------------- containing subbasin -----------------------------
    join = gpd.sjoin(snapped[[idf, "geometry"]],
                     sub[[namef, "geometry"]],
                     how="left", predicate="within")
    join = join[~join.index.duplicated(keep="first")]
    snapped["parent_sub"] = join[namef].reindex(snapped.index).values
    lost = snapped[snapped.parent_sub.isna()]
    if len(lost):
        print(f"  NOTE: {len(lost)} snapped dam(s) outside all subbasins "
              f"(edge snap): {lost[idf].tolist()} - excluded")
    inside = snapped.dropna(subset=["parent_sub"]).copy()
    if inside.empty:
        raise RuntimeError("no snapped dams inside subbasins")

    # ---------------- raster cells + duplicates ----------------------
    dam_rc = {}
    with rasterio.open(flowdir_path) as src:
        for _, r in inside.iterrows():
            rr, cc = src.index(r.geometry.x, r.geometry.y)
            dam_rc[r[idf]] = (int(rr), int(cc))
    cell_to_ids = {}
    for d_id, rc in dam_rc.items():
        cell_to_ids.setdefault(rc, []).append(d_id)
    for rc, ids in cell_to_ids.items():
        if len(ids) > 1:
            print(f"  WARNING: dams share one snapped cell {rc}: {ids}")
    dam_cell_to_id = {rc: ids[0] for rc, ids in cell_to_ids.items()}

    # ---------------- nesting via downstream trace -------------------
    parent_dam = {d_id: find_first_downstream_dam(
        flowdir, rc, d8_map, dam_cell_to_id, d_id, C["max_trace_steps"])
        for d_id, rc in dam_rc.items()}
    print("\nDam nesting (downstream trace):")
    for d_id in sorted(parent_dam):
        print(f"  {d_id} -> {parent_dam[d_id] or '(original downstream)'}")

    # ---------------- basin parsing + name matching ------------------
    basin_text = read_basin_text(C["basin_file"]) \
        if C["basin_file"] and os.path.exists(C["basin_file"]) else ""
    basin_subs = parse_basin_subbasins(basin_text) if basin_text else {}
    if basin_subs:
        name_map, rep = match_shp_to_basin(sub, namef, basin_subs)
        if rep:
            print("\nShapefile <-> basin element name matching:")
            print("\n".join(rep))
    else:
        name_map = {str(r[namef]): str(r[namef]) for _, r in sub.iterrows()}
        print("\nWARNING: no basin file parsed - downstream names unknown")

    def basin_downstream(shp_name):
        bn = name_map.get(str(shp_name))
        return (basin_subs.get(bn, {}) or {}).get("downstream") \
            or "ORIGINAL_DOWNSTREAM"

    # ---------------- optional WBD validation ------------------------
    if C.get("wbd_shp") and os.path.exists(C["wbd_shp"]):
        print("\nWBD ground-truth validation (largest dams, full-grid "
              "delineation vs WBD, both clipped to watershed):")
        wbd = gpd.read_file(C["wbd_shp"]).to_crs(raster_crs)
        wbd = fix_geometries(wbd)
        wbd_d = wbd[wbd[idf].isin(inside[idf])].dissolve(idf)
        order = wbd_d.geometry.area.sort_values(ascending=False).index[:4]
        for d_id in order:
            if d_id not in dam_rc:
                continue
            m = delineate_upstream_mask(flowdir, dam_rc[d_id], d8_map,
                                        valid_mask=valid_fd)
            g = mask_to_polygon(m, transform)
            if g is None:
                print(f"  {d_id}: EMPTY delineation - check snap")
                continue
            g = g.intersection(watershed).buffer(0)
            ref = wbd_d.loc[d_id, "geometry"].intersection(watershed).buffer(0)
            u = g.union(ref).area
            iou = g.intersection(ref).area / u if u else 0
            tag = "" if iou >= 0.70 else "   <-- LOW, investigate"
            print(f"  {d_id}: delineated {g.area*to_mi2:7.2f} mi2 | "
                  f"WBD {ref.area*to_mi2:7.2f} mi2 | IoU {iou:.2f}{tag}")

    # ---------------- split affected subbasins -----------------------
    print("\nSplitting affected subbasins...")
    split_rows, node_rows, reach_rows, conn_rows = [], [], [], []
    residual_area = {}          # shp subbasin name -> residual mi2
    new_basin_blocks = []
    affected = set(inside.parent_sub.astype(str))

    for _, sr in sub.iterrows():
        nm = str(sr[namef])
        if nm not in affected:
            split_rows.append({"hms_name": clean_name(nm), "parent":
                               clean_name(nm), "dam_id": "", "kind":
                               "unchanged", "down_to": basin_downstream(nm),
                               "area_mi2": sr.geometry.area * to_mi2,
                               "geometry": sr.geometry})

    with rasterio.open(flowdir_path) as src:
        for sub_name in sorted(affected):
            sr = sub[sub[namef].astype(str) == sub_name].iloc[0]
            sub_geom = sr.geometry
            win = get_window_from_geom(src, sub_geom)
            wtr = src.window_transform(win)
            r0, r1 = int(win.row_off), int(win.row_off + win.height)
            c0, c1 = int(win.col_off), int(win.col_off + win.width)
            flow_w = flowdir[r0:r1, c0:c1]
            valid_w = valid_fd[r0:r1, c0:c1] & rasterize_geom(
                sub_geom, flow_w.shape, wtr)
            here = inside[inside.parent_sub.astype(str) == sub_name]
            print(f"\n  {sub_name}: {len(here)} dam(s)")

            up_masks = {}
            for _, dr_ in here.iterrows():
                d_id = dr_[idf]
                lrc = (dam_rc[d_id][0] - r0, dam_rc[d_id][1] - c0)
                up_masks[d_id] = delineate_upstream_mask(
                    flow_w, lrc, d8_map, valid_mask=valid_w)
                print(f"    {d_id}: {int(up_masks[d_id].sum()):,} cells "
                      f"upstream in-subbasin")

            piece_geoms = []
            for _, dr_ in here.iterrows():
                d_id = dr_[idf]
                dam_node = dr_["dam_node"]
                cum_cells = int(up_masks[d_id].sum())
                m = up_masks[d_id].copy()
                for ch, par in parent_dam.items():
                    if par == d_id and ch in up_masks:
                        m &= ~up_masks[ch]
                g = mask_to_polygon(m, wtr)
                if g is None or g.is_empty:
                    print(f"    WARNING: empty piece for {d_id}")
                    continue
                g = g.intersection(sub_geom).buffer(0)
                a_mi2 = g.area * to_mi2
                if g.is_empty or a_mi2 < C["min_piece_area_mi2"]:
                    print(f"    skip tiny piece {d_id} ({a_mi2:.6f} mi2)")
                    continue

                new_sub = clean_name(f"{C['dam_sub_prefix']}{d_id}")
                # main-stem check: facc-implied drainage vs in-subbasin cum.
                facc_mi2 = float(dr_["snap_facc"]) * cell_area * to_mi2
                cum_mi2 = cum_cells * cell_area * to_mi2
                mainstem = (facc_mi2 > 0 and
                            cum_mi2 < C["mainstem_area_ratio"] * facc_mi2)
                node_down = (clean_name(parent_dam[d_id],
                                        C["dam_node_prefix"])
                             if parent_dam.get(d_id)
                             else basin_downstream(sub_name))

                piece_geoms.append(g)
                split_rows.append({"hms_name": new_sub,
                                   "parent": clean_name(sub_name),
                                   "dam_id": d_id, "kind": "dam_piece",
                                   "down_to": dam_node, "area_mi2": a_mi2,
                                   "geometry": g})
                node_rows.append({"node_name": dam_node, "dam_id": d_id,
                                  "parent_sub": clean_name(sub_name),
                                  "parent_dam": parent_dam.get(d_id, ""),
                                  "down_to": node_down,
                                  "snap_dist": float(dr_["snap_dist"]),
                                  "snap_facc": float(dr_["snap_facc"]),
                                  "mainstem_flag": "YES" if mainstem else "",
                                  "geometry": dr_.geometry})
                rg = trace_downstream_line(
                    flowdir, dam_rc[d_id], transform, d8_map,
                    stop_rc=dam_rc.get(parent_dam.get(d_id, "")),
                    max_steps=C["max_trace_steps"])
                if rg is not None:
                    reach_rows.append({"reach": clean_name(f"R_{d_id}"),
                                       "from_node": dam_node,
                                       "to_node": node_down,
                                       "dam_id": d_id, "geometry": rg})
                conn_rows.append({
                    "dam_id": d_id, "dam_node": dam_node,
                    "existing_subbasin": sub_name, "new_subbasin": new_sub,
                    "new_subbasin_routes_to": dam_node,
                    "dam_node_routes_to": node_down,
                    "parent_dam": parent_dam.get(d_id, ""),
                    "piece_area_mi2": round(a_mi2, 6),
                    "facc_area_mi2": round(facc_mi2, 4),
                    "mainstem_flag": "YES" if mainstem else "",
                    "snap_distance": round(float(dr_["snap_dist"]), 3),
                })
                if mainstem:
                    print(f"    ** {d_id}: MAIN-STEM dam - facc-implied "
                          f"drainage {facc_mi2:.1f} mi2 vs in-subbasin "
                          f"{cum_mi2:.1f} mi2. Upstream subbasins bypass "
                          f"this node; review routing manually. **")

                # ---- basin blocks for this dam ----
                if C["write_basin"] and basin_subs:
                    bn = name_map.get(sub_name)
                    tmpl = basin_subs[bn]["block"] if bn in basin_subs \
                        else next(iter(basin_subs.values()))["block"]
                    cen = g.centroid
                    lonlat = gpd.GeoSeries([cen], crs=raster_crs) \
                        .to_crs(4326).iloc[0].coords[0]
                    new_basin_blocks.append(make_subbasin_block(
                        tmpl, new_sub, a_mi2, dam_node,
                        (cen.x, cen.y), lonlat))
                    nd = node_down if node_down != "ORIGINAL_DOWNSTREAM" \
                        else ""
                    new_basin_blocks.append(make_node_block(
                        C["node_type"], dam_node,
                        (dr_.geometry.x, dr_.geometry.y), nd,
                        f"Dam node {d_id} (edit routing when ready)"))

            # residual keeps ORIGINAL element name (basin-safe)
            resid = sub_geom.difference(
                unary_union(piece_geoms).buffer(0)).buffer(0) \
                if piece_geoms else sub_geom
            ra = max(resid.area * to_mi2, 0.0)
            residual_area[sub_name] = ra
            if not resid.is_empty and ra >= C["min_piece_area_mi2"]:
                split_rows.append({"hms_name": clean_name(sub_name),
                                   "parent": clean_name(sub_name),
                                   "dam_id": "", "kind": "local_resid",
                                   "down_to": basin_downstream(sub_name),
                                   "area_mi2": ra, "geometry": resid})
            else:
                print(f"    NOTE: residual of {sub_name} is ~empty "
                      f"({ra:.6f} mi2); element kept with that area - "
                      f"consider removing it manually")

    # ---------------- write basin file --------------------------------
    if C["write_basin"] and basin_text and new_basin_blocks:
        out_text = basin_text
        for sub_name, ra in residual_area.items():
            bn = name_map.get(sub_name)
            if bn in basin_subs and np.isfinite(basin_subs[bn]["area"]):
                old_blk = basin_subs[bn]["block"]
                out_text = out_text.replace(
                    old_blk, set_block_area(old_blk, ra), 1)
        insert = "\n\n".join(new_basin_blocks) + "\n"
        anchor = re.search(r"(?m)^Computation Point:", out_text)
        if anchor:
            i = anchor.start()
            out_text = out_text[:i] + insert + "\n" + out_text[i:]
        else:
            out_text = out_text.rstrip("\n") + "\n\n" + insert
        n_elem, bad_refs = validate_basin_connectivity(out_text)
        base = os.path.splitext(os.path.basename(C["basin_file"]))[0]
        basin_out = os.path.join(C["out_dir"], base + "_withdams.basin")
        with open(basin_out, "w", encoding="utf-8") as f:
            f.write(out_text.replace("\n", "\r\n"))
        print(f"\nBasin written: {basin_out}")
        print(f"  elements: {n_elem} | unresolved Downstream refs: "
              f"{bad_refs if bad_refs else 'NONE'}")
        if any(b == "ORIGINAL_DOWNSTREAM" for _, b in bad_refs):
            print("  !! some nodes point to ORIGINAL_DOWNSTREAM - basin "
                  "file lacked a downstream for their parent subbasin")

    # ---------------- write GIS + CSV --------------------------------
    print("\nWriting GIS outputs...")
    O = C["out_dir"]
    write_vector(gpd.GeoDataFrame(snapped, crs=raster_crs),
                 os.path.join(O, "snapped_dams"), fmt)
    write_vector(gpd.GeoDataFrame(split_rows, geometry="geometry",
                                  crs=raster_crs),
                 os.path.join(O, "proposed_split_subbasins"), fmt)
    write_vector(gpd.GeoDataFrame(node_rows, geometry="geometry",
                                  crs=raster_crs),
                 os.path.join(O, "proposed_dam_nodes"), fmt)
    write_vector(gpd.GeoDataFrame(reach_rows, geometry="geometry",
                                  crs=raster_crs),
                 os.path.join(O, "proposed_reaches"), fmt)
    conn = pd.DataFrame(conn_rows)
    conn.to_csv(os.path.join(O, "connectivity_check.csv"), index=False)
    print(f"  wrote {os.path.join(O, 'connectivity_check.csv')}")

    print("\nConnectivity preview:")
    print(conn.to_string(index=False) if len(conn) else "  (none)")
    print("\nDone. REMINDER: regenerate area-dependent parameters "
          "(Snyder Tp, CN, ...) for split subbasins; review any "
          "MAIN-STEM flags before trusting routing.")


if __name__ == "__main__":
    main()