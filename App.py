import geopandas as gpd
import pandas as pd
import tkinter as tk
from tkinter import filedialog, messagebox
import os
import pyogrio
import json
import folium
from folium.plugins import Search

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    import time
    SELENIUM_AVAILABLE = True
except Exception:
    SELENIUM_AVAILABLE = False

SCHOOLS_PATH = r"C:\GIP\schools\schools.shp"
HOSPITALS_PATH = r"C:\GIP\hospitals\hospitals.shp"
REGIONS_PATH = r"C:\GIP\regions\regions.shp"
ROADS_PATH = r"C:\GIP\Roads\Roads.shp"
TOWNS_PATH = r"C:\GIP\TownsandVillages\TownsandVillages.shp"

schools = None
hospitals = None
population = None
regions = None
roads = None
towns = None


def get_label_field(gdf, candidates):
    for c in candidates:
        if c in gdf.columns:
            return c
    return None


def fix_geometries(gdf, name="layer"):
    gdf = gdf.copy()
    if "geometry" not in gdf.columns:
        raise Exception(f"{name} does not contain a geometry column.")
    gdf = gdf[gdf.geometry.notna()]
    gdf = gdf[~gdf.geometry.is_empty]
    if gdf.empty:
        raise Exception(f"{name} has no valid geometries.")
    geom_types = set(gdf.geom_type.dropna().unique())
    if geom_types.issubset({"Polygon", "MultiPolygon"}):
        try:
            gdf["geometry"] = gdf.geometry.buffer(0)
            gdf = gdf[gdf.geometry.notna()]
            gdf = gdf[~gdf.geometry.is_empty]
        except Exception:
            pass
    if gdf.empty:
        raise Exception(f"{name} has no valid geometries after cleaning.")
    return gdf


def safe_point(geom):
    if geom is None or geom.is_empty:
        return None
    if geom.geom_type == "Point":
        return geom
    return geom.representative_point()


def load_shapefile(path, name):
    if not os.path.exists(path):
        raise Exception(f"{name} path not found:\n{path}")
    gdf = gpd.read_file(path)
    if gdf.empty:
        raise Exception(f"{name} shapefile is empty.")
    if gdf.crs is None:
        raise Exception(f"{name} shapefile has no CRS.")
    return fix_geometries(gdf, name)


def standardize_population_columns(gdf):
    cols = gdf.columns
    pop2011_candidates = ["pop2011", "population2011", "POP2011", "Population2011"]
    pop2023_candidates = ["pop2023", "population2023", "POP2023", "Population2023", "population"]
    pop2011_field = next((c for c in pop2011_candidates if c in cols), None)
    pop2023_field = next((c for c in pop2023_candidates if c in cols), None)
    if pop2011_field is None:
        raise Exception("Population layer must contain a 2011 field like 'pop2011' or 'population2011'.")
    if pop2023_field is None:
        raise Exception("Population layer must contain a 2023 field like 'pop2023' or 'population2023'.")
    return gdf.rename(columns={pop2011_field: "pop2011", pop2023_field: "pop2023"})


def save_safe_shapefile(gdf, path):
    gdf = gdf.copy()
    rename_map = {}
    used_names = set()
    for col in gdf.columns:
        if col == "geometry":
            continue
        new_col = col[:10] if len(col) > 10 else col
        base = new_col
        i = 1
        while new_col in used_names:
            suffix = str(i)
            new_col = base[:10 - len(suffix)] + suffix
            i += 1
        rename_map[col] = new_col
        used_names.add(new_col)
    gdf = gdf.rename(columns=rename_map)
    gdf.to_file(path)


def priority_color(priority):
    colors = {
        "Very High": "#8B0000",
        "High": "#FF4500",
        "Moderate": "#FFA500",
        "Low": "#FFD700",
        "Served": "#2E8B57",
    }
    return colors.get(priority, "#999999")


def classify_priority(score):
    if score >= 0.75:
        return "Very High"
    if score >= 0.50:
        return "High"
    if score >= 0.25:
        return "Moderate"
    return "Low"


def normalize(series):
    if len(series) == 0:
        return series
    if series.max() == series.min():
        return pd.Series([0] * len(series), index=series.index)
    return (series - series.min()) / (series.max() - series.min())


def load_default_data():
    global schools, hospitals, regions, roads, towns
    errors = []
    for label, path, var_name in [
        ("Schools", SCHOOLS_PATH, "schools"),
        ("Hospitals", HOSPITALS_PATH, "hospitals"),
        ("Regions", REGIONS_PATH, "regions"),
        ("Roads", ROADS_PATH, "roads"),
        ("Towns/Villages", TOWNS_PATH, "towns"),
    ]:
        try:
            globals()[var_name] = load_shapefile(path, label)
        except Exception as e:
            globals()[var_name] = None
            errors.append(f"{label}: {e}")
    if errors:
        messagebox.showwarning(
            "Loaded With Warnings",
            "Some layers could not be loaded:\n\n" + "\n\n".join(errors) +
            "\n\nThe app will continue with the valid layers."
        )
    else:
        messagebox.showinfo("Success", "Default data loaded successfully.")


def load_population():
    global population
    path = filedialog.askopenfilename(
        title="Select Population Shapefile (2011 + 2023)",
        filetypes=[("Shapefile", "*.shp")]
    )
    if not path:
        return
    try:
        population = gpd.read_file(path)
        if population.empty:
            raise Exception("Population shapefile is empty.")
        if population.crs is None:
            raise Exception("Population shapefile has no CRS.")
        population = standardize_population_columns(population)
        population = fix_geometries(population, "Population")
        messagebox.showinfo("Success", "Population shapefile with 2011 and 2023 loaded successfully.")
    except Exception as e:
        messagebox.showerror("Error", str(e))


def prepare_base_layers():
    global regions, roads, towns, population
    if population is None:
        raise Exception("Load the population shapefile first.")
    if regions is None:
        raise Exception("Load the regions shapefile first.")
    pop = fix_geometries(population.to_crs(epsg=3857), "Population")
    reg = fix_geometries(regions.to_crs(epsg=3857), "Regions")
    namibia_boundary = gpd.GeoDataFrame(geometry=[reg.geometry.union_all()], crs=reg.crs)
    pop_clip = gpd.clip(pop, namibia_boundary)
    pop_clip["geometry"] = pop_clip.geometry.simplify(50)
    if "region_nam" not in pop_clip.columns and "region_nam" in reg.columns:
        try:
            pop_points = pop_clip.copy()
            pop_points["geometry"] = pop_points.geometry.representative_point()
            joined = gpd.sjoin(pop_points, reg[["region_nam", "geometry"]], how="left", predicate="within")
            pop_clip["region_nam"] = joined["region_nam"].fillna("Unknown").values
        except Exception:
            pop_clip["region_nam"] = "Unknown"
    roads_clip = None
    towns_clip = None
    if roads is not None:
        try:
            roads_clip = gpd.clip(roads.to_crs(epsg=3857), namibia_boundary)
        except Exception:
            roads_clip = None
    if towns is not None:
        try:
            towns_clip = gpd.clip(fix_geometries(towns.to_crs(epsg=3857), "Towns/Villages"), namibia_boundary)
        except Exception:
            towns_clip = None
    return pop_clip, reg, roads_clip, towns_clip, namibia_boundary


def add_2033_prediction(pop_gdf):
    pop_gdf = pop_gdf.copy()
    pop_gdf["pop2011"] = pd.to_numeric(pop_gdf["pop2011"], errors="coerce").fillna(0)
    pop_gdf["pop2023"] = pd.to_numeric(pop_gdf["pop2023"], errors="coerce").fillna(0)
    safe2011 = pop_gdf["pop2011"].replace(0, 1)
    pop_gdf["growth_rt"] = ((pop_gdf["pop2023"] / safe2011) ** (1 / 12)) - 1
    pop_gdf["growth_rt"] = pop_gdf["growth_rt"].replace([float("inf"), -float("inf")], 0).fillna(0)
    pop_gdf["growth_rt"] = pop_gdf["growth_rt"].clip(lower=-0.05, upper=0.15)
    pop_gdf["pop2033"] = pop_gdf["pop2023"] * ((1 + pop_gdf["growth_rt"]) ** 10)
    return pop_gdf


def calculate_priority(pop_gdf, facilities_gdf, roads_gdf=None, towns_gdf=None):
    fac = fix_geometries(facilities_gdf.to_crs(epsg=3857), "Facilities")
    cent = pop_gdf.copy()
    cent["geometry"] = cent.geometry.representative_point()
    cent["dist_fac"] = cent.geometry.apply(lambda geom: geom.distance(fac.geometry.union_all()))
    if roads_gdf is not None and not roads_gdf.empty:
        roads_union = roads_gdf.geometry.union_all()
        cent["dist_road"] = cent.geometry.apply(lambda geom: geom.distance(roads_union))
    else:
        cent["dist_road"] = 0
    if towns_gdf is not None and not towns_gdf.empty:
        towns_union = towns_gdf.geometry.union_all()
        cent["dist_town"] = cent.geometry.apply(lambda geom: geom.distance(towns_union))
    else:
        cent["dist_town"] = 0
    out = pop_gdf.copy()
    out["dist_fac"] = cent["dist_fac"].values
    out["dist_road"] = cent["dist_road"].values
    out["dist_town"] = cent["dist_town"].values
    out["n_pop23"] = normalize(out["pop2023"])
    out["n_pop33"] = normalize(out["pop2033"])
    out["n_growth"] = normalize(out["growth_rt"])
    out["n_facdst"] = normalize(out["dist_fac"])
    out["n_rclose"] = 1 - normalize(out["dist_road"]) if out["dist_road"].max() != out["dist_road"].min() else 0
    out["n_tclose"] = 1 - normalize(out["dist_town"]) if out["dist_town"].max() != out["dist_town"].min() else 0
    out["prio_score"] = (
        0.20 * out["n_pop23"] +
        0.30 * out["n_pop33"] +
        0.20 * out["n_growth"] +
        0.20 * out["n_facdst"] +
        0.07 * out["n_rclose"] +
        0.03 * out["n_tclose"]
    )
    out["prio_class"] = out["prio_score"].apply(classify_priority)
    return out


def suggest_new_facilities(pop_scored, underserved_gdf, existing_facilities, n=10, min_distance_m=30000):
    if underserved_gdf is None or underserved_gdf.empty:
        return gpd.GeoDataFrame(geometry=[], crs=pop_scored.crs)
    try:
        candidates = gpd.overlay(pop_scored, underserved_gdf[["geometry"]], how="intersection")
    except Exception:
        candidates = underserved_gdf.copy()
    if candidates.empty:
        return gpd.GeoDataFrame(geometry=[], crs=pop_scored.crs)
    candidates = candidates.sort_values(by="prio_score", ascending=False).copy()
    candidates["geometry"] = candidates.geometry.representative_point()
    existing_union = existing_facilities.geometry.union_all() if existing_facilities is not None and not existing_facilities.empty else None
    selected_rows = []
    selected_geoms = []
    for _, row in candidates.iterrows():
        p = row.geometry
        if p is None or p.is_empty:
            continue
        if existing_union is not None and p.distance(existing_union) < 20000:
            continue
        if any(p.distance(g) < min_distance_m for g in selected_geoms):
            continue
        selected_rows.append(row)
        selected_geoms.append(p)
        if len(selected_rows) >= n:
            break
    top = candidates.head(n).copy() if not selected_rows else gpd.GeoDataFrame(selected_rows, crs=pop_scored.crs)
    top["recommend"] = "Suggested New Facility"
    return top


def build_service_area(facilities_gdf, service_distance):
    fac = fix_geometries(facilities_gdf.to_crs(epsg=3857), "Facilities")
    buffers = fac.copy()
    buffers["geometry"] = buffers.geometry.buffer(service_distance)
    merged_service = gpd.GeoDataFrame(geometry=[buffers.geometry.union_all()], crs=fac.crs)
    merged_service["geometry"] = merged_service.geometry.simplify(100)
    return buffers, merged_service


def split_served_underserved(pop_gdf, merged_service, pop_field_name):
    pop_gdf = pop_gdf[pop_gdf.geometry.is_valid].copy()
    merged_service = merged_service[merged_service.geometry.is_valid].copy()
    served = gpd.overlay(pop_gdf, merged_service, how="intersection")
    underserved = gpd.overlay(pop_gdf, merged_service, how="difference")
    if not served.empty:
        served["status"] = "Served"
    if not underserved.empty:
        underserved["status"] = "Underserved"
    return served, underserved


def add_regions_to_map(m, reg_wgs):
    tooltip = None
    if "region_nam" in reg_wgs.columns:
        tooltip = folium.GeoJsonTooltip(fields=["region_nam"], aliases=["Region:"])
    region_geo = folium.GeoJson(
        reg_wgs,
        name="Namibia Regions",
        style_function=lambda x: {"fillColor": "none", "color": "black", "weight": 2.5, "fillOpacity": 0},
        highlight_function=lambda x: {"weight": 4, "color": "blue"},
        tooltip=tooltip
    ).add_to(m)
    if "region_nam" in reg_wgs.columns:
        Search(
            layer=region_geo,
            search_label="region_nam",
            placeholder="Search Region...",
            collapsed=False,
            position="topleft"
        ).add_to(m)


def add_roads_to_map(m, roads_wgs):
    if roads_wgs is not None and not roads_wgs.empty:
        folium.GeoJson(
            roads_wgs,
            name="Roads",
            style_function=lambda x: {"color": "black", "weight": 1.5}
        ).add_to(m)


def add_towns_to_map(m, towns_wgs):
    if towns_wgs is not None and not towns_wgs.empty:
        for _, row in towns_wgs.iterrows():
            p = safe_point(row.geometry)
            if p is None:
                continue
            folium.CircleMarker(
                location=[p.y, p.x],
                radius=3,
                color="black",
                weight=1,
                fill=True,
                fill_color="blue",
                fill_opacity=1
            ).add_to(m)


def add_facilities_to_map(m, facilities_wgs, facility_type):
    if facility_type == "schools":
        label_field = get_label_field(facilities_wgs, ["levels", "LEVELS", "level", "LEVEL"])
    else:
        label_field = get_label_field(facilities_wgs, ["fac_type", "FAC_TYPE", "type", "TYPE"])
    for _, row in facilities_wgs.iterrows():
        p = safe_point(row.geometry)
        if p is None:
            continue
        fill = "white" if facility_type == "schools" else "#ffb6c1"
        folium.CircleMarker(
            location=[p.y, p.x],
            radius=5,
            color="black",
            weight=1.5,
            fill=True,
            fill_color=fill,
            fill_opacity=1
        ).add_to(m)
        if label_field:
            folium.Marker(
                [p.y, p.x],
                icon=folium.DivIcon(html=f"<div style='font-size:9px;font-weight:bold;color:black;white-space: nowrap;'>{row[label_field]}</div>")
            ).add_to(m)


def add_suggested_sites_to_map(m, suggested_sites):
    if suggested_sites is None or suggested_sites.empty:
        return
    suggested_wgs = suggested_sites.to_crs(epsg=4326)
    for _, row in suggested_wgs.iterrows():
        p = safe_point(row.geometry)
        if p is None:
            continue
        score = row.get("prio_score", "")
        prio = row.get("prio_class", "")
        tooltip = f"Suggested New Facility | Priority: {prio} | Score: {round(score, 3) if score != '' else ''}"
        folium.Marker(
            location=[p.y, p.x],
            icon=folium.Icon(color="purple", icon="plus", prefix="fa"),
            tooltip=tooltip
        ).add_to(m)


def add_legend(m):
    legend_html = """
    <div style="position: fixed; bottom: 30px; left: 30px; width: 285px; z-index: 9999; font-size: 14px; background-color: white; border: 2px solid black; border-radius: 6px; padding: 12px; box-shadow: 2px 2px 6px rgba(0,0,0,0.3);">
    <b>Map Legend</b><br><br>
    <span style="display:inline-block;width:18px;height:12px;background:#2E8B57;border:1px solid black;"></span> Served<br>
    <span style="display:inline-block;width:18px;height:12px;background:#8B0000;border:1px solid black;"></span> Very High Priority<br>
    <span style="display:inline-block;width:18px;height:12px;background:#FF4500;border:1px solid black;"></span> High Priority<br>
    <span style="display:inline-block;width:18px;height:12px;background:#FFA500;border:1px solid black;"></span> Moderate Priority<br>
    <span style="display:inline-block;width:18px;height:12px;background:#FFD700;border:1px solid black;"></span> Low Priority<br>
    <span style="display:inline-block;width:18px;height:12px;background:#6baed6;border:1px solid black;"></span> 20 km Service Area<br>
    <span style="display:inline-block;width:18px;height:12px;background:#d9d9d9;border:1px solid black;"></span> Population Background<br>
    <span style="display:inline-block;width:18px;height:2px;background:black;"></span> Roads / Namibia Regions<br>
    <span style="display:inline-block;width:10px;height:10px;background:white;border:2px solid black;border-radius:50%;"></span> Schools<br>
    <span style="display:inline-block;width:10px;height:10px;background:#ffb6c1;border:2px solid black;border-radius:50%;"></span> Hospitals<br>
    <span style="display:inline-block;width:12px;height:12px;background:purple;border:1px solid black;border-radius:50%;"></span> Suggested New Facility<br>
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))


def add_year_layer(m, gdf, layer_name, value_field, served=False, priority=False):
    if gdf is None or gdf.empty:
        return
    tooltip_fields = [value_field]
    tooltip_aliases = [f"{value_field}:"]
    for field, alias in [("pop2011", "Population 2011:"), ("pop2023", "Population 2023:"), ("pop2033", "Population 2033:")]:
        if field in gdf.columns and field not in tooltip_fields:
            tooltip_fields.append(field)
            tooltip_aliases.append(alias)
    if priority and "prio_class" in gdf.columns:
        tooltip_fields.append("prio_class")
        tooltip_aliases.append("Priority Level:")
    if served:
        folium.GeoJson(
            gdf,
            name=layer_name,
            style_function=lambda x: {
                "fillColor": priority_color("Served"),
                "color": "#2e8b57",
                "weight": 0.8,
                "fillOpacity": 0.50
            },
            tooltip=folium.GeoJsonTooltip(fields=tooltip_fields, aliases=tooltip_aliases)
        ).add_to(m)
    elif priority:
        folium.GeoJson(
            gdf,
            name=layer_name,
            style_function=lambda feature: {
                "fillColor": priority_color(feature["properties"].get("prio_class")),
                "color": "black",
                "weight": 1.0,
                "fillOpacity": 0.70
            },
            tooltip=folium.GeoJsonTooltip(fields=tooltip_fields, aliases=tooltip_aliases)
        ).add_to(m)
    else:
        folium.GeoJson(gdf, name=layer_name).add_to(m)


def export_map_to_png(html_path, output_png):
    if not SELENIUM_AVAILABLE:
        return False
    try:
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--window-size=2560,1600")
        driver = webdriver.Chrome(options=options)
        driver.get("file:///" + os.path.abspath(html_path).replace("\\", "/"))
        time.sleep(3)
        driver.save_screenshot(output_png)
        driver.quit()
        return True
    except Exception:
        return False


def build_region_stats(pop_scored, served2033, underserved2033):
    region_stats = {}
    if "region_nam" in pop_scored.columns:
        for region, group in pop_scored.groupby("region_nam"):
            region_stats[str(region)] = {
                "pop2011": int(group["pop2011"].sum()),
                "pop2023": int(group["pop2023"].sum()),
                "pop2033": int(group["pop2033"].sum()),
                "served": 0,
                "underserved": 0
            }
    if "region_nam" in served2033.columns and not served2033.empty:
        for region, group in served2033.groupby("region_nam"):
            region_stats.setdefault(str(region), {"pop2011": 0, "pop2023": 0, "pop2033": 0, "served": 0, "underserved": 0})
            region_stats[str(region)]["served"] = int(group["pop2033"].sum())
    if "region_nam" in underserved2033.columns and not underserved2033.empty:
        for region, group in underserved2033.groupby("region_nam"):
            region_stats.setdefault(str(region), {"pop2011": 0, "pop2023": 0, "pop2033": 0, "served": 0, "underserved": 0})
            region_stats[str(region)]["underserved"] = int(group["pop2033"].sum())
    region_stats["Namibia"] = {
        "pop2011": int(pop_scored["pop2011"].sum()),
        "pop2023": int(pop_scored["pop2023"].sum()),
        "pop2033": int(pop_scored["pop2033"].sum()),
        "served": int(served2033["pop2033"].sum() if not served2033.empty else 0),
        "underserved": int(underserved2033["pop2033"].sum() if not underserved2033.empty else 0)
    }
    return region_stats


def create_analysis_map(
    facilities,
    pop_scored,
    reg,
    roads_gdf,
    towns_gdf,
    merged_service,
    served2011,
    underserved2011,
    served2023,
    underserved2023,
    served2033,
    underserved2033,
    output_folder,
    filename,
    facility_type,
    suggested_sites
):
    facilities_wgs = facilities.to_crs(epsg=4326)
    pop_wgs = pop_scored.to_crs(epsg=4326)
    reg_wgs = reg.to_crs(epsg=4326)
    roads_wgs = roads_gdf.to_crs(epsg=4326) if roads_gdf is not None and not roads_gdf.empty else None
    towns_wgs = towns_gdf.to_crs(epsg=4326) if towns_gdf is not None and not towns_gdf.empty else None
    merged_service_wgs = merged_service.to_crs(epsg=4326)
    served2011_wgs = served2011.to_crs(epsg=4326) if not served2011.empty else served2011
    underserved2011_wgs = underserved2011.to_crs(epsg=4326) if not underserved2011.empty else underserved2011
    served2023_wgs = served2023.to_crs(epsg=4326) if not served2023.empty else served2023
    underserved2023_wgs = underserved2023.to_crs(epsg=4326) if not underserved2023.empty else underserved2023
    served2033_wgs = served2033.to_crs(epsg=4326) if not served2033.empty else served2033
    underserved2033_wgs = underserved2033.to_crs(epsg=4326) if not underserved2033.empty else underserved2033

    center = reg_wgs.geometry.union_all().centroid
    m = folium.Map(location=[center.y, center.x], zoom_start=6, tiles=None)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap").add_to(m)
    folium.TileLayer("CartoDB dark_matter", name="Dark Map").add_to(m)
    folium.TileLayer("CartoDB positron", name="Light Map").add_to(m)

    folium.GeoJson(
        pop_wgs,
        name="Population Background",
        style_function=lambda x: {"fillColor": "#d9d9d9", "color": "#aaaaaa", "weight": 0.4, "fillOpacity": 0.15},
        tooltip=folium.GeoJsonTooltip(fields=["pop2011", "pop2023", "pop2033"], aliases=["Population 2011:", "Population 2023:", "Predicted 2033:"])
    ).add_to(m)
    folium.GeoJson(
        merged_service_wgs,
        name="20 km Service Area",
        style_function=lambda x: {"fillColor": "#6baed6", "color": "#3182bd", "weight": 1, "fillOpacity": 0.10}
    ).add_to(m)

    add_year_layer(m, served2011_wgs, "2011 Served", "pop2011", served=True)
    add_year_layer(m, underserved2011_wgs, "2011 Underserved", "pop2011", priority=True)
    add_year_layer(m, served2023_wgs, "2023 Served", "pop2023", served=True)
    add_year_layer(m, underserved2023_wgs, "2023 Underserved", "pop2023", priority=True)
    add_year_layer(m, served2033_wgs, "2033 Predicted Served", "pop2033", served=True)
    add_year_layer(m, underserved2033_wgs, "2033 Predicted Underserved", "pop2033", priority=True)

    add_roads_to_map(m, roads_wgs)
    add_towns_to_map(m, towns_wgs)
    add_regions_to_map(m, reg_wgs)
    add_facilities_to_map(m, facilities_wgs, facility_type)
    add_suggested_sites_to_map(m, suggested_sites)
    add_legend(m)

    region_stats = build_region_stats(pop_scored, served2033, underserved2033)
    region_stats_json = json.dumps(region_stats)
    total2011 = int(pop_scored["pop2011"].sum())
    total2023 = int(pop_scored["pop2023"].sum())
    total2033 = int(pop_scored["pop2033"].sum())
    total_served = int(served2033["pop2033"].sum() if not served2033.empty else 0)
    total_underserved = int(underserved2033["pop2033"].sum() if not underserved2033.empty else 0)
    region_options = "".join(f'<option value="{region}">{region}</option>' for region in sorted(region_stats.keys()))

    info_html = f"""
    <div style="position: fixed; bottom: 20px; right: 20px; z-index: 9999;">
        <button onclick="toggleInfo()" style="background-color: #2E8B57; color: white; padding: 8px 12px; border: none; cursor: pointer; font-weight: bold; border-radius: 4px;">ℹ Info</button>
        <div id="infoPanel" style="display: none; width: 360px; margin-top: 5px; background-color: white; border: 2px solid black; padding: 12px; font-size: 12px; box-shadow: 2px 2px 6px rgba(0,0,0,0.3); max-height: 520px; overflow-y: auto;">
            <b>Analysis Summary</b><br><br>
            <label><b>Select Region:</b></label><br>
            <select id="regionSelect" onchange="updateCharts(this.value)" style="width:100%; margin-bottom:8px;">{region_options}</select>
            <b>Population Growth</b><br>
            <canvas id="growthChart" height="120"></canvas><br>
            <b>Served vs Underserved</b><br>
            <canvas id="accessChart" height="120"></canvas><br>
            <hr>
            <b>Method:</b><br>
            • 20 km buffer analysis<br>
            • Overlay analysis<br>
            • Growth rate modelling<br>
            • Weighted priority scoring<br><br>
            <b>Interpretation:</b><br>
            ✔ Served = within 20 km of facilities<br>
            ✔ Underserved = outside service areas<br><br>
            <b>Priority Levels:</b><br>
            🔴 Very High – urgent need<br>
            🟠 High – significant demand<br>
            🟡 Moderate – moderate need<br>
            🟡 Low – lower priority<br><br>
            🟣 Suggested Locations = best new facility sites
        </div>
    </div>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
    var regionStats = {region_stats_json};
    function toggleInfo() {{
        var panel = document.getElementById("infoPanel");
        panel.style.display = (panel.style.display === "none") ? "block" : "none";
    }}
    var growthChart = new Chart(document.getElementById("growthChart"), {{
        type: "line",
        data: {{
            labels: ["2011", "2023", "2033"],
            datasets: [{{ label: "Population", data: [{total2011}, {total2023}, {total2033}], borderColor: "#2E8B57", backgroundColor: "#2E8B57", fill: false }}]
        }},
        options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }} }}
    }});
    var accessChart = new Chart(document.getElementById("accessChart"), {{
        type: "bar",
        data: {{
            labels: ["Served", "Underserved"],
            datasets: [{{ label: "Population", data: [{total_served}, {total_underserved}], backgroundColor: ["#2E8B57", "#8B0000"] }}]
        }},
        options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }} }}
    }});
    function updateCharts(regionName) {{
        var d = regionStats[regionName];
        if (!d) return;
        growthChart.data.datasets[0].data = [d.pop2011, d.pop2023, d.pop2033];
        growthChart.update();
        accessChart.data.datasets[0].data = [d.served, d.underserved];
        accessChart.update();
    }}
    </script>
    """
    m.get_root().html.add_child(folium.Element(info_html))

    title_html = f"""
    <div style="position: fixed; top: 12px; left: 50%; transform: translateX(-50%); z-index: 9999; background-color: white; border: 2px solid black; padding: 10px 18px; font-size: 16px; font-weight: bold; box-shadow: 2px 2px 6px rgba(0,0,0,0.3);">
        Namibia {facility_type.title()} Accessibility Analysis (2011–2023–2033)
    </div>
    """
    m.get_root().html.add_child(folium.Element(title_html))
    north_arrow_html = """
    <div style="position: fixed; top: 70px; right: 20px; z-index: 9999; text-align: center; background-color: white; border: 2px solid black; padding: 8px; font-size: 18px; font-weight: bold; box-shadow: 2px 2px 6px rgba(0,0,0,0.3);">
        N<br>▲
    </div>
    """
    m.get_root().html.add_child(folium.Element(north_arrow_html))
    folium.LayerControl(collapsed=False).add_to(m)
    map_path = os.path.join(output_folder, filename)
    m.save(map_path)
    return map_path


def run_analysis_for(facility_type):
    pop_clip, reg, roads_clip, towns_clip, namibia_boundary = prepare_base_layers()
    if facility_type == "schools":
        facilities = schools
        if facilities is None:
            raise Exception("Schools not loaded.")
        filename = "school_2011_2023_2033_analysis_map.html"
        out_prefix = "schools"
    else:
        facilities = hospitals
        if facilities is None:
            raise Exception("Hospitals not loaded.")
        filename = "hospital_2011_2023_2033_analysis_map.html"
        out_prefix = "hospitals"

    service_distance = 20000
    facilities_3857 = fix_geometries(facilities.to_crs(epsg=3857), facility_type)
    facilities_clip = gpd.clip(facilities_3857, namibia_boundary)
    pop_pred = add_2033_prediction(pop_clip)
    pop_scored = calculate_priority(pop_pred, facilities_clip, roads_clip, towns_clip)
    buffers, merged_service = build_service_area(facilities_clip, service_distance)

    base_cols = ["dist_fac", "prio_score", "prio_class", "geometry"]
    if "region_nam" in pop_scored.columns:
        base_cols.insert(0, "region_nam")
    pop2011_layer = pop_scored[["pop2011"] + base_cols].copy()
    pop2023_layer = pop_scored[["pop2023"] + base_cols].copy()
    pop2033_layer = pop_scored[["pop2033"] + base_cols].copy()

    served2011, underserved2011 = split_served_underserved(pop2011_layer, merged_service, "pop2011")
    served2023, underserved2023 = split_served_underserved(pop2023_layer, merged_service, "pop2023")
    served2033, underserved2033 = split_served_underserved(pop2033_layer, merged_service, "pop2033")
    suggested_sites = suggest_new_facilities(pop_scored, underserved2033, facilities_clip, n=10, min_distance_m=30000)

    output_folder = filedialog.askdirectory(title=f"Select folder to save {facility_type} outputs")
    if not output_folder:
        return

    save_safe_shapefile(facilities_clip, os.path.join(output_folder, f"{out_prefix}_clip.shp"))
    save_safe_shapefile(buffers, os.path.join(output_folder, f"{out_prefix}_20buf.shp"))
    save_safe_shapefile(merged_service, os.path.join(output_folder, f"{out_prefix}_20serv.shp"))
    save_safe_shapefile(pop_scored, os.path.join(output_folder, f"{out_prefix}_allyrs.shp"))
    save_safe_shapefile(served2011, os.path.join(output_folder, f"{out_prefix}_s2011.shp"))
    save_safe_shapefile(underserved2011, os.path.join(output_folder, f"{out_prefix}_u2011.shp"))
    save_safe_shapefile(served2023, os.path.join(output_folder, f"{out_prefix}_s2023.shp"))
    save_safe_shapefile(underserved2023, os.path.join(output_folder, f"{out_prefix}_u2023.shp"))
    save_safe_shapefile(served2033, os.path.join(output_folder, f"{out_prefix}_s2033.shp"))
    save_safe_shapefile(underserved2033, os.path.join(output_folder, f"{out_prefix}_u2033.shp"))
    save_safe_shapefile(suggested_sites, os.path.join(output_folder, f"{out_prefix}_suggest.shp"))

    map_path = create_analysis_map(
        facilities_clip, pop_scored, reg, roads_clip, towns_clip, merged_service,
        served2011, underserved2011, served2023, underserved2023, served2033, underserved2033,
        output_folder, filename, facility_type, suggested_sites
    )

    png_path = map_path.replace(".html", ".png")
    png_ok = export_map_to_png(map_path, png_path)
    total2011 = pop_scored["pop2011"].sum()
    total2023 = pop_scored["pop2023"].sum()
    total2033 = pop_scored["pop2033"].sum()
    served2011_sum = served2011["pop2011"].sum() if not served2011.empty else 0
    underserved2011_sum = underserved2011["pop2011"].sum() if not underserved2011.empty else 0
    served2023_sum = served2023["pop2023"].sum() if not served2023.empty else 0
    underserved2023_sum = underserved2023["pop2023"].sum() if not underserved2023.empty else 0
    served2033_sum = served2033["pop2033"].sum() if not served2033.empty else 0
    underserved2033_sum = underserved2033["pop2033"].sum() if not underserved2033.empty else 0
    png_message = f"\nPNG saved to:\n{png_path}" if png_ok else "\nPNG export skipped or failed. HTML map was still created."
    messagebox.showinfo(
        f"{facility_type.title()} Analysis Complete",
        f"{facility_type.title()} analysis completed.\n\n"
        f"2011 Total: {round(total2011, 0)}\n"
        f"2011 Served: {round(served2011_sum, 0)}\n"
        f"2011 Underserved: {round(underserved2011_sum, 0)}\n\n"
        f"2023 Total: {round(total2023, 0)}\n"
        f"2023 Served: {round(served2023_sum, 0)}\n"
        f"2023 Underserved: {round(underserved2023_sum, 0)}\n\n"
        f"2033 Predicted Total: {round(total2033, 0)}\n"
        f"2033 Predicted Served: {round(served2033_sum, 0)}\n"
        f"2033 Predicted Underserved: {round(underserved2033_sum, 0)}\n\n"
        f"Suggested new locations: {len(suggested_sites)}\n"
        f"Buffer distance used: 20 km\n\n"
        f"Map saved to:\n{map_path}"
        f"{png_message}"
    )


def run_school_analysis():
    try:
        run_analysis_for("schools")
    except Exception as e:
        messagebox.showerror("Error", f"School analysis failed:\n{str(e)}")


def run_hospital_analysis():
    try:
        run_analysis_for("hospitals")
    except Exception as e:
        messagebox.showerror("Error", f"Hospital analysis failed:\n{str(e)}")


root = tk.Tk()
root.title("Namibia 2011, 2023 and 2033 Planning Analysis Tool")
root.geometry("760x480")
tk.Label(root, text="Namibia 2011, 2023 and 2033 Planning Analysis Tool", font=("Arial", 15, "bold")).pack(pady=14)
tk.Label(root, text="Served / Underserved Analysis for 2011 and 2023, plus 2033 Predictions | 20 km Buffers", font=("Arial", 10)).pack(pady=4)
tk.Button(root, text="Load Default Base Data", width=54, command=load_default_data).pack(pady=8)
tk.Button(root, text="Load Population Shapefile (2011 + 2023)", width=54, command=load_population).pack(pady=8)
tk.Button(root, text="Run School Analysis (2011, 2023, 2033)", width=54, command=run_school_analysis).pack(pady=12)
tk.Button(root, text="Run Hospital Analysis (2011, 2023, 2033)", width=54, command=run_hospital_analysis).pack(pady=8)
root.mainloop()
