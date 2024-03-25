# opera-RTC-otf
On the fly production of sentinel-1 OPERA RTC backscatter 

# Requirments
- Git
- Docker
- The following setup expects fedora linux instance (e.g. aws linux ami - packages installed with yum)

# Setup
- Add user credentials to the files stored in the credentials folder
    - Earthdata credentials - https://urs.earthdata.nasa.gov/users/new
        - Add these to both *credentials_earthdata.yaml* and *.netrc* file
    - Copernicus Dataspace - https://dataspace.copernicus.eu/
        - Add these to *credentials_copernicus.yaml*
    - AWS credentials 
        - Add these to *credentials_aws.yaml* to enable DEM download and upoad to desired destination 
- run install script (note credentials must be set on build, if these change the image will need to be rebuilt)
- if conda is not installed on instance (mamba will be installed)
```bash
sh setup.sh --install-conda
```
- if running with suitable conda install
```bash
sh setup.sh 
```


# Instructions
- set scene and data path details in config.yaml
- Change the OPERA-rtc-template.yaml. This is the opera template used for all scenes specified
- run process scripts
```bash
source run_process.sh
```

# Common errors
- ensure credentials have been set before running the setup script. If this was not the case, update the credentials in the credentials folder and run the folllowing:
```bash
cp -fr credentials RTC
cd RTC
sh build_docker_image_otf.sh
cd ..
```
Update credentials locally
```bash
cp -fr credentials/.netrc ~/
chmod og-rw ~/.netrc
```
- Error when downloding orbit files using sentineleof
```bash
ValueError: max() arg is an empty sequence
```
Try changing the *.netrc* credentials to copernicus CDSE (https://pypi.org/project/sentineleof/). Then run the following to replace the *.netrc* file.
```bash
cp -fr credentials/.netrc ~/
chmod og-rw ~/.netrc
```

