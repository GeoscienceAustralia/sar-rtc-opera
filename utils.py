import os
import sys
import threading
import logging
import boto3
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError
from botocore.client import Config
import os
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import calculate_default_transform, reproject, Resampling
from shapely.geometry import Polygon
import numpy as np
import zipfile
from urllib.request import urlretrieve
import tarfile
import pyproj
import time

logger = logging.getLogger(__name__)

def find_files(folder, contains):
    paths = []
    for root, dirs, files in os.walk(folder):
        for name in files:
            if contains in name:
                filename = os.path.join(root,name)
                paths.append(filename)
    return paths

class ProgressPercentage(object):

    def __init__(self, filename):
        self._filename = filename
        self._size = float(os.path.getsize(filename))
        self._seen_so_far = 0
        self._lock = threading.Lock()

    def __call__(self, bytes_amount):
        # To simplify, assume this is hooked up to a single filename
        with self._lock:
            self._seen_so_far += bytes_amount
            percentage = (self._seen_so_far / self._size) * 100
            sys.stdout.write(
                "\r%s  %s / %s  (%.2f%%)" % (
                    self._filename, self._seen_so_far, self._size,
                    percentage))
            sys.stdout.flush()


def upload_file(file_name, bucket, object_name=None):
    """Upload a file to an S3 bucket

    :param file_name: File to upload
    :param bucket: Bucket to upload to
    :param object_name: S3 object name. If not specified then file_name is used
    :return: True if file was uploaded, else False
    """

    # If S3 object_name was not specified, use file_name
    if object_name is None:
        object_name = os.path.basename(file_name)

    # edit config to stop timeout on large files
    config = TransferConfig(multipart_threshold=1024*25, max_concurrency=10,
                        multipart_chunksize=1024*25, use_threads=True)

    s3_client = boto3.client('s3')
    try:
        response = s3_client.upload_file(
            file_name, 
            bucket, 
            object_name, 
            Callback=ProgressPercentage(file_name),
            Config = config,
            )
    except Exception as e:
        logging.warning(e)
        try:
            time.sleep(10)
            logging.info('boto3.client("s3").upload_file failed')
            logging.info('attempting upload with aws cli')
            command = f'aws s3 cp {file_name} s3://{bucket}/{object_name}'
            logging.info(command)
            os.system(command)
        except Exception as e:
            logging.info('aws cli cp failed')
            logging.error(e)
            raise e

def transform_polygon(src_crs, dst_crs, geometry, always_xy=True):
    src_crs = pyproj.CRS(f"EPSG:{src_crs}")
    dst_crs = pyproj.CRS(f"EPSG:{dst_crs}") 
    transformer = pyproj.Transformer.from_crs(src_crs, dst_crs, always_xy=always_xy)
     # Transform the polygon's coordinates
    transformed_exterior = [
        transformer.transform(x, y) for x, y in geometry.exterior.coords
    ]
    # Create a new Shapely polygon with the transformed coordinates
    transformed_polygon = Polygon(transformed_exterior)
    return transformed_polygon

def expand_raster_with_bounds(input_raster, output_raster, old_bounds, new_bounds, fill_value=None):
    """Expand the raster to the desired bounds. Resolution and Location are preserved.

    Args:
        input_raster (str): input raster path
        output_raster (str): out raster path
        old_bounds (tuple): current bounds
        new_bounds (tuple): new bounds
        fill_value (float, int, optional): Fill value to pad with. Defaults to None and nodata is used.
    """
    # Open the raster dataset
    with rasterio.open(input_raster, 'r') as src:
        # get old bounds
        old_left, old_bottom, old_right, old_top = old_bounds
        # Define the new bounds
        new_left, new_bottom, new_right, new_top = new_bounds
        # adjust the new bounds with even pixel multiples of existing
        # this will stop small offsets
        logging.info(f'Making new raster with target bounds: {new_bounds}')
        new_left = old_left - int(abs(new_left-old_left)/src.res[0])*src.res[0]
        new_right = old_right + int(abs(new_right-old_right)/src.res[0])*src.res[0]
        new_bottom = old_bottom - int(abs(new_bottom-old_bottom)/src.res[1])*src.res[1]
        new_top = old_top + int(abs(new_top-old_top)/src.res[1])*src.res[1]
        logging.info(f'New raster bounds: {(new_left, new_bottom, new_right, new_top)}')
        # Calculate the new width and height, should be integer values
        new_width = int((new_right - new_left) / src.res[0])
        new_height = int((new_top - new_bottom) / src.res[1])
        # Define the new transformation matrix
        transform = from_origin(new_left, new_top, src.res[0], src.res[1])
        # Create a new raster dataset with expanded bounds
        profile = src.profile
        profile.update({
            'width': new_width,
            'height': new_height,
            'transform': transform
        })
        # make a temp file
        tmp = output_raster.replace('.tif','_tmp.tif')
        logging.info(f'Making temp file: {tmp}')
        with rasterio.open(tmp, 'w', **profile) as dst:
            # Read the data from the source and write it to the destination
            fill_value = profile['nodata'] if fill_value is None else fill_value
            logging.info(f'Padding new raster extent with value: {fill_value}')
            data = np.full((new_height, new_width), fill_value=fill_value, dtype=profile['dtype'])
            dst.write(data, 1)
        # merge the old raster into the new raster with expanded bounds 
        logging.info(f'Merging original raster and expanding bounds...')
    del data
    rasterio.merge.merge(
        datasets=[tmp, input_raster],
        method='max',
        dst_path=output_raster)
    os.remove(tmp)

def reproject_raster(in_path: str, out_path: str, crs: int):
    """Reproject a raster to the desired crs

    Args:
        in_path (str): path to src raster
        out_path (str): save path of reproj raster
        crs (int): crs e.g. 3031

    Returns:
        str: save path of reproj raster
    """
    # reproject raster to project crs
    with rasterio.open(in_path) as src:
        src_crs = src.crs
        transform, width, height = calculate_default_transform(
            src_crs, crs, src.width, src.height, *src.bounds)
        kwargs = src.meta.copy()

        # get crs proj 
        crs = pyproj.CRS(f"EPSG:{crs}")

        kwargs.update({
            'crs': crs,
            'transform': transform,
            'width': width,
            'height': height})

        with rasterio.open(out_path, 'w', **kwargs) as dst:
            for i in range(1, src.count + 1):
                reproject(
                    source=rasterio.band(src, i),
                    destination=rasterio.band(dst, i),
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=transform,
                    dst_crs=crs,
                    resampling=Resampling.nearest)
    return out_path

def get_REMA_index_file(save_folder):
    rema_index_url = 'https://data.pgc.umn.edu/elev/dem/setsm/REMA/indexes/REMA_Mosaic_Index_latest_gdb.zip'
    filename = 'REMA_Mosaic_Index_latest_gdb.zip'
    # download and store locally
    zip_save_path = os.path.join(save_folder, filename)
    urlretrieve(rema_index_url, zip_save_path)
    #unzip 
    with zipfile.ZipFile(zip_save_path, 'r') as zip_ref:
        zip_ref.extractall(save_folder)
        files=zip_ref.infolist()
        rema_index_file = '/'.join(files[0].filename.split('/')[0:-1])
    rema_index_path = os.path.join(save_folder, rema_index_file)
    os.remove(zip_save_path)
    return rema_index_path

def get_REMA_dem(url_list, resolution, save_folder, dem_name, crs=4326):

    valid_res = [2, 10, 32, 100, 500, 1000]
    assert resolution in valid_res, f"resolution must be in {valid_res}"

    # format for request, all metres except 1km
    resolution = f'{resolution}m' if resolution != 1000 else '1km'

    # download individual dems
    dem_paths = []
    for i, file_url in enumerate(url_list):
        # all urls are for 10m, set to other baws on resololution
        file_url = file_url.replace('10m',f'{resolution}')
        local_path = os.path.join(save_folder, file_url.split('setsm')[1][1:])
        local_folder = '/'.join(local_path.split('/')[0:-1])
        # check if the dem.tif already exists
        dem_path = find_files(local_folder, 'dem.tif')
        if len(dem_path) > 0:
            logging.info(f'{dem_path[0]} already exists, skipping download')
            dem_paths.append(dem_path[0])
            continue
        os.makedirs(local_folder, exist_ok=True)
        logging.info(f'{local_folder}')
        logging.info(f'downloading {i+1} of {len(url_list)}: src: {file_url} dst: {local_path}')
        urlretrieve(file_url, local_path)
        logging.info('unzipping...')
        with tarfile.open(local_path, "r:gz") as tar:
            # Extract all the contents to the target folder
            dem_folder = local_path.replace('.tar.gz','')
            tar.extractall(path=dem_folder)
        os.remove(local_path)
        dem_path = find_files(local_folder, 'dem.tif')
        dem_paths.append(dem_path[0])

    # combine DEMS
    logging.info('combining DEMS')
    merge_dem_path = os.path.join(save_folder,'tmp_merged.tif')
    rasterio.merge.merge(
            datasets=dem_paths,
            dst_path=merge_dem_path)
    # reporject to desired crs
    reproj_dem_path = os.path.join(save_folder,dem_name)
    logging.info(f'reprojecting DEM to {crs}')
    reproject_raster(merge_dem_path, reproj_dem_path, crs)
    os.remove(merge_dem_path)

    return merge_dem_path

def adjust_scene_poly_at_extreme_lat(bbox, src_crs, ref_crs, delta=0.1):
    """
    Adjust the bounding box around a scene in src_crs (4326) due to warping at high
    Latitudes. For example, the min and max boudning values for an antarctic scene in
    4326 may not actually be the true min and max due to distortions at high latitudes. 

    Parameters:
    - bbox: Tuple of four coordinates (x_min, y_min, x_max, y_max).
    - src_crs: Source EPSG. e.g. 4326
    - ref_crs: reference crs to create the true bbox. i.e. 3031 in southern 
                hemisphere and 3995 in northern (polar stereographic)
    - delta: distance between generation points along the bounding box sides in
            src_crs. e.g. 0.1 degrees in lat/lon 

    Returns:
    - A polygon bounding box expanded to the true min max
    """
    x_min, y_min, x_max, y_max = bbox
    # Generate points along the top side
    top_side = [(x, y_max) for x in list(np.arange(x_min, x_max, delta)) + [x_max]]    
    # Generate points along the right side
    right_side = [(x_max, y) for y in list(np.arange(y_max, y_min, -delta)) + [y_min]]
    # Generate points along the bottom side
    bottom_side = [(x, y_min) for x in list(np.arange(x_max, x_min, -delta)) + [x_min]]
    # Generate points along the left side
    left_side = [(x_min, y) for y in list(np.arange(y_min, y_max, delta)) + [y_max]]
    # Combine all sides' points
    all_points = top_side + right_side + bottom_side + left_side
    # convert to a polygon 
    polygon = Polygon(all_points)
    # convert polygon to desired crs and get bounds in those coordinates
    trans_bounds = transform_polygon(src_crs, ref_crs, polygon).bounds
    trans_poly = Polygon(
        [(trans_bounds[0], trans_bounds[1]), 
         (trans_bounds[2], trans_bounds[1]), 
         (trans_bounds[2], trans_bounds[3]), 
         (trans_bounds[0], trans_bounds[3])]
        )
    corrected_poly = transform_polygon(ref_crs, src_crs, trans_poly)
    return corrected_poly


def check_s1_bounds_cross_antimeridian(bounds, max_scene_width=20):
    """Check if the s1 bounds cross the antimeridian. We set a max
    scene width of 10. if the scene is greater than this either side
    of the dateline, we assume it crosses the dateline as the
    alternate scenario is a bounds with a very large width.

    e.g. [-178.031982, -71.618958, 178.577438, -68.765755]

    Args:
        bounds (list or tuple): Bounds in 4326
        max_scene_width (int, optional): maxumum width of bounds in 
        lon degrees. Defaults to 20.

    Returns:
        bool: True if crosses the antimeridian
    """

    min_x = -180 + max_scene_width # -160
    max_x = 180 - max_scene_width # 160
    if (bounds[0] < min_x) and (bounds[0] > -180):
        if bounds[2] > max_x and bounds[2] < 180:
            return True
    
def split_am_crossing(scene_polygon, lat_buff=0.2):
    """split the polygon into bounds on the left and
    right of the antimeridian.

    Args:
        scene_polygon (shapely.polygon): polygon of the scene from asf
        lat_buff (float): A lattitude degrees buffer to add/subtract from
                            max and min lattitudes 

    Returns:
    list(tuple) : a list containing two sets of bounds for the left
                    and right of the antimeridian
    """
    max_negative_x = max([point[0] for point in scene_polygon.exterior.coords if point[0] < 0])
    min_positive_x = min([point[0] for point in scene_polygon.exterior.coords if point[0] > 0])
    min_y = min([point[1] for point in scene_polygon.exterior.coords]) - lat_buff
    max_y = max([point[1] for point in scene_polygon.exterior.coords]) + lat_buff
    min_y = -90 if min_y < -90 else min_y
    max_y = 90 if max_y > 90 else max_y
    bounds_left = (-180, min_y, max_negative_x, max_y)
    bounds_right = (min_positive_x, min_y, 180, max_y)
    return [tuple(bounds_left), tuple(bounds_right)]