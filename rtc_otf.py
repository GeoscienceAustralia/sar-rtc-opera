import yaml
import argparse
import os
import asf_search as asf
from eof.download import download_eofs
import logging
import zipfile
from shapely.geometry import Polygon
import rasterio
from dem_stitcher import stitch_dem
import docker
from utils import upload_file, find_files
import time
import shutil
import json

logging.basicConfig(
    format='%(asctime)s %(levelname)-8s %(message)s',
    level=logging.INFO,
    datefmt='%Y-%m-%d %H:%M:%S')



if __name__ == "__main__":

    t_start = time.time()
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", "-c", help="path to config.yml", required=True, type=str)
    args = parser.parse_args()

    # define success tracker
    success = {'opera-rtc': []}

    # read in the config for on the fly (otf) processing
    with open(args.config, 'r', encoding='utf8') as fin:
        otf_cfg = yaml.safe_load(fin.read())
    
    # read in credentials to download from ASF
    with open(otf_cfg['earthdata_credentials'], "r") as f:
        txt = str(f.read())
        uid = txt.split('\n')[1].split('login')[-1][1:]
        pswd = txt.split('\n')[2].split('password')[-1][1:]

    # loop through the list of scenes
    # download data -> produce backscatter -> save
    for i, scene in enumerate(otf_cfg['scenes']):
        
        timing = {}
        t0 = time.time()

        logging.info(f'PROCESS 1: Downloads')
        logging.info(f'processing scene {i+1} of {len(otf_cfg["scenes"])} : {scene}')
        # search for the scene in asf
        logging.info(f'searching asf for scene...')
        asf_results = asf.granule_search([scene], asf.ASFSearchOptions(processingLevel='SLC'))
        
        if len(asf_results) > 0:
            logging.info(f'scene found')
            asf_result = asf_results[0]
        else:
            logging.error(f'scene not found : {scene}')
            continue
        
        # download scene
        logging.info(f'downloading scene')
        session = asf.ASFSession()
        session.auth_with_creds(uid,pswd)
        SCENE_NAME = asf_results[0].__dict__['umm']['GranuleUR'].split('-')[0]
        scene_zip = os.path.join(otf_cfg['scene_folder'], SCENE_NAME + '.zip')
        asf_result.download(path=otf_cfg['scene_folder'], session=session)

        # unzip scene
        if otf_cfg['unzip_scene']: 
            SAFE_PATH = scene_zip.replace(".zip",".SAFE")
            logging.info(f'unzipping scene to {SAFE_PATH}')     
            with zipfile.ZipFile(scene_zip, 'r') as zip_ref:
                zip_ref.extractall(otf_cfg['scene_folder'])

        t1 = time.time()
        timing['Download Scene'] = t1 - t0
        
        # download orbits
        logging.info(f'downloading orbit files for scene')
        prec_orb_files = download_eofs(sentinel_file=scene_zip, 
                      save_dir=otf_cfg['precise_orbit_folder'], 
                      orbit_type='precise')
        if len(prec_orb_files) > 0:
            ORBIT_PATH = prec_orb_files[0]
            logging.info(f'using precise orbits')
        else:
            #download restituted orbits
            res_orb_files = download_eofs(sentinel_file=scene_zip, 
                          save_dir=otf_cfg['restituted_orbit_folder'], 
                          orbit_type='restituted')  
            ORBIT_PATH = res_orb_files[0]
            logging.info(f'using restituted orbits')
        
        t2 = time.time()
        timing['Download Orbits'] = t2 - t1

        # download a DEM covering the region of interest
        # first get the coordinates from the asf search result
        points = (asf_result.__dict__['umm']['SpatialExtent']['HorizontalSpatialDomain']
                ['Geometry']['GPolygons'][0]['Boundary']['Points'])
        points = [(p['Longitude'],p['Latitude']) for p in points]
        poly = Polygon(points)
        bounds = poly.buffer(1).bounds #buffered
        
        logging.info(f'downloding DEM for scene bounds : {bounds}')
        logging.info(f'type of DEM being downloaded : {otf_cfg["dem_type"]}')
        # get the DEM and geometry information
        X, p = stitch_dem(bounds,
                        dem_name=otf_cfg['dem_type'],
                        dst_ellipsoidal_height=False,
                        dst_area_or_point='Point')

        # make folders and set filenames
        dem_dl_folder = os.path.join(otf_cfg['dem_folder'],otf_cfg['dem_type'])
        os.makedirs(dem_dl_folder, exist_ok=True)
        dem_filename = SCENE_NAME + '_dem.tif'
        DEM_PATH = os.path.join(dem_dl_folder,dem_filename)
        
        # save with rasterio
        logging.info(f'saving dem to {DEM_PATH}')
        with rasterio.open(DEM_PATH, 'w', **p) as ds:
            ds.write(X, 1)
            ds.update_tags(AREA_OR_POINT='Point')
        del X

        t3 = time.time()
        timing['Download DEM'] = t3 - t2

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
                                              otf_cfg['OPERA_output_folder'])

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
        volumes = [f'{otf_cfg["data_folder"]}:{otf_cfg["data_folder"]}']
        
        # setup file for logs
        logging.info(f'Running the container, this may take some time...')
        prod_id = (opera_rtc_cfg['runconfig']
                   ['groups']['product_group']['product_id'])
        log_path = os.path.join(otf_cfg['OPERA_output_folder'], prod_id +'.logs')
        logging.info(f'logs will be saved to {log_path}')
        container = client.containers.run(f'opera/rtc:final_1.0.1', 
                              docker_command, 
                              volumes=volumes, 
                              user='root',
                              detach=True, 
                              stdout=True, 
                              stderr=True,
                              stream=True)
        
        l, t = 0, 0
        # show the logs while the container is running
        while container.status in ['created', 'running']:
            # reflesh every 5 seconds
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
        h5_path = os.path.join(otf_cfg['OPERA_output_folder'],
                               prod_id + '.h5')
        # keep track of success
        if os.path.exists(h5_path):
            success['opera-rtc'].append(h5_path)
            logging.info(f'RTC Backscatter successfully made')

        t4 = time.time()
        timing['RTC Processing'] = t4 - t3
            
        if otf_cfg['push_to_s3']:
            logging.info(f'PROCESS 3: Push results to S3 bucket')
            bucket = otf_cfg['s3_bucket']
            outputs = [x for x in os.listdir(otf_cfg['OPERA_output_folder']) if SCENE_NAME in x]
            # set the path in the bucket
            bucket_folder = os.path.join('rtc-opera/',
                                         otf_cfg['dem_type'],
                                         SCENE_NAME)
            for file_ in outputs:
                file_path = os.path.join(otf_cfg['OPERA_output_folder'],file_)
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
        timing['S3 Upload'] = t5 - t4

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
            logging.info(f'Clearing SAFE directory: {SAFE_PATH}')
            shutil.rmtree(SAFE_PATH)
            logging.info(f'Clearing directory: {otf_cfg["OPERA_output_folder"]}')
            try:
                shutil.rmtree(otf_cfg['OPERA_output_folder'])
                shutil.rmtree(otf_cfg['OPERA_scratch_folder'])
            except:
                os.system(f'sudo chmod -R 777 {otf_cfg["OPERA_output_folder"]}')
                os.system(f'sudo chmod -R 777 {otf_cfg["OPERA_scratch_folder"]}')
                shutil.rmtree(otf_cfg['OPERA_output_folder'])
                shutil.rmtree(otf_cfg['OPERA_scratch_folder'])
            # remake the outdir
            os.makedirs(otf_cfg['OPERA_output_folder'])
            os.makedirs(otf_cfg['OPERA_scratch_folder'])
        
        t6 = time.time()
        timing['Delete Files'] = t6 - t5

        logging.info(f'Scene finished: {SCENE_NAME}')
        logging.info(f'Elapsed time: {((t6 - t0)/60)} minutes')
        timing['Total'] = t6 - t0

        # push timings + logs to s3
        if otf_cfg['push_to_s3']:
            timing_file = SCENE_NAME + '_timing.json'
            bucket_path = os.path.join(bucket_folder, timing_file)
            with open(timing_file, 'w') as fp:
                json.dump(timing, fp)
            logging.info(f'Uploading file: {timing_file}')
            logging.info(f'Destination: {bucket_path}')
            upload_file(file_name=timing_file, 
                        bucket=bucket, 
                        object_name=bucket_path)
            os.remove(timing_file)

    logging.info(f'Run complete, {len(otf_cfg["scenes"])} scenes processed')
    logging.info(f'Elapsed time:  {((time.time() - t_start)/60)} minutes')








    
