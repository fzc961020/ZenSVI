from typing import List, Union, Tuple, Any
from pathlib import Path
from matplotlib import pyplot as plt, font_manager, colors
import geopandas as gpd
import pandas as pd
import numpy as np
import glob
import h3
from shapely.geometry import Polygon
import osmnx as ox
import contextily as ctx

from .font_property import get_font_properties

def lat_lng_to_h3(row, resolution=7):
    """Convert latitude and longitude to H3 hex ID at the specified resolution."""
    return h3.geo_to_h3(row['lat'], row['lon'], resolution)

def h3_to_polygon(hex_id):
    """Convert H3 hex ID to a Shapely polygon."""
    vertices = h3.h3_to_geo_boundary(hex_id, geo_json=True)
    return Polygon(vertices)

def create_line(gdf, variable_name = None):
    # gdf is a point GeoDataFrame, so convert it to polygon by taking convex hull
    polygon = gdf["geometry"].buffer(100).to_crs(4326).unary_union
    # then use osmnx to get street network graph_from_polygon
    G = ox.graph_from_polygon(polygon, network_type = "all", retain_all=True)
    # convert to GeoDataFrame
    line_gdf = ox.graph_to_gdfs(G, nodes=False, edges=True).reset_index()
    # attached point data to the nearest edge (within some distance tolerance)
    line_gdf = line_gdf.to_crs(gdf.crs)
    # join point to nearest line segment
    gdf = gpd.sjoin_nearest(gdf, line_gdf, max_distance=100, how = "left")
    # left join back to the line_gd
    if variable_name:
        # aggregate by the id and get mean of the variable
        aggregated_data = gdf.groupby('u')[variable_name].mean().reset_index(name='mean_value')
        gdf[variable_name] = gdf['u'].map(aggregated_data.set_index('u')['mean_value'])
    else: 
        # aggregate by the id and get count
        aggregated_data = gdf.groupby('u').size().reset_index(name='count')
        gdf['count'] = gdf['u'].map(aggregated_data.set_index('u')['count'])
    # drop geomtry column in gdf
    gdf = gdf.drop(columns = "geometry")
    # join gdf to line_gdf
    line_gdf = gpd.GeoDataFrame(line_gdf.merge(gdf, on = 'u', how="left"))
    return line_gdf.to_crs(3857)

def create_hexagon(gdf, resolution=7, variable_name = None):
    gdf = gdf.to_crs(4326)
    gdf['h3_id'] = gdf.apply(lat_lng_to_h3, resolution=resolution, axis=1)
    if variable_name:
        aggregated_data = gdf.groupby('h3_id')[variable_name].mean().reset_index(name='mean_value')
        gdf[variable_name] = gdf['h3_id'].map(aggregated_data.set_index('h3_id')['mean_value'])
    else:
        aggregated_data = gdf.groupby('h3_id').size().reset_index(name='count')
        gdf['count'] = gdf['h3_id'].map(aggregated_data.set_index('h3_id')['count'])
    gdf['geometry'] = gdf['h3_id'].apply(h3_to_polygon)
    hex_gdf = gpd.GeoDataFrame(gdf, geometry='geometry')
    return hex_gdf.to_crs(3857)
    
def add_colorbar(fig, ax, vmin, vmax, cmap, legend_title, prop, prop_legend, font_color, orientation='vertical', dark_mode=False):
    """Adds a colorbar to the figure based on given parameters, with optional dark mode."""
    norm = colors.Normalize(vmin=vmin, vmax=vmax)
    sm = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=ax, orientation=orientation, fraction=0.036, pad=0.04)
    cbar.set_label(legend_title, fontproperties=prop, color=font_color)
    cbar.ax.tick_params(labelsize=prop_legend.get_size(), color=font_color)
    cbar.outline.set_edgecolor(font_color)
    for label in cbar.ax.get_xticklabels():
        label.set_fontproperties(prop_legend)
        label.set_color(font_color)

def plot_map(path_pid: Union[str, Path], 
             pid_column: str = "panoid",
             dir_input: Union[str, Path] = None,
             csv_file_pattern: str = None,
             variable_name: str = None, 
             plot_type: str = "point",
             path_output: Union[str, Path] = None,
             resolution: int = 7,
             cmap: str = "viridis", 
             legend: bool = True,
             title: str = None,  
             legend_title: str = None, 
             basemap_source: Any = ctx.providers.CartoDB.PositronNoLabels,
             dpi: int = 300,
             font_size: int = 30,
             dark_mode: bool = False,
             **kwargs) -> Tuple[plt.Figure, plt.Axes]:
    """Plot map with points or polygons from a csv file."""
    font_color = '#2b2b2b' if not dark_mode else 'white'
    # Load path_pid with longitude and latitude
    pid_df = pd.read_csv(path_pid)

    fig, ax = plt.subplots(figsize=(10, 10)) 
    if dark_mode:
        plt.style.use('dark_background')
    gdf = None
    if dir_input and csv_file_pattern and variable_name:
        dir_input = Path(dir_input)
        csv_files = glob.glob(str(dir_input / '**' / csv_file_pattern), recursive=True)
        df_list = [pd.read_csv(file) for file in csv_files]
        merged_df = pd.concat(df_list, ignore_index=True)
        final_df = pd.merge(pid_df, merged_df, left_on=pid_column, right_on="filename_key", how="inner")
        gdf = gpd.GeoDataFrame(final_df, geometry=gpd.points_from_xy(final_df.lon, final_df.lat), crs="EPSG:4326")
        gdf = gdf.to_crs(3857)
        if plot_type == "line":
            gdf = create_line(gdf, variable_name = variable_name)
        elif plot_type == "hexagon":
            gdf = create_hexagon(gdf, resolution=resolution, variable_name = variable_name)
        gdf.plot(ax=ax, column = variable_name, cmap = cmap,  **kwargs)
    else:
        gdf = gpd.GeoDataFrame(pid_df, geometry=gpd.points_from_xy(pid_df.lon, pid_df.lat), crs="EPSG:4326")
        gdf = gdf.to_crs(3857)
        if plot_type == "point":
            gdf.plot(ax=ax, **kwargs)
        elif plot_type == "line":
            gdf = create_line(gdf)
            gdf.plot(ax=ax, column = "count", cmap = cmap,  **kwargs)
        elif plot_type == "hexagon":
            gdf = create_hexagon(gdf, resolution=resolution)
            gdf.plot(ax=ax, column = "count", cmap = cmap,  **kwargs)
        else:
            raise ValueError("Invalid plot type")
    # Add basemap if basemap_source is provided
    if basemap_source is not None:
        ctx.add_basemap(ax, source=basemap_source)
        ax.set_axis_off()  # Optional: Remove axis for visual clarity

    # After plotting the GeoDataFrame
    prop_title, prop, prop_legend = get_font_properties(font_size)
    ax.set_title(title, fontproperties=prop_title, color=font_color)  # Set the title with font properties
    
    if legend and not (plot_type == "point" and not variable_name):
        # Assuming variable_name represents continuous data. Adjust vmin and vmax accordingly.
        if variable_name:
            vmin, vmax = gdf[variable_name].min(), gdf[variable_name].max()
        else:
            vmin, vmax = gdf['count'].min(), gdf['count'].max()
        add_colorbar(fig, ax, vmin, vmax, cmap, legend_title, prop, prop_legend, font_color, orientation='horizontal', dark_mode=dark_mode)

    if path_output:
        plt.savefig(path_output, bbox_inches='tight', dpi=dpi)
    return fig, ax
