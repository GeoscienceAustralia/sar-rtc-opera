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
