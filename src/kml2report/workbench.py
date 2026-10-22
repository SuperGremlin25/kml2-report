"""Streamlit 'GIS Workbench' tab: map, layers, attribute table, analysis, QA, export."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Optional

import pandas as pd
import streamlit as st

from kml2report import contractor_split as _cs_mod
from kml2report import geoexport, gis
from kml2report.models import Feature

_PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#46f0f0", "#f032e6",
            "#bcf60c", "#008080", "#9a6324", "#800000", "#808000", "#000075"]

COMMON_CRS = {
    "WGS84 lat/lon (EPSG:4326)": "EPSG:4326",
    "Auto UTM (meters)": "AUTO_UTM",
    "Texas South Central ftUS (EPSG:2278)": "EPSG:2278",
    "Texas Central ftUS (EPSG:2277)": "EPSG:2277",
    "Texas North Central ftUS (EPSG:2276)": "EPSG:2276",
    "Texas South ftUS (EPSG:2279)": "EPSG:2279",
    "Texas North ftUS (EPSG:2275)": "EPSG:2275",
    "Web Mercator (EPSG:3857)": "EPSG:3857",
    "Custom EPSG...": "CUSTOM",
}


def _hex_to_rgb(h: str) -> list[int]:
    h = h.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


def _layer_key(f: Feature) -> str:
    return f.folder_path or f.layer or "(no folder)"


def _resolve_crs(label: str, features: list[Feature]) -> str:
    code = COMMON_CRS[label]
    if code == "AUTO_UTM":
        c = gis.centroid_lonlat(features)
        return f"EPSG:{gis.utm_epsg(*c)}" if c else "EPSG:4326"
    if code == "CUSTOM":
        raw = st.text_input("EPSG code", value="2278")
        return f"EPSG:{raw.strip().replace('EPSG:', '')}" if raw.strip() else "EPSG:4326"
    return code


def render(features: list[Feature], warnings: list[str], stem: str) -> None:
    cs_mime = _cs_mod.MIME
    st.header("GIS Workbench")
    if not features:
        st.warning("No features to show.")
        return
    if warnings:
        with st.expander(f"⚠️ {len(warnings)} parse warning(s) - file was read, bad items skipped", expanded=False):
            st.code("\n".join(warnings[:500]))

    layers = sorted({_layer_key(f) for f in features})
    left, right = st.columns([1, 3])
    with left:
        st.subheader("Layers")
        visible = st.multiselect("Visible layers", layers, default=layers, key="wb_layers")
        color_by = st.radio("Color by", ["Layer", "File style", "Geometry type"], key="wb_color")
        basemap = st.selectbox("Basemap", ["road", "light", "dark"], key="wb_basemap")
        show_labels = st.checkbox("Labels on points", value=False, key="wb_labels")
        search = st.text_input("🔎 Search name / attributes", key="wb_search")

    vis = [f for f in features if _layer_key(f) in visible]
    if search:
        s = search.lower()
        vis = [f for f in vis if s in " ".join([f.name, f.raw_description, *map(str, gis.feature_attributes(f).values())]).lower()]

    t_map, t_table, t_stats, t_analysis, t_qa, t_split, t_prints, t_export = right.tabs(
        ["🗺️ Map", "📋 Attribute Table", "📊 Statistics", "🧭 Analysis", "✅ QA / QC", "👷 Contractor Split",
         "📐 Prints (PDF)", "📦 Export"])

    # ------------------------------------------------------------------ map
    with t_map:
        _render_map(vis, layers, color_by, basemap, show_labels)

    # ---------------------------------------------------------------- table
    with t_table:
        df = gis.attribute_table(vis)
        if df.empty:
            st.info("Nothing selected.")
        else:
            cols = [c for c in df.columns if c != "_idx"]
            pick = st.multiselect("Columns", cols, default=[c for c in cols if c not in ("source_file",)][:12], key="wb_cols")
            st.caption(f"{len(df):,} features")
            st.dataframe(df[pick] if pick else df, use_container_width=True, height=480)
            vtab = st.checkbox("Show vertex / coordinate table (every point, with station)", key="wb_vtx")
            if vtab:
                crs_label = st.selectbox("Add projected X/Y in", list(COMMON_CRS)[:-1], index=1, key="wb_vtx_crs")
                epsg = _resolve_crs(crs_label, vis)
                vdf = gis.project_vertices(vis, epsg) if epsg != "EPSG:4326" else gis.extract_vertices(vis)
                st.dataframe(vdf, use_container_width=True, height=380)
                st.download_button("⬇️ Vertices CSV", vdf.to_csv(index=False), f"{stem}_vertices.csv", "text/csv")

    # ---------------------------------------------------------------- stats
    with t_stats:
        ls = gis.layer_stats(vis)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Features", f"{len(vis):,}")
        c2.metric("Total length (ft)", f"{ls['length_ft'].sum():,.0f}" if not ls.empty else "0")
        c3.metric("Total length (mi)", f"{ls['length_ft'].sum() / 5280:,.2f}" if not ls.empty else "0")
        c4.metric("Total area (ac)", f"{ls['area_acres'].sum():,.2f}" if not ls.empty else "0")
        st.subheader("By layer")
        st.dataframe(ls, use_container_width=True)
        attr_df = gis.attribute_table(vis)
        if not attr_df.empty:
            fields = [c for c in attr_df.columns if c not in ("_idx", "wkt", "latitude", "longitude", "calc_length_ft",
                                                              "calc_area_sqft", "calc_area_acres")]
            field = st.selectbox("Summarize by attribute", fields, key="wb_group")
            gs = gis.group_summary(vis, field)
            st.dataframe(gs, use_container_width=True)
            if not gs.empty and len(gs) <= 40:
                st.bar_chart(gs.set_index(field)["count"])

    # ------------------------------------------------------------- analysis
    with t_analysis:
        a1, a2 = st.columns(2)
        with a1:
            st.subheader("Buffer")
            dist = st.number_input("Distance (ft)", 1.0, 5000.0, 25.0, key="wb_buf")
            if st.button("Create buffer", key="wb_buf_go"):
                gj = gis.buffer_features(vis, dist)
                st.session_state["wb_buffer"] = gj
                st.rerun()  # the map tab renders earlier in this run; redraw so the buffer shows
            if st.session_state.get("wb_buffer"):
                n = len(st.session_state["wb_buffer"]["features"])
                st.success(f"{n} buffer polygon(s) shown on the map tab")
                st.download_button("⬇️ Buffer GeoJSON", json.dumps(st.session_state["wb_buffer"]),
                                   f"{stem}_buffer.geojson", "application/geo+json")
        with a2:
            st.subheader("Points in polygons")
            pj = gis.points_in_polygons(vis)
            st.dataframe(pj, use_container_width=True, height=240) if not pj.empty else st.info(
                "Needs both point and polygon features.")
        st.subheader("Nearest features")
        names = [f"{i}: {f.name or f.placemark_id or '(unnamed)'} [{f.layer}]" for i, f in enumerate(vis)]
        if names:
            sel = st.selectbox("From feature", names, key="wb_near")
            n = st.slider("How many", 1, 25, 5, key="wb_near_n")
            st.dataframe(gis.nearest_features(vis, vis[int(sel.split(":")[0])], n), use_container_width=True)
        st.subheader("Elevation profile")
        liney = [(i, f) for i, f in enumerate(vis) if f.geometry and any(
            g.geometry_type in ("LineString", "Track") for g in f.geometry.iter_simple())]
        if liney:
            sel = st.selectbox("Line / track", [f"{i}: {f.name or '(unnamed)'}" for i, f in liney], key="wb_prof")
            prof = gis.elevation_profile(vis[int(sel.split(":")[0])])
            if not prof.empty and prof["elev_m"].abs().sum() > 0:
                st.line_chart(prof.set_index("station_ft")["elev_ft"])
            else:
                st.caption("No elevation data in this line (altitude is 0).")

    # ------------------------------------------------------------------- QA
    with t_qa:
        tol = st.slider("Snap tolerance (ft)", 0.5, 25.0, 3.0, key="wb_tol")
        if st.button("Run QA/QC checks", type="primary", key="wb_qa"):
            st.session_state["wb_qa_res"] = [i.as_dict() for i in gis.run_qa(vis, snap_tolerance_ft=tol)]
        res = st.session_state.get("wb_qa_res")
        if res is not None:
            if not res:
                st.success("No issues found.")
            else:
                qdf = pd.DataFrame(res)
                st.dataframe(qdf, use_container_width=True, height=420)
                st.download_button("⬇️ QA report CSV", qdf.to_csv(index=False), f"{stem}_qa.csv", "text/csv")

    # ----------------------------------------------------------- contractor split
    with t_split:
        _render_split(vis, stem)

    # --------------------------------------------------------------- prints
    with t_prints:
        _render_prints(vis, stem)

    # --------------------------------------------------------------- export
    with t_export:
        st.caption(f"Exports include the {len(vis):,} currently visible feature(s).")
        crs_label = st.selectbox("Coordinate system for Shapefile / DXF", list(COMMON_CRS), index=2, key="wb_exp_crs")
        epsg = _resolve_crs(crs_label, vis)
        e1, e2, e3, e4 = st.columns(4)
        e1.download_button("GeoJSON", geoexport.to_geojson_bytes(vis), f"{stem}.geojson", "application/geo+json")
        kml_data, kml_ext = geoexport.to_kml_package([(stem, vis, None)], stem, wrap_groups=False)
        e2.download_button("KML (clean)", kml_data, f"{stem}_clean{kml_ext}", cs_mime[kml_ext])
        try:
            e3.download_button("Shapefile (.zip)", geoexport.to_shapefile_zip(vis, epsg), f"{stem}_shp.zip", "application/zip")
        except Exception as exc:
            e3.error(f"Shapefile failed: {exc}")
        try:
            e4.download_button("AutoCAD DXF", geoexport.to_dxf_bytes(vis, None if epsg == "EPSG:4326" else epsg),
                               f"{stem}.dxf", "application/dxf")
        except Exception as exc:
            e4.error(f"DXF failed: {exc}")
        st.caption("DXF layers follow your KML folders; coordinates are written in feet when the CRS is metric.")


def _render_prints(vis: list[Feature], stem: str) -> None:
    """Upload a print-set PDF, place its sheets on the map and compare footage."""
    from kml2report import prints

    st.caption("Upload the construction print set (vector PDF with a text layer). Each sheet is placed from the "
               "pole latitude/longitude callouts printed on it and the plan scale stored in the PDF.")
    up = st.file_uploader("Print set (PDF)", type=["pdf"], key="wb_pdf")
    if up is None:
        st.info("No prints loaded yet.")
        return
    data = up.getvalue()
    key = ("wb_plan", hashlib.sha1(data).hexdigest())
    if st.session_state.get("wb_plan_key") != key:
        with st.spinner("Reading prints..."):
            try:
                plan = prints.georeference(prints.read_plan_set(data, up.name))
            except Exception as exc:  # unreadable / encrypted PDFs
                st.error(f"Could not read this PDF: {exc}")
                return
        st.session_state["wb_plan"] = plan
        st.session_state["wb_plan_key"] = key
    plan = st.session_state["wb_plan"]
    for w in plan.warnings:
        st.warning(w)

    c1, c2, c3 = st.columns(3)
    c1.metric("Sheets", len(plan.sheets))
    c2.metric("Placed", len(plan.placed()))
    c3.metric("Flagged callouts", sum(1 for _, c in plan.callouts() if c.flag))

    if len(plan.placed()) < len(plan.sheets) and vis and st.button(
            "Place remaining sheets using KML handhole names", key="wb_place_names"):
        n = prints.place_from_kml_names(plan, vis)
        st.success(f"Placed {n} more sheet(s)." if n else "No sheet could be matched to KML point names.")

    prints.place_callouts(plan)
    polys = [{"name": s.label, "path": [list(p) for p in s.footprint()]} for s in plan.placed()]
    pins = [{"name": c.text, "lon": c.lon, "lat": c.lat} for _, c in plan.callouts()
            if c.kind == "pole" and c.lon is not None]
    if polys:
        import pydeck as pdk
        lons = [p[0] for pl in polys for p in pl["path"]]
        lats = [p[1] for pl in polys for p in pl["path"]]
        layers = [pdk.Layer("PathLayer", polys, get_path="path", get_color=[255, 140, 0], width_min_pixels=2),
                  pdk.Layer("ScatterplotLayer", pins, get_position=["lon", "lat"], get_radius=6,
                            get_fill_color=[220, 30, 30])]
        st.pydeck_chart(pdk.Deck(layers=layers, tooltip={"text": "{name}"}, map_style=None,
                                 initial_view_state=pdk.ViewState(latitude=sum(lats) / len(lats),
                                                                  longitude=sum(lons) / len(lons), zoom=13)))

    st.subheader("Sheets")
    st.dataframe(prints.sheet_table(plan), use_container_width=True)
    flagged = prints.callout_table(plan)
    flagged = flagged[flagged["flag"] != ""]
    if not flagged.empty:
        st.subheader("Check these printed coordinates")
        st.dataframe(flagged, use_container_width=True)
    if vis and plan.placed():
        st.subheader("Print callouts vs KML footage")
        st.caption("Print sum adds every footage callout on the sheet (spans, bores, risers), so expect it to differ "
                   "from KML line length wherever labels repeat or lines run off the sheet.")
        st.dataframe(prints.compare_with_features(plan, vis), use_container_width=True)

    d1, d2 = st.columns(2)
    d1.download_button("⬇️ Sheets + callouts KML", prints.to_kml(plan), f"{stem}_prints.kml",
                       "application/vnd.google-earth.kml+xml")
    d2.download_button("⬇️ Callouts CSV", prints.callout_table(plan).to_csv(index=False),
                       f"{stem}_callouts.csv", "text/csv")


def _render_map(vis: list[Feature], layers: list[str], color_by: str, basemap: str, show_labels: bool) -> None:
    import pydeck as pdk

    if not vis:
        st.info("No visible features.")
        return
    layer_color = {name: _PALETTE[i % len(_PALETTE)] for i, name in enumerate(layers)}
    type_color = {"Point": "#d62728", "LineString": "#1f77b4", "Track": "#17becf", "Polygon": "#2ca02c",
                  "LinearRing": "#2ca02c", "MultiGeometry": "#9467bd"}

    feats = []
    label_rows = []
    for f in vis:
        gj = f.geometry.to_geojson() if f.geometry else None
        if not gj:
            continue
        if color_by == "File style":
            col = f.style.get("line_color") or f.style.get("fill_color") or f.style.get("icon_color") or "#3388ff"
        elif color_by == "Geometry type":
            col = type_color.get(f.geometry.geometry_type, "#888888")
        else:
            col = layer_color[_layer_key(f)]
        rgb = _hex_to_rgb(col)
        desc = f.name or f.placemark_id or "(unnamed)"
        feats.append({"type": "Feature", "geometry": gj, "properties": {
            "name": desc, "layer": _layer_key(f), "type": f.geometry.geometry_type,
            "len": f"{f.geometry.length_feet:,.1f} ft" if f.geometry.length_m else "",
            "area": f"{f.geometry.area_acres:,.3f} ac" if f.geometry.area_m2 else "",
            "rgb": rgb}})
        if show_labels and f.geometry.geometry_type == "Point":
            label_rows.append({"lon": f.geometry.longitude, "lat": f.geometry.latitude, "text": f.name})

    fc = {"type": "FeatureCollection", "features": feats}
    deck_layers = [pdk.Layer(
        "GeoJsonLayer", fc, pickable=True, stroked=True, filled=True, extruded=False,
        get_fill_color="[properties.rgb[0], properties.rgb[1], properties.rgb[2], 90]",
        get_line_color="[properties.rgb[0], properties.rgb[1], properties.rgb[2], 255]",
        get_point_radius=3, point_radius_min_pixels=4, point_radius_max_pixels=14,
        line_width_min_pixels=2, get_line_width=1)]
    buf = st.session_state.get("wb_buffer")
    if buf and buf.get("features"):
        deck_layers.append(pdk.Layer("GeoJsonLayer", buf, stroked=True, filled=True,
                                     get_fill_color=[255, 200, 0, 50], get_line_color=[255, 160, 0, 200],
                                     line_width_min_pixels=1))
    if label_rows:
        deck_layers.append(pdk.Layer("TextLayer", pd.DataFrame(label_rows), get_position="[lon, lat]", get_text="text",
                                     get_size=13, get_color=[20, 20, 20, 255], get_pixel_offset=[0, -14]))

    b = gis.bounds_of(vis)
    if b is None:
        st.info("None of the visible features have usable coordinates - see the Attribute Table and QA/QC tabs.")
        return
    lon, lat = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
    span = max(b[2] - b[0], b[3] - b[1], 1e-5)
    import math
    zoom = max(1.0, min(19.0, math.log2(360 / span) - 1))
    st.pydeck_chart(pdk.Deck(
        layers=deck_layers, map_style=basemap,
        initial_view_state=pdk.ViewState(longitude=lon, latitude=lat, zoom=zoom),
        tooltip={"html": "<b>{name}</b><br/>{layer}<br/>{type} {len} {area}"}), use_container_width=True)
    if color_by == "Layer":
        st.caption("  ".join(f"<span style='color:{c}'>■</span> {n}" for n, c in layer_color.items()
                             if n in {_layer_key(f) for f in vis}), unsafe_allow_html=True)


def _render_split(vis: list[Feature], stem: str) -> None:
    import pydeck as pdk
    from kml2report import contractor_split as cs

    st.caption("Divide the build into two contiguous contractor areas with matched footage, duct and handholes, "
               "then export a clean KML for each contractor containing only their work.")
    mode = st.radio("How should the work be handed out?",
                    ["All of it, split between the two contractors", "In releases - a set footage at a time"],
                    horizontal=True, key="sp_mode")
    releases_mode = mode.startswith("In releases")
    c1, c2 = st.columns(2)
    name_a = c1.text_input("Contractor 1", "Contractor A", key="sp_na")
    name_b = c2.text_input("Contractor 2", "Contractor B", key="sp_nb")
    share = 0.5 if releases_mode else st.slider(f"{name_a} share of the work (%)", 30, 70, 50, key="sp_share") / 100
    clip = st.checkbox("Cut lines exactly at the boundary (equalizes footage precisely)", True, key="sp_clip",
                       help="Off = whole segments go to whichever side holds their center; no line is cut.")
    with st.expander("What matters most? (balance weights)"):
        w_total = st.slider("Total footage", 0.0, 10.0, cs.DEFAULT_WEIGHTS["total"], 0.5, key="sp_w1")
        w_duct = st.slider("Duct-feet (footage x ducts)", 0.0, 10.0, cs.DEFAULT_WEIGHTS["duct"], 0.5, key="sp_w2")
        w_hh = st.slider("Handholes", 0.0, 10.0, cs.DEFAULT_WEIGHTS["handholes"], 0.5, key="sp_w3")
        w_size = st.slider("Each duct size", 0.0, 10.0, cs.DEFAULT_WEIGHTS["per_size"], 0.5, key="sp_w4")
        w_oth = st.slider("Other assets (poles, cabinets...)", 0.0, 10.0, cs.DEFAULT_WEIGHTS["other_assets"], 0.5,
                          key="sp_w5")
        w_diff = st.slider("Difficulty-weighted work", 0.0, 10.0, cs.DEFAULT_WEIGHTS["difficulty"], 0.5, key="sp_w6",
                           help="Footage counted harder in city / dense / tagged areas.")
        w_dense = st.slider("Dense-area footage", 0.0, 10.0, cs.DEFAULT_WEIGHTS["dense"], 0.5, key="sp_w7",
                            help="Makes sure each contractor gets a fair share of the city, not just open runs.")
    with st.expander("City vs open runs: how hard is each stretch?", expanded=True):
        st.caption("Feet are not all equal. Dense stretches (many turns, short blocks, many handholes) cost more "
                   "than long straight runs, so the split balances the hard work too and each contractor gets a "
                   "share of the city.")
        auto = st.checkbox("Detect dense / city areas automatically", True, key="sp_auto",
                           help="Measured from the file: vertices and assets per area, compared with this file's own typical density.")
        factor = st.slider("Dense work costs ... x an open run", 1.0, 4.0, 2.0, 0.25, key="sp_factor", disabled=not auto)
        layer_names = sorted({cs.layer_key(f) for f in vis})
        hard_layers = st.multiselect("Folders you know are difficult (city, rock, river crossing...)", layer_names,
                                     key="sp_hard")
        hard_mult = st.slider("Those folders cost ... x", 1.0, 5.0, 2.0, 0.25, key="sp_hardmult",
                              disabled=not hard_layers)
        show_diff = st.checkbox("Show what counts as difficult on the map", False, key="sp_showdiff")
    difficulty = cs.Difficulty(auto=auto, factor=factor, hard_layers=tuple(hard_layers), hard_multiplier=hard_mult)
    direction = st.radio("Cut direction", ["Best (auto)", "North-South line", "East-West line"], horizontal=True,
                         key="sp_dir")
    fixed = {"Best (auto)": None, "North-South line": 0.0, "East-West line": 90.0}[direction]
    # the cache key covers the visible features' geometry and attributes, not just their count
    sig = hashlib.sha1()
    for f in vis:
        sig.update(repr((f.name, f.layer, f.geometry.to_wkt() if f.geometry else "", sorted(f.extended_data.items()),
                         sorted(f.parsed_attributes.items()), f.raw_description)).encode())
    weights = {"total": w_total, "duct": w_duct, "handholes": w_hh, "per_size": w_size, "other_assets": w_oth,
               "difficulty": w_diff, "dense": w_dense}
    params = (sig.hexdigest(), name_a, name_b, share, clip, w_total, w_duct, w_hh, w_size, w_oth, w_diff, w_dense,
              direction, auto, factor, tuple(hard_layers), hard_mult)
    if releases_mode:
        _render_releases(vis, stem, params, (name_a, name_b), clip, weights, fixed, difficulty)
        return
    if st.button("⚖️ Split into two contractors", type="primary", key="sp_go"):
        try:
            cs.validate_names((name_a, name_b))
            cands, _ = cs.search_splits(vis, share, clip, weights, fixed_angle=fixed, difficulty=difficulty)
            st.session_state["sp_cands"] = cands
            st.session_state["sp_params"] = params
        except ValueError as exc:
            st.error(str(exc))
            return
    if st.session_state.get("sp_params") != params or not st.session_state.get("sp_cands"):
        return
    cands = st.session_state["sp_cands"]
    pick = 0
    if len(cands) > 1:
        labels = [f"#{i + 1}  cut at {c.angle_deg:.0f}°  (imbalance score {c.score:.3f})" for i, c in enumerate(cands)]
        pick = labels.index(st.selectbox("Alternative cuts", labels, key="sp_pick"))
    try:
        res = cs.apply_split(vis, cands[pick], clip, (name_a, name_b), difficulty)
    except ValueError as exc:
        st.error(str(exc))
        return
    name_a, name_b = res.names

    tot = res.stats.iloc[0]
    m1, m2, m3 = st.columns(3)
    m1.metric(f"{name_a} footage", f"{tot[name_a]:,.0f} ft")
    m2.metric(f"{name_b} footage", f"{tot[name_b]:,.0f} ft")
    hh = res.stats[res.stats["metric"] == cs.HANDHOLES]
    m3.metric("Handholes A / B", f"{int(hh[name_a].iloc[0])} / {int(hh[name_b].iloc[0])}" if len(hh) else "-")
    st.dataframe(res.stats, use_container_width=True)

    def gj(feats, color):
        rgb = _hex_to_rgb(color)
        out = []
        for f in feats:
            g = f.geometry.to_geojson() if f.geometry else None
            if g:
                out.append({"type": "Feature", "geometry": g, "properties": {
                    "name": f.name, "len": f"{f.geometry.length_feet:,.0f} ft" if f.geometry.length_m else "",
                    "rgb": rgb}})
        return {"type": "FeatureCollection", "features": out}

    col = "[properties.rgb[0], properties.rgb[1], properties.rgb[2], 255]"
    layers = [pdk.Layer("GeoJsonLayer", gj(f, c), pickable=True, stroked=True, filled=True, get_line_color=col,
                        get_fill_color="[properties.rgb[0], properties.rgb[1], properties.rgb[2], 80]",
                        get_point_radius=3, point_radius_min_pixels=4, line_width_min_pixels=3)
              for f, c in ((res.a, cs.COLORS[0]), (res.b, cs.COLORS[1]))]
    if show_diff and difficulty.enabled:
        segs = cs.difficulty_segments(vis, difficulty)
        for sgm in segs:  # open run = grey, hardest = deep orange
            t = min(max((sgm["mult"] - 1.0) / max(difficulty.factor * difficulty.hard_multiplier - 1.0, 1e-9), 0.0), 1.0)
            sgm["color"] = [int(150 + 105 * t), int(150 - 70 * t), int(150 - 150 * t), 230]
        layers = [pdk.Layer("PathLayer", segs, get_path="path", get_color="color", width_min_pixels=4, pickable=False)]
    layers.append(pdk.Layer("PathLayer", [{"path": [list(p) for p in res.cut_lonlat]}], get_path="path",
                            get_color=[255, 200, 0, 255], width_min_pixels=3, get_width=2))
    b = gis.bounds_of(vis)
    if b:
        span = max(b[2] - b[0], b[3] - b[1], 1e-5)
        st.pydeck_chart(pdk.Deck(layers=layers, map_style="road", tooltip={"html": "<b>{name}</b> {len}"},
                                 initial_view_state=pdk.ViewState(longitude=(b[0] + b[2]) / 2,
                                                                  latitude=(b[1] + b[3]) / 2,
                                                                  zoom=max(1.0, min(19.0, math.log2(360 / span) - 1)))),
                        use_container_width=True)
    if show_diff and difficulty.enabled:
        st.caption("Difficulty map: grey = open run, orange = dense / difficult. Uncheck to see the contractor colors again.")
    st.markdown(f"<span style='color:{cs.COLORS[0]}'>■</span> {name_a} &nbsp; "
                f"<span style='color:{cs.COLORS[1]}'>■</span> {name_b} &nbsp; "
                f"<span style='color:#ffc800'>■</span> boundary", unsafe_allow_html=True)

    recolor = st.checkbox("Recolor the overview by contractor (replaces your icons in the overview only)", False,
                          key="sp_recolor", help="Off: every file keeps your icons, colours and folders exactly as built.")
    file_a, ext_a = cs.contractor_file(res.a, name_a, stem)
    file_b, ext_b = cs.contractor_file(res.b, name_b, stem)
    file_o, ext_o = cs.overview_file(res, stem, recolor)
    d1, d2, d3, d4 = st.columns(4)
    d1.download_button(f"⬇️ {name_a}", file_a, f"{stem}_contractor1{ext_a}", cs.MIME[ext_a])
    d2.download_button(f"⬇️ {name_b}", file_b, f"{stem}_contractor2{ext_b}", cs.MIME[ext_b])
    d3.download_button("⬇️ Overview", file_o, f"{stem}_split_overview{ext_o}", cs.MIME[ext_o])
    d4.download_button("📦 Everything (.zip)", cs.bundle_zip(res, stem, recolor), f"{stem}_contractor_split.zip",
                       "application/zip")


def _render_releases(vis: list[Feature], stem: str, base_params: tuple, names: tuple, clip: bool, weights: dict,
                     fixed: Optional[float], difficulty) -> None:
    """Hand the work out a set footage at a time: release 1, then release 2 from where 1 stopped, and so on."""
    import pydeck as pdk
    from kml2report import contractor_split as cs

    st.caption("Each release gives **each** contractor the footage below, picked with the same automatic balancing "
               "(fair share of the hard work, handholes and duct sizes). Release 2 starts where release 1 stopped.")
    c1, c2, c3 = st.columns(3)
    per = c1.number_input("Footage per contractor per release (ft)", 500, 200000, 5000, 500, key="rl_per")
    start_label = c2.selectbox("Hand out work starting from", ["Auto (west or south end)", "West end", "East end",
                                                               "South end", "North end"], key="rl_start")
    upto = int(c3.number_input("Plan releases up to", 1, 200, 3, 1, key="rl_upto",
                               help="Earlier releases never change when you plan more of them."))
    start = {"Auto (west or south end)": "auto", "West end": "west", "East end": "east", "South end": "south",
             "North end": "north"}[start_label]
    params = (base_params, st.session_state.get("geometry_sig"), per, start, upto)
    if st.button("📦 Plan releases", type="primary", key="rl_go"):
        try:
            with st.spinner("Planning releases..."):
                plan = cs.plan_releases(vis, float(per), start, max_releases=upto, clip=clip, weights=weights,
                                        fixed_angle=fixed, names=names, difficulty=difficulty)
            st.session_state["rl_plan"], st.session_state["rl_params"] = plan, params
        except ValueError as exc:
            st.error(str(exc))
            return
    plan = st.session_state.get("rl_plan")
    if plan is None or st.session_state.get("rl_params") != params:
        return
    for w in plan.warnings:
        st.warning(w)
    if not plan.releases:
        return
    left_ft = sum(f.geometry.length_feet for f in plan.remaining if f.geometry)
    m1, m2, m3 = st.columns(3)
    m1.metric("Releases planned", len(plan.releases))
    m2.metric("Handed out so far (each)", f"{plan.overview.iloc[-1, plan.overview.columns.get_loc(f'{plan.names[0]} total so far ft')]:,.0f} ft")
    m3.metric("Not yet released", f"{left_ft:,.0f} ft")
    st.dataframe(plan.overview, use_container_width=True)

    rel_no = int(st.number_input("Show release", 1, len(plan.releases), 1, 1, key="rl_show"))
    rel = plan.releases[rel_no - 1]
    st.dataframe(rel.split.stats, use_container_width=True)

    def layer(feats, rgb, alpha, wide=3):
        feats_json = [{"type": "Feature", "geometry": f.geometry.to_geojson(),
                       "properties": {"name": f.name, "len": f"{f.geometry.length_feet:,.0f} ft" if f.geometry.length_m else ""}}
                      for f in feats if f.geometry and f.geometry.to_geojson()]
        return pdk.Layer("GeoJsonLayer", {"type": "FeatureCollection", "features": feats_json}, pickable=True,
                         stroked=True, filled=True, get_line_color=rgb + [alpha], get_fill_color=rgb + [alpha // 3],
                         get_point_radius=3, point_radius_min_pixels=4, line_width_min_pixels=wide)

    layers = [layer(plan.remaining, [170, 170, 170], 140, 2)]
    for r in plan.releases:
        if r.number < rel_no:
            layers += [layer(r.a, _hex_to_rgb(cs.COLORS[0]), 70, 2), layer(r.b, _hex_to_rgb(cs.COLORS[1]), 70, 2)]
    layers += [layer(rel.a, _hex_to_rgb(cs.COLORS[0]), 255, 4), layer(rel.b, _hex_to_rgb(cs.COLORS[1]), 255, 4)]
    later = [f for r in plan.releases if r.number > rel_no for f in r.a + r.b]
    layers.insert(1, layer(later, [120, 120, 120], 120, 2))
    b = gis.bounds_of(vis)
    if b:
        span = max(b[2] - b[0], b[3] - b[1], 1e-5)
        st.pydeck_chart(pdk.Deck(layers=layers, map_style="road", tooltip={"html": "<b>{name}</b> {len}"},
                                 initial_view_state=pdk.ViewState(longitude=(b[0] + b[2]) / 2,
                                                                  latitude=(b[1] + b[3]) / 2,
                                                                  zoom=max(1.0, min(19.0, math.log2(360 / span) - 1)))),
                        use_container_width=True)
    st.markdown(f"<span style='color:{cs.COLORS[0]}'>■</span> {plan.names[0]} &nbsp; "
                f"<span style='color:{cs.COLORS[1]}'>■</span> {plan.names[1]} &nbsp; "
                "<span style='color:#aaa'>■</span> not yet released / other releases (faded)", unsafe_allow_html=True)

    fa, ea = cs.release_file(plan, rel_no, 0, stem)
    fb, eb = cs.release_file(plan, rel_no, 1, stem)
    d1, d2, d3 = st.columns(3)
    d1.download_button(f"⬇️ Release {rel_no} - {plan.names[0]}", fa, f"{stem}_release{rel_no:02d}_contractor1{ea}",
                       cs.MIME[ea])
    d2.download_button(f"⬇️ Release {rel_no} - {plan.names[1]}", fb, f"{stem}_release{rel_no:02d}_contractor2{eb}",
                       cs.MIME[eb])
    d3.download_button("📦 All releases (.zip)", cs.releases_zip(plan, stem), f"{stem}_releases.zip", "application/zip")
