import yaml
import argparse
import os
import asf_search as asf
from eof.download import download_eofs
import logging
import zipfile
from shapely.geometry import Polygon, box
import rasterio
from dem_stitcher import stitch_dem
import docker
from utils import (upload_file, 
        find_files, 
        transform_polygon,
        expand_raster_with_bounds,
        get_REMA_index_file,
        get_REMA_dem)
import time
import shutil
import json
import geopandas as gpd


logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S')

fh = logging.FileHandler('run.log')
logger = logging.getLogger()
logger.addHandler(fh)

def update_timing_file(key, time, path, replace=False):
    """Update the timing json at specified path. Creates if doesn't exists

    Args:
        key (str): key for the timing dict
        time (floar): time in seconds
        path (str): path to the timing file
        replace (bool, optional): Replace a value in the file if it already exists. Defaults to False.
    """
    if os.path.exists(path):
        with open(path, 'r') as fp:
            timing = json.load(fp)
    else:
        timing = {}
    if ((key not in timing) or replace):
        timing[key] = time
    # update total
    total_t = sum([timing[k] for k in timing.keys() if k != 'Total'])
    timing['Total'] = total_t
    with open(path, 'w') as fp:
        json.dump(timing, fp)
    
def run_process(args):
    
    t_start = time.time()
    # define success tracker
    success = {'opera-rtc': []}
    failed = {'opera-rtc': []}

    # read in the config for on the fly (otf) processing
    with open(args.config, 'r', encoding='utf8') as fin:
        otf_cfg = yaml.safe_load(fin.read())

    # read in aws credentials and set as environ vars
    logging.info(f'setting aws credentials from : {otf_cfg["aws_credentials"]}')
    with open(otf_cfg['aws_credentials'], "r", encoding='utf8') as f:
        aws_cfg = yaml.safe_load(f.read())
        # set all keys as environment variables
        for k in aws_cfg.keys():
            logging.info(f'setting {k}')
            os.environ[k] = aws_cfg[k]

    # loop through the list of scenes
    # download data -> produce backscatter -> save
    for i, scene in enumerate(otf_cfg['scenes']):

        timing = {}
        t0 = time.time()
        
        # add the scene name to the out folder
        OUT_FOLDER = otf_cfg['OPERA_output_folder']
        SCENE_OUT_FOLDER = os.path.join(OUT_FOLDER,scene)
        os.makedirs(SCENE_OUT_FOLDER, exist_ok=True)
        
        # make the timing file
        TIMING_FILE = scene + '_timing.json'
        TIMING_FILE_PATH = os.path.join(otf_cfg['OPERA_output_folder'],TIMING_FILE)

        logging.info(f'processing scene {i+1} of {len(otf_cfg["scenes"])} : {scene}')
        logging.info(f'PROCESS 1: Downloads')
        # search for the scene in asf
        logging.info(f'searching asf for scene...')
        asf.constants.CMR_TIMEOUT = 45
        logging.debug(f'CMR will timeout in {asf.constants.CMR_TIMEOUT}s')
        asf_results = asf.granule_search([scene], asf.ASFSearchOptions(processingLevel='SLC'))
        
        if len(asf_results) > 0:
            logging.info(f'scene found')
            asf_result = asf_results[0]
        else:
            logging.error(f'scene not found : {scene}')
            run_success = False
            failed['opera-rtc'].append(scene)
            continue
        
        # read in credentials to download from ASF
        logging.info(f'setting earthdata credentials from: {otf_cfg["earthdata_credentials"]}')
        with open(otf_cfg['earthdata_credentials'], "r", encoding='utf8') as f:
            earthdata_cfg = yaml.safe_load(f.read())
            earthdata_uid = earthdata_cfg['login']
            earthdata_pswd = earthdata_cfg['password']
        
        # download scene
        logging.info(f'downloading scene')
        session = asf.ASFSession()
        session.auth_with_creds(earthdata_uid,earthdata_pswd)
        SCENE_NAME = asf_results[0].__dict__['umm']['GranuleUR'].split('-')[0]
        scene_zip = os.path.join(otf_cfg['scene_folder'], SCENE_NAME + '.zip')
        asf_result.download(path=otf_cfg['scene_folder'], session=session)

        # apply the ETAD corrections to the SLC
        # if otf_cfg['apply_ETAD']:
            
        # unzip scene
        SAFE_PATH = scene_zip.replace(".zip",".SAFE")
        if otf_cfg['unzip_scene'] and not os.path.exists(SAFE_PATH): 
            logging.info(f'unzipping scene to {SAFE_PATH}')     
            with zipfile.ZipFile(scene_zip, 'r') as zip_ref:
                zip_ref.extractall(otf_cfg['scene_folder'])

        t1 = time.time()
        update_timing_file('Download Scene', t1 - t0, TIMING_FILE_PATH)

        # download orbits
        logging.info(f'downloading orbit files for scene')
        prec_orb_files = download_eofs(sentinel_file=scene_zip, 
                      save_dir=otf_cfg['precise_orbit_folder'], 
                      orbit_type='precise')
        if len(prec_orb_files) > 0:
            ORBIT_PATH = str(prec_orb_files[0])
            logging.info(f'using precise orbits: {ORBIT_PATH}')
        else:
            #download restituted orbits
            res_orb_files = download_eofs(sentinel_file=scene_zip, 
                          save_dir=otf_cfg['restituted_orbit_folder'], 
                          orbit_type='restituted',
                          asf_user=earthdata_uid,
                          asf_password=earthdata_pswd,
                          )  
            ORBIT_PATH = str(res_orb_files[0])
            logging.info(f'using restituted orbits: {ORBIT_PATH}')
        
        t2 = time.time()
        update_timing_file('Download Orbits', t2 - t1, TIMING_FILE_PATH)

        # download a DEM covering the region of interest
        # first get the coordinates from the asf search result
        points = (asf_result.__dict__['umm']['SpatialExtent']['HorizontalSpatialDomain']
                ['Geometry']['GPolygons'][0]['Boundary']['Points'])
        points = [(p['Longitude'],p['Latitude']) for p in points]
        buffer = 0.5
        scene_poly = Polygon(points)
        scene_poly_buf = scene_poly.buffer(buffer)
        scene_bounds = scene_poly.bounds 
        scene_bounds_buf = scene_poly.buffer(buffer).bounds #buffered
        logging.info(f'Scene bounds : {scene_bounds}')
        logging.info(f'Downloding DEM for  bounds : {scene_bounds_buf}')
        logging.info(f'type of DEM being downloaded : {otf_cfg["dem_type"]}')

        # transform the scene geometries to 3031
        scene_poly_3031 = transform_polygon(4326, 3031, scene_poly)
        scene_poly_buf_3031 = transform_polygon(4326, 3031, scene_poly_buf)
        scene_bounds_3031 = transform_polygon(4326, 3031, box(*scene_bounds))
        scene_bounds_buf_3031 = transform_polygon(4326, 3031, box(*scene_bounds_buf))

        # make folders and set filenames
        dem_dl_folder = os.path.join(otf_cfg['dem_folder'],otf_cfg['dem_type'])
        os.makedirs(dem_dl_folder, exist_ok=True)
        dem_filename = SCENE_NAME + '_dem.tif'
        DEM_PATH = os.path.join(dem_dl_folder,dem_filename)
        
        if not os.path.exists(DEM_PATH) or otf_cfg['overwrite_dem']:
            if 'REMA' not in str(otf_cfg['dem_type']).upper():
                # get the DEM and geometry information
                dem_data, dem_meta = stitch_dem(scene_bounds_buf,
                                dem_name=otf_cfg['dem_type'],
                                dst_ellipsoidal_height=False,
                                dst_area_or_point='Point',
                                merge_nodata_value=0
                                )
                
                # save with rasterio
                logging.info(f'saving dem to {DEM_PATH}')
                with rasterio.open(DEM_PATH, 'w', **dem_meta) as ds:
                    ds.write(dem_data, 1)
                    ds.update_tags(AREA_OR_POINT='Point')
                del dem_data
            else:
                # handle REMA DEM
                # download the index file for the rema dem
                # hosted on https://data.pgc.umn.edu
                logging.info('Downloading REMA index file')
                rema_index_path = get_REMA_index_file(dem_dl_folder)
                # load into gpdf
                rema_index_df = gpd.read_file(rema_index_path)
                # find the intersecting tiles
                intersecting_rema_files = rema_index_df[
                    rema_index_df.geometry.intersects(scene_bounds_buf_3031)]
                resolution = int(otf_cfg['dem_type'].split('_')[1])
                url_list = intersecting_rema_files['fileurl'].to_list()
                get_REMA_dem(url_list, resolution, dem_dl_folder, dem_filename, crs=4326)
                # read the metadata from the dem
                with rasterio.open(DEM_PATH) as src:
                    dem_meta = src.meta.copy()

            # get the bounds of the downloaded DEM
            # the full area requested may not be covered
            dem_bounds = rasterio.transform.array_bounds(
                dem_meta['height'], 
                dem_meta['width'], 
                dem_meta['transform'])
            logging.info(f'Downloaded DEM bounds: {dem_bounds}')
            # the DEM not covering the full extent of the scene is an issue
            if not box(*dem_bounds).contains_properly(box(*scene_bounds_buf)):
                logging.warning('Downloaded DEM does not cover scene bounds, filling with nodata')
                logging.info('Expanding the bounds of the downloaded DEM')
                DEM_ADJ_PATH = DEM_PATH.replace('.tif','_adj.tif') #adjusted DEM path
                expand_raster_with_bounds(DEM_PATH, DEM_ADJ_PATH, dem_bounds, scene_bounds_buf, fill_value=0)
                logging.info(f'Replacing DEM: {DEM_PATH}')
                os.remove(DEM_PATH)
                os.rename(DEM_ADJ_PATH, DEM_PATH)
            else:
                logging.info('Scene bounds are covered by downloaded DEM')
        else:
            logging.info(f'Using existing DEM : {DEM_PATH}')

        t3 = time.time()
        update_timing_file('Download DEM', t3 - t2, TIMING_FILE_PATH)

        # now we have downloaded all the necessary data, we can create a
        # config for the scene we want to process
        with open(otf_cfg['OPERA_rtc_remplate'], 'r') as f:
            template_text = f.read()
        # search for the strings we want to replace
        template_text = template_text.replace('SAFE_PATH',SAFE_PATH)
        template_text = template_text.replace('ORBIT_PATH',ORBIT_PATH)
        template_text = template_text.replace('DEM_PATH',DEM_PATH)
        template_text = template_text.replace('SCENE_NAME',SCENE_NAME)
        template_text = template_text.replace('OPERA_SCRATCH_FOLDER',
                                              otf_cfg['OPERA_scratch_folder'])
        template_text = template_text.replace('OPERA_OUTPUT_FOLDER',
                                              SCENE_OUT_FOLDER)

        
        # NOTE temporary change for mosaic modes XXX
        # msk_d = {0 : 'average', 1 : 'first', 2: 'bursts_center'}
        # template_text = template_text.replace('mosaic_mode:', f'mosaic_mode: {msk_d[i]}')
        # otf_cfg["scene_prefix"] = f'{msk_d[i]}_'

        opera_config_name = SCENE_NAME + '.yaml'
        opera_config_path = os.path.join(otf_cfg['OPERA_config_folder'], opera_config_name)
        with open(opera_config_path, 'w') as f:
            f.write(template_text)

        # read in the config template for the RTC runs
        with open(opera_config_path, 'r', encoding='utf8') as fin:
            opera_rtc_cfg = yaml.safe_load(fin.read())
        
        # run the docker container from the command line
        logging.info(f'PROCESS 2: Produce Backscatter')
        client = docker.from_env()
        # the command we run in the container. 
        docker_command = f'rtc_s1.py {opera_config_path}'
        # We mount the data folder in the container in the same location.
        # This is so the files can be accessed by the program at the paths specified
        volumes = [
            f'{otf_cfg["OPERA_config_folder"]}:{otf_cfg["OPERA_config_folder"]}',
            f'{otf_cfg["OPERA_scratch_folder"]}:{otf_cfg["OPERA_scratch_folder"]}',
            f'{otf_cfg["OPERA_output_folder"]}:{otf_cfg["OPERA_output_folder"]}',
            f'{otf_cfg["scene_folder"]}:{otf_cfg["scene_folder"]}',
            f'{otf_cfg["precise_orbit_folder"]}:{otf_cfg["precise_orbit_folder"]}',
            f'{otf_cfg["restituted_orbit_folder"]}:{otf_cfg["restituted_orbit_folder"]}',
            f'{otf_cfg["dem_folder"]}:{otf_cfg["dem_folder"]}',
            ]
        
        # setup file for logs
        logging.info(f'Running the container, this may take some time...')
        prod_id = (opera_rtc_cfg['runconfig']
                   ['groups']['product_group']['product_id'])
        log_path = os.path.join(SCENE_OUT_FOLDER, prod_id +'.logs')
        logging.info(f'logs will be saved to {log_path}')
        
        if not otf_cfg["skip_rtc"]:
            container = client.containers.run(f'opera/rtc:final_1.0.1', 
                                docker_command, 
                                volumes=volumes, 
                                user='rtc_user',
                                detach=True, 
                                stdout=True, 
                                stderr=True,
                                stream=True)
            
            l, t = 0, 0
            # show the logs while the container is running
            while container.status in ['created', 'running']:
                # refresh every 5 seconds
                if ((int(time.time())%5 == 0) and (int(time.time()!=t))):
                    logs = container.logs()
                    if len(logs) != l:
                        # new logs added in container, show these here
                        new_logs = logs[l:].decode("utf-8") 
                        logging.info(new_logs)
                        l = len(logs)
                    container.reload()
                    t = int(time.time())

            # write the logs from the container
            # TODO write to file in above, no need to keep
            # the full logs in memory
            with open(log_path, 'w') as f:
                f.write(logs.decode("utf-8"))

            # kill the container once processing is done
            try:
                container.kill()
                logging.info('killing container')
            except:
                logging.info('container already killed')

        # check if the final products exist, indicating success 
        h5_path = os.path.join(SCENE_OUT_FOLDER,
                               prod_id + '.h5')
        # keep track of success
        if os.path.exists(h5_path):
            clear_logs=True
            run_success = True
            success['opera-rtc'].append(h5_path)
            logging.info(f'RTC Backscatter successfully made')
        else:
            clear_logs = False
            run_success = False
            failed['opera-rtc'].append(h5_path)
            logging.info(f'RTC Backscatter failed')

        # get the crs of the final scene
        tifs = [x for x in os.listdir(SCENE_OUT_FOLDER) if '.tif' in x]
        tif = os.path.join(SCENE_OUT_FOLDER,tifs[0])
        with rasterio.open(tif) as src:
            trg_crs = str(src.meta['crs'])
        logging.info(f'CRS of mosaic: {trg_crs}')
        
        t4 = time.time()
        update_timing_file('RTC Processing', t4 - t3, TIMING_FILE_PATH)
            
        if otf_cfg['push_to_s3'] and run_success:
            logging.info(f'PROCESS 3: Push results to S3 bucket')
            bucket = otf_cfg['s3_bucket']
            outputs = [x for x in os.listdir(SCENE_OUT_FOLDER) if SCENE_NAME in x]
            # set the path in the bucket
            SCENE_PREFIX = '' if otf_cfg["scene_prefix"] == None else otf_cfg["scene_prefix"]
            S3_BUCKET_FOLDER = '' if otf_cfg["s3_bucket_folder"] == None else otf_cfg["s3_bucket_folder"]
            bucket_folder = os.path.join(S3_BUCKET_FOLDER,
                                        'rtc-opera/',
                                         otf_cfg['dem_type'],
                                         f'{trg_crs.split(":")[-1]}',
                                         f'{SCENE_PREFIX}{SCENE_NAME}')
            for file_ in outputs:
                file_path = os.path.join(SCENE_OUT_FOLDER,file_)
                bucket_path = os.path.join(bucket_folder,file_)
                logging.info(f'Uploading file: {file_path}')
                logging.info(f'Destination: {bucket_path}')
                upload_file(file_name=file_path, 
                            bucket=bucket, 
                            object_name=bucket_path)
            # push the config
            logging.info(f'Uploading file: {opera_config_path}')
            bucket_path = os.path.join(bucket_folder,opera_config_name)
            logging.info(f'Destination: {bucket_path}')
            upload_file(file_name=opera_config_path, 
                            bucket=bucket, 
                            object_name=bucket_path)
                
            if otf_cfg['upload_dem']:
                bucket_path = os.path.join(bucket_folder,dem_filename)
                logging.info(f'Uploading file: {DEM_PATH}')
                upload_file(DEM_PATH, 
                            bucket=bucket, 
                            object_name=bucket_path)
                
        t5 = time.time()
        update_timing_file('S3 Upload', t5 - t4, TIMING_FILE_PATH)

        if otf_cfg['delete_local_files']:
            logging.info(f'PROCESS 4: Clear files locally')
            #clear downloads
            for file_ in [scene_zip,
                        DEM_PATH,
                        ORBIT_PATH,
                        #opera_config_path,
                        ]:
                logging.info(f'Deleteing {file_}')
                os.remove(file_)
            logging.info(f'Clearing SAFE directory: {SAFE_PATH}')
            shutil.rmtree(SAFE_PATH)
            logging.info(f'Clearing directory: {SCENE_OUT_FOLDER}')
            try:
                shutil.rmtree(SCENE_OUT_FOLDER)
                shutil.rmtree(otf_cfg['OPERA_scratch_folder'])
            except:
                os.system(f'sudo chmod -R 777 {SCENE_OUT_FOLDER}')
                os.system(f'sudo chmod -R 777 {otf_cfg["OPERA_scratch_folder"]}')
                shutil.rmtree(SCENE_OUT_FOLDER)
                shutil.rmtree(otf_cfg['OPERA_scratch_folder'])
            # remake the scratch folder
            os.makedirs(otf_cfg['OPERA_scratch_folder'])
        
        t6 = time.time()
        update_timing_file('Delete Files', t6 - t5, TIMING_FILE_PATH)

        logging.info(f'Scene finished: {SCENE_NAME}')
        logging.info(f'Elapsed time: {((t6 - t0)/60)} minutes')

        # push timings + logs to s3
        if otf_cfg['push_to_s3'] and run_success:
            bucket_path = os.path.join(bucket_folder, TIMING_FILE)
            logging.info(f'Uploading file: {TIMING_FILE_PATH}')
            logging.info(f'Destination: {bucket_path}')
            upload_file(file_name=TIMING_FILE_PATH, 
                        bucket=bucket, 
                        object_name=bucket_path)
            os.remove(TIMING_FILE_PATH)

    logging.info(f'Run complete, {len(otf_cfg["scenes"])} scenes processed')
    logging.info(f'{len(success["opera-rtc"])} scenes successfully processed: ')
    for s in success['opera-rtc']:
        logging.info(f'{s}')
    logging.info(f'{len(failed["opera-rtc"])} scenes FAILED: ')
    for s in failed['opera-rtc']:
        logging.info(f'{s}')
    logging.info(f'Elapsed time:  {((time.time() - t_start)/60)} minutes')
    

if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", "-c", help="path to config.yml", required=True, type=str)
    args = parser.parse_args()

    run_process(args)