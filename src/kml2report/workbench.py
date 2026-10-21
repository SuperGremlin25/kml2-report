"""Streamlit 'GIS Workbench' tab: map, layers, attribute table, analysis, QA, export."""

from __future__ import annotations

import json
from typing import Optional

import pandas as pd
import streamlit as st

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

    t_map, t_table, t_stats, t_analysis, t_qa, t_export = right.tabs(
        ["🗺️ Map", "📋 Attribute Table", "📊 Statistics", "🧭 Analysis", "✅ QA / QC", "📦 Export"])

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

    # --------------------------------------------------------------- export
    with t_export:
        st.caption(f"Exports include the {len(vis):,} currently visible feature(s).")
        crs_label = st.selectbox("Coordinate system for Shapefile / DXF", list(COMMON_CRS), index=2, key="wb_exp_crs")
        epsg = _resolve_crs(crs_label, vis)
        e1, e2, e3, e4 = st.columns(4)
        e1.download_button("GeoJSON", geoexport.to_geojson_bytes(vis), f"{stem}.geojson", "application/geo+json")
        e2.download_button("KML (clean)", geoexport.to_kml_bytes(vis, stem), f"{stem}_clean.kml",
                           "application/vnd.google-earth.kml+xml")
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
