import os
import random
import time
import datetime
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed, ProcessPoolExecutor
import warnings
import requests
import cv2
import pkg_resources
from pathlib import Path
import geopandas as gpd
from tqdm import tqdm
from shapely.geometry import Point
import warnings
from shapely.errors import ShapelyDeprecationWarning
warnings.filterwarnings("ignore", category=ShapelyDeprecationWarning)
import csv
import glob
import shutil
import numpy as np
import osmnx as ox
from abc import ABC, abstractmethod
from shapely import wkt
from PIL import Image
from typing import List, Tuple, Union

import zensvi.download.mapillary.interface as mly
from zensvi.download.utils.imtool import ImageTool
from zensvi.download.utils.get_pids import panoids
from zensvi.download.utils.geoprocess import GeoProcessor
from zensvi.download.utils.helpers import standardize_column_names, create_buffer_gdf

# set logging level to warning
import logging
logging.getLogger('mapillary.utils.client').setLevel(logging.WARNING)

class BaseDownloader(ABC):
    @abstractmethod
    def __init__(self, log_path = None, distance = 1, grid = False, grid_size = 1):
        self._log_path = log_path
        self._distance = distance
        self._grid = grid
        self._grid_size = grid_size
        self._user_agents = self._get_ua()
        self._proxies = self._get_proxies()

    @property
    def log_path(self):
        return self._log_path    
    @log_path.setter
    def log_path(self,log_path):
        self._log_path = log_path
    
    @property
    def distance(self):
        return self._distance    
    @distance.setter
    def distance(self,distance):
        self._distance = distance
    
    @property
    def grid(self):
        return self._grid    
    @grid.setter
    def grid(self,grid):
        self._grid = grid
        
    @property
    def grid_size(self):
        return self._grid_size    
    @grid_size.setter
    def grid_size(self,grid_size):
        self._grid_size = grid_size

    @property
    def proxies(self):
        return self._proxies
    
    def _get_proxies(self):
        proxies_file = pkg_resources.resource_filename('zensvi.download.utils', 'proxies.csv')
        proxies = []
        # open with "utf-8" encoding to avoid UnicodeDecodeError
        with open(proxies_file, 'r', encoding = "utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                ip = row['ip']
                port = row['port']
                protocols = row['protocols']
                proxy_dict = {protocols: f"{ip}:{port}"}
                proxies.append(proxy_dict)
        return proxies
    
    @property
    def user_agents(self):
        return self._user_agents
    
    def _get_ua(self):
        user_agent_file = pkg_resources.resource_filename('zensvi.download.utils', 'UserAgent.csv')
        UA = []
        with open(user_agent_file, 'r') as f:
            for line in f:
                ua = {"user_agent": line.strip()}
                UA.append(ua)
        return UA
    
    def _log_write(self, pids):
        if self.log_path == None:
            return
        with open(self.log_path, 'a+') as fw:
            for pid in pids:
                fw.write(pid+'\n')
                
    def _check_already(self, all_panoids):
        # Get the set of already downloaded images
        name_r = set()
        for dirpath, dirnames, filenames in os.walk(self.panorama_output):
            for name in filenames:
                name_r.add(name.split(".")[0])

        # Filter the list of all panoids to only include those not already downloaded
        all_panoids = list(set(all_panoids) - name_r)
        return all_panoids
    
    def _read_pids(self, path_pid, start_date, end_date):
        pid_df = pd.read_csv(path_pid)
        # filter pids by date
        pid_df = self._filter_pids_date(pid_df, start_date, end_date)
        # get unique pids as a list
        pids = pid_df.iloc[:,0].unique().tolist()
        return pids

    @abstractmethod
    def _filter_pids_date(self, pid_df, start_date, end_date):
        pass

    @abstractmethod
    def download_svi(self, 
                    dir_output, 
                    lat=None, 
                    lon=None, 
                    input_csv_file="", 
                    input_shp_file = "", 
                    input_place_name = "", 
                    id_columns=None, 
                    buffer = 0,
                    update_pids = False,
                    start_date = None,
                    end_date = None):
        pass


class GSVDownloader(BaseDownloader):
    def __init__(self, gsv_api_key: str = None, log_path: str = None, distance: int = 1, grid: bool = False, grid_size: int = 1):
        """
        Google Street View Downloader class.

        Args:
            gsv_api_key (str, optional): Google Street View API key. Defaults to None.
            log_path (str, optional): Path to the log file. Defaults to None.
            distance (int, optional): Distance parameter for the GeoProcessor. Defaults to 1.
            grid (bool, optional): Grid parameter for the GeoProcessor. Defaults to False.
            grid_size (int, optional): Grid size parameter for the GeoProcessor. Defaults to 1.

        Raises:
            Warning: If gsv_api_key is not provided.
        """
        super().__init__(log_path, distance, grid, grid_size)
        if gsv_api_key == None:
            warnings.warn("Please provide your Google Street View API key to augment metadata.")
        self._gsv_api_key = gsv_api_key

    @property
    def gsv_api_key(self) -> str:
        """
        Property to get the Google Street View API key.

        Returns:
            str: Google Street View API key.
        """
        return self._gsv_api_key    
    
    @gsv_api_key.setter
    def gsv_api_key(self, gsv_api_key: str):
        """
        Setter to set the Google Street View API key.

        Args:
            gsv_api_key (str): Google Street View API key.
        """
        self._gsv_api_key = gsv_api_key

    def _augment_metadata(self, df):
        if self.cache_pids_augmented.exists():
            df = pd.read_csv(self.cache_pids_augmented)
            print("The augmented panorama IDs have been read from the cache")
            return df
        
        # Create a new directory called "augmented_metadata_checkpoints"
        dir_cache_augmented_metadata = self.dir_cache / 'augmented_pids'
        dir_cache_augmented_metadata.mkdir(parents=True, exist_ok=True)

        # Load all the checkpoint csv files
        checkpoints = glob.glob(str(dir_cache_augmented_metadata / '*.csv'))
        checkpoint_start_index = len(checkpoints)

        if checkpoint_start_index > 0:
            completed_rows = pd.concat([pd.read_csv(checkpoint) for checkpoint in checkpoints], ignore_index=True)
            completed_indices = completed_rows.index.unique()

            # Filter df to get remaining indices to augment metadata for
            df = df.loc[~df.index.isin(completed_indices)]

        def get_year_month(pid, proxies):
            url = "https://maps.googleapis.com/maps/api/streetview/metadata?pano={}&key={}".format(pid, self.gsv_api_key)
            while True:
                proxy = random.choice(proxies)
                try:
                    response = requests.get(url, proxies=proxy, timeout=5)
                    break
                except Exception as e:
                    print(f"Proxy {proxy} is not working. Exception: {e}")
                    continue

            response = response.json()
            if response['status'] == 'OK':
                # get year and month from date
                try:
                    date = response['date']
                    year = date.split("-")[0]
                    month = date.split("-")[1]
                except Exception:
                    year = None
                    month = None
                return {"year": year, "month": month}
            return {"year": None, "month": None}    

        def worker(row, proxies):
            panoid = row.panoid
            year_month = get_year_month(panoid, proxies)
            return row.Index, year_month

        batch_size = 1000  # Modify this to a suitable value
        num_batches = (len(df) + batch_size - 1) // batch_size

        for i in tqdm(range(num_batches), desc=f"Augmenting metadata by batch size {min(batch_size, len(df))}"):
            batch_df = df.iloc[i*batch_size : (i+1)*batch_size].copy()  # Copy the batch data to a new dataframe
            with ThreadPoolExecutor() as executor:
                batch_futures = {executor.submit(worker, row, self.proxies): row.Index for row in batch_df.itertuples()}
                for future in tqdm(as_completed(batch_futures), total=len(batch_futures), desc=f"Augmenting metadata for batch #{i+1}"):
                    row_index, year_month = future.result()
                    batch_df.at[row_index, 'year'] = year_month['year']
                    batch_df.at[row_index, 'month'] = year_month['month']

            # Save checkpoint for each batch
            batch_df.to_csv(f'{dir_cache_augmented_metadata}/checkpoint_batch_{checkpoint_start_index+i+1}.csv', index=False)
        
        # Merge all checkpoints into a single dataframe
        df = pd.concat([pd.read_csv(checkpoint) for checkpoint in glob.glob(str(dir_cache_augmented_metadata / '*.csv'))], ignore_index=True)

        # save the augmented metadata
        df.to_csv(self.cache_pids_augmented, index=False)
        # delete cache_lat_lon
        if self.cache_lat_lon.exists():
            self.cache_lat_lon.unlink() 
        # delete cache_pids_raw
        if self.cache_pids_raw.exists():
            self.cache_pids_raw.unlink()
        # delete the cache directory
        if dir_cache_augmented_metadata.exists():
            shutil.rmtree(dir_cache_augmented_metadata)
        return df

    def _get_pids_from_df(self, df, id_columns=None):
        # 1. Create a new directory called "pids" to store each batch pids
        dir_cache_pids = self.dir_cache / 'raw_pids'
        dir_cache_pids.mkdir(parents=True, exist_ok=True)

        # 2. Load all the checkpoint csv files
        checkpoints = glob.glob(str(dir_cache_pids / '*.csv'))
        checkpoint_start_index = len(checkpoints)
        if checkpoint_start_index > 0:
            dataframes = []
            for checkpoint in checkpoints:
                try:
                    df_checkpoint = pd.read_csv(checkpoint)
                    dataframes.append(df_checkpoint)
                except pd.errors.EmptyDataError:
                    print(f"Warning: {checkpoint} is empty and has been skipped.")
                    continue
            completed_rows = pd.concat(dataframes, ignore_index=True)

            completed_ids = completed_rows['lat_lon_id'].drop_duplicates()

            # Merge on the ID column, keeping track of where each row originates
            merged = df.merge(completed_ids, on='lat_lon_id', how='outer', indicator=True)

            # Filter out rows that come from the 'completed_ids' DataFrame
            df = merged[merged['_merge'] == 'left_only'].drop(columns='_merge')

        def get_street_view_info(longitude, latitude, proxies):
            results = panoids(latitude, longitude, proxies, )
            return results

        def worker(row):
            input_longitude = row.longitude
            input_latitude = row.latitude
            lat_lon_id = row.lat_lon_id
            id_dict = {column: getattr(row, column) for column in id_columns} if id_columns else {}
            return lat_lon_id, (input_longitude, input_latitude), get_street_view_info(input_longitude, input_latitude, self.proxies), id_dict

        # set lat_lon_id if it doesn't exist
        if 'lat_lon_id' not in df.columns:
            df['lat_lon_id'] = np.arange(1, len(df) + 1)
        results = []
        batch_size = 1000  # Modify this to a suitable value
        num_batches = (len(df) + batch_size - 1) // batch_size
        failed_rows = []
        
        # if there's no rows to process, return completed_ids
        if len(df) == 0:
            return completed_ids
        
        # if not, process the rows
        for i in tqdm(range(num_batches), desc=f"Getting pids by batch size {min(batch_size, len(df))}"):
            with ThreadPoolExecutor() as executor:
                batch_futures = {executor.submit(worker, row): row for row in df.iloc[i*batch_size : (i+1)*batch_size].itertuples()}
                for future in tqdm(as_completed(batch_futures), total=len(batch_futures), desc=f"Getting pids for batch #{i+1}"):
                    try:
                        lat_lon_id, (input_longitude, input_latitude), row_results, id_dict = future.result()
                        for result in row_results:
                            result['input_latitude'] = input_latitude
                            result['input_longitude'] = input_longitude
                            result['lat_lon_id'] = lat_lon_id
                            result.update(id_dict)
                            results.append(result)
                    except Exception as e:
                        print(f"Error: {e}")
                        failed_rows.append(batch_futures[future])  # Store the failed row

                # Save checkpoint for each batch
                if len(results) > 0:
                    pd.DataFrame(results).to_csv(f'{dir_cache_pids}/checkpoint_batch_{checkpoint_start_index+i+1}.csv', index=False)
                results = []  # Clear the results list for the next batch

        # Merge all checkpoints into a single dataframe
        results_df = pd.concat([pd.read_csv(checkpoint) for checkpoint in glob.glob(str(dir_cache_pids / '*.csv'))], ignore_index=True)

        # Retry failed rows
        if failed_rows:
            print("Retrying failed rows...")
            with ThreadPoolExecutor() as executor:
                retry_futures = {executor.submit(worker, row): row for row in failed_rows}
                for future in tqdm(as_completed(retry_futures), total=len(retry_futures), desc="Retrying failed rows"):
                    try:
                        lat_lon_id, (input_longitude, input_latitude), row_results, id_dict = future.result()
                        for result in row_results:
                            result['input_latitude'] = input_latitude
                            result['input_longitude'] = input_longitude
                            result['lat_lon_id'] = lat_lon_id
                            result.update(id_dict)
                            results.append(result)
                    except Exception as e:
                        print(f"Failed again: {e}")

            # Save the results of retried rows as another checkpoint
            if len(results) > 0:
                pd.DataFrame(results).to_csv(f'{dir_cache_pids}/checkpoint_retry.csv', index=False)
                # Merge the retry checkpoint into the final dataframe
                retry_df = pd.read_csv(f'{dir_cache_pids}/checkpoint_retry.csv')
                results_df = pd.concat([results_df, retry_df], ignore_index=True)

        # now save results_df as a new cache after dropping lat_lon_id
        results_df = results_df.drop(columns='lat_lon_id')
        # drop duplicates in panoid and id_columns
        results_df = results_df.drop_duplicates(subset=['panoid'] + id_columns)
        results_df.to_csv(self.cache_pids_raw, index=False)

        # delete the cache directory
        if dir_cache_pids.exists():
            shutil.rmtree(dir_cache_pids)
        return results_df

    def _get_pids_from_gdf(self, gdf, **kwargs):  
        if self.cache_lat_lon.exists():
            df = pd.read_csv(self.cache_lat_lon)
            print("The lat and lon have been read from the cache")
        else:
            if gdf.crs is None:
                gdf = gdf.set_crs('EPSG:4326')
            elif gdf.crs != 'EPSG:4326':
                # convert to EPSG:4326
                gdf = gdf.to_crs('EPSG:4326')
            # read shapefile
            gp = GeoProcessor(gdf, distance=self.distance, grid=self.grid, grid_size=self.grid_size, **kwargs)
            df = gp.get_lat_lon()
            df['lat_lon_id'] = np.arange(1, len(df) + 1)
            # save df to cache
            df.to_csv(self.cache_lat_lon, index=False)

        if self.cache_pids_raw.exists():
            print("The raw panorama IDs have been read from the cache")
            results_df = pd.read_csv(self.cache_pids_raw)
        else:
            # Use _get_pids_from_df to get pids from df
            results_df = self._get_pids_from_df(df, kwargs["id_columns"])

        # Check if lat and lon are within input polygons
        polygons = gpd.GeoSeries([geom for geom in gdf['geometry'] if geom.type in ['Polygon', 'MultiPolygon']])

        # the rest is only for polygons, so return results_df if there's no polygons
        if len(polygons) == 0:
            return results_df

        # Convert lat, lon to Points and create a GeoSeries
        points = gpd.GeoSeries([Point(lon, lat) for lon, lat in zip(results_df['lon'], results_df['lat'])])

        # Create a GeoDataFrame with the points and an index column
        points_gdf = gpd.GeoDataFrame(geometry=points, crs=gdf.crs)
        points_gdf['index'] = range(len(points_gdf))

        # Create a spatial index on the polygons GeoSeries
        polygons_sindex = polygons.sindex

        # Function to check whether a point is within any polygon
        def is_within_polygon(point):
            possible_matches_index = list(polygons_sindex.intersection(point.bounds))
            possible_matches = polygons.iloc[possible_matches_index]
            precise_matches = possible_matches.contains(point)
            return precise_matches.any()

        # Add progress bar for within_polygon calculation
        with tqdm(total=len(points), desc="Checking points within polygons") as pbar:
            within_polygon = []
            for point in points_gdf['geometry']:
                within_polygon.append(is_within_polygon(point))
                pbar.update()

        results_df['within_polygon'] = within_polygon

        # Return only those points within polygons
        results_within_polygons_df = results_df[results_df['within_polygon']]
        # Drop the 'within_polygon' column
        results_within_polygons_df = results_within_polygons_df.drop(columns='within_polygon')
        return results_within_polygons_df

    def _get_raw_pids(self, **kwargs):
        if self.cache_pids_raw.exists():
            pid = pd.read_csv(self.cache_pids_raw)
            print("The raw panorama IDs have been read from the cache")
            return pid

        if kwargs['lat'] is not None and kwargs['lon'] is not None:
            pid = panoids(kwargs['lat'], kwargs['lon'], self.proxies)
            pid = pd.DataFrame(pid)
            # add input_lat and input_lon
            pid['input_latitude'] = kwargs['lat']
            pid['input_longitude'] = kwargs['lon']
        elif kwargs['input_csv_file'] != "":
            df = pd.read_csv(kwargs['input_csv_file'])
            df = standardize_column_names(df)
            if kwargs['buffer'] > 0:
                gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.longitude, df.latitude), crs='EPSG:4326')
                gdf = create_buffer_gdf(gdf, kwargs['buffer'])
                pid = self._get_pids_from_gdf(gdf, **kwargs)
            else:
                pid = self._get_pids_from_df(df, kwargs['id_columns'])
        elif kwargs['input_shp_file'] != "":
            gdf = gpd.read_file(kwargs['input_shp_file'])
            if kwargs['buffer'] > 0:
                gdf = create_buffer_gdf(gdf, kwargs['buffer'])
            pid = self._get_pids_from_gdf(gdf, **kwargs)
        elif kwargs['input_place_name'] != "":
            print("Geocoding the input place name")
            gdf = ox.geocoder.geocode_to_gdf(kwargs['input_place_name'])
            # raise error if the input_place_name is not found
            if len(gdf) == 0:
                raise ValueError("The input_place_name is not found. Please try another place name.")
            if kwargs['buffer'] > 0:
                gdf = create_buffer_gdf(gdf, kwargs['buffer'])
            pid = self._get_pids_from_gdf(gdf, **kwargs) 
        else:
            raise ValueError("Please input the lat and lon, csv file, or shapefile.")

        return pid

    def _get_pids(self, path_pid, **kwargs):
        id_columns = kwargs['id_columns']
        if id_columns is not None:
            if isinstance(id_columns, str):
                id_columns = [id_columns.lower()]
            elif isinstance(id_columns, list):
                id_columns = [column.lower() for column in id_columns]
        else:
            id_columns = []
        # update id_columns
        kwargs['id_columns'] = id_columns
        
        # get raw pid
        pid = self._get_raw_pids(**kwargs)
        
        if kwargs["augment_metadata"] & (self.gsv_api_key != None):
            pid = self._augment_metadata(pid)
        elif kwargs["augment_metadata"] & (self.gsv_api_key == None):
            raise ValueError("Please set the gsv api key by calling the gsv_api_key method.")
        pid.to_csv(path_pid, index=False)
        print("The panorama IDs have been saved to {}".format(path_pid)) 

    def _set_dirs(self, dir_output):
        # set dir_output as attribute and create the directory
        self.dir_output = Path(dir_output)
        self.dir_output.mkdir(parents=True, exist_ok=True)
        # set dir_cache as attribute and create the directory
        self.dir_cache = self.dir_output / "cache_zensvi"
        self.dir_cache.mkdir(parents=True, exist_ok=True)
        # set other cache directories
        self.cache_lat_lon = self.dir_cache / "lat_lon.csv"
        self.cache_pids_raw = self.dir_cache / "pids_raw.csv"
        self.cache_pids_augmented = self.dir_cache / "pids_augemented.csv"
        
    def _filter_pids_date(self, pid_df, start_date, end_date):
        # create a temporary column date from year and month
        pid_df['date'] = pid_df['year'].astype(str) + "-" + pid_df['month'].astype(str)
        # convert to datetime
        pid_df['date'] = pd.to_datetime(pid_df['date'], format="%Y-%m")
        # check if start_date and end_date are in the correct format with regex. If not, raise error
        if start_date is not None:
            try:
                start_date = datetime.datetime.strptime(start_date, "%Y-%m-%d")
            except ValueError:
                raise ValueError("Incorrect start_date format, should be YYYY-MM-DD")
        if end_date is not None:
            try:
                end_date = datetime.datetime.strptime(end_date, "%Y-%m-%d")
            except ValueError:
                raise ValueError("Incorrect end_date format, should be YYYY-MM-DD")
        # if start_date is not None, filter out the rows with date < start_date
        pid_df = pid_df[pid_df['date'] >= start_date] if start_date is not None else pid_df
        # if end_date is not None, filter out the rows with date > end_date
        pid_df = pid_df[pid_df['date'] <= end_date] if end_date is not None else pid_df
        # drop the temporary column date
        pid_df = pid_df.drop(columns='date')
        return pid_df
    
    def download_svi(self, dir_output: str, path_pid: str = None, zoom: int = 2, h_tiles: int = 4, v_tiles: int = 2, 
                      cropped: bool = False, full: bool = True, lat: float = None, lon: float = None, 
                      input_csv_file: str = "", input_shp_file: str = "", input_place_name: str = "", 
                      id_columns: Union[str, List[str]] = None, buffer: int = 0, augment_metadata: bool = False, 
                      batch_size: int = 1000, update_pids: bool = False, start_date = None, end_date = None, **kwargs) -> None:
        """
        Downloads street view images.

        Args:
            dir_output (str): The output directory.
            path_pid (str, optional): The path to the panorama ID file. Defaults to None.
            zoom (int, optional): The zoom level for the images. Defaults to 2.
            h_tiles (int, optional): The number of horizontal tiles. Defaults to 4.
            v_tiles (int, optional): The number of vertical tiles. Defaults to 2.
            cropped (bool, optional): Whether to crop the images. Defaults to False.
            full (bool, optional): Whether to download full images. Defaults to True.
            lat (float, optional): The latitude for the images. Defaults to None.
            lon (float, optional): The longitude for the images. Defaults to None.
            input_csv_file (str, optional): The input CSV file. Defaults to "".
            input_shp_file (str, optional): The input shapefile. Defaults to "".
            input_place_name (str, optional): The input place name. Defaults to "".
            id_columns (Union[str, List[str]], optional): The ID columns. Defaults to None.
            buffer (int, optional): The buffer size. Defaults to 0.
            augment_metadata (bool, optional): Whether to augment the metadata. Defaults to False.
            batch_size (int, optional): The batch size for downloading. Defaults to 1000.
            update_pids (bool, optional): Whether to update the panorama IDs. Defaults to False.
            start_date (str, optional): The start date for the panorama IDs. Format is isoformat (YYYY-MM-DD). Defaults to None.
            end_date (str, optional): The end date for the panorama IDs. Format is isoformat (YYYY-MM-DD). Defaults to None.
            **kwargs: Additional keyword arguments.

        Returns:
            None
        """
        # set necessary directories
        self._set_dirs(dir_output)
        
        # call _get_pids function first if path_pid is None
        if (path_pid is None) & (self.cache_pids_augmented.exists() == False):
            print("Getting pids...")
            path_pid = self.dir_output / "gsv_pids.csv"
            if path_pid.exists() & (update_pids == False):
                print("update_pids is set to False. So the following csv file will be used: {}".format(path_pid))
            else:
                self._get_pids(path_pid, lat=lat, lon=lon,
                            input_csv_file=input_csv_file, input_shp_file = input_shp_file, input_place_name = input_place_name, 
                            id_columns=id_columns, buffer = buffer, augment_metadata=augment_metadata, **kwargs)
        elif self.cache_pids_augmented.exists():
            # copy the cache pids_augmented to path_pid
            path_pid = self.dir_output / "gsv_pids.csv"
            shutil.copy2(self.cache_pids_augmented, path_pid)
            print("The augmented panorama IDs have been saved to {}".format(path_pid))
        # Horizontal Google Street View tiles
        # zoom 3: (8, 4); zoom 5: (26, 13) zoom 2: (4, 2) zoom 1: (2, 1);4:(8,16)
        # zoom = 2
        # h_tiles = 4  # 26
        # v_tiles = 2  # 13
        # cropped = False
        # full = True
        # create a folder within self.dir_output
        self.panorama_output = self.dir_output / "gsv_panorama"
        self.panorama_output.mkdir(parents=True, exist_ok=True)
        
        panoids = self._read_pids(path_pid, start_date, end_date)
        
        if len(panoids) == 0:
            print("There is no panorama ID to download")
            return
        else:
            panoids_rest = self._check_already(panoids)

        if len(panoids_rest) > 0:
            UAs = random.choices(self.user_agents, k = len(panoids_rest))
            ImageTool.dwl_multiple(panoids_rest, zoom, v_tiles, h_tiles, self.panorama_output, UAs, self.proxies, cropped, full, batch_size = batch_size, log_path=self.log_path)
        else:
            print("All images have been downloaded")
        
        # delete the cache directory
        if self.dir_cache.exists():
            shutil.rmtree(self.dir_cache)
            print("The cache directory has been deleted")


class MLYDownloader(BaseDownloader):
    def __init__(self, mly_api_key, log_path = None, distance = 1, grid = False, grid_size = 1, max_workers = None):
        super().__init__(log_path, distance, grid, grid_size) 
        self._mly_api_key = mly_api_key
        self._max_workers = max_workers
        mly.set_access_token(self.mly_api_key)
        
    @property
    def mly_api_key(self):
        return self._mly_api_key
    @mly_api_key.setter
    def mly_api_key(self,mly_api_key):
        self._mly_api_key = mly_api_key
    
    @property
    def max_workers(self):
        return self._max_workers
    @max_workers.setter
    def max_workers(self,max_workers):
        self._max_workers = max_workers
    
    def _read_pids(self, path_pid):
        pid_df = pd.read_csv(path_pid)
        # drop NA values in id columns
        pid_df = pid_df.dropna(subset=['id'])
        # get unique pids (ie "id" columns) as a list
        pids = pid_df["id"].astype('int64').unique().tolist()
        return pids

    def _set_dirs(self, dir_output):
        # set dir_output as attribute and create the directory
        self.dir_output = Path(dir_output)
        self.dir_output.mkdir(parents=True, exist_ok=True)
        self.pids_url = self.dir_output / "pids_urls.csv"
        # set dir_cache as attribute and create the directory
        self.dir_cache = self.dir_output / "cache_zensvi"
        self.dir_cache.mkdir(parents=True, exist_ok=True)
        # set other cache directories
        self.cache_lat_lon = self.dir_cache / "lat_lon.csv"
        self.cache_pids_raw = self.dir_cache / "pids_raw.csv"
    
    def _get_pids_from_df(self, df, id_columns, **kwargs):
        # 1. Create a new directory called "pids" to store each batch pids
        dir_cache_pids = self.dir_cache / 'raw_pids'
        dir_cache_pids.mkdir(parents=True, exist_ok=True)

        # 2. Load all the checkpoint csv files
        checkpoints = glob.glob(str(dir_cache_pids / '*.csv'))
        checkpoint_start_index = len(checkpoints)
        if checkpoint_start_index > 0:
            dataframes = []
            for checkpoint in checkpoints:
                try:
                    df_checkpoint = pd.read_csv(checkpoint)
                    dataframes.append(df_checkpoint)
                except pd.errors.EmptyDataError:
                    print(f"Warning: {checkpoint} is empty and has been skipped.")
                    continue
            completed_rows = pd.concat(dataframes, ignore_index=True)

            completed_ids = completed_rows['lat_lon_id'].drop_duplicates()

            # Merge on the ID column, keeping track of where each row originates
            merged = df.merge(completed_ids, on='lat_lon_id', how='outer', indicator=True)

            # Filter out rows that come from the 'completed_ids' DataFrame
            df = merged[merged['_merge'] == 'left_only'].drop(columns='_merge')

        def get_street_view_info(latitude, longitude, **kwargs):
            results = mly.get_image_close_to(latitude = latitude, longitude = longitude, **kwargs).to_dict()["features"]
            return results

        def worker(row, **kwargs):
            input_latitude = row.latitude
            input_longitude = row.longitude
            lat_lon_id = row.lat_lon_id
            id_dict = {column: getattr(row, column) for column in id_columns} if id_columns else {}
            return lat_lon_id, (input_latitude, input_longitude), get_street_view_info(input_latitude, input_longitude, **kwargs), id_dict

        # set lat_lon_id if it doesn't exist
        if 'lat_lon_id' not in df.columns:
            df['lat_lon_id'] = np.arange(1, len(df) + 1)
        results = []
        batch_size = 1000  # Modify this to a suitable value
        num_batches = (len(df) + batch_size - 1) // batch_size
        failed_rows = []
        
        # if there's no rows to process, return completed_ids
        if len(df) == 0:
            return completed_ids
        
        # if not, process the rows
        for i in tqdm(range(num_batches), desc=f"Getting pids by batch size {min(batch_size, len(df))}"):
            with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
                batch_futures = {executor.submit(worker, row, **kwargs): row for row in df.iloc[i*batch_size : (i+1)*batch_size].itertuples()}
                for future in tqdm(as_completed(batch_futures), total=len(batch_futures), desc=f"Getting pids for batch #{i+1}"):
                    try:
                        lat_lon_id, (input_longitude, input_latitude), row_results, id_dict = future.result()
                        for result in row_results:
                            result['properties']['input_latitude'] = input_latitude
                            result['properties']['input_longitude'] = input_longitude
                            result['properties']['lat_lon_id'] = lat_lon_id
                            result['properties'].update(id_dict)
                            results.append(result)
                    except Exception as e:
                        print(f"Error: {e}")
                        failed_rows.append(batch_futures[future])  # Store the failed row

                # Save checkpoint for each batch
                if len(results) > 0:
                    # convert to geodataframe
                    results_gdf = gpd.GeoDataFrame.from_features(results)
                    results_gdf['lon'] = results_gdf['geometry'].apply(lambda geom: geom.x)
                    results_gdf['lat'] = results_gdf['geometry'].apply(lambda geom: geom.y)
                    # drop geometry column
                    results_gdf = results_gdf.drop(columns='geometry')  
                    pd.DataFrame(results_gdf).to_csv(f'{dir_cache_pids}/checkpoint_batch_{checkpoint_start_index+i+1}.csv', index=False)
                results = []  # Clear the results list for the next batch

        # Merge all checkpoints into a single dataframe
        checkpoints = glob.glob(str(dir_cache_pids / '*.csv'))
        if len(checkpoints) > 0:
            results_df = pd.concat([pd.read_csv(checkpoint) for checkpoint in checkpoints], ignore_index=True)

        # Retry failed rows
        if failed_rows:
            print("Retrying failed rows...")
            with ThreadPoolExecutor() as executor:
                retry_futures = {executor.submit(worker, row, **kwargs): row for row in failed_rows}
                for future in tqdm(as_completed(retry_futures), total=len(retry_futures), desc="Retrying failed rows"):
                    try:
                        lat_lon_id, (input_longitude, input_latitude), row_results, id_dict = future.result()
                        for result in row_results:
                            result['properties']['input_latitude'] = input_latitude
                            result['properties']['input_longitude'] = input_longitude
                            result['properties']['lat_lon_id'] = lat_lon_id
                            result['properties'].update(id_dict)
                            results.append(result)
                    except Exception as e:
                        print(f"Failed again: {e}")

            # Save the results of retried rows as another checkpoint
            if len(results) > 0:
                # convert to geodataframe
                results_gdf = gpd.GeoDataFrame.from_features(results)
                results_gdf['lon'] = results_gdf['geometry'].apply(lambda geom: geom.x)
                results_gdf['lat'] = results_gdf['geometry'].apply(lambda geom: geom.y)
                # drop geometry column
                results_gdf = results_gdf.drop(columns='geometry')   
                pd.DataFrame(results).to_csv(f'{dir_cache_pids}/checkpoint_retry.csv', index=False)
                # Merge the retry checkpoint into the final dataframe
                retry_df = pd.read_csv(f'{dir_cache_pids}/checkpoint_retry.csv')
                results_df = pd.concat([results_df, retry_df], ignore_index=True)
        
        if checkpoints == [] and failed_rows == []:
            print("There is no panorama ID to download")
            return

        # now save results_df as a new cache after dropping lat_lon_id and drop duplicates in panoid
        results_df = results_df.drop(columns='lat_lon_id')
        results_df = results_df.drop_duplicates(subset=['id'] + id_columns)
        results_df.to_csv(self.cache_pids_raw, index=False)

        # delete the cache directory
        if dir_cache_pids.exists():
            shutil.rmtree(dir_cache_pids)
        return results_df

    def _get_pids_from_gdf(self, gdf, mly_kwargs, **kwargs):
        if self.cache_lat_lon.exists():
            df = pd.read_csv(self.cache_lat_lon)
            print("The lat and lon have been read from the cache")
        else:
            if gdf.crs is None:
                gdf = gdf.set_crs('EPSG:4326')
            elif gdf.crs != 'EPSG:4326':
                # convert to EPSG:4326
                gdf = gdf.to_crs('EPSG:4326')
            # read shapefile
            gp = GeoProcessor(gdf, distance=self.distance, grid=self.grid, grid_size=self.grid_size, **kwargs)
            df = gp.get_lat_lon()
            df['lat_lon_id'] = np.arange(1, len(df) + 1)
            # save df to cache
            df.to_csv(self.cache_lat_lon, index=False)

        if self.cache_pids_raw.exists():
            print("The raw panorama IDs have been read from the cache")
            results_df = pd.read_csv(self.cache_pids_raw)
        else:
            # Use _get_pids_from_df to get pids from df
            results_df = self._get_pids_from_df(df, kwargs["id_columns"], **mly_kwargs)
            
        if not isinstance(results_df, pd.DataFrame):
            return
        
        # Check if lat and lon are within input polygons
        polygons = gpd.GeoSeries([geom for geom in gdf['geometry'] if geom.type in ['Polygon', 'MultiPolygon']])

        # the rest is only for polygons, so return results_df if there's no polygons
        if len(polygons) == 0:
            return results_df
        
        # Convert lat, lon to Points and create a GeoSeries
        points = gpd.GeoSeries([Point(lon, lat) for lon, lat in zip(results_df['lon'], results_df['lat'])])

        # Create a GeoDataFrame with the points and an index column
        points_gdf = gpd.GeoDataFrame(geometry=points, crs=gdf.crs)
        points_gdf['index'] = range(len(points_gdf))

        # Create a spatial index on the polygons GeoSeries
        polygons_sindex = polygons.sindex

        # Function to check whether a point is within any polygon
        def is_within_polygon(point):
            possible_matches_index = list(polygons_sindex.intersection(point.bounds))
            possible_matches = polygons.iloc[possible_matches_index]
            precise_matches = possible_matches.contains(point)
            return precise_matches.any()

        # Add progress bar for within_polygon calculation
        with tqdm(total=len(points), desc="Checking points within polygons") as pbar:
            within_polygon = []
            for point in points_gdf['geometry']:
                within_polygon.append(is_within_polygon(point))
                pbar.update()

        results_df['within_polygon'] = within_polygon

        # Return only those points within polygons
        results_within_polygons_df = results_df[results_df['within_polygon']]
        # Drop the 'within_polygon' column
        results_within_polygons_df = results_within_polygons_df.drop(columns='within_polygon')
        return results_within_polygons_df
        
    def _get_raw_pids(self, **kwargs):
        mly_allowed_keys = {'fields', 'zoom', 'radius', 'image_type', 'min_captured_at', 'max_captured_at', 'organization_id'} 
        mly_kwargs = {k: v for k, v in kwargs.items() if k in mly_allowed_keys}
        if self.cache_pids_raw.exists():
            pid = pd.read_csv(self.cache_pids_raw)
            print("The raw panorama IDs have been read from the cache")
            return pid

        if kwargs['lat'] is not None and kwargs['lon'] is not None:
            pid = mly.get_image_close_to(latitude = kwargs['lat'], longitude = kwargs['lon'], **mly_kwargs)
            # Convert to DataFrame
            pid = gpd.GeoDataFrame.from_features(pid.to_dict()['features'])
            pid['lon'] = pid['geometry'].apply(lambda geom: geom.x)
            pid['lat'] = pid['geometry'].apply(lambda geom: geom.y)
            # drop geometry column
            pid = pid.drop(columns='geometry')
            pid = pd.DataFrame(pid)
            # add input_lat and input_lon
            pid['input_latitude'] = kwargs['lat']
            pid['input_longitude'] = kwargs['lon']
        elif kwargs['input_csv_file'] != "":
            df = pd.read_csv(kwargs['input_csv_file'])
            df = standardize_column_names(df)
            if kwargs['buffer'] > 0:
                gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.longitude, df.latitude), crs='EPSG:4326')
                gdf = create_buffer_gdf(gdf, kwargs['buffer'])
                pid = self._get_pids_from_gdf(gdf, mly_kwargs, **kwargs)
            else:
                pid = self._get_pids_from_df(df, kwargs['id_columns'], mly_kwargs)
        elif kwargs['input_shp_file'] != "":
            gdf = gpd.read_file(kwargs['input_shp_file'])
            if kwargs['buffer'] > 0:
                gdf = create_buffer_gdf(gdf, kwargs['buffer'])
            pid = self._get_pids_from_gdf(gdf, mly_kwargs, **kwargs)
        elif kwargs['input_place_name'] != "":
            print("Geocoding the input place name")
            gdf = ox.geocoder.geocode_to_gdf(kwargs['input_place_name'])
            # raise error if the input_place_name is not found
            if len(gdf) == 0:
                raise ValueError("The input_place_name is not found. Please try another place name.")
            if kwargs['buffer'] > 0:
                gdf = create_buffer_gdf(gdf, kwargs['buffer'])
            pid = self._get_pids_from_gdf(gdf, mly_kwargs, **kwargs) 
        else:
            raise ValueError("Please input the lat and lon, csv file, or shapefile.")

        return pid
    
    def _filter_pids_date(self, pid_df, start_date, end_date):
        # create a temporary column date from captured_at (milliseconds from Unix epoch)
        pid_df['date'] = pd.to_datetime(pid_df['captured_at'], unit='ms')
        # check if start_date and end_date are in the correct format with regex. If not, raise error
        if start_date is not None:
            try:
                start_date = datetime.datetime.strptime(start_date, "%Y-%m-%d")
            except ValueError:
                raise ValueError("Incorrect start_date format, should be YYYY-MM-DD")
        if end_date is not None:
            try:
                end_date = datetime.datetime.strptime(end_date, "%Y-%m-%d")
            except ValueError:
                raise ValueError("Incorrect end_date format, should be YYYY-MM-DD")
        # if start_date is not None, filter out the rows with date < start_date
        pid_df = pid_df[pid_df['date'] >= start_date] if start_date is not None else pid_df
        # if end_date is not None, filter out the rows with date > end_date
        pid_df = pid_df[pid_df['date'] <= end_date] if end_date is not None else pid_df
        # drop the temporary column date
        pid_df = pid_df.drop(columns='date')
        return pid_df

    def _get_pids(self, path_pid, **kwargs):
        id_columns = kwargs['id_columns']
        if id_columns is not None:
            if isinstance(id_columns, str):
                id_columns = [id_columns.lower()]
            elif isinstance(id_columns, list):
                id_columns = [column.lower() for column in id_columns]
        else:
            id_columns = []
        # update id_columns
        kwargs['id_columns'] = id_columns
        
        # get raw pid
        pid = self._get_raw_pids(**kwargs)
        
        if pid is None:
            print("There is no panorama ID to download")
            return

        # # Assuming that df is your DataFrame and that 'geometry' column contains Point objects
        # # convert the geometry column into shapely geometry objects
        # pid['geometry'] = pid['geometry'].apply(wkt.loads)
        # pid['lon'] = pid['geometry'].apply(lambda geom: geom.x)
        # pid['lat'] = pid['geometry'].apply(lambda geom: geom.y)
        # # drop geometry column
        # pid = pid.drop(columns='geometry')
        # move the "id" column to the first column
        pid = pid[["id"] + [col for col in pid.columns if col != "id"]]

        # keep id_columns and id,captured_at,compass_angle,is_pano,organization_id,sequence_id,input_latitude,input_longitude,lon,lat drop other columns
        pid = pid[['id','captured_at','compass_angle','is_pano','organization_id','sequence_id','input_latitude','input_longitude','lon','lat'] + id_columns]

        pid.to_csv(path_pid, index=False)
        print("The panorama IDs have been saved to {}".format(path_pid)) 
    
    def _get_urls_mly(self, path_pid, resolution=1024):
        # check if seld.cache_pids_urls exists
        if self.pids_url.exists():
            print("The panorama URLs have been read from the cache")
            return
        
        dir_cache_urls = self.dir_cache / 'urls'
        dir_cache_urls.mkdir(parents=True, exist_ok=True)

        checkpoints = glob.glob(str(dir_cache_urls / '*.csv'))
        checkpoint_start_index = len(checkpoints)

        panoids = set(self._read_pids(path_pid))  # Convert to set for faster operations
        if len(panoids) == 0:
            print("There is no panorama ID to download")
            return

        # Read all panoids from the checkpoint files
        completed_panoids = set()  # Use set for faster operations
        for checkpoint in checkpoints:
            try:
                df_checkpoint = pd.read_csv(checkpoint)
                completed_panoids.update(df_checkpoint['id'].tolist())
            except pd.errors.EmptyDataError:
                print(f"Warning: {checkpoint} is empty and has been skipped.")
                continue

        # Filter out the panoids that have already been processed
        panoids = list(panoids - completed_panoids)  # Subtract sets and convert back to list
        if len(panoids) == 0:
            print("All images have been downloaded")
            return

        def worker(panoid, resolution):
            url = mly.image_thumbnail(panoid, resolution=resolution)
            return panoid, url

        results = {}
        batch_size = 1000  # Modify this to a suitable value
        num_batches = (len(panoids) + batch_size - 1) // batch_size

        for i in tqdm(range(num_batches), desc=f"Getting urls by batch size {min(batch_size, len(panoids))}"):
            with ThreadPoolExecutor() as executor:
                batch_futures = {executor.submit(worker, panoid, resolution): panoid for panoid in panoids[i*batch_size : (i+1)*batch_size]}
                
                for future in tqdm(as_completed(batch_futures), total=len(batch_futures), desc=f"Getting urls for batch #{i+1}"):
                    current_panoid = batch_futures[future]
                    try:
                        panoid, url = future.result()
                        results[panoid] = url
                    except Exception as e:
                        print(f"Error: {e}")
                        self._log_write(current_panoid)
                        continue

            if len(results) > 0:
                pd.DataFrame.from_dict(results, orient='index').reset_index().rename(columns={'index': 'id', 0: 'url'}).to_csv(f'{dir_cache_urls}/checkpoint_batch_{checkpoint_start_index+i+1}.csv', index=False)
            results = {}

        # Merge all checkpoints into a single dataframe
        results_df = pd.concat([pd.read_csv(checkpoint) for checkpoint in glob.glob(str(dir_cache_urls / '*.csv'))], ignore_index=True)
        results_df.to_csv(self.pids_url, index=False)

        if dir_cache_urls.exists():
            shutil.rmtree(dir_cache_urls)


    def _download_images_mly(self, path_pid, cropped, batch_size, start_date, end_date):
        checkpoints = glob.glob(str(self.panorama_output / '**/*.png'), recursive=True)

        # Read already downloaded images and convert to ids
        downloaded_ids = set([Path(file_path).stem for file_path in checkpoints])  # Use set for faster operations

        pid_df = pd.read_csv(path_pid).dropna(subset=['id'])
        pid_df["id"] = pid_df["id"].astype('int64')
        urls_df = pd.read_csv(self.pids_url)
        urls_df["id"] = urls_df["id"].astype('int64')
        # merge pid_df and urls_df
        urls_df = urls_df.merge(pid_df, on='id', how='left')
        # filter out the rows by date
        urls_df = self._filter_pids_date(urls_df, start_date, end_date)

        # Filter out the ids that have already been processed
        urls_df = urls_df[~urls_df['id'].isin(downloaded_ids)]  # Use isin for efficient operation

        def worker(row, output_dir, cropped):
            url, panoid = row.url, row.id
            user_agent = random.choice(self.user_agents)
            proxy = random.choice(self.proxies)

            image_name = f'{panoid}.png'  # Use id for file name
            image_path = output_dir / image_name
            try:
                response = requests.get(url, headers=user_agent, proxies=proxy, timeout=10)
                if response.status_code == 200:
                    with open(image_path, 'wb') as f:
                        f.write(response.content)
                    
                    if cropped:
                        img = Image.open(image_path)
                        w, h = img.size
                        img_cropped = img.crop((0, 0, w, h // 2))
                        img_cropped.save(image_path)

                else:
                    self._log_write(panoid)
            except Exception as e:
                self._log_write(panoid)
                print(f"Error: {e}" )

        num_batches = (len(urls_df) + batch_size - 1) // batch_size

        # Calculate current highest batch number
        existing_batches = glob.glob(str(self.panorama_output / "batch_*"))
        existing_batch_numbers = [int(Path(batch).name.split('_')[-1]) for batch in existing_batches]
        start_batch_number = max(existing_batch_numbers, default=0)

        for i in tqdm(range(start_batch_number, start_batch_number + num_batches), desc=f"Downloading images by batch size {min(batch_size, len(urls_df))}"):
            # Create a new sub-folder for each batch
            batch_out_path = self.panorama_output / f"batch_{i+1}"
            batch_out_path.mkdir(exist_ok=True)
            
            with ThreadPoolExecutor() as executor:
                batch_futures = {executor.submit(worker, row, batch_out_path, cropped): row.id for row in urls_df.iloc[i*batch_size : (i+1)*batch_size].itertuples()}
                for future in tqdm(as_completed(batch_futures), total=len(batch_futures), desc=f"Downloading images for batch #{i+1}"):
                    try:
                        future.result()
                    except Exception as e:
                        print(f"Error: {e}")

                
    def download_svi(self, dir_output, path_pid = None, lat=None, lon=None, input_csv_file="", input_shp_file = "", input_place_name =
                    "", id_columns=None, buffer = 0, update_pids = False, resolution = 1024, cropped = False, batch_size = 1000,
                    start_date = None, end_date = None, **kwargs):
        # set necessary directories
        self._set_dirs(dir_output)
        
        # call _get_pids function first if path_pid is None
        if path_pid is None:
            print("Getting pids...")
            path_pid = self.dir_output / "mly_pids.csv"
            if path_pid.exists() & (update_pids == False):
                print("update_pids is set to False. So the following csv file will be used: {}".format(path_pid))
            else:
                self._get_pids(path_pid, lat=lat, lon=lon,
                            input_csv_file=input_csv_file, input_shp_file = input_shp_file, input_place_name = input_place_name, 
                            id_columns=id_columns, buffer = buffer, start_date = start_date, end_date = end_date, **kwargs)
        else:
            # check if the path_pid exists
            if path_pid.exists():
                print("The following csv file will be used: {}".format(path_pid))
            else:
                self._get_pids(path_pid, lat=lat, lon=lon,
                            input_csv_file=input_csv_file, input_shp_file = input_shp_file, input_place_name = input_place_name, 
                            id_columns=id_columns, buffer = buffer, **kwargs)
    
        # create a folder within self.dir_output
        self.panorama_output = self.dir_output / "mly_svi"
        self.panorama_output.mkdir(parents=True, exist_ok=True)
        
        # get urls
        if path_pid.exists():
            self._get_urls_mly(path_pid, resolution=resolution) 
            # download images
            self._download_images_mly(path_pid, cropped, batch_size, start_date, end_date)
        else: 
            print("There is no panorama ID to download within the given input parameters")
        
        # delete the cache directory
        if self.dir_cache.exists():
            shutil.rmtree(self.dir_cache)
            print("The cache directory has been deleted") 
