import yaml
import argparse
import os
import asf_search as asf
from eof.download import download_eofs
from dem_handler.dem.cop_glo30 import get_cop30_dem_for_bounds
import logging
import zipfile
from shapely.geometry import shape
import rasterio
import docker
from utils import *
from etad import *
import time
import shutil
import json


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
        SCENE_NAME = asf_result.__dict__['umm']['GranuleUR'].split('-')[0]
        POLARIZATION = asf_result.properties['polarization']
        POLARIZATION_TYPE = 'dual-pol' if len(POLARIZATION) > 2 else 'co-pol' # string for template value
        scene_zip = os.path.join(otf_cfg['scene_folder'], SCENE_NAME + '.zip')
        asf_result.download(path=otf_cfg['scene_folder'], session=session)
            
        # unzip scene
        ORIGINAL_SAFE_PATH = scene_zip.replace(".zip",".SAFE")
        if (otf_cfg['unzip_scene'] or otf_cfg['apply_ETAD']) and not os.path.exists(ORIGINAL_SAFE_PATH): 
            logging.info(f'unzipping scene to {ORIGINAL_SAFE_PATH}')     
            with zipfile.ZipFile(scene_zip, 'r') as zip_ref:
                zip_ref.extractall(otf_cfg['scene_folder'])

        # apply the ETAD corrections to the SLC
        if otf_cfg['apply_ETAD']:
            logging.info('Applying ETAD corrections')
            logging.info(f'loading copernicus credentials from: {otf_cfg["copernicus_credentials"]}')
            with open(otf_cfg['copernicus_credentials'], "r", encoding='utf8') as f:
                copernicus_cfg = yaml.safe_load(f.read())
                copernicus_uid = copernicus_cfg['login']
                copernicus_pswd = copernicus_cfg['password']
            etad_path = download_scene_etad(
                SCENE_NAME, 
                copernicus_uid, 
                copernicus_pswd, etad_dir=otf_cfg['ETAD_folder'])
            ETAD_SCENE_FOLDER = f'{otf_cfg["scene_folder"]}_ETAD'
            logging.info(f'making new directory for etad corrected slc : {ETAD_SCENE_FOLDER}')
            ETAD_SAFE_PATH = apply_etad_correction(
                ORIGINAL_SAFE_PATH, 
                etad_path, 
                out_dir=ETAD_SCENE_FOLDER,
                nthreads=otf_cfg['gdal_threads'])
        
        # set as the safe file for processing
        SAFE_PATH = ORIGINAL_SAFE_PATH if not otf_cfg['apply_ETAD'] else ETAD_SAFE_PATH
        
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
        scene_poly = shape(asf_result.geometry)
        scene_bounds = scene_poly.bounds 
        logging.info(f'Scene bounds : {scene_bounds}')

        if otf_cfg['dem_path'] is not None:
            # set the dem to be the one specified if supplied
            logging.info(f'using DEM path specified : {otf_cfg["dem_path"]}')
            if not os.path.exists(otf_cfg['dem_path']):
                raise FileExistsError(f'{otf_cfg["dem_path"]} c')
            else:
                DEM_PATH = otf_cfg['dem_path']
                dem_filename = os.path.basename(DEM_PATH)
                otf_cfg['dem_folder'] = os.path.dirname(DEM_PATH) # set the dem folder
                otf_cfg['overwrite_dem'] = False # do not overwrite dem
        else:
            # make folders and set filenames
            dem_dl_folder = os.path.join(otf_cfg['dem_folder'],otf_cfg['dem_type'])
            os.makedirs(dem_dl_folder, exist_ok=True)
            dem_filename = SCENE_NAME + '_dem.tif'
            DEM_PATH = os.path.join(dem_dl_folder,dem_filename)

        
        if any([otf_cfg['overwrite_dem'],not os.path.exists(DEM_PATH)]):
            
            get_cop30_dem_for_bounds(
                bounds=scene_bounds,
                save_path=DEM_PATH,
                ellipsoid_heights=True,
                adjust_at_high_lat=True,
                buffer_degrees=0.3,
                cop30_folder_path=dem_dl_folder,
                geoid_tif_path=os.path.join(dem_dl_folder,f"{scene}_geoid.tif"),
                download_dem_tiles=True,
                download_geoid=True,
            )
        else:
            logging.info(f'Using existing DEM : {DEM_PATH}')

        t3 = time.time()
        update_timing_file('Download DEM', t3 - t2, TIMING_FILE_PATH)

        # now we have downloaded all the necessary data, we can create a
        # config for the scene we want to process
        with open(otf_cfg['OPERA_rtc_template'], 'r') as f:
            template_text = f.read()
        # search for the strings we want to replaces
        template_text = template_text.replace('SAFE_PATH',SAFE_PATH)
        template_text = template_text.replace('ORBIT_PATH',ORBIT_PATH)
        template_text = template_text.replace('DEM_PATH',DEM_PATH)
        template_text = template_text.replace('SCENE_NAME',SCENE_NAME)
        template_text = template_text.replace('OPERA_SCRATCH_FOLDER',
                                              otf_cfg['OPERA_scratch_folder'])
        template_text = template_text.replace('OPERA_OUTPUT_FOLDER',
                                              SCENE_OUT_FOLDER)
        template_text = template_text.replace('POLARIZATION_TYPE',
                                              POLARIZATION_TYPE)
        template_text = template_text.replace('X_RESOLUTION',
                                              str(otf_cfg['OPERA_x_resolution']))
        template_text = template_text.replace('Y_RESOLUTION',
                                              str(otf_cfg['OPERA_y_resolution']))
        TARGET_CRS = otf_cfg['OPERA_crs'] if otf_cfg['OPERA_crs'] is not None else ''
        template_text = template_text.replace('TARGET_CRS',
                                              str(TARGET_CRS))

        opera_config_name = SCENE_NAME + '.yaml'
        opera_config_path = os.path.join(otf_cfg['OPERA_config_folder'], opera_config_name)
        with open(opera_config_path, 'w') as f:
            f.write(template_text)

        # read in the config template for the RTC runs
        with open(opera_config_path, 'r', encoding='utf8') as fin:
            opera_rtc_cfg = yaml.safe_load(fin.read())
        
        logging.info(f'PROCESS 2: Produce Backscatter')
        # run the docker container from the command line\
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
        
        if otf_cfg['apply_ETAD']:
            volumes.append(f'{otf_cfg["scene_folder"]}_ETAD:{otf_cfg["scene_folder"]}_ETAD')
        
        # setup file for logs
        logging.info(f'Running the container, this may take some time...')
        prod_id = (opera_rtc_cfg['runconfig']
                   ['groups']['product_group']['product_id'])
        log_path = os.path.join(SCENE_OUT_FOLDER, prod_id +'.logs')
        logging.info(f'logs will be saved to {log_path}')
        
        if not otf_cfg["skip_rtc"]:
            container = client.containers.run(f'opera/rtc:final_1.0.4-atmosbugfix', 
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
            run_success = True
            success['opera-rtc'].append(h5_path)
            logging.info(f'RTC Backscatter successfully made')
        else:
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
                                         otf_cfg["software"],
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
                        opera_config_path,
                        ]:
                logging.info(f'Deleteing {file_}')
                os.remove(file_)
            logging.info(f'Clearing SAFE directory: {ORIGINAL_SAFE_PATH}')
            shutil.rmtree(ORIGINAL_SAFE_PATH)
            if otf_cfg['apply_ETAD']:
                logging.info(f'Clearing ETAD corrected SAFE directory: {ETAD_SAFE_PATH}')
                shutil.rmtree(ETAD_SAFE_PATH)
                logging.info(f'Clearing directory: {ETAD_SAFE_PATH}')
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