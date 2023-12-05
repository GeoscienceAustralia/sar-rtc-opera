import os
import sys
import threading
import logging
import boto3
from botocore.exceptions import ClientError
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

    # Upload the file
    s3_client = boto3.client('s3')
    try:
        response = s3_client.upload_file(file_name, bucket, object_name, Callback=ProgressPercentage(file_name))
    except ClientError as e:
        logging.error(e)
        return False

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

def expand_raster_with_bounds(input_raster, output_raster, old_bounds, new_bounds):

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
        logging.debug(f'Making temp file: {tmp}')
        with rasterio.open(tmp, 'w', **profile) as dst:
            # Read the data from the source and write it to the destination
            data = np.full((new_height, new_width), fill_value=profile['nodata'], dtype=profile['dtype'])
            dst.write(data, 1)
        # merge the old raster into the new raster with expanded bounds 
        logging.info(f'Merging original raster and expanding bounds...')
        rasterio.merge.merge(
            datasets=[tmp, input_raster],
            method='max',
            dst_path=output_raster)
        os.remove(tmp)

def reproject_raster(in_path, out_path, crs):
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